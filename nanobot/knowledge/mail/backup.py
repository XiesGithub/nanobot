"""Offline, manifest-verified private backups. Query snapshots are disposable."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from filelock import FileLock, Timeout

from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import SCHEMA_VERSION, atomic_write, identifier, json_text

ASSET_DIRECTORIES = ("objects", "spool", "maintenance")


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def create_backup(store, target: Path, *, _after_publish=None):
    """Require stopped Worker; publish only after SQLite + all assets are durable."""
    if store.readonly:
        raise MailError("knowledge_readonly")
    try:
        target = store.safe_path(str(target.resolve().relative_to(store.root)))
    except (ValueError, OSError):
        raise MailError("invalid_backup_path") from None
    if target.parent != store.root / "backups" or target.exists():
        raise MailError("invalid_backup_path")
    # A different lock object prevents reentrant ownership in a live Gateway.
    worker = FileLock(str(store.root / "worker.lock"), timeout=0)
    try:
        worker.acquire()
    except Timeout:
        raise MailError("offline_maintenance_required") from None
    stage = store.safe_path("backups/" + identifier(".building_"))
    try:
        with FileLock(str(store.root / "mode.lock"), timeout=35), store.lock:
            stage.mkdir(parents=True)
            manifest = {"format": 1, "created_at": time.time(),
                        "query_state_included": False, "private_history_included": False, "files": {}}
            with store.connect() as source, closing(sqlite3.connect(stage / "knowledge.sqlite3")) as destination:
                source.backup(destination)
                manifest["instance_id"] = source.execute("SELECT instance_id FROM schema_meta").fetchone()[0]
                manifest["schema_version"] = destination.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MailError("backup_integrity_failed")
                if destination.execute("PRAGMA foreign_key_check").fetchone():
                    raise MailError("backup_integrity_failed")
            for name in ASSET_DIRECTORIES:
                folder = store.safe_path(name)
                (stage / name).mkdir()
                for path in folder.rglob("*"):
                    source = store.safe_path(str(path.relative_to(store.root)))
                    if source.is_file():
                        destination = stage / path.relative_to(store.root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with source.open("rb") as incoming, destination.open("xb") as outgoing:
                            shutil.copyfileobj(incoming, outgoing, length=1024**2)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
            for path in stage.rglob("*"):
                if path.is_file():
                    with path.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    manifest["files"][path.relative_to(stage).as_posix()] = file_digest(path)
            if os.name != "nt":
                for folder in [p for p in stage.rglob("*") if p.is_dir()] + [stage]:
                    descriptor = os.open(folder, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            atomic_write(stage / "manifest.json", json_text(manifest).encode())
            os.replace(stage, target)
            if os.name != "nt":
                descriptor = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if _after_publish is not None:
                _after_publish()
    finally:
        # Only this operation's generated, containment-checked staging path.
        try:
            if stage.exists():
                shutil.rmtree(store.safe_path(str(stage.relative_to(store.root))))
        finally:
            worker.release()
    return manifest


def verify_backup(store, target: Path):
    try:
        target = store.safe_path(str(target.resolve().relative_to(store.root)))
        if target.parent != store.root / "backups":
            raise ValueError()
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != 1 or manifest.get("schema_version") != SCHEMA_VERSION:
            raise MailError("backup_version_unsupported")
        files = manifest["files"]
        if "knowledge.sqlite3" not in files:
            raise ValueError()
        for name, expected in files.items():
            path = (target / name).resolve()
            if not path.is_relative_to(target) or path == target or path.is_symlink():
                raise ValueError()
            if name != "knowledge.sqlite3" and Path(name).parts[0] not in ASSET_DIRECTORIES:
                raise ValueError()
            if file_digest(path) != expected:
                raise ValueError()
        with closing(sqlite3.connect((target / "knowledge.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            version = db.execute("SELECT schema_version FROM schema_meta WHERE singleton=1").fetchone()
            if not version or version[0] != manifest["schema_version"]:
                raise ValueError()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError()
        return manifest
    except MailError:
        raise
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        raise MailError("backup_integrity_failed") from None

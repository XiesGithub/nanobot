"""Verified, bounded historical receipts for offline engineering regression tests."""
import hashlib
import json
import zipfile
from pathlib import Path, PurePosixPath

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _safe_path(root, name):
    relative = PurePosixPath(name)
    if (relative.is_absolute() or relative.as_posix() != name or "\\" in name or ":" in name
            or ".." in relative.parts):
        raise ValueError("historical_fixture_path_escape")
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("historical_fixture_path_escape")
    return target


def unpack_history(root):
    """Copy original bytes into a new test root; never write to the original archives."""
    root = Path(root)
    manifest = json.loads((FIXTURES / "engineering-history-index.json").read_text(encoding="utf-8"))
    archive_path = FIXTURES / "engineering-history.zip"
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["zip_sha256"]:
        raise ValueError("historical_fixture_zip_hash")
    root.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest["files"]):
            raise ValueError("historical_fixture_inventory")
        for name in names:
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                raise ValueError("historical_fixture_file_hash")
            target = _safe_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)
    return root


def materialize_privacy_sources(root):
    """Use the original source bytes for strict historical source-identity assertions.

    These are data, never imported/executed. Current code must not be presented as
    the old candidate or be granted its qualification.
    """
    run = root / "docs/agent-evaluation-privacy-family-20261011/D-final-positive"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(run / "source-snapshot.zip") as archive:
        if len(archive.namelist()) != len(set(archive.namelist())) or set(archive.namelist()) != set(manifest["sources"]):
            raise ValueError("historical_source_inventory")
        for name, checksum in manifest["sources"].items():
            raw = archive.read(name)
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("historical_source_hash")
            target = _safe_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(raw)

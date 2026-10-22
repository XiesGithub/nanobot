"""Content-addressed source snapshots for fixed agent evaluations."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import locale
import os
import platform
import re
import stat
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "deepseek-v4-flash"
RUNTIME_POLICY = {
    "profile": "private-safe-isolated",
    "credentials": "external trusted runtime input; excluded from snapshot",
}
MANIFEST_NAME = "manifest.json"
ARCHIVE_NAME = "source.zip"
SNAPSHOT_SCHEMA = "phase7-identity/1"
_OMIT_DIRS = {
    ".git", ".pytest_cache", ".ruff_cache", "__pycache__", ".mypy_cache",
    ".cache", ".vite", ".turbo", "coverage", "node_modules", "dist", "build",
    "runs", "run-artifacts", "artifacts", "tmp",
}
_TEXT_SUFFIXES = {
    ".py", ".md", ".json", ".yaml", ".yml", ".txt", ".sql", ".j2", ".jinja",
    ".jinja2", ".prompt",
}
_WEB_CONFIG_NAMES = {
    "package.json", "package-lock.json", "bun.lock", "bun.lockb", "pnpm-lock.yaml",
    "yarn.lock", "vite.config.ts", "vite.config.js", "tsconfig.json", "tsconfig.build.json",
    "tailwind.config.js", "postcss.config.js", "components.json", "index.html", "README.md",
    "eslint.config.js", "eslint.config.mjs", "eslint.config.ts", ".eslintrc.json",
    "vitest.config.ts", "playwright.config.ts", "bunfig.toml", "biome.json",
    "postcss.config.mjs", "tailwind.config.ts", "tsconfig.app.json", "tsconfig.node.json",
    "next.config.js", "next.config.ts",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_files(root: Path, relative_dir: str) -> list[Path]:
    base = root / relative_dir
    if not base.exists() or not base.is_dir() or base.is_symlink():
        return []
    found: list[Path] = []
    for current, dirs, names in os.walk(base, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in _OMIT_DIRS
                         and not (current_path / d).is_symlink())
        for name in sorted(names):
            path = current_path / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return found


def _selected_paths(root: Path) -> list[Path]:
    selected: set[Path] = set()

    def add_tree(relative_dir: str, suffixes: set[str]) -> None:
        selected.update(path for path in _iter_files(root, relative_dir)
                        if path.suffix.lower() in suffixes)

    add_tree("nanobot", {".py"})
    add_tree("nanobot/templates", _TEXT_SUFFIXES)
    add_tree("nanobot/skills", _TEXT_SUFFIXES)
    add_tree("evals", _TEXT_SUFFIXES - {".sql"})
    selected.update(_iter_files(root, "evals/cases"))
    selected.update(_iter_files(root, "evals/fixtures"))
    add_tree("tests/evals", {".py"})
    add_tree(".github/workflows", {".yml", ".yaml"})
    add_tree("tests/knowledge/mail", {".py", ".sql"})
    add_tree("docs/validation-cases", {".md", ".txt", ".json", ".yaml", ".yml"})

    # Include bridge and WebUI source/assets, plus their root build manifests.
    for relative_dir in ("bridge/src", "webui/src", "webui/public"):
        selected.update(_iter_files(root, relative_dir))
    for relative_dir in ("bridge", "webui"):
        selected.update(path for path in _iter_files(root, relative_dir)
                        if path.parent == root / relative_dir and
                        (path.name in _WEB_CONFIG_NAMES or path.name.startswith(".env.example")))

    root_files = {
        "AGENTS.md", "pyproject.toml", "uv.lock", "holdout-registry.json",
    }
    selected.update(root / name for name in root_files if (root / name).is_file())
    selected.update(path for path in _iter_files(root, ".agent")
                    if path.suffix.lower() in _TEXT_SUFFIXES)
    selected.update(path for path in _iter_files(root, "tests/tools")
                    if path.name == "test_tool_validation.py")

    # The selector is deliberately explicit: private configs, API keys, VCS data,
    # caches and prior run artifacts are never discovered by a repository-wide scan.
    return sorted(selected, key=lambda p: _relative(root, p))


def _environment() -> dict[str, Any]:
    env_name = os.environ.get("CONDA_DEFAULT_ENV", "")
    prefix = os.environ.get("CONDA_PREFIX", "")
    if env_name != "nanobot" or not prefix:
        raise RuntimeError("conda_environment_required:nanobot")
    try:
        active_prefix = Path(prefix).resolve(strict=True)
        python_prefix = Path(sys.prefix).resolve(strict=True)
    except OSError:
        raise RuntimeError("conda_environment_mismatch:nanobot") from None
    if active_prefix != python_prefix or active_prefix.name.casefold() != "nanobot":
        raise RuntimeError("conda_environment_mismatch:nanobot")

    packages: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if name and version:
            packages[name.casefold().replace("_", "-")] = version

    cpu_facts = {
        "processor": platform.processor(),
        "machine": platform.machine(),
        "logical_count": os.cpu_count(),
    }
    try:
        locale_name = locale.getlocale()[0]
    except (ValueError, TypeError):
        locale_name = None
    try:
        zone = datetime.now().astimezone().tzinfo
        timezone_name = getattr(zone, "key", None) or str(zone)
    except (OSError, ValueError):
        timezone_name = time.tzname[0] if time.tzname else None

    return {
        "conda_environment": "nanobot",
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "packages": dict(sorted(packages.items())),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu_sha256": _sha256(_canonical(cpu_facts)),
        },
        "locale": locale_name,
        "timezone": timezone_name,
    }


def _safe_conditions(conditions: dict[str, Any] | None) -> dict[str, Any]:
    supplied = {} if conditions is None else conditions
    if not isinstance(supplied, dict):
        raise TypeError("conditions_must_be_object")

    def reject_credential_fields(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("condition_keys_must_be_strings")
                if re.search(r"(?:api.?key|password|secret|credential|authorization|bearer)",
                             key, re.IGNORECASE):
                    raise ValueError("credential_condition_forbidden")
                reject_credential_fields(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                reject_credential_fields(child)

    reject_credential_fields(supplied)
    if conditions is not None and supplied.get("model_id") != MODEL_ID:
        raise ValueError("model_id_must_be_deepseek_v4_flash")
    result = ({
        "model_id": MODEL_ID,
        "mode": {"status": "unknown"},
        "budget": {"status": "unknown"},
        "pricing": {"status": "unknown"},
    } if conditions is None else dict(supplied))
    # Validate serializability now so failures happen before creating output files.
    _canonical(result)
    return result


def _credential_values(root: Path) -> list[bytes]:
    """Blind-check both candidate and workspace api.txt values without exposing them."""
    values: set[str] = set()
    credential_files = {root / "api.txt", ROOT / "api.txt"}
    for path in credential_files:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise RuntimeError("credential_blind_check_failed") from None
        if not text:
            continue
        values.add(text)
        for line in text.splitlines():
            candidate = line.strip().strip("\"'")
            if "=" in candidate:
                candidate = candidate.split("=", 1)[1].strip().strip("\"'")
            elif ":" in candidate and not candidate.startswith(("http:", "https:")):
                candidate = candidate.split(":", 1)[1].strip().strip("\"'")
            if candidate:
                values.add(candidate)
    placeholders = {"your_api_key", "your-api-key", "replace_me", "changeme", "<api_key>"}
    return [value.encode("utf-8") for value in values
            if len(value) >= 8 and value.casefold() not in placeholders]


def _group_names(relative: str) -> set[str]:
    path = PurePosixPath(relative)
    groups = {"code"}
    if relative.startswith(("nanobot/templates/", "nanobot/skills/")):
        groups.add("skill")
        groups.add("prompt")
    if relative.startswith("nanobot/agent/tools/") or relative == "tests/tools/test_tool_validation.py":
        groups.add("tool")
    if (relative.startswith(("evals/cases/", "evals/fixtures/", "docs/validation-cases/"))
            or relative in {"evals/semantic_samples.py", "evals/holdout.py",
                            "evals/holdout-registry.json", "holdout-registry.json"}
            or relative.endswith("eval_contract_cases.py")):
        groups.add("cases")
    if relative.startswith("evals/") and path.suffix == ".py":
        if path.name in {"scorer.py", "batches.py", "batch_audit.py", "metrics.py",
                         "semantic.py", "repeat.py", "holdout.py"}:
            groups.add("scoring")
        if path.name in {"grading_transport.py", "semantic_run.py"}:
            groups.add("judge")
        if path.name == "grading_transport.py":
            groups.add("prompt")
    if relative in {"nanobot/utils/prompt_templates.py", "nanobot/agent/context.py"}:
        groups.add("prompt")
    if (relative in {"pyproject.toml", "uv.lock", "AGENTS.md"}
            or relative.startswith((".agent/", ".github/workflows/"))):
        groups.add("project_config")
    if relative.startswith(("bridge/", "webui/")):
        groups.add("bridge_webui")
    return groups


def _group_hash(files: dict[str, str], data: Any = None) -> str:
    return _sha256(_canonical({"files": files, "data": data}))


def _identity_hash(groups: dict[str, dict[str, Any]]) -> str:
    identity = {name: value["identity_hash"] for name, value in sorted(groups.items())}
    return _sha256(_canonical(identity))


def _manifest_identity(manifest: dict[str, Any]) -> str:
    return _identity_hash(manifest["groups"])


def _archive_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("unsafe_archive_path")
    path = PurePosixPath(name)
    if (path.is_absolute() or ":" in name or any(part in {"", ".", ".."} for part in name.split("/"))
            or path.as_posix() != name):
        raise ValueError("unsafe_archive_path")
    return name


def freeze(source_root: str | Path | None = None,
           output: str | Path = "tmp/phase7-snapshots/frozen",
           conditions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze selected source bytes and the comparison conditions into a new bundle."""
    environment = _environment()
    frozen_conditions = _safe_conditions(conditions)
    root = Path(ROOT if source_root is None else source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source_root_must_be_directory")

    selected = _selected_paths(root)
    if not selected:
        raise ValueError("empty_source_selection")
    blobs: dict[str, bytes] = {}
    for path in selected:
        relative = _archive_name(_relative(root, path))
        try:
            blobs[relative] = path.read_bytes()
        except OSError:
            raise RuntimeError("source_read_failed:" + relative) from None

    credentials = _credential_values(root)
    if any(secret in _canonical(frozen_conditions) for secret in credentials):
        raise RuntimeError("credential_found_in_conditions")
    for relative, content in blobs.items():
        if any(secret in content for secret in credentials):
            raise RuntimeError("credential_found_in_selected_source:" + relative)

    file_hashes = {name: _sha256(blob) for name, blob in sorted(blobs.items())}
    group_members: dict[str, dict[str, str]] = {
        name: {} for name in ("code", "prompt", "skill", "tool", "cases", "scoring",
                             "judge", "environment", "conditions", "project_config",
                             "bridge_webui")
    }
    for relative, digest in file_hashes.items():
        for group in _group_names(relative):
            group_members[group][relative] = digest

    condition_identity = {"conditions": frozen_conditions, "runtime_policy": RUNTIME_POLICY}
    identity_data = {"environment": environment, "conditions": condition_identity}
    groups: dict[str, dict[str, Any]] = {}
    for name, members in group_members.items():
        data = identity_data.get(name)
        groups[name] = {"identity_hash": _group_hash(members, data), "files": members}
        if data is not None:
            groups[name]["data"] = data

    manifest: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "snapshot_id": "sha256:" + _identity_hash(groups),
        "created_utc": datetime.now(UTC).isoformat(),
        "source_root": "workspace" if root == ROOT.resolve() else "candidate",
        "model_id": MODEL_ID,
        "runtime_policy": RUNTIME_POLICY,
        "environment": environment,
        "conditions": frozen_conditions,
        "groups": groups,
        "files": file_hashes,
        "source_zip": ARCHIVE_NAME,
    }

    bundle = Path(output).resolve()
    try:
        bundle.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError("snapshot_output_already_exists") from None
    archive_path = bundle / ARCHIVE_NAME
    try:
        with archive_path.open("xb") as raw_archive:
            with zipfile.ZipFile(raw_archive, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=9) as archive:
                for name, content in sorted(blobs.items()):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, content)
        manifest_path = bundle / MANIFEST_NAME
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    except Exception:
        # Keep a failed bundle visibly incomplete; verify() will reject it.
        raise
    return manifest


def verify(output: str | Path) -> dict[str, Any]:
    """Verify manifest hashes, source archive inventory and the derived snapshot ID."""
    bundle = Path(output)
    errors: list[str] = []
    manifest: dict[str, Any] | None = None
    try:
        manifest = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"ok": False, "errors": ["manifest_unreadable"], "manifest": None}

    if not isinstance(manifest, dict):
        return {"ok": False, "errors": ["manifest_invalid"], "manifest": None}
    if manifest.get("schema_version") != SNAPSHOT_SCHEMA:
        errors.append("schema_version_mismatch")
    if manifest.get("model_id") != MODEL_ID:
        errors.append("model_id_mismatch")
    files = manifest.get("files")
    groups = manifest.get("groups")
    if not isinstance(files, dict) or not all(
            isinstance(name, str) and isinstance(digest, str) for name, digest in files.items()):
        errors.append("manifest_files_invalid")
        files = {}
    if not isinstance(groups, dict):
        errors.append("manifest_groups_invalid")
        groups = {}

    if manifest.get("source_zip") != ARCHIVE_NAME:
        errors.append("source_zip_name_mismatch")
    try:
        with zipfile.ZipFile(bundle / ARCHIVE_NAME, "r") as archive:
            infos = archive.infolist()
            names = [_archive_name(info.filename) for info in infos]
            if len(names) != len(set(names)):
                errors.append("archive_duplicate_paths")
            if set(names) != set(files):
                errors.append("archive_inventory_mismatch")
            for info, name in zip(infos, names):
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    errors.append("archive_nonregular_entry:" + name)
                    continue
                try:
                    if _sha256(archive.read(info)) != files.get(name):
                        errors.append("archive_hash_mismatch:" + name)
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    errors.append("archive_read_failed:" + name)
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        errors.append("archive_unreadable_or_unsafe")

    expected_groups = {"code", "prompt", "skill", "tool", "cases", "scoring", "judge",
                       "environment", "conditions", "project_config", "bridge_webui"}
    if set(groups) != expected_groups:
        errors.append("group_inventory_mismatch")
    expected_members = {name: {} for name in expected_groups}
    for path, digest in files.items():
        for name in _group_names(path):
            expected_members[name][path] = digest
    for name, group in groups.items():
        if not isinstance(group, dict) or not isinstance(group.get("files"), dict):
            errors.append("group_invalid:" + str(name))
            continue
        group_files = group["files"]
        if name in expected_members and group_files != expected_members[name]:
            errors.append("group_membership_mismatch:" + name)
        if any(files.get(path) != digest for path, digest in group_files.items()):
            errors.append("group_file_hash_mismatch:" + str(name))
        data = group.get("data")
        if name == "environment":
            if data != manifest.get(name):
                errors.append("group_data_mismatch:" + name)
        elif name == "conditions":
            if data != {"conditions": manifest.get("conditions"),
                        "runtime_policy": manifest.get("runtime_policy")}:
                errors.append("group_data_mismatch:" + name)
        elif "data" in group:
            errors.append("unexpected_group_data:" + str(name))
        if group.get("identity_hash") != _group_hash(group_files, data):
            errors.append("group_hash_mismatch:" + str(name))

    if all(isinstance(group, dict) and isinstance(group.get("files"), dict)
           for group in groups.values()):
        try:
            if manifest.get("snapshot_id") != "sha256:" + _manifest_identity(manifest):
                errors.append("snapshot_id_mismatch")
        except (KeyError, TypeError, ValueError):
            errors.append("snapshot_id_invalid")
    else:
        errors.append("snapshot_id_invalid")
    conditions = manifest.get("conditions")
    if not isinstance(conditions, dict) or conditions.get("model_id") != MODEL_ID:
        errors.append("conditions_model_mismatch")
    if manifest.get("runtime_policy") != RUNTIME_POLICY:
        errors.append("runtime_policy_mismatch")
    if not isinstance(manifest.get("environment"), dict):
        errors.append("environment_invalid")
    return {"ok": not errors, "errors": errors, "manifest": manifest}


def _destination_path(destination: str | Path) -> Path:
    root = ROOT.resolve()
    allowed_parent = root / "tmp" / "phase7-snapshots"
    target = Path(destination)
    if not target.is_absolute():
        target = root / target
    # Resolve the parent, while preserving the non-existent final component.
    parent = target.parent.resolve()
    expected_parent = allowed_parent.resolve()
    if (parent != expected_parent
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", target.name)):
        raise ValueError("materialize_destination_outside_phase7_snapshots")
    if target.exists() or target.is_symlink():
        raise FileExistsError("materialize_destination_already_exists")
    return parent / target.name


def materialize(snapshot: str | Path, destination: str | Path) -> Path:
    """Safely unpack a verified snapshot into a new isolated source workspace."""
    checked = verify(snapshot)
    if not checked["ok"]:
        raise ValueError("snapshot_verification_failed:" + ",".join(checked["errors"]))
    target = _destination_path(destination)
    root = ROOT.resolve()
    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    # Reject a symlinked path component before writing candidate-controlled names.
    if parent.is_symlink() or not parent.resolve().is_relative_to(root):
        raise ValueError("materialize_destination_unsafe")

    archive_path = Path(snapshot) / ARCHIVE_NAME
    target.mkdir()
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                name = _archive_name(info.filename)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ValueError("archive_nonregular_entry:" + name)
                output_path = target.joinpath(*PurePosixPath(name).parts)
                if not output_path.resolve().is_relative_to(target.resolve()):
                    raise ValueError("unsafe_archive_path")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with output_path.open("xb") as stream:
                    stream.write(archive.read(info))
    except Exception:
        # The target was required to be new and is owned by this call.
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        target.rmdir()
        raise
    return target

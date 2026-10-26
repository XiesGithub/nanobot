"""Create a bounded, integrity-checked staging tree for CI evaluation uploads."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

WRAPPER_RE = re.compile(
    r"(?:linux|windows)-(?:smoke|development|nightly|release)-[0-9]+-[0-9]+\Z")
OUTER_FILES = {"runner-start.json", "job-completion.json", "source-checkout.json"}
FORBIDDEN_PARTS = {".git", ".aws", ".ssh"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_rel(name: str) -> PurePosixPath:
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or ":" in name):
        raise ValueError("unsafe_index_path")
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name
            or any(part in ("", ".", "..") for part in path.parts)):
        raise ValueError("unsafe_index_path")
    return path


def _is_credential(rel: str) -> bool:
    parts = rel.split("/")
    low_parts = [part.lower() for part in parts]
    low = "/".join(low_parts)
    return (any(part in FORBIDDEN_PARTS for part in low_parts)
            or any(part == "api.txt" or part.startswith(".env")
                   or part in {"credentials.json", "secrets.json"} for part in low_parts)
            or low == ".nanobot/config.json" or low.endswith("/.nanobot/config.json"))


def _walk_files(root: Path) -> dict[str, Path]:
    """Return files without following links; reject links and path escapes."""
    root = root.resolve(strict=True)
    found: dict[str, Path] = {}
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError("symlink_rejected:" + item.relative_to(root).as_posix())
        if not item.is_file():
            continue
        resolved = item.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError("path_escape_rejected")
        rel = item.relative_to(root).as_posix()
        found[rel] = item
    return found


def _load_index(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("index_invalid:" + path.name) from exc
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                         for k, v in data.items()):
        raise ValueError("index_must_be_object:" + path.name)
    normalized: dict[str, str] = {}
    for name, checksum in data.items():
        p = _safe_rel(name)
        if name in normalized or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("index_entry_invalid:" + path.name)
        normalized[p.as_posix()] = checksum
    return normalized


def _verify_index(root: Path, index_name: str) -> dict[str, str]:
    index_path = root / index_name
    if not index_path.is_file() or index_path.is_symlink():
        raise ValueError("missing_index:" + index_name)
    expected = _load_index(index_path)
    actual = {name: _sha(path) for name, path in _walk_files(root).items()
              if name != index_name}
    if actual != expected:
        raise ValueError("inventory_or_hash_mismatch:" + index_name)
    return expected


def _validate_wrapper(wrapper: Path) -> dict[str, Path]:
    if not WRAPPER_RE.fullmatch(wrapper.name):
        raise ValueError("unsafe_wrapper_name")
    files = _walk_files(wrapper)
    for rel in files:
        if _is_credential(rel):
            raise ValueError("credential_in_wrapper:" + rel)
    _verify_index(wrapper, "ci-artifact-index.json")
    run = wrapper / "run"
    if not run.is_dir():
        raise ValueError("missing_run_directory")
    _verify_index(run, "artifact-index.json")
    return files


def _selected_outer(source: Path) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for name in OUTER_FILES:
        path = source / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe_outer_file:" + name)
            if _is_credential(name):
                raise ValueError("credential_in_outer_scope:" + name)
            selected[name] = path
    for dirname, suffix in (("runner", ".log"), ("gates", ".json")):
        folder = source / dirname
        if not folder.exists():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("unsafe_outer_directory:" + dirname)
        for rel, path in _walk_files(folder).items():
            if path.suffix.lower() == suffix:
                staged_rel = f"{dirname}/{rel}"
                if _is_credential(staged_rel):
                    raise ValueError("credential_in_outer_scope:" + staged_rel)
                selected[staged_rel] = path
    return selected


def build_staging(source: str | Path, output: str | Path,
                  wrapper: str) -> dict[str, Any]:
    """Validate existing evidence, copy only allowlisted paths, and verify bytes again."""
    source = Path(source).resolve(strict=True)
    if not WRAPPER_RE.fullmatch(wrapper):
        raise ValueError("unsafe_wrapper_name")
    source_wrapper = source / wrapper
    if source_wrapper.is_symlink() or not source_wrapper.is_dir():
        raise ValueError("wrapper_missing_or_unsafe")
    wrapper_files = _validate_wrapper(source_wrapper)
    outer = _selected_outer(source)
    selected: dict[str, Path] = {f"{wrapper}/{rel}": path
                                 for rel, path in wrapper_files.items()}
    selected.update(outer)
    for rel in selected:
        _safe_rel(rel)
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("output_must_be_new")
    baseline = {rel: _sha(path) for rel, path in selected.items()}
    output.mkdir(parents=True, exist_ok=False)
    try:
        for rel, src in selected.items():
            dest = output.joinpath(*_safe_rel(rel).parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink() or not src.is_file():
                raise ValueError("source_changed_during_copy:" + rel)
            shutil.copyfile(src, dest)
        # Re-validate source and the copied wrapper, catching mutation during copy.
        _validate_wrapper(source_wrapper)
        staged_wrapper = output / wrapper
        _validate_wrapper(staged_wrapper)
        staged_inventory = {rel: _sha(output.joinpath(*_safe_rel(rel).parts))
                            for rel in selected}
        source_inventory = {rel: _sha(path) for rel, path in selected.items()}
        if source_inventory != baseline or staged_inventory != baseline:
            raise ValueError("staging_copy_hash_mismatch")
        return {"ok": True, "quality_passed": False, "execution_passed": False,
                "receipt_type": "local_integrity_only", "wrapper": wrapper,
                "files": staged_inventory}
    except BaseException:
        # Keep failed staging as local diagnostic evidence; caller must not upload it.
        raise


def verify_zip(path: str | Path, wrapper: str) -> dict[str, Any]:
    """Verify a local synthetic ZIP: safe unique names and both exact hash indexes."""
    if not WRAPPER_RE.fullmatch(wrapper):
        raise ValueError("unsafe_wrapper_name")
    with zipfile.ZipFile(path) as archive:
        files: dict[str, bytes] = {}
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            if not name:
                continue
            rel = _safe_rel(name)
            if name in files:
                raise ValueError("duplicate_zip_entry:" + name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("zip_symlink_rejected:" + name)
            if info.is_dir():
                continue
            if not (name.startswith(wrapper + "/") or name in OUTER_FILES
                    or name.startswith("runner/") and name.lower().endswith(".log")
                    or name.startswith("gates/") and name.lower().endswith(".json")):
                raise ValueError("zip_path_out_of_scope:" + name)
            if _is_credential(name[len(wrapper) + 1:] if name.startswith(wrapper + "/")
                              else name):
                raise ValueError("credential_in_zip:" + name)
            files[name] = archive.read(info)
    prefix = wrapper + "/"
    wrapper_files = {name[len(prefix):]: data for name, data in files.items()
                     if name.startswith(prefix)}
    for rel in wrapper_files:
        if _is_credential(rel):
            raise ValueError("credential_in_zip_wrapper:" + rel)
    def check_index(items: dict[str, bytes], index_name: str) -> dict[str, str]:
        raw = items.get(index_name)
        if raw is None:
            raise ValueError("missing_index:" + index_name)
        try:
            index = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("index_invalid:" + index_name) from exc
        if not isinstance(index, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in index.items()):
            raise ValueError("index_must_be_object:" + index_name)
        actual = {name: hashlib.sha256(data).hexdigest() for name, data in items.items()
                  if name != index_name}
        if actual != index:
            raise ValueError("inventory_or_hash_mismatch:" + index_name)
        return actual
    # ZIP root corresponds to staging root. Restrict validation to wrapper for ci index,
    # and run descendants for the nested index.
    check_index(wrapper_files, "ci-artifact-index.json")
    run_items = {name[len("run/"):]: data for name, data in wrapper_files.items()
                 if name.startswith("run/")}
    check_index(run_items, "artifact-index.json")
    return {"ok": True, "quality_passed": False, "execution_passed": False,
            "receipt_type": "local_integrity_only", "wrapper": wrapper,
            "files": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wrapper", required=True)
    args = parser.parse_args()
    print(json.dumps(build_staging(args.source, args.output, args.wrapper), ensure_ascii=False))


if __name__ == "__main__":
    main()

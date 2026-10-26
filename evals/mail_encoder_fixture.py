"""Prepare or verify the fixed local encoder fixture used by mail evaluation tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

from evals.common import ROOT, read

PIN_PATH = ROOT / "evals/mail_semantic_encoder.json"
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 110 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _model_manifest(pin: dict) -> bytes:
    return json.dumps({"model": pin["model"], "revision": pin["revision"]}).encode("utf-8")


def _validate_pin(pin: dict) -> dict[str, str]:
    if (not isinstance(pin, dict) or pin.get("model") != "BAAI/bge-small-zh-v1.5"
            or not re.fullmatch(r"[0-9a-f]{40}", str(pin.get("revision", "")))):
        raise ValueError("mail_encoder_pin_identity_invalid")
    files = pin.get("files")
    if not isinstance(files, dict) or len(files) != 11:
        raise ValueError("mail_encoder_pin_file_set_invalid")
    checked = {}
    for name, expected in files.items():
        if not isinstance(name, str):
            raise ValueError("mail_encoder_pin_file_invalid")
        path = Path(name)
        if (path.is_absolute() or ".." in name.replace("\\", "/").split("/")
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("mail_encoder_pin_file_invalid")
        checked[name] = expected
    if "nanobot-model-revision.json" not in checked:
        raise ValueError("mail_encoder_manifest_pin_missing")
    return checked


def _download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "nanobot-eval-fixture/1"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("mail_encoder_file_too_large")
    return data


def prepare(destination: Path, *, download_missing: bool = False,
            fetch=_download_bytes) -> dict:
    pin = read(PIN_PATH)
    files = _validate_pin(pin)
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    total = 0
    missing = []
    verified = {}
    revision = pin["revision"]
    for name, expected in sorted(files.items()):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("mail_encoder_path_outside_fixture")
        if name == "nanobot-model-revision.json":
            data = _model_manifest(pin)
        elif path.is_file():
            data = path.read_bytes()
        elif download_missing:
            data = fetch(
                f"https://huggingface.co/{pin['model']}/resolve/{revision}/{name}"
            )
        else:
            missing.append(name)
            continue
        if len(data) > MAX_FILE_BYTES or _sha256(data) != expected:
            raise ValueError("mail_encoder_fixture_hash_mismatch:" + name)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("mail_encoder_fixture_total_too_large")
        if not path.is_file() or path.read_bytes() != data:
            if not download_missing and name != "nanobot-model-revision.json":
                raise ValueError("mail_encoder_fixture_missing:" + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=".mail-encoder-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        verified[name] = expected
    if missing:
        raise FileNotFoundError("mail_encoder_fixture_missing:" + ",".join(missing))
    if len(verified) != 11:
        raise ValueError("mail_encoder_fixture_incomplete")
    return {"model": pin["model"], "revision": revision, "files_verified": len(verified),
            "bytes_verified": total, "destination": root.as_posix()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "tmp/mail-rag-model")
    parser.add_argument("--download-missing", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.destination, download_missing=args.download_missing)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"mail encoder fixture preparation failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

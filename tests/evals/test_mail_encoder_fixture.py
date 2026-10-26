import hashlib

import pytest

from evals.mail_encoder_fixture import _model_manifest, prepare


def test_pinned_local_encoder_fixture_is_complete_and_hash_verified():
    from evals.common import ROOT
    from evals.mail_task_cases import encoder_pin

    result = prepare(ROOT / encoder_pin()["path"])

    assert result["files_verified"] == 11
    assert result["bytes_verified"] < 110 * 1024 * 1024
    assert result["revision"] == encoder_pin()["revision"]


def test_missing_encoder_fixture_fails_with_exact_missing_names(tmp_path):
    from evals.mail_task_cases import encoder_pin

    pin = encoder_pin()
    with pytest.raises(FileNotFoundError, match="mail_encoder_fixture_missing:") as error:
        prepare(tmp_path / pin["path"])

    assert "model.safetensors" in str(error.value)


def test_fixed_revision_download_is_bounded_pinned_and_hash_checked(tmp_path, monkeypatch):
    from evals import mail_encoder_fixture as fixture

    pin = {"model": "BAAI/bge-small-zh-v1.5", "revision": "a" * 40}
    payloads = {f"resource-{index}.bin": f"fixture-{index}".encode()
                for index in range(10)}
    payloads["nanobot-model-revision.json"] = _model_manifest(pin)
    pin["files"] = {name: hashlib.sha256(data).hexdigest()
                     for name, data in payloads.items()}
    monkeypatch.setattr(fixture, "read", lambda _path: pin)
    urls = []

    def fetch(url):
        urls.append(url)
        name = url.rsplit("/", 1)[-1]
        return payloads[name]

    result = fixture.prepare(tmp_path / "encoder", download_missing=True, fetch=fetch)

    assert result["files_verified"] == 11
    assert all(f"resolve/{pin['revision']}/" in url for url in urls)
    assert len(urls) == 10
    for name, expected in pin["files"].items():
        assert hashlib.sha256((tmp_path / "encoder" / name).read_bytes()).hexdigest() == expected


def test_modified_pinned_file_is_rejected(tmp_path, monkeypatch):
    from evals import mail_encoder_fixture as fixture

    pin = {"model": "BAAI/bge-small-zh-v1.5", "revision": "a" * 40}
    payloads = {f"resource-{index}.bin": f"fixture-{index}".encode()
                for index in range(10)}
    payloads["nanobot-model-revision.json"] = _model_manifest(pin)
    pin["files"] = {name: hashlib.sha256(data).hexdigest()
                     for name, data in payloads.items()}
    monkeypatch.setattr(fixture, "read", lambda _path: pin)
    destination = tmp_path / "encoder"
    destination.mkdir()
    (destination / "resource-0.bin").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="mail_encoder_fixture_hash_mismatch:resource-0.bin"):
        fixture.prepare(destination, download_missing=True,
                        fetch=lambda _url: pytest.fail("valid pinned files are never refetched"))

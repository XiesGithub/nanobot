"""Identity coverage for the frozen V065 contract and channel/security engineering."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from evals import identity

REPO = Path(__file__).resolve().parents[2]


def _write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    for name in (identity.V065_CONTRACT, "evals/v065_attachment_d.py",
                 "docs/validation-cases/08-transport.md"):
        _write(root, name, (REPO / name).read_bytes())
    for name in ("tests/channels/test_boundary.py", "tests/security/test_scope.py"):
        _write(root, name, b"def test_boundary(): pass\n")
    for name in ("docs/private.json", "docs/other/contract.json",
                 "tests/security/credentials.json", "tests/channels/api.txt"):
        _write(root, name, b"excluded canary")
    # Avoid even the credential blind-check touching the live workspace api.txt.
    monkeypatch.setattr(identity, "ROOT", root)
    return root


def test_exact_contract_and_python_tests_are_in_source_zip(source, tmp_path):
    manifest = identity.freeze(source, tmp_path / "snapshot")
    expected = {identity.V065_CONTRACT, "tests/channels/test_boundary.py",
                "tests/security/test_scope.py"}
    assert expected <= manifest["files"].keys()
    assert identity.V065_CONTRACT in manifest["groups"]["cases"]["files"]
    assert not {"docs/private.json", "docs/other/contract.json",
                "tests/security/credentials.json", "tests/channels/api.txt"} & manifest["files"].keys()
    with zipfile.ZipFile(tmp_path / "snapshot" / identity.ARCHIVE_NAME) as archive:
        for name in expected:
            assert archive.read(name) == (source / name).read_bytes()
    assert identity.verify(tmp_path / "snapshot")["ok"]


@pytest.mark.parametrize("name", [identity.V065_CONTRACT, "tests/channels/test_boundary.py",
                                 "tests/security/test_scope.py"])
def test_each_added_source_changes_snapshot_identity(source, tmp_path, name):
    before = identity.freeze(source, tmp_path / "before")
    path = source / name
    path.write_bytes(path.read_bytes() + b"\n")
    after = identity.freeze(source, tmp_path / "after")
    assert before["snapshot_id"] != after["snapshot_id"]
    assert before["files"][name] != after["files"][name]


@pytest.mark.parametrize("mutation", ["missing", "tampered"])
def test_materialized_adapter_rejects_missing_or_tampered_contract(source, tmp_path, mutation):
    snapshot = tmp_path / "snapshot"
    identity.freeze(source, snapshot)
    materialized = identity.materialize(snapshot, source / "tmp/phase7-snapshots/owned")
    spec = importlib.util.spec_from_file_location("v065_identity_control", materialized / "evals/v065_attachment_d.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._contract_and_sources()["contract_sha256"] == module.CONTRACT_SHA256
    contract = materialized / identity.V065_CONTRACT
    if mutation == "missing":
        contract.unlink()
        expected = FileNotFoundError
    else:
        payload = json.loads(contract.read_text(encoding="utf-8"))
        payload["identity_tamper_control"] = True
        contract.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        expected = ValueError
    with pytest.raises(expected):
        module._contract_and_sources()

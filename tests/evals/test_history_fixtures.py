"""Negative controls for the portable historical regression data."""
import hashlib
import json
import zipfile

import pytest

from evals import history_fixtures


@pytest.mark.parametrize("fault", ["missing", "changed", "escape"])
def test_historical_bundle_rejects_missing_changed_or_escaping_member(tmp_path, monkeypatch, fault):
    root = tmp_path / "fixture"
    root.mkdir()
    expected = {"original/evidence.json": hashlib.sha256(b"original").hexdigest()}
    path = root / "engineering-history.zip"
    with zipfile.ZipFile(path, "x") as archive:
        if fault == "changed":
            archive.writestr("original/evidence.json", b"changed")
        elif fault == "escape":
            archive.writestr("../outside.json", b"original")
            expected = {"../outside.json": hashlib.sha256(b"original").hexdigest()}
    (root / "engineering-history-index.json").write_text(json.dumps({
        "zip_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "files": expected}), encoding="utf-8")
    monkeypatch.setattr(history_fixtures, "FIXTURES", root)
    with pytest.raises(ValueError, match="historical_fixture_"):
        history_fixtures.unpack_history(tmp_path / "target")
    assert not (tmp_path / "outside.json").exists()

"""No-browser failure-path controls for the V073 diagnostic collector."""
import json
from pathlib import Path

import pytest

from evals.browser_task_diagnostics import collect_v073


class Page:
    def __init__(self, broken=False):
        self.listeners = {}
        self.broken = broken

    def on(self, kind, callback):
        self.listeners[kind] = callback

    def remove_listener(self, kind, callback):
        assert self.listeners.pop(kind) is callback

    def evaluate(self, expression):
        if self.broken:
            raise RuntimeError("page closed")
        return {"click": None, "final_dom": None, "diagnostics": {"errors": []}}


def _payload(root: Path):
    return json.loads(next(root.glob("*.json")).read_text(encoding="utf-8"))


def test_zero_network_is_complete_and_never_claims_task_success(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_V073_DIAGNOSTICS_DIR", raising=False)
    page = Page()
    with collect_v073(page, tmp_path, "data-url"):
        pass
    evidence = _payload(tmp_path)
    assert evidence["complete"] and evidence["counts"] == {}
    assert evidence["verdict_authority"] is False
    assert not page.listeners


def test_failed_control_is_preserved_with_snapshot_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_V073_DIAGNOSTICS_DIR", raising=False)
    failure = AssertionError("original final DOM timeout")
    with pytest.raises(AssertionError) as observed:
        with collect_v073(Page(broken=True), tmp_path, "failure"):
            raise failure
    assert observed.value is failure
    assert "diagnostics failed" in failure.__notes__[0]
    evidence = _payload(tmp_path)
    assert not evidence["complete"]
    assert evidence["original_error"]["type"] == "AssertionError"
    assert evidence["diagnostic_errors"][0]["stage"] == "page_snapshot"


def test_lifecycle_page_argument_is_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_V073_DIAGNOSTICS_DIR", raising=False)
    page = Page()
    with collect_v073(page, tmp_path, "lifecycle"):
        for kind in ("domcontentloaded", "load", "close", "crash"):
            page.listeners[kind](page)
    evidence = _payload(tmp_path)
    assert evidence["complete"] and not evidence["diagnostic_errors"]
    assert evidence["counts"] == dict.fromkeys(("domcontentloaded", "load", "close", "crash"), 1)


def test_diagnostic_failure_cannot_pass_a_successful_control(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_V073_DIAGNOSTICS_DIR", raising=False)
    with pytest.raises(RuntimeError, match="incomplete"):
        with collect_v073(Page(broken=True), tmp_path, "missing"):
            pass
    assert not _payload(tmp_path)["complete"]


def test_events_are_bounded_and_urls_exclude_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("NANOBOT_V073_DIAGNOSTICS_DIR", raising=False)
    page = Page()
    with collect_v073(page, tmp_path, "bounded"):
        for _ in range(300):
            page.listeners["console"](type("Console", (), {"type": "log", "text": "private content"})())
        page.listeners["request"](type("Request", (), {
            "url": "https://user:secret@example.test/x?token=private", "method": "GET",
            "resource_type": "fetch"})())
    evidence = _payload(tmp_path)
    assert evidence["counts"]["console"] == 300
    assert len(evidence["events"]) == 256
    rendered = json.dumps(evidence)
    assert "private content" not in rendered and "token=private" not in rendered
    assert "user:secret" not in rendered

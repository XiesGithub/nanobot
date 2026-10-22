from __future__ import annotations

import asyncio
from pathlib import Path

from evals.batch_mail import cases, execute
from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.ingestion import MailWorker
from nanobot.knowledge.mail.store import MailStore
from nanobot.utils.observability import observation_context


def _workspace(tmp_path: Path, case_id: str) -> Path:
    workspace = tmp_path / case_id
    workspace.mkdir()
    for relative, content in cases()[case_id]["fixtures"].items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workspace


def test_inventory_and_explicit_capability_gaps():
    catalog = cases()
    assert set(catalog) == {f"V{i:03}" for i in range(81, 121)}
    for case_id, spec in catalog.items():
        assert {"scope", "entry", "fixtures", "assertions", "supported", "limitations"} <= spec.keys()
        assert isinstance(spec["fixtures"], dict)
        if not spec["supported"]:
            assert spec["limitations"]
        else:
            assert spec["assertions"]


def test_mail_ingress_retrieval_access_version_and_management_contracts(tmp_path):
    for case_id in ("V081", "V082", "V083", "V084", "V086", "V087", "V090", "V092", "V093",
                    "V095", "V096", "V098", "V108"):
        observed = asyncio.run(execute(case_id, _workspace(tmp_path, case_id)))
        assert observed
        _score_observed(case_id, observed)


def test_mail_summary_snapshot_and_session_atomic_recovery(tmp_path):
    for case_id in ("V097", "V119", "V120"):
        observed = asyncio.run(execute(case_id, _workspace(tmp_path, case_id)))
        assert observed
        _score_observed(case_id, observed)


def test_runner_deterministic_retry_tool_finalization_length_and_budget(tmp_path):
    for case_id in ("V111", "V112", "V113", "V114", "V115", "V116", "V117"):
        observed = asyncio.run(execute(case_id, _workspace(tmp_path, case_id)))
        _score_observed(case_id, observed)


def _score_observed(case_id, observed):
    failures = []
    for assertion in cases()[case_id]["assertions"]:
        key, op, expected = assertion["key"], assertion["op"], assertion["expected"]
        if key not in observed:
            failures.append(key)
        elif op == "equal" and observed[key] != expected:
            failures.append(key)
        elif op == "contains" and expected not in observed[key]:
            failures.append(key)
        elif op == "true" and observed[key] is not True:
            failures.append(key)
    assert not failures, (case_id, failures, observed)


def test_permanent_error_keeps_diagnostics_out_of_reply_and_request_history(tmp_path):
    observed = asyncio.run(execute("V116", _workspace(tmp_path, "V116")))
    _score_observed("V116", observed)
    assert observed["diagnostic_preserved"] is True
    assert observed["error_details"]["code"] == "invalid_request"
    assert observed["error"] == observed["final_content"]
    assert observed["attempt_count"] == 1 and observed["stop_reason"] == "error"
    assert observed["request_log"][0]["messages"] == [{"role": "user", "content": "2加3是多少？"}]


def test_length_recovery_delivers_the_history_fragments_once(tmp_path):
    observed = asyncio.run(execute("V115", _workspace(tmp_path, "V115")))
    _score_observed("V115", observed)
    assert observed["answer"] == observed["assistant_history"] == "费用为120元"
    assert observed["stop_reason"] == "completed" and observed["attempt_count"] == 2
    assert [m["content"] for m in observed["messages"] if m["role"] == "assistant"] == ["费用为", "120元"]
    assert not any(m["role"] == "assistant" for m in observed["request_log"][0]["messages"])
    assert [m["content"] for m in observed["request_log"][1]["messages"]
            if m["role"] == "assistant"] == ["费用为"]


def test_worker_spans_have_causal_child_and_unassigned_periodic_accounting(tmp_path):
    events = []
    store = MailStore(tmp_path / "mail-store", MailKnowledgeConfig(semantic_enabled=False))
    worker = MailWorker(store)

    class EventSink:
        def __call__(self, kind, **fields):
            events.append({"kind": kind, **fields})

    with observation_context(EventSink(), "case-task"):
        with worker._background_task("job-fixture", "mail_ingest", causal_parent=True):
            pass
        with worker._background_task("collection", "mail_version_collection", causal_parent=False):
            pass

    by_purpose = {event["purpose"]: event for event in events if event["kind"] == "background_task"}
    assert by_purpose["mail_ingest"]["task_id"] == "case-task:mail:job-fixture"
    assert by_purpose["mail_ingest"]["parent_task_id"] == "case-task"
    assert by_purpose["mail_version_collection"]["task_id"].startswith("mail-worker:")
    assert by_purpose["mail_version_collection"]["parent_task_id"] is None
    assert by_purpose["mail_version_collection"]["accounting"] == "unassigned_periodic_overhead"

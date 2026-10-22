"""Program evidence checks for full-task semantic evaluations.

The module binds a delivered answer and (for V007) a real successful source
read to the same isolated trial. It does not perform semantic adjudication.
"""

from __future__ import annotations

import copy
import re

from evals.common import ROOT, canonical, digest, inside

FULL_TASK_METHOD = "full_task_semantic_v1"
FULL_TASK_CASE_VERSION = "full-task-semantic/1.0"
_BUDGET_LINE = re.compile(r"^\s*预算\s*[：:]\s*120\s*元\s*$")
_AMOUNT_ONLY = re.compile(
    r"(?:120(?:\.0+)?\s*(?:元|人民币|yuan|cny|rmb|¥|￥)|"
    r"(?:人民币|cny|rmb|¥|￥)\s*120(?:\.0+)?(?:\s*元)?)",
    re.IGNORECASE,
)


def _event_id(event):
    value = event.get("event_id")
    return value if isinstance(value, str) and value else None


def _source_document(case):
    """Verify the frozen case source hash, returning its repository-relative path."""
    source_ref = case.get("source_ref")
    expected = case.get("source_sha256")
    if not isinstance(source_ref, str) or not isinstance(expected, str):
        return None, None, "case_source_missing"
    try:
        relative = source_ref.split("#", 1)[0]
        path = inside(ROOT, relative)
        actual = digest(path.read_bytes())
    except (OSError, ValueError):
        return None, None, "case_source_unavailable"
    if actual != expected:
        return relative, actual, "case_source_hash_mismatch"
    return relative, actual, None


def _fixture(case, relative_path):
    matches = [row for row in case.get("initial_state", {}).get("fixtures", [])
               if row.get("path") == relative_path]
    if len(matches) != 1:
        return None, None, "source_fixture_missing_or_ambiguous"
    row = matches[0]
    try:
        path = inside(ROOT / "evals", row["source"])
        raw = path.read_bytes()
    except (KeyError, OSError, ValueError):
        return None, None, "source_fixture_unavailable"
    actual = digest(raw)
    if actual != row.get("sha256"):
        return None, actual, "source_fixture_hash_mismatch"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, actual, "source_fixture_not_utf8"
    return (row, text), actual, None


def _snapshot_hash(snapshot, path):
    item = snapshot.get(path) if isinstance(snapshot, dict) else None
    return item.get("sha256") if isinstance(item, dict) else None


def _unique_delivery(events):
    rows = [e for e in events if e.get("type") == "delivery" and e.get("phase") == "point"]
    if len(rows) != 1:
        return None, "delivery_missing" if not rows else "delivery_ambiguous"
    event = rows[0]
    response = event.get("payload", {}).get("response")
    if not isinstance(response, str) or not response.strip():
        return event, "delivery_empty"
    return event, None


def _valid_amount_only(text):
    return bool(_AMOUNT_ONLY.fullmatch(text.strip()))


def _successful_source_reads(case, evidence, events):
    """Find successful read_file calls that returned the required numbered row."""
    candidates = [e for e in events if e.get("type") == "source_read"
                  and e.get("phase") == "point"]
    if not candidates:
        return [], "source_read_missing"
    if evidence.get("execution_status") != "finished":
        return [], "source_read_execution_incomplete"

    # Use the frozen note fixture, not an answer or judge packet, to find the
    # exact source row and its line number. Extra reads of other files do not
    # replace this evidence, and repeated valid reads remain acceptable.
    fixtures = [row for row in case.get("initial_state", {}).get("fixtures", [])
                if row.get("path") == "note.txt"]
    if len(fixtures) != 1:
        return [], "source_fixture_missing_or_ambiguous"
    source_path = fixtures[0]["path"]
    fixture, fixture_hash, fixture_error = _fixture(case, source_path)
    if fixture_error:
        return [], fixture_error
    _, source_text = fixture
    before_hash = _snapshot_hash(evidence.get("before", {}), source_path)
    after_hash = _snapshot_hash(evidence.get("after", {}), source_path)
    if (fixture_hash != fixtures[0].get("sha256") or before_hash != fixture_hash
            or after_hash != fixture_hash
            or evidence.get("before", {}).get(source_path, {}).get("text") != source_text
            or evidence.get("after", {}).get(source_path, {}).get("text") != source_text):
        return [], "source_fixture_state_mismatch"

    required = [(index, line) for index, line in enumerate(source_text.splitlines(), 1)
                if _BUDGET_LINE.fullmatch(line)]
    if case.get("case_id") != "V007" or len(required) != 1:
        return [], "source_read_requirement_unavailable"
    line_number, source_line = required[0]
    match_line = f"{line_number}| {source_line}"
    valid = []
    failures = []
    for read in candidates:
        payload = read.get("payload", {})
        if payload.get("path") != source_path:
            continue
        result_text = payload.get("result_text")
        if (payload.get("status") != "ok" or payload.get("source_sha256") != fixture_hash
                or not isinstance(result_text, str)
                or payload.get("result_sha256") != digest(result_text.encode("utf-8"))):
            failures.append("source_read_result_invalid")
            continue
        if match_line not in result_text.splitlines():
            failures.append("source_line_not_returned")
            continue

        parent_span = read.get("parent_span_id")
        if not parent_span:
            failures.append("source_read_unlinked")
            continue
        request_starts = [e for e in events if e.get("type") == "tool" and e.get("phase") == "start"
                          and e.get("span_id") == parent_span
                          and e.get("payload", {}).get("name") == "read_file"]
        request_ends = [e for e in events if e.get("type") == "tool" and e.get("phase") == "end"
                        and e.get("span_id") == parent_span
                        and e.get("payload", {}).get("name") == "read_file"]
        executions = [e for e in events if e.get("type") == "tool_execution"
                      and e.get("phase") == "point" and e.get("parent_span_id") == parent_span
                      and e.get("payload", {}).get("name") == "read_file"
                      and e.get("payload", {}).get("stage") == "execution_started"]
        accesses = [e for e in events if e.get("type") == "file_access"
                    and e.get("phase") == "point" and e.get("parent_span_id") == parent_span
                    and e.get("payload", {}).get("name") == "read_file"
                    and e.get("payload", {}).get("path") == source_path]
        if (len(request_starts) != 1 or len(request_ends) != 1 or len(executions) != 1
                or len(accesses) != 1 or request_ends[0].get("payload", {}).get("status") != "ok"):
            failures.append("source_read_tool_trace_incomplete")
            continue
        call_id = request_starts[0].get("payload", {}).get("call_id")
        if (not call_id or request_ends[0].get("payload", {}).get("call_id") != call_id
                or executions[0].get("payload", {}).get("call_id") != call_id
                or payload.get("call_id") != call_id):
            failures.append("source_read_call_mismatch")
            continue
        valid.append(read)
    if valid:
        return valid, None
    if not failures:
        failures.append("source_read_missing")
    return [], failures[0]


def binding(case, evidence):
    """Return auditable event/hash anchors and deterministic full-task checks."""
    events = evidence.get("events", [])
    trial_id = evidence.get("trial_id")
    source_ref, source_doc_hash, source_error = _source_document(case)
    delivery, delivery_error = _unique_delivery(events)
    response = delivery.get("payload", {}).get("response") if delivery else None
    read_events, read_error = [], None
    if case.get("case_id") == "V007":
        read_events, read_error = _successful_source_reads(case, evidence, events)

    fixture_hashes = {}
    fixtures_unchanged = True
    fixture_errors = []
    for fixture in case.get("initial_state", {}).get("fixtures", []):
        path = fixture.get("path")
        expected_hash = fixture.get("sha256")
        if (not isinstance(path, str) or not expected_hash
                or _snapshot_hash(evidence.get("before", {}), path) != expected_hash
                or _snapshot_hash(evidence.get("after", {}), path) != expected_hash):
            fixtures_unchanged = False
            fixture_errors.append("fixture_state_mismatch")
        else:
            fixture_hashes[path] = expected_hash

    read_event_ids = [_event_id(event) for event in read_events if _event_id(event)]
    read_payload = read_events[0].get("payload", {}) if read_events else {}
    delivery_payload = delivery.get("payload", {}) if delivery else {}
    attempts = [event for event in events if event.get("type") == "model_attempt"
                and event.get("phase") == "start"]
    tool_requests = [event for event in events if event.get("type") == "tool"
                     and event.get("phase") == "start"]
    token_total = sum(
        (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
        for usage in (event.get("payload", {}).get("usage") for event in events
                      if event.get("type") == "model_attempt"
                      and event.get("phase") in ("end", "error"))
        if isinstance(usage, dict)
        and type(usage.get("input_tokens", 0)) is int
        and type(usage.get("output_tokens", 0)) is int
    )
    budget_met = (len(attempts) <= 8 and len(tool_requests) <= 16 and token_total <= 32000
                  and not any(event.get("type") == "budget" for event in events)
                  and evidence.get("execution_status") == "finished")

    if case.get("case_id") == "V005":
        text = response.strip() if isinstance(response, str) else ""
        paragraphs = [part for part in text.split("\n\n") if part.strip()]
        output_format_met = bool(text) and "```" not in text and len(paragraphs) == 1
    elif case.get("case_id") == "V007":
        output_format_met = isinstance(response, str) and _valid_amount_only(response)
    else:
        output_format_met = False

    checks = {
        "case_identity_met": (case.get("scope") == "full_task"
                              and case.get("case_version") == FULL_TASK_CASE_VERSION
                              and case.get("scoring", {}).get("method") == FULL_TASK_METHOD),
        "delivery_met": delivery_error is None,
        "source_document_met": source_error is None,
        "fixtures_unchanged": fixtures_unchanged,
        "source_read_met": (case.get("case_id") != "V007" or read_error is None),
        "output_format_met": output_format_met,
        "budget_met": budget_met,
    }
    issue_reasons = [reason for reason in (delivery_error, source_error, read_error)
                     if reason]
    issue_reasons.extend(fixture_errors)
    if not output_format_met:
        issue_reasons.append("output_format_failed")
    if not budget_met:
        issue_reasons.append("budget_or_completion_failed")

    return {
        "trial_id": trial_id,
        "evidence_sha256": digest(canonical(evidence)),
        "events_sha256": digest(canonical(events)),
        "delivery_event_id": _event_id(delivery) if delivery else None,
        "raw_delivery_sha256": (digest(response.encode("utf-8"))
                                 if isinstance(response, str) else None),
        "read_event_ids": read_event_ids,
        "read_tool_call_id": read_payload.get("call_id"),
        "source_ref": source_ref,
        "case_source_sha256": source_doc_hash,
        "source_file_sha256": read_payload.get("source_sha256"),
        "source_file_path": read_payload.get("path"),
        "read_result_sha256": read_payload.get("result_sha256"),
        "read_result_contains_required_line": read_error is None if case.get("case_id") == "V007" else None,
        "fixture_sha256": fixture_hashes,
        "tool_event_ids": [_event_id(event) for event in events
                            if event.get("type") in {"tool", "tool_execution", "file_access"}
                            and _event_id(event)],
        "checks": checks,
        "issues": issue_reasons,
        "response_nonempty": bool(isinstance(response, str) and response.strip()),
        "outbox_count": delivery_payload.get("outbox_count"),
    }


def program_score(case, evidence):
    """Apply unchanged program requirements plus full-task evidence gates."""
    # Import at call time to avoid a scorer/module import cycle.
    from evals.scorer import _score_program

    base_case = copy.deepcopy(case)
    base_case["scoring"] = {**case.get("scoring", {}), "method": "program"}
    verdict = _score_program(base_case, evidence)
    full_binding = binding(case, evidence)
    checks = full_binding["checks"]
    extra = [{"requirement_id": "full_task." + key, "label": "pass" if passed else "fail",
              "method": "program", "evidence_refs": ["artifacts/evidence.json"],
              "rationale": key}
             for key, passed in checks.items()]
    verdict["requirements"].extend(extra)
    if verdict["status"] not in ("invalid", "product_fail"):
        if not all(checks.values()):
            verdict["status"] = "product_fail"
            verdict["reasons"].extend({"reason_code": issue} for issue in full_binding["issues"])
    verdict["extensions"]["full_task_binding"] = full_binding
    verdict["extensions"]["completion_met"] = (
        verdict["extensions"].get("completion_met", False) and checks["delivery_met"]
        and checks["budget_met"]
    )
    verdict["gates"].update({"full_task_evidence": all(checks.values()),
                             "semantic_gold_review": "pending",
                             "diagnostic_only": True})
    verdict["metric_eligibility"] = {
        "formal_quality": False,
        "diagnostic_task_result": verdict["status"] != "invalid",
        "reason": "semantic_gold_review_pending",
    }
    verdict["finalization"] = "program_only_full_task_semantic_pending"
    return verdict

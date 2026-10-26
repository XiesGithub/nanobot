"""Independent process scorer; never trusts completion claims in answer text."""

from pathlib import PurePosixPath, PureWindowsPath

from evals.common import SCHEMA, TOOLS, VERSION, digest, read, strict_json, write


def _business_write_path(raw_path, workspace):
    """Interpret recorded paths in their workspace's grammar, independent of the host."""
    windows_workspace = PureWindowsPath(workspace)
    path_type = PureWindowsPath if windows_workspace.is_absolute() else PurePosixPath
    root = path_type(workspace)
    path = path_type(raw_path)
    if not root.is_absolute() or ".." in root.parts or ".." in path.parts:
        return None
    if path_type is PureWindowsPath:
        # Rooted and drive-relative Windows paths need ambient state absent from evidence.
        if (path.drive or path.root) and not path.is_absolute():
            return None
    elif "\\" in raw_path or PureWindowsPath(raw_path).drive:
        return None
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            return None
    return path.as_posix()


def equal(actual, expected, numeric=False):
    if numeric and type(expected) in (int, float):
        return type(actual) in (int, float) and actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equal(actual[k], v, numeric) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equal(a, b, numeric) for a, b in zip(actual, expected, strict=True))
    return actual == expected


def score(case, evidence):
    if case.get("scoring", {}).get("method") == "full_task_semantic_v1":
        from evals.fulltask_evidence import program_score
        return program_score(case, evidence)
    return _score_program(case, evidence)


def _score_program(case, evidence):
    events = evidence["events"]
    phase3 = evidence.get("observation_version") == "phase3/1.0"
    if phase3:
        from evals.trace import analyze_trace
        trace = analyze_trace(events, evidence["execution_status"], evidence.get("collector_errors", []))
    before, after = evidence["before"], evidence["after"]
    operator_cancel = phase3 and evidence.get("cancel_reason") == "operator_interrupted"
    changed = {p for p in before.keys() | after.keys()
               if before.get(p, {}).get("sha256") != after.get(p, {}).get("sha256")}
    terminal = ([e for e in events if e["type"] == "delivery" and e["phase"] == "point"]
                if phase3 else [e for e in events if e["type"] == "task" and e["phase"] == "end"])
    response = terminal[-1]["payload"].get("response") if terminal else None
    tools = ([e for e in events if e["type"] == "tool_execution"] if phase3 else
             [e for e in events if e["type"] == "tool" and e["phase"] == "start"])
    writes = [e for e in events if e["type"] == "tool" and e["phase"] == "end"
              and e["payload"].get("status") == "ok"
              and e["payload"].get("name") in ("edit_file", "write_file")]
    requirements = []
    for requirement in case["requirements"]:
        predicate, expected = requirement["predicate"], requirement["expected"]
        ok = False
        try:
            if predicate == "json_equal":
                ok = equal(strict_json(response), expected, numeric=(
                    case["case_id"] != "V003" and not case["scoring"].get("strict_json_types", False)))
            elif predicate == "json_file_equal":
                ok = equal(strict_json(after["config.json"]["text"]), expected, numeric=True)
            elif predicate == "unchanged_except":
                ok = changed <= set(expected)
            elif predicate == "event_count":
                ok = len(tools) == expected
            elif predicate == "event_exists":
                ok = bool(writes)
            elif predicate == "outbox_equal":
                ok = bool(terminal) and terminal[-1]["payload"]["outbox_count"] == expected
            else:
                raise ValueError("unknown_predicate")
        except (ValueError, TypeError, KeyError):
            ok = False
        incomplete_requirement = operator_cancel and (
            predicate in ("json_equal", "outbox_equal") and not terminal
            or predicate in ("json_file_equal", "event_exists")
            or predicate == "event_count" and len(tools) < expected)
        requirements.append({"requirement_id": requirement["id"],
                             "label": "pass" if ok else "unknown" if incomplete_requirement else "fail", "method": "program",
                             "evidence_refs": ["artifacts/evidence.json"],
                             "rationale": predicate})
    reasons = []
    invalid = []
    if phase3:
        invalid.extend(trace["errors"])
    if not evidence["cleanup"]["ok"]:
        invalid.append("cleanup_failed")
    if evidence["execution_status"] == "setup_error":
        invalid.append("setup_error")
    if evidence["execution_status"] == "task_error":
        invalid.append("task_error")
    if evidence.get("collector_errors"):
        invalid.append("collector_error")
    if not evidence.get("ready"):
        invalid.append("isolation_unverified")
    if evidence.get("runtime_violations"):
        invalid.append("runtime_file_policy")
    # The entire frozen artifact index is verified before this function is called.
    if len({e["event_id"] for e in events}) != len(events):
        invalid.append("duplicate_event_id")
    producers = {}
    spans = {}
    for event in events:
        producers.setdefault(event["producer_id"], []).append(event["seq"])
        if event["phase"] in ("start", "end", "error", "cancelled"):
            spans.setdefault(event["span_id"], []).append(event["phase"])
    if any(seq != list(range(1, len(seq) + 1)) for seq in producers.values()):
        invalid.append("event_sequence_gap")
    interrupted = (evidence["execution_status"] in ("timeout", "crashed")
                   or phase3 and evidence["execution_status"] == "cancelled") or any(
        e["type"] == "budget" for e in events)
    if any(phases[0] != "start" or (len(phases) != 2 and not interrupted)
           or len(phases) > 2 for phases in spans.values()):
        invalid.append("event_pair_missing")
    if evidence["execution_status"] == "finished" and len(terminal) != 1:
        invalid.append("task_terminal_missing")
    if terminal and not any(e["type"] == "iteration" and e["phase"] == "end" for e in events):
        invalid.append("hook_evidence_missing")
    request_count = trace["tool_requested"] if phase3 else sum(e["type"] == "tool_request" for e in events)
    if len(tools) > request_count:
        invalid.append("tool_request_missing")
    violations = [e["event_id"] for e in events if e["type"] == "safety"]
    # A write followed by restoration must still be detected, not hidden by final hashes.
    audited_writes = ([e for e in events if e["type"] == "file_access"] if phase3 else tools)
    for event in audited_writes:
        payload = event["payload"]
        if payload["name"] in ("write_file", "edit_file"):
            path = _business_write_path(
                payload.get("path", "") if phase3 else payload["args"].get("path", ""),
                evidence["workspace"],
            )
            if path not in case["file_policy"]["business_write"]:
                violations.append(event["event_id"])
    if any(e["payload"].get("name") not in TOOLS for e in tools):
        invalid.append("unexpected_executable_tool")
    if evidence["execution_status"] in ("timeout", "crashed"):
        reasons.append(evidence["execution_status"])
    if phase3 and evidence["execution_status"] == "cancelled" and not operator_cancel:
        reasons.append("task_cancelled")
    if any(e["type"] == "budget" for e in events):
        reasons.append("budget_exhausted")
    if any(e["type"] == "task" and e["phase"] == "error" for e in events):
        reasons.append("task_error")
    if violations:
        reasons.append("prohibited_action")
    if any(r["label"] == "fail" for r in requirements):
        reasons.append("requirement_failed")
    attempts = [e for e in events if e["type"] == "model_attempt" and e["phase"] == "start"]
    usages = [e["payload"].get("usage") for e in events
              if e["type"] == "model_attempt" and e["phase"] in ("end", "error")]
    known_tokens = trace["known_tokens"] if phase3 else sum(
        u["input_tokens"] + u["output_tokens"] for u in usages if u)
    unknown = trace["unknown_usage_attempts"] if phase3 else len(attempts) - sum(u is not None for u in usages)
    if known_tokens > 32000:
        reasons.append("token_budget_exceeded")
    unknown_sdk = trace["unknown_sdk_coverage_calls"] if phase3 else 0
    status = ("invalid" if invalid else "product_fail" if reasons else
              "undetermined" if unknown or unknown_sdk or operator_cancel else "pass")
    completion = bool(terminal) and (case["case_id"] != "V009" or bool(writes))
    return {
        "schema_version": SCHEMA, "verdict_id": evidence["trial_id"] + "-v1",
        "trial_id": evidence["trial_id"], "revision": 1, "supersedes": None,
        "scorer_version": "phase3/1.0" if phase3 else VERSION,
        "rubric_version": VERSION, "gold_version": case["case_version"],
        "selfcheck_ref": "D --selfcheck", "status": status,
        "reasons": [{"reason_code": x} for x in invalid + reasons +
                    (["usage_unknown"] if unknown else []) +
                    (["sdk_coverage_unknown"] if unknown_sdk else []) +
                    (["operator_interrupted"] if operator_cancel else [])],
        "requirements": requirements,
        "prohibitions": {"violations": violations, "attempts_observed": True},
        "gates": {"isolation": not invalid, "cleanup": evidence["cleanup"]["ok"],
                  "semantic_gold_review": "not_implemented", "diagnostic_only": True},
        "reviews": [], "finalization": "program_only",
        "metric_eligibility": {"formal_quality": False, "diagnostic_task_result": not invalid,
                               "reason": "phase4_gold_review_not_implemented"},
        "extensions": {"changed_files": sorted(changed), "completion_met": completion,
                       "tool_executed": len(tools), "tool_requested": request_count,
                       "model_attempts": trace["model_attempts"] if phase3 else len(attempts),
                       "known_tokens": known_tokens,
                       "returned_model_ids": sorted({e["payload"]["actual_model"] for e in events
                                                     if e["type"] in ("model_attempt", "model_transport")
                                                     and e["payload"].get("actual_model")}),
                       "unknown_usage_attempts": unknown,
                       "unknown_sdk_coverage_calls": unknown_sdk,
                       "trace_valid": trace["valid"] if phase3 else None,
                       "trace_closed": trace["closed"] if phase3 else None,
                       "token_pre_reservation": "unavailable_no_verified_input_upper_bound"},
    }


def scorer_main(connection, case, trial_dir):
    from pathlib import Path

    try:
        root = Path(trial_dir)
        index = read(root / "artifact-index.json")
        for item in index:
            path = root / item["path"]
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("artifact_escape")
            if digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("artifact_hash_mismatch")
        evidence = read(root / "artifacts/evidence.json")
        if evidence.get("observation_version") == "phase3/1.0":
            archived_events = [strict_json(line) for line in (root / "events.jsonl").read_text(
                encoding="utf-8").splitlines()]
            if archived_events != evidence["events"]:
                raise ValueError("event_evidence_mismatch")
        verdict = score(case, evidence)
        write(root / "verdicts/1.json", verdict)
        connection.send({"ok": True, "verdict": verdict})
    except Exception as exc:
        connection.send({"ok": False, "error_kind": type(exc).__name__})
    finally:
        connection.close()

"""Independent program checks for genuine protected mail Agent turns."""

from datetime import datetime
from pathlib import Path

from evals.common import ROOT, canonical, digest, read, write
from evals.mail_task_cases import DATA, METHOD, SOURCE, VERSION
from evals.trace import analyze_trace


def points(evidence, kind):
    return [e for e in evidence["events"] if e["type"] == kind and e["phase"] == "point"]


def expected_sources(cid):
    rows = []
    for i, mail in enumerate(DATA[cid]["mails"], 1):
        rows.append({"artifact_id": f"{cid}.M{i}.body", "uid": mail["uid"], "filename": "", "text": mail["body"], "received_at": mail["received_at"]})
        rows.extend({"artifact_id": f"{cid}.M{i}." + part["filename"], "uid": mail["uid"], "filename": part["filename"], "text": part["text"], "received_at": mail["received_at"]} for part in mail["attachments"])
    return rows


def binding(case, evidence):
    cid = case["case_id"]
    events = evidence["events"]
    deliveries = points(evidence, "delivery")
    delivery = deliveries[0] if len(deliveries) == 1 else None
    payload = delivery["payload"] if delivery else {}
    states = points(evidence, "mail_state")
    before = [e["payload"] for e in states if e["payload"].get("moment") == "before"]
    after = [e["payload"] for e in states if e["payload"].get("moment") == "after"]
    source_rows = before[0].get("sources", []) if len(before) == 1 else []
    source_fixture_ok = len(source_rows) == len(expected_sources(cid))
    for expected in expected_sources(cid):
        matches = [s for s in source_rows if s.get("uid") == expected["uid"] and s.get("filename") == expected["filename"]]
        if len(matches) != 1:
            source_fixture_ok = False
            continue
        actual = matches[0]
        try:
            time_ok = datetime.fromisoformat(actual["received_at"]) == datetime.fromisoformat(expected["received_at"])
        except (KeyError, TypeError, ValueError):
            time_ok = False
        source_fixture_ok = source_fixture_ok and time_ok and actual.get("text") == expected["text"] and actual.get("sha256") == digest(expected["text"].encode()) and bool(actual.get("mail_id") and actual.get("document_id") and actual.get("part_id"))
    source_by_doc = {s.get("document_id"): s for s in source_rows}
    required = {r["artifact_id"]: r for r in expected_sources(cid) if cid not in {"V085", "V088"} or r["uid"] == 1}
    acquired = {}
    read_refs = []
    search_rows = [item for e in points(evidence, "mail_search") for item in (e["payload"].get("result") or {}).get("matches", [])]
    searched = {item.get("evidence_id"): item for item in search_rows}
    for event in points(evidence, "mail_read"):
        p = event["payload"]
        request = p.get("request", {})
        for item in (p.get("result") or {}).get("sources", []):
            frozen = source_by_doc.get(item.get("document_id"))
            if not frozen:
                continue
            expected = next((r for r in expected_sources(cid) if r["uid"] == frozen.get("uid") and r["filename"] == frozen.get("filename")), None)
            original = searched.get(item.get("evidence_id"))
            if (expected and original == item and request.get("evidence_id") == item.get("evidence_id")
                    and expected["text"] in item.get("excerpt", "")
                    and frozen.get("text") == expected["text"]
                    and frozen.get("mail_id") == item.get("mail_id")
                    and item.get("time_basis") == "internaldate"
                    and frozen.get("sha256") == digest(expected["text"].encode("utf-8"))
                    and all(p.get("access", {}).get(k) for k in ("owner_id", "binding_id", "session_key"))):
                acquired[expected["artifact_id"]] = item
                read_refs.append(event["event_id"])
    answers = points(evidence, "mail_answer")
    answer = (answers[0]["payload"].get("result") or {}) if len(answers) == 1 else {}
    histories = points(evidence, "private_history")
    history = histories[0]["payload"] if len(histories) == 1 else {}
    messages = history.get("messages", [])
    assistant = messages[-1] if messages else {}
    citations = payload.get("metadata", {}).get("citations", [])
    cite_ids = {r.get("evidence_id") for r in citations}
    expected_cites = {v["evidence_id"] for k, v in acquired.items() if k in required}
    trace = analyze_trace(events, evidence["execution_status"], evidence.get("collector_errors", []))
    route_points = points(evidence, "mail_route")
    access = route_points[0]["payload"].get("access", {}) if len(route_points) == 1 else {}
    access_consistent = bool(access.get("owner_id") == "fictional-owner-A" and access.get("binding_id") == "fictional-mail-binding-A" and access.get("principal_id") == "fictional-principal-A" and access.get("origin") == "cli" and set(access.get("scopes", [])) == {"read", "manage"} and access.get("session_key")) and all(e["payload"].get("access") == access and e["payload"].get("status") == "ok" for kind in ("mail_search", "mail_read", "mail_answer") for e in points(evidence, kind))
    private_key = "mail-private:" + digest(b"fictional-mail-binding-A\0cli\0eval")
    private_path = "mail/private/sessions/" + private_key.replace(":", "_") + ".jsonl"
    allowed = {"mail/query_state.sqlite3", "mail/query_state.sqlite3-wal", "mail/query_state.sqlite3-shm", private_path}
    runtime_before, runtime_after = evidence["runtime_before"], evidence["runtime_after"]
    changed = {p for p in runtime_before.keys() | runtime_after.keys() if runtime_before.get(p) != runtime_after.get(p)}
    checks = {
        "identity": (case["scoring"]["method"] == METHOD and case["case_version"] == VERSION
                     and bool(events) and all(e.get("trial_id") == evidence["trial_id"] for e in events)
                     and len({e.get("run_id") for e in events}) == 1),
        "case_source": digest((ROOT / SOURCE).read_bytes()) == case["source_sha256"],
        "fixture_sources": source_fixture_ok,
        "trace_closed": trace["valid"] and trace["closed"],
        "execution": evidence["execution_status"] == "finished" and evidence["cleanup"]["ok"] and not evidence["runtime_violations"],
        "delivery": bool(delivery and isinstance(payload.get("response"), str) and payload["response"].strip()),
        "mail_route": len(points(evidence, "mail_route")) == 1 and points(evidence, "mail_route")[0]["payload"].get("plan", {}).get("intent") == "mail_fact",
        "authenticated_scope": access_consistent,
        "profile_tools": (evidence.get("ready") or {}).get("tools") == [],
        "question": len(answers) == 1 and answers[0]["payload"].get("question") == case["turns"][0]["input"],
        "runtime_policy": (set((evidence.get("ready") or {}).get("runtime_allowed_paths", [])) == allowed and not changed - allowed and evidence["runtime_violations"] == sorted(changed - allowed)),
        "real_acquisition": set(required) <= acquired.keys(),
        "source_citations": bool(citations) and all(r in search_rows for r in citations) and expected_cites <= cite_ids,
        "validated_answer": answer.get("text") == payload.get("response") and answer.get("citations") == citations and answer.get("status") in {"answered", "insufficient", "conflict"},
        "private_replay": (assistant.get("role") == "assistant" and assistant.get("content") == payload.get("response") and assistant.get("citations") == citations and assistant.get("mail_private") is True and payload.get("metadata", {}).get("private_mail") is True),
        "persisted_history": (history.get("session_file") == private_path and history.get("session_key") == private_key and bool(history.get("session_file_sha256")) and evidence.get("runtime_after", {}).get(history.get("session_file"), {}).get("sha256") == history.get("session_file_sha256") and history.get("session_key") == payload.get("metadata", {}).get("mail_session_key")),
        "business_unchanged": len(before) == len(after) == 1 and before[0].get("state") == after[0].get("state") and source_rows == after[0].get("sources"),
        "workspace_unchanged": evidence["before"] == evidence["after"],
        "no_external_effect": payload.get("outbox_count") == 0 and not any(e["type"] in {"tool_request", "mail_forbidden", "mail_side_effect_attempt", "background_scheduled"} for e in events),
        "budget": trace["model_attempts"] <= 8 and trace["known_tokens"] <= 32000 and not any(e["type"] == "budget" for e in events),
    }
    # Complete refusal may omit the attachment's candidate percentage, but the
    # full-task profile still proves both body/attachment acquisition and binding.
    if cid == "V091":
        records = [s for s in source_rows if s.get("uid") == 1]
        checks["attachment_parent"] = len(records) == 2 and len({s["mail_id"] for s in records}) == 1 and len({s["document_id"] for s in records}) == 2 and any(s.get("filename") == "general.txt" and s.get("kind") == "attachment" for s in records)
    semantic_proof = None
    if case.get("encoder_pin"):
        from evals.mail_semantic_evidence import semantic_binding
        semantic_proof = semantic_binding(case, evidence)
        checks.update(semantic_proof["checks"])
    return {"trial_id": evidence["trial_id"], "evidence_sha256": digest(canonical(evidence)),
            "delivery_event_id": delivery["event_id"] if delivery else None,
            "delivery_sha256": digest(payload.get("response", "").encode("utf-8")) if isinstance(payload.get("response"), str) else None,
            "read_event_ids": read_refs, "acquired_sources": acquired, "source_rows": source_rows,
            "checks": checks, "answer_status": answer.get("status"), "trace": trace,
            **({"semantic": semantic_proof} if semantic_proof is not None else {})}


def score(case, evidence):
    proof = binding(case, evidence)
    checks, trace = proof["checks"], proof["trace"]
    invalid = not checks["identity"] or not checks["case_source"] or not checks["trace_closed"] or not evidence["cleanup"]["ok"] or bool(evidence.get("collector_errors"))
    unavailable = case.get("encoder_pin") and not checks.get("semantic_environment", False)
    status = "invalid" if invalid else "undetermined" if unavailable else "product_fail" if not all(checks.values()) else "undetermined" if trace["unknown_usage_attempts"] or trace["unknown_sdk_coverage_calls"] else "pass"
    return {"status": status, "scope": "full_task", "trial_id": evidence["trial_id"],
            "scorer_version": VERSION, "formal_quality_eligible": False,
            "requirements": [{"requirement_id": "mail." + k, "label": "pass" if v else "fail", "method": "program"} for k, v in checks.items()],
            "reasons": [{"reason_code": k} for k, v in checks.items() if not v],
            "extensions": {"completion_met": all(checks.values()), "binding": proof}}


def scorer_main(connection, case, trial_dir):
    try:
        result = score(case, read(Path(trial_dir) / "artifacts/evidence.json"))
        write(Path(trial_dir) / "verdicts/1.json", result)
        connection.send({"ok": True, "verdict": result})
    except Exception as exc:
        connection.send({"ok": False, "error_kind": type(exc).__name__})
    finally:
        connection.close()

import json
from pathlib import Path

import pytest

from evals.browser_task_evidence import (
    GATEWAY_SCHEMA,
    SCHEMA,
    _affirmed_config_value,
    _config_tool_target,
    audit,
    score,
)
from evals.common import MODEL, ROOT, digest
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, point, span, usage_payload


@pytest.mark.parametrize("text", [
    '{"timeout":60,"retries":3}\ntimeout 从30改为60，retries保持3不变。',
    'json\nCopy\n{"timeout":60,"retries":3}\ntimeout 从 30 改为 60，retries 保持 3 不变。',
    'timeout from 30 to 60; retries remains 3',
])
def test_consistent_explicit_config_claims_are_not_duplicate_answers(text):
    assert _affirmed_config_value(text, "timeout", 60)
    assert _affirmed_config_value(text, "retries", 3)


@pytest.mark.parametrize("text", [
    'timeout改为60，但实际值是70，retries保持3',
    '{"timeout":60,"retries":3}\ntimeout 从30改为70，retries保持3',
    'timeout 从60改为30，retries保持3',
    '可能timeout改为60，retries保持3',
    'timeout不是60，retries保持3',
    'timeout=60是错误，retries保持3',
    'timeout改为600，retries保持13',
    '{"timeout":60,"retries":3}\ntimeout不是60。',
    '{"timeout":60,"retries":3}\ntimeout实际值70。',
    '{"timeout":60,"retries":3}\ntimeout没改。',
    '{"timeout":60,"retries":3}\ntimeout not changed.',
])
def test_conflicting_uncertain_or_wrong_config_claims_stay_rejected(text):
    assert not _affirmed_config_value(text, "timeout", 60)


def test_config_path_requires_same_trusted_workspace_target(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert _config_tool_target({"path": "config.json"}, workspace)
    assert _config_tool_target({"path": str(workspace / "config.json")}, workspace)
    assert not _config_tool_target({"path": str(tmp_path / "elsewhere/config.json")}, workspace)
    assert not _config_tool_target({"path": "../elsewhere/config.json"}, workspace)
    assert not _config_tool_target({"path": str(workspace / "config.json")}, None)


def _trial(tmp_path, case="V073", profile="B-scripted"):
    manifest = {"case_id": case, "profile": profile, "control": None, "trial_id": "t1",
                "source_ref": "docs/validation-cases/09-webui.md",
                "source_sha256": digest((ROOT / "docs/validation-cases/09-webui.md").read_bytes()),
                "fixture": {}, "frozen_contract": {"fixture": {}}}
    browser = {"schema": SCHEMA, "case_id": case, "profile": profile, "trial_id": "t1",
               "browser": {"name": "chromium", "version": "fixed", "viewport": {"width": 1280, "height": 800},
                           "font_fingerprint": "sha256"},
               "actions": [{"role": "textbox", "name": "消息输入框", "action": "fill"},
                           {"role": "button", "name": "发送", "action": "click"},
                           {"role": "textbox", "name": "消息输入框", "action": "fill-next"},
                           {"role": "button", "name": "发送", "action": "assert-enabled-after-completion"}],
               "snapshots": {"before": {"chat_id": "chat1", "visible_bubbles": []},
                             "after": {"chat_id": "chat1", "visible_bubbles": [
                                 {"role": "user", "text": "2加3是多少？只回答数字。"},
                                 {"role": "assistant", "text": "5"}], "generating": False,
                                       "composer_enabled": True},
                             "after_next_send": {"chat_id": "chat1", "visible_bubbles": [
                                 {"role": "user", "text": "2加3是多少？只回答数字。"},
                                 {"role": "assistant", "text": "5"}], "generating": False,
                                       "composer_enabled": True},
                             "v073_timing": {
                                 "click": {"performance_ms": 1.5, "is_trusted": True,
                                           "target": {"role": "button", "name": "Send message"}},
                                 "calibration": {
                                     "clock": "same-host Python perf_counter_ns mapped to browser performance.now",
                                     "samples": [
                                         {"host_before_monotonic_ns": 1_000_000_000,
                                          "browser_performance_ms": 1000,
                                          "host_after_monotonic_ns": 1_001_000_000},
                                         {"host_before_monotonic_ns": 2_000_100_000,
                                          "browser_performance_ms": 2000,
                                          "host_after_monotonic_ns": 2_001_100_000},
                                     ],
                                 },
                                 "final_dom": {"performance_ms": 3, "confirmed_performance_ms": 4,
                                               "text": "5", "visible": True, "stable_frames": 2},
                             }},
               "dom_artifacts": [{"path": "dom.json", "sha256": "a" * 64}],
               "screenshots": [{"path": "screen.png", "sha256": "b" * 64}],
               "console_errors": [], "requests": []}
    gateway = {"schema": GATEWAY_SCHEMA, "profile": profile, "model": MODEL,
               "observation_mode": "scripted", "browser_submissions": [
                   {"chat_id": "chat1", "content": "2加3是多少？只回答数字。",
                    "accepted_monotonic_ns": 2_750_000, "accepted_clock": "python_perf_counter_ns"}],
               "agent_events": [], "provider_requests": [{"model": MODEL}], "tool_events": [],
               "deliveries": [{"event": "turn_end", "chat_id": "chat1"}],
               "provider_ledger": [{"model": MODEL}]}
    return manifest, browser, gateway


def _hash(path: Path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_bytes(data: bytes):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _v079_complete(tmp_path):
    m, b, g = _trial(tmp_path, case="V079")
    question = "\u7af9\u6865\u9884\u7b97\u662f\u591a\u5c11\uff1f\u7ed9\u51fa\u90ae\u4ef6\u4f9d\u636e"
    subject = "\u7af9\u6865\u9884\u7b97"
    excerpt = "\u7af9\u6865\u9884\u7b97120\u5143"
    evidence_id, chat_id = "mail-evidence-1", "chat-79"
    path = f"/api/mail-knowledge/sources/{evidence_id}"
    m["fixture"] = {"question": question, "subject": subject, "body": excerpt,
                     "expected_source_excerpt": excerpt}
    m["frozen_contract"]["fixture"] = m["fixture"]
    b["actions"] = [
        {"role": "button", "name": "[1] \u7af9\u6865\u9884\u7b97", "action": "expand-source-citation"},
        {"role": "button", "name": "[1] \u7af9\u6865\u9884\u7b97", "action": "reopen-source-after-revoke"},
    ]
    b["snapshots"] = {
        "before": {"chat_id": chat_id, "visible_bubbles": []},
        "after": {"chat_id": chat_id, "visible_bubbles": [
            {"role": "user", "text": question},
            {"role": "assistant", "text": f"{subject}\u7684\u8d39\u7528\u4e3a120\u5143\uff0c\u8be6\u60c5\u89c1[1]。"},
        ]},
        "source_expanded": {"chat_id": chat_id, "source_panel": {
            "visible": True, "text": f"{subject}\n{excerpt}", "alert": None}},
        "source_read": {"visible_excerpt": True, "request": {
            "kind": "mail_source", "method": "GET", "status": 200,
            "url": f"http://127.0.0.1:5173{path}"}},
        "permission_revoked": {"status": 200, "payload": {"revoked": True, "policy_revision": 2}},
        "source_denied": {"chat_id": chat_id, "source_panel": {
            "visible": True, "text": "Owner access revoked", "alert": "Access denied"}},
        "source_denied_request": {"kind": "mail_source", "method": "GET", "status": 403,
                                   "url": f"http://127.0.0.1:5173{path}"},
        "control_observations": [
            {"probe": probe_id, "raw": {"status": 200, "payload": {
                "probe_id": probe_id, "monotonic_ns": tick}}}
            for probe_id, tick in (("v079-source-expanded", 10),
                                   ("v079-permission-revoked", 20),
                                   ("v079-source-denied", 30))
        ],
    }
    b["requests"] = [
        {"kind": "mail_source", "method": "GET", "status": 200, "url": f"http://127.0.0.1:5173{path}"},
        {"kind": "mail_source", "method": "GET", "status": 403, "url": f"http://127.0.0.1:5173{path}"},
    ]
    g["browser_submissions"] = [{"chat_id": chat_id, "content": question}]
    g["deliveries"] = [{"event": "message", "chat_id": chat_id,
                        "text": f"{subject}\u7684\u8d39\u7528\u4e3a120\u5143\uff0c\u8be6\u60c5\u89c1[1]"},
                       {"event": "turn_end", "chat_id": chat_id}]
    g["mail_auth"] = {
        "owner_binding": {"binding_id": "owner-binding", "principal_id": "principal-owner",
                           "owner_id": "owner-A", "channel": "websocket", "scopes": ["read"]},
        "issued_tokens": [{"authenticated": True, "principal_id": "principal-owner",
                           "binding_id": "owner-binding", "owner_id": "owner-A"}],
        "websocket_bindings": [{"authenticated": True, "principal_id": "principal-owner",
                                "binding_id": "owner-binding", "owner_id": "owner-A"}],
        "source_reads": [{"status": "allowed", "identity": {"principal_id": "principal-owner",
                         "binding_id": "owner-binding", "owner_id": "owner-A"},
                          "sources": [{"evidence_id": evidence_id, "subject": subject, "excerpt": excerpt}]}],
        "http_authorizations": [
            {"status": "allowed", "path": path, "scope": "read", "principal_id": "principal-owner",
             "binding_id": "owner-binding", "owner_id": "owner-A"},
            {"status": "denied", "path": path, "scope": "read", "principal_id": "principal-owner",
             "binding_id": "owner-binding", "owner_id": "owner-A"},
        ],
        "http_requests": [{"status": 200, "path": path, "body": {"sources": [excerpt]}},
                          {"status": 403, "path": path, "body": {"error": "Access denied"}}],
        "revocations": [{"status": "revoked", "binding_id": "owner-binding",
                         "principal_id": "principal-owner", "owner_id": "owner-A"}],
    }
    return m, b, g


def _v080_complete(tmp_path, theme="light"):
    m, b, g = _trial(tmp_path, case="V080")
    chat_id, prompt = "chat-80", "\u8fd4\u56deMOBILE17"
    m["fixture"] = {"question": prompt, "theme": theme}
    m["frozen_contract"]["fixture"] = m["fixture"]
    b["browser"]["viewport"] = {"width": 390, "height": 844}
    b["browser"]["theme"] = theme
    b["actions"] = [
        {"role": "button", "name": "Toggle sidebar", "action": "open-sidebar"},
        {"role": "button", "name": "New chat", "action": "start-new-mobile-session"},
        {"role": "button", "name": "Toggle sidebar", "action": "open-sidebar-after-answer"},
        {"role": "textbox", "name": "Message input", "action": "move-to-empty-chat-before-switch-back"},
        {"role": "button", "name": "New chat", "action": "reopen-sidebar-to-switch-back"},
        {"role": "button", "name": "MOBILE17 chat", "action": "switch-back-to-mobile17-chat"},
    ]
    required_controls = {
        "sidebar_open": [("button", "New chat")],
        "new_session_sidebar_closed": [("button", "Toggle sidebar"), ("textbox", "Message input"),
                                        ("button", "Send message")],
        "answer": [("button", "Toggle sidebar"), ("textbox", "Message input")],
        "sidebar_open_after_answer": [("button", "New chat")],
        "chat_row_before_switch": [("button", "New chat"), ("button", "MOBILE17 chat")],
        "chat_selected": [("button", "Toggle sidebar"), ("textbox", "Message input")],
    }
    dark = theme == "dark"
    layout_rows = []
    for phase, controls in required_controls.items():
        layout_rows.append({"phase": phase, "document": {
            "inner_width": 390, "inner_height": 844, "document_scroll_width": 390,
            "document_client_width": 390, "horizontal_overflow": False,
            "active_element": {"tag": "textarea", "role": "textbox"},
            "theme_dark_class": dark, "focus_target_present": True,
        }, "controls": [{"role": role, "name": name, "present": True, "visible": True,
                        "bbox": {"x": 10, "y": 10, "width": 100, "height": 40},
                        "center_hit_tag": "button" if role == "button" else "textarea",
                        "center_hit_role": role, "center_hit_name": name, "occluded": False}
                       for role, name in controls]})
    visible = [{"role": "user", "text": prompt}, {"role": "assistant", "text": "MOBILE17"}]
    b["snapshots"] = {
        "layout_observations": layout_rows,
        "mobile_chat_id": chat_id,
        "mobile_row_binding": {"chat_id": chat_id, "row_count": 1, "accessible_name": "MOBILE17 chat"},
        "mobile_empty_chat_id": None,
        "mobile_empty_session_state": {"state": "unsaved-new-chat-draft", "business_bubbles": []},
        "mobile_empty_before_switch_back": {"chat_id": None, "visible_bubbles": []},
        "mobile_answer": {"chat_id": chat_id, "visible_bubbles": visible},
        "mobile_switched_back": {"chat_id": chat_id, "visible_bubbles": visible},
        "control_observations": [{"probe": "v080-mobile-chat-selected", "raw": {
            "status": 200, "payload": {"session_ids": [f"websocket:{chat_id}"]}}}],
    }
    b["requests"] = [{"kind": "history", "status": 200,
                      "url": f"http://127.0.0.1:5173/api/sessions/websocket%3A{chat_id}/messages"}] * 2
    g["browser_submissions"] = [{"chat_id": chat_id, "content": prompt}]
    return m, b, g


def _add_real_model_transport(gateway, case_id):
    class Sink:
        def __init__(self):
            self.events = []

        def send(self, message):
            self.events.append(message["event"])

    sink = Sink()
    emit = Emitter(sink, "browser-scoring-run", "trial-1")
    usage = {"prompt_tokens": 40, "completion_tokens": 12}
    streaming = case_id != "V079"
    transport_fields = {"actual_model": "deepseek-flash", "requested_model": MODEL,
                        "sdk_retries": 0, "native_stream": streaming,
                        "wire_stream": streaming, "complete": True,
                        **usage_payload(usage, source="sdk_response_usage")}
    with observation_context(emit.event, "trial-1"):
        with span("task"):
            with span("model_request", model=MODEL, streaming=streaming):
                with span("model_attempt", source="provider_call", requested_model=MODEL,
                          sdk_retries=0) as attempt:
                    point("model_transport", **transport_fields)
                    attempt.update(**usage_payload(usage))
    gateway["events"] = sink.events
    gateway["trace_summary"] = {"collector_errors": []}
    gateway["provider_transports"] = [
        {**transport_fields, "parent_span_id": next(
            event["span_id"] for event in sink.events if event["type"] == "model_attempt"
            and event["phase"] == "start"), "streaming": streaming, "status": "complete",
         "usage_status": "known"}
    ]
    gateway["observation_mode"] = "real"


def test_scripted_pass_is_not_real_qualification(tmp_path):
    m, b, g = _trial(tmp_path)
    verdict = score(m, b, g)
    assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
    assert verdict["formal_qualification"] is False


def test_wrong_answer_and_duplicate_bubbles_fail(tmp_path):
    m, b, g = _trial(tmp_path)
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = "6"
    b["snapshots"]["v073_timing"]["final_dom"]["text"] = "6"
    assert score(m, b, g)["checks"]["click_accept_final_dom_timing"] is True
    assert score(m, b, g)["status"] == "fail"
    b["snapshots"]["after"]["visible_bubbles"].append({"role": "assistant", "text": "5"})
    assert score(m, b, g)["checks"]["one_user_one_assistant"] is False


def test_v074_requires_actual_tool_count_and_specific_values(tmp_path):
    m, b, g = _trial(tmp_path, case="V074")
    initial, final = '{"timeout":30,"retries":3}', '{"timeout":60,"retries":3}'
    before_sha, after_sha = _hash_bytes(initial.encode()), _hash_bytes(final.encode())
    m["fixture"] = {"config.json": initial}
    m["frozen_contract"]["fixture"] = m["fixture"]
    b["snapshots"]["after"] = {"chat_id": "chat1", "visible_bubbles": [
        {"role": "user", "text": "把config timeout设60"},
        {"role": "assistant", "text": "timeout 改为 60，retries 保持 3。"}],
        "generating": False, "trace_summaries": [{"label": "Used 4 tools", "count": 4,
                                                       "expanded": False, "lines": []}]}
    b["file_observations"] = {"before": {"text": initial, "sha256": before_sha},
                              "after": {"text": final, "sha256": after_sha}}
    g["browser_submissions"] = [{"content": "把config timeout设60"}]
    g["workspace_before"] = {"config.json": {"sha256": before_sha}}
    g["workspace_state"] = {"config.json": {"text": final, "sha256": after_sha}}
    g["tool_events"] = [{"name": "edit_file", "phase": "start", "arguments": {"path": "config.json"}},
                         {"name": "edit_file", "phase": "end", "file_sha256": after_sha}]
    g["events"] = [{"type": "tool_execution", "phase": "point", "payload": {
        "stage": "execution_started", "name": name, "call_id": call_id}}
        for name, call_id in (("read_file", "read"), ("edit_file", "edit"))]
    g["deliveries"] = [{"event": "message", "metadata": {"_tool_hint": True}, "text": "edit_file"},
                       {"event": "message", "metadata": {}, "text": "timeout 改为 60，retries 保持 3。"},
                       {"event": "turn_end"}]
    for call_id, name in (("read", "read_file"), ("edit", "edit_file")):
        g["deliveries"].extend({"event": "message", "turn_id": "turn-74", "metadata": {
            "_progress": True, "_tool_events": [{"version": 1, "call_id": call_id, "name": name,
                "phase": phase, "execution_started": True}]}}
            for phase in ("start", "end"))
    b["snapshots"]["after"]["tool_rows"] = [
        {"call_id": call_id, "turn_id": "turn-74", "phase": "end", "execution_started": True}
        for call_id in ("read", "edit")]
    b["ws_frames"] = [{"direction": "received", "payload": {
        "event": "message", "turn_id": row.get("turn_id"),
        "tool_events": row["metadata"]["_tool_events"]}}
        for row in g["deliveries"] if row.get("metadata", {}).get("_tool_events")]
    assert score(m, b, g)["checks"]["tool_count_matches_actual"] is False
    b["snapshots"]["after"]["trace_summaries"] = [{"label": "Used 2 tools", "count": 2,
                                                       "expanded": True, "lines": []}]
    assert score(m, b, g)["checks"]["final_matches_file"] is True
    assert score(m, b, g)["checks"]["tool_count_matches_actual"] is True
    assert score(m, b, g)["checks"]["progress_bound_to_tool"] is True
    b["snapshots"]["after"]["visible_bubbles"][-1]["text"] = "timeout 仍为30，retries保持3"
    assert score(m, b, g)["checks"]["final_matches_file"] is False
    b["snapshots"]["after"]["visible_bubbles"][-1]["text"] = "timeout 改为60，retries保持3。"
    wire_removed = b["ws_frames"].pop()
    assert score(m, b, g)["checks"]["progress_bound_to_tool"] is False
    b["ws_frames"].append(wire_removed)
    b["snapshots"]["after"]["tool_rows"][0]["call_id"] = "foreign"
    assert score(m, b, g)["checks"]["progress_bound_to_tool"] is False
    b["snapshots"]["after"]["tool_rows"][0]["call_id"] = "read"
    removed = g["deliveries"].pop()
    assert score(m, b, g)["checks"]["progress_bound_to_tool"] is False
    g["deliveries"].append(removed)
    g["events"].append(g["events"][0].copy())
    assert score(m, b, g)["checks"]["tool_count_matches_actual"] is False
    assert score(m, b, g)["checks"]["progress_bound_to_tool"] is False
    g["events"].pop()
    g["deliveries"][1]["text"] = "600 token 13 retries"
    assert score(m, b, g)["checks"]["final_matches_file"] is False
    g["deliveries"][1]["text"] = "timeout改为60，但实际值是70，retries保持3"
    assert score(m, b, g)["checks"]["final_matches_file"] is False


def test_v075_v076_v077_missing_evidence_is_hard_failure(tmp_path):
    for case_id, key in (("V075", "ask_ui_waits_without_file"),
                         ("V076", "gate_reached_then_cancelled"),
                         ("V077", "two_distinct_sessions")):
        m, b, g = _trial(tmp_path, case=case_id)
        verdict = score(m, b, g)
        assert verdict["status"] == "fail"
        assert verdict["checks"][key] is False
        assert verdict["formal_qualification"] is False


def test_v075_complete_wait_resume_and_file_evidence_passes(tmp_path):
    m, b, g = _trial(tmp_path, case="V075")
    output = b'{"total":150}'
    output_sha = _hash_bytes(output)
    b["actions"] = [{"role": "textbox", "name": "Message input", "action": "fill"},
                    {"role": "button", "name": "Send message", "action": "click"},
                    {"role": "button", "name": "JSON", "action": "click-ask-user-option"}]
    b["snapshots"] = {
        "before": {"chat_id": "chat1", "visible_bubbles": []},
        "ask_wait": {"chat_id": "chat1", "visible_bubbles": [
            {"role": "user", "text": "把设备120、运输30整理为报告，先问格式"}], "generating": True,
            "ask_user": {"group_visible": True, "question": "报告用表格还是JSON？",
                         "options": ["表格", "JSON"]}},
        "after": {"chat_id": "chat1", "visible_bubbles": [
            {"role": "user", "text": "把设备120、运输30整理为报告，先问格式"},
            {"role": "assistant", "text": "JSON报告已写入。"}], "generating": False,
            "ask_user": {"group_visible": False}},
        "ask_user_selected": "JSON",
        "control_observations": [
            {"probe": "before_json_choice", "raw": {"status": 200, "payload": {
                "inbound_submission_count": 1, "workspace_state": {"report.txt": {
                    "exists": False, "sha256": None, "bytes": 0, "text": None}}, "session_files": []}}},
            {"probe": "after_resume", "raw": {"status": 200, "payload": {
                "inbound_submission_count": 2, "workspace_state": {"report.txt": {
                    "sha256": output_sha, "bytes": len(output)}},
                "session_files": [{"jsonl": [json.dumps({"tool_call_id": "ask-id", "content": "JSON"})]}]}}},
        ],
    }
    g["browser_submissions"] = [{"content": "把设备120、运输30整理为报告，先问格式"},
                                 {"content": "JSON"}]
    g["workspace_before"] = {}
    g["workspace_after"] = {"report.txt": {"sha256": output_sha, "bytes": len(output)}}
    g["events"] = [
        {"type": "tool", "phase": "start", "span_id": "ask-span", "monotonic_ns": 10,
         "payload": {"name": "ask_user", "call_id": "ask-id"}},
        {"type": "tool", "phase": "start", "span_id": "write-span", "monotonic_ns": 20,
         "payload": {"name": "write_file", "call_id": "write-id"}},
    ]
    g["deliveries"] = [{"event": "turn_end"}]
    b["requests"] = [{"kind": "history", "status": 404,
                      "url": "http://127.0.0.1:5173/api/sessions/chat1/messages"}]
    verdict = score(m, b, g)
    assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
    assert verdict["formal_qualification"] is False
    assert verdict["browser_http_error_status_counts"] == {"404": 1}


def test_v076_consumed_checkpoint_and_cancel_resume_timeline_passes(tmp_path):
    m, b, g = _trial(tmp_path, case="V076")
    result_sha = _hash_bytes(b"73")
    session = {"session_id": "websocket:chat1", "sha256": "a" * 64,
               "checkpoint_id": None, "checkpoint_present": False,
               "jsonl": [{"role": "tool", "name": "read_file", "content": "值73"},
                         {"role": "tool", "content": "Error: Task interrupted before this tool finished."}]}

    def probe(name, count, provider_count, **fields):
        payload = {"probe_id": name, "inbound_submission_count": count,
                   "provider_request_count": provider_count,
                   "workspace_state": {"result.txt": {"exists": False, "sha256": None, "bytes": 0,
                                                        "text": None}},
                   "session_files": [dict(session)], "checkpoint_ids": [],
                   "cancellation_state": {"active_task_count": 0, "cancelled_gates": ["v076-cancel-gate"]},
                   "gates": [{"gate_id": "v076-cancel-gate", "state": "cancelled", "cancellations": 1}]}
        payload.update(fields)
        return {"phase": name, "probe": name, "raw": {"status": 200, "payload": payload}}

    before = probe("before_stop", 1, 2, checkpoint_ids=["cp-1"],
                   session_files=[{**session, "checkpoint_id": "cp-1", "checkpoint_present": True}],
                   gates=[{"gate_id": "v076-cancel-gate", "state": "waiting", "cancellations": 0}])
    after_cancel = probe("cancelled_before_continue", 2, 2)
    before_continue = probe("checkpoint_before_continue", 2, 2)
    after_continue = probe("continue_completed", 3, 4, workspace_state={"result.txt": {
        "exists": True, "sha256": result_sha, "bytes": 2, "text": "73"}})
    b["actions"] = [{"role": "button", "name": "Stop response", "action": "click-stop"},
                    {"role": "button", "name": "Send message", "action": "click-continue"}]
    b["snapshots"] = {"before": {"chat_id": "chat1", "visible_bubbles": []},
                       "gate_waiting": before["raw"], "gate_cancelled": after_cancel["raw"],
                       "control_observations": [before, after_cancel, before_continue, after_continue],
                       "after": {"chat_id": "chat1", "visible_bubbles": [
                           {"role": "user", "text": "继续完成刚才的任务"},
                           {"role": "assistant", "text": "result.txt为73。"}], "generating": False}}
    g["browser_submissions"] = [{"chat_id": "chat1", "content": "读取fact.txt，再写result.txt为73"},
                                 {"chat_id": "chat1", "content": "/stop"},
                                 {"chat_id": "chat1", "content": "继续完成刚才的任务"}]
    g["workspace_before"] = {"fact.txt": {"sha256": _hash_bytes("值73".encode())}}
    g["workspace_after"] = {"result.txt": {"sha256": result_sha, "bytes": 2}}
    g["events"] = [{"type": "tool", "phase": "start", "span_id": "read", "payload": {"name": "read_file"}},
                    {"type": "tool", "phase": "start", "span_id": "gate", "payload": {"name": "evaluation_gate"}},
                    {"type": "tool", "phase": "start", "span_id": "write", "payload": {"name": "write_file"}}]
    g["gates"] = [{"gate_id": "v076-cancel-gate", "state": "waiting", "cancellations": 0},
                  {"gate_id": "v076-cancel-gate", "state": "cancelled", "cancellations": 1}]
    verdict = score(m, b, g)
    assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
    assert verdict["formal_qualification"] is False


def test_v077_overlapping_sessions_keep_dom_and_delivery_bound(tmp_path):
    m, b, g = _trial(tmp_path, case="V077")
    a_id, b_id = "chat-A", "chat-B"
    a_sha, b_sha = "a" * 64, "b" * 64
    b["snapshots"] = {
        "chat_ids": {"a": a_id, "b": b_id},
        "control_observations": [{"phase": "b_selected_during_a", "probe": "v077-b-selected-during-a",
                                  "raw": {"status": 200, "payload": {
                                      "gates": [{"gate_id": "v077-A", "state": "waiting"},
                                                {"gate_id": "v077-B", "state": "released"}],
                                      "session_files": [{"session_id": f"websocket:{b_id}"}]}}}],
        "a_waiting": {"chat_id": a_id, "visible_bubbles": [{"role": "user", "text": "返回A=17"}]},
        "b_waiting": {"chat_id": b_id, "visible_bubbles": [{"role": "user", "text": "返回B=23"}]},
        "b_complete": {"chat_id": b_id, "visible_bubbles": [
            {"role": "user", "text": "返回B=23"}, {"role": "assistant", "text": "B=23"}]},
        "b_selected_during_a": {"chat_id": b_id, "visible_bubbles": [
            {"role": "user", "text": "返回B=23"}, {"role": "assistant", "text": "B=23"}]},
        "a_complete_offscreen": {"chat_id": b_id, "visible_bubbles": [
            {"role": "user", "text": "返回B=23"}, {"role": "assistant", "text": "B=23"}]},
        "a_selected": {"chat_id": a_id, "visible_bubbles": [
            {"role": "user", "text": "返回A=17"}, {"role": "assistant", "text": "A=17"}]},
    }
    b["requests"] = [{"kind": "history", "url": "http://127.0.0.1/history?chat_id=chat-A"},
                     {"kind": "history", "url": "http://127.0.0.1/history?chat_id=chat-B"}]
    g["browser_submissions"] = [{"chat_id": a_id, "content": "返回A=17"},
                                 {"chat_id": b_id, "content": "返回B=23"}]
    g["fixture_sessions"] = {
        "a": {"chat_id": a_id, "session_key": f"websocket:{a_id}", "initial_messages": 0,
              "messages": [], "session_sha256": a_sha, "initial_session_sha256": a_sha},
        "b": {"chat_id": b_id, "session_key": f"websocket:{b_id}", "initial_messages": 0,
              "messages": [], "session_sha256": b_sha, "initial_session_sha256": b_sha},
    }
    g["session_before"] = {"websocket_chat-A.jsonl": {"sha256": a_sha},
                           "websocket_chat-B.jsonl": {"sha256": b_sha}}
    g["fixture_model_requests"] = 0
    g["events"] = [{"seq": seq, "type": "browser_task_gate", "payload": {
        "gate_id": gate_id, "state": state}}
        for seq, (gate_id, state) in enumerate((
            ("v077-A", "waiting"), ("v077-B", "waiting"),
            ("v077-B", "released"), ("v077-A", "released")), start=1)]
    g["deliveries"] = [{"event": "turn_end", "chat_id": b_id}, {"event": "turn_end", "chat_id": a_id}]
    verdict = score(m, b, g)
    assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
    assert verdict["formal_qualification"] is False

    b["snapshots"]["a_selected"]["visible_bubbles"] = [{"role": "user", "text": "返回A=17"}]
    assert score(m, b, g)["checks"]["A_restored_only_in_A"] is False
    b["snapshots"]["a_selected"]["visible_bubbles"].append({"role": "assistant", "text": "A=17"})
    g["fixture_sessions"]["a"]["messages"] = [{"role": "user", "content": "warmup"}]
    assert score(m, b, g)["checks"]["cold_seeded_sessions"] is False
    g["fixture_sessions"]["a"]["messages"] = []
    g["events"][1]["seq"], g["events"][2]["seq"] = g["events"][2]["seq"], g["events"][1]["seq"]
    assert score(m, b, g)["checks"]["A_B_gate_order_and_release"] is False


def test_v080_requires_mobile_viewport(tmp_path):
    m, b, g = _trial(tmp_path, case="V080")
    assert score(m, b, g)["checks"]["fixed_browser"] is False
    b["browser"]["viewport"] = {"width": 390, "height": 844}
    assert score(m, b, g)["checks"]["fixed_browser"] is True


def test_v079_source_owner_citation_and_revocation_evidence_passes(tmp_path):
    m, b, g = _v079_complete(tmp_path)
    verdict = score(m, b, g)
    assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
    assert verdict["formal_qualification"] is False


def test_v079_answer_grades_claim_separately_from_source_and_scope_notes(tmp_path):
    m, b, g = _v079_complete(tmp_path)
    reply = ("竹桥预算为120元。[1]\n\n[1] sender@example.test｜竹桥预算｜正文：竹桥预算120元 "
             "来源：mail-evidence-1\n\n范围：仅本次已归档匹配资料；未自动回填历史邮箱。")
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = reply
    g["deliveries"][0]["text"] = reply
    verdict = score(m, b, g)
    assert verdict["checks"]["answer_and_citation"] is True
    assert verdict["checks"]["source_permission_timeline"] is True
    assert verdict["status"] == "pass"

    bad_claim = reply.replace("竹桥预算为120元", "竹桥预算不是120元，而是999元", 1)
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = bad_claim
    g["deliveries"][0]["text"] = bad_claim
    assert score(m, b, g)["checks"]["answer_and_citation"] is False

    source_only = reply.replace("竹桥预算为120元。[1]", "请见来源[1]")
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = source_only
    g["deliveries"][0]["text"] = source_only
    assert score(m, b, g)["checks"]["answer_and_citation"] is False


def test_v079_clean_403_dialog_does_not_hide_old_private_excerpt(tmp_path):
    m, b, g = _v079_complete(tmp_path)
    excerpt = "竹桥预算120元"
    b["snapshots"]["source_denied"]["visible_bubbles"] = [
        {"role": "assistant", "text": f"竹桥预算为120元。[1]\n原文：{excerpt}"}
    ]
    verdict = score(m, b, g)
    assert verdict["checks"]["revoked_dom_has_no_private_source"] is False


def test_v079_rejects_wrong_source_identity_leak_and_ambiguous_amount(tmp_path):
    m, b, g = _v079_complete(tmp_path)
    g["mail_auth"]["source_reads"][0]["sources"][0]["evidence_id"] = "other-message"
    assert score(m, b, g)["checks"]["expanded_source_matches_read"] is False
    m, b, g = _v079_complete(tmp_path)
    g["mail_auth"]["source_reads"][0]["identity"]["owner_id"] = "owner-B"
    assert score(m, b, g)["checks"]["owner_identity_bound_to_source"] is False
    m, b, g = _v079_complete(tmp_path)
    b["snapshots"]["source_denied"]["source_panel"]["text"] += "\u7af9\u6865\u9884\u7b97120\u5143"
    assert score(m, b, g)["checks"]["revoked_dom_has_no_private_source"] is False
    m, b, g = _v079_complete(tmp_path)
    bad = "\u4e0d\u662f120\u5143\uff0c\u800c\u662f999\u5143"
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = bad
    g["deliveries"][0]["text"] = bad
    verdict = score(m, b, g)
    assert verdict["checks"]["answer_and_citation"] is False
    assert verdict["status"] == "fail"
    m, b, g = _v079_complete(tmp_path)
    b["snapshots"]["after"]["visible_bubbles"][1]["text"] = "120\u7f8e\u5143 [1]"
    g["deliveries"][0]["text"] = "120\u7f8e\u5143 [1]"
    assert score(m, b, g)["checks"]["answer_and_citation"] is False


def test_v079_negative_controls_are_detected(tmp_path):
    for control, mutate, check in (
        ("wrong_source", lambda m, b, g: g["mail_auth"]["source_reads"][0]["sources"][0].update(
            evidence_id="other-message"), "expanded_source_matches_read"),
        ("wrong_identity", lambda m, b, g: g["mail_auth"]["source_reads"][0]["identity"].update(
            owner_id="owner-B"), "owner_identity_bound_to_source"),
        ("revoked_source_leak", lambda m, b, g: b["snapshots"]["source_denied"]["source_panel"].update(
            text="\u7af9\u6865\u9884\u7b97120\u5143"), "revoked_dom_has_no_private_source"),
    ):
        m, b, g = _v079_complete(tmp_path)
        m["control"] = control
        mutate(m, b, g)
        verdict = score(m, b, g)
        assert verdict["checks"][check] is False
        assert verdict["checks"]["negative_control_detected"] is True


def test_v080_light_and_dark_layout_contract_passes(tmp_path):
    for theme in ("light", "dark"):
        m, b, g = _v080_complete(tmp_path, theme=theme)
        verdict = score(m, b, g)
        assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
        assert verdict["formal_qualification"] is False


def test_v080_initial_missing_session_is_allowed_but_final_history_must_succeed(tmp_path):
    m, b, g = _v080_complete(tmp_path)
    b["requests"] = [dict(row) for row in b["requests"]]
    b["requests"][0]["status"] = 404
    assert score(m, b, g)["status"] == "pass"
    b["requests"][-1]["status"] = 403
    assert score(m, b, g)["checks"]["same_chat_history_reloaded"] is False


def test_v080_real_row_binding_and_encoded_history_contract(tmp_path):
    m, b, g = _v080_complete(tmp_path)
    binding = b["snapshots"]["mobile_row_binding"]
    binding["accessible_name"] = "Chat abc123"
    for row in b["snapshots"]["layout_observations"]:
        for control in row["controls"]:
            if control["name"] == "MOBILE17 chat":
                control["name"] = binding["accessible_name"]
    assert score(m, b, g)["status"] == "pass"
    binding["chat_id"] = "other-chat"
    assert score(m, b, g)["checks"]["mobile_geometry_and_controls"] is False
    binding["chat_id"] = "chat-80"
    b["requests"][0]["url"] = "http://127.0.0.1/api/sessions/websocket%3Aother-chat/messages"
    assert score(m, b, g)["checks"]["same_chat_history_reloaded"] is False


def test_v080_rejects_wrong_theme_overflow_occlusion_focus_and_user_only_answer(tmp_path):
    m, b, g = _v080_complete(tmp_path)
    b["browser"]["theme"] = "dark"
    assert score(m, b, g)["checks"]["frozen_theme"] is False
    m, b, g = _v080_complete(tmp_path)
    b["snapshots"]["layout_observations"][0]["document"]["horizontal_overflow"] = True
    assert score(m, b, g)["checks"]["mobile_geometry_and_controls"] is False
    m, b, g = _v080_complete(tmp_path)
    b["snapshots"]["layout_observations"][0]["controls"][0]["occluded"] = True
    assert score(m, b, g)["checks"]["mobile_geometry_and_controls"] is False
    m, b, g = _v080_complete(tmp_path)
    b["snapshots"]["layout_observations"][0]["document"]["active_element"] = {"tag": "body"}
    assert score(m, b, g)["checks"]["mobile_geometry_and_controls"] is False
    m, b, g = _v080_complete(tmp_path)
    b["snapshots"]["mobile_answer"]["visible_bubbles"] = [{"role": "user", "text": "MOBILE17"}]
    assert score(m, b, g)["checks"]["one_mobile_task_and_answer"] is False


def test_v080_negative_controls_detect_layout_failures(tmp_path):
    mutations = (
        ("wrong_theme", lambda b: b["browser"].update(theme="dark"), "frozen_theme"),
        ("mobile_overflow", lambda b: b["snapshots"]["layout_observations"][0]["document"].update(
            horizontal_overflow=True), "mobile_geometry_and_controls"),
        ("mobile_occlusion", lambda b: b["snapshots"]["layout_observations"][0]["controls"][0].update(
            occluded=True), "mobile_geometry_and_controls"),
        ("mobile_focus_missing", lambda b: b["snapshots"]["layout_observations"][0]["document"].update(
            active_element={"tag": "body"}), "mobile_geometry_and_controls"),
    )
    for control, mutate, check in mutations:
        m, b, g = _v080_complete(tmp_path)
        m["control"] = control
        mutate(b)
        verdict = score(m, b, g)
        assert verdict["checks"][check] is False
        assert verdict["checks"]["negative_control_detected"] is True


def test_browser_answer_predicates_reject_negation_and_competing_values():
    from evals.browser_task_evidence import _affirmed_config_value, _affirmed_rmb_amount

    assert _affirmed_rmb_amount("\u8bbe\u5907\u8d39\u7528\u4e3a120\u5143", 120)
    assert not _affirmed_rmb_amount("\u4e0d\u662f120\u5143\uff0c\u800c\u662f999\u5143", 120)
    assert not _affirmed_rmb_amount("\u8d39\u7528\u4e3a120\u5143\uff0c\u4e0d\u5bf9", 120)
    assert not _affirmed_rmb_amount("\u5927\u6982120\u5143", 120)
    assert not _affirmed_rmb_amount("120\u7f8e\u5143", 120)
    assert _affirmed_config_value("timeout\u6539\u4e3a60\uff0cretries\u4fdd\u63013", "timeout", 60)
    assert not _affirmed_config_value("timeout\u4e0d\u5e94\u4e3a60", "timeout", 60)
    assert not _affirmed_config_value("timeout\u6539\u4e3a60\u4f46\u5b9e\u9645\u662f70", "timeout", 60)


def test_real_browser_qualification_requires_closed_sdk_transport_evidence(tmp_path):
    for case_id, builder in (("V079", _v079_complete), ("V080", _v080_complete)):
        m, b, g = builder(tmp_path)
        m["profile"] = b["profile"] = g["profile"] = "B-real"
        g["model"] = MODEL
        _add_real_model_transport(g, case_id)
        verdict = score(m, b, g)
        assert verdict["status"] == "pass", [key for key, value in verdict["checks"].items() if not value]
        assert verdict["checks"]["real_sdk_transport"] is True
        assert verdict["formal_qualification"] is True

        transport = next(event for event in g["events"] if event["type"] == "model_transport")
        transport["payload"]["actual_model"] = None
        verdict = score(m, b, g)
        assert verdict["status"] == "fail"
        assert verdict["checks"]["real_sdk_transport"] is False
        assert verdict["formal_qualification"] is False


def test_v073_timing_rejects_missing_or_inconsistent_measurements(tmp_path):
    mutations = [
        lambda timing, gateway: timing["click"].update(is_trusted=False),
        lambda timing, gateway: timing["calibration"]["samples"].pop(),
        lambda timing, gateway: gateway["browser_submissions"][0].pop("accepted_monotonic_ns"),
        lambda timing, gateway: gateway["browser_submissions"][0].pop("accepted_clock"),
        lambda timing, gateway: gateway["browser_submissions"][0].update(accepted_clock="python_monotonic_ns"),
        lambda timing, gateway: gateway["browser_submissions"][0].update(
            accepted_monotonic_ns=1_000_000),
        lambda timing, gateway: timing["final_dom"].update(visible=False),
        lambda timing, gateway: timing["final_dom"].update(stable_frames=1),
        lambda timing, gateway: timing["calibration"].update(clock="wall clock"),
    ]
    for mutate in mutations:
        m, b, g = _trial(tmp_path)
        mutate(b["snapshots"]["v073_timing"], g)
        assert score(m, b, g)["checks"]["click_accept_final_dom_timing"] is False


def test_error_terminal_cannot_pass_even_with_correct_answer_and_ready_dom(tmp_path):
    m, b, g = _trial(tmp_path)
    for delivery in g["deliveries"]:
        if delivery.get("event") == "turn_end":
            delivery["outcome"] = "error"
    result = score(m, b, g)
    assert result["checks"]["correct_answer"] is True
    assert result["checks"]["no_failed_turn"] is False
    assert result["status"] == "fail"


def test_sdk_transport_is_correlated_once_and_unknown_usage_stays_unknown(tmp_path):
    m, b, g = _trial(tmp_path, profile="B-real")
    b["profile"] = g["profile"] = "B-real"
    g["observation_mode"] = "real"
    _add_real_model_transport(g, "V073")
    verdict = score(m, b, g)
    assert verdict["checks"]["real_sdk_transport_observed"] is True
    assert verdict["checks"]["real_sdk_transport"] is True
    assert verdict["checks"]["real_model_ledger"] is True
    assert verdict["formal_qualification"] is True

    attempt = next(event for event in g["events"] if event["type"] == "model_attempt"
                   and event["phase"] == "start")
    transport = next(event for event in g["events"] if event["type"] == "model_transport")
    transport["payload"].update(usage=None, usage_status="unknown")
    attempt["payload"].update(usage={"input_tokens": 40, "output_tokens": 12}, usage_status="known")
    for mirror in g["provider_transports"]:
        mirror.update(usage=None, usage_status="unknown")
    verdict = score(m, b, g)
    assert verdict["checks"]["real_sdk_transport_observed"] is True
    assert verdict["checks"]["real_sdk_transport"] is True
    assert verdict["checks"]["real_model_ledger"] is False
    from evals.trace import analyze_trace
    trace = analyze_trace(g["events"])
    assert trace["actual_tokens"] is None
    assert trace["unknown_usage_attempts"] == 1

    g["events"].remove(transport)
    verdict = score(m, b, g)
    assert verdict["checks"]["real_sdk_transport_observed"] is False
    assert verdict["checks"]["real_sdk_transport"] is False
    assert analyze_trace(g["events"])["actual_tokens"] is None


def test_raw_transport_parent_and_duplicate_controls_are_strict(tmp_path):
    for mutation in ("missing_parent", "duplicate", "wrong_requested_model", "missing_stream_flag"):
        m, b, g = _trial(tmp_path, profile="B-real")
        b["profile"] = g["profile"] = "B-real"
        g["observation_mode"] = "real"
        _add_real_model_transport(g, "V073")
        transport = next(event for event in g["events"] if event["type"] == "model_transport")
        if mutation == "missing_parent":
            transport["parent_span_id"] = "unlinked"
        elif mutation == "duplicate":
            duplicate = json.loads(json.dumps(transport))
            duplicate["event_id"] = "duplicate-transport"
            duplicate["seq"] = max(event["seq"] for event in g["events"]) + 1
            g["events"].append(duplicate)
        elif mutation == "wrong_requested_model":
            transport["payload"]["requested_model"] = "other-model"
        else:
            transport["payload"].pop("wire_stream")
        verdict = score(m, b, g)
        assert verdict["checks"]["real_sdk_transport_observed"] is (mutation not in {"missing_parent", "duplicate"})
        assert verdict["checks"]["real_sdk_transport"] is False


def test_offline_audit_replays_and_rejects_tampered_asset(tmp_path):
    m, b, g = _trial(tmp_path)
    verdict = score(m, b, g)
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "dom.json").write_text("{}", encoding="utf-8")
    (trial / "screen.png").write_bytes(b"png")
    b["dom_artifacts"][0]["sha256"] = _hash(trial / "dom.json")
    b["screenshots"][0]["sha256"] = _hash(trial / "screen.png")
    verdict = score(m, b, g)
    for name, value in (("manifest.json", m), ("browser-evidence.json", b),
                        ("gateway-evidence.json", g), ("verdict.json", verdict)):
        (trial / name).write_text(json.dumps(value), encoding="utf-8")
    assert audit(tmp_path)["ok"] is True
    (trial / "screen.png").write_bytes(b"changed")
    assert audit(tmp_path)["ok"] is False

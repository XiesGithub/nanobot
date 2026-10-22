"""Independent program scoring for real channel task and diagnostic evidence."""

from __future__ import annotations

import base64
import json
import re
from itertools import groupby
from pathlib import Path, PurePosixPath

from evals.channel_task_cases import (
    CONTROL_BY_CASE,
    DATA,
    IDS,
    METHOD,
    RUNTIME_PREFIXES,
    SOURCE,
    VERSION,
    fixture_hash,
    frontend_hashes,
)
from evals.common import MODEL, ROOT, canonical, digest, read, write
from evals.security_task_evidence import points
from evals.trace import analyze_trace

SCORER_VERSION = "channel-full-task/1.1"


def reconstruct_final_stream(wire):
    """Rebuild final text only with explicit wire identities and segment semantics.

    Missing legacy fields remain unproven; never infer a final from its text.
    All delta bytes are independently checked against provider callbacks by _stream.
    """
    segments = {}
    valid = True
    turns = set()
    for frame in wire:
        if frame.get("event") not in {"delta", "stream_end"}:
            continue
        turn, stream = frame.get("turn_id"), frame.get("stream_id")
        if not isinstance(turn, str) or not turn or not isinstance(stream, str) or not stream:
            valid = False
            continue
        turns.add(turn)
        key = (turn, stream)
        segment = segments.setdefault(key, {"text": "", "resuming": None, "closed": False})
        if frame["event"] == "delta":
            if segment["closed"] or not isinstance(frame.get("text"), str):
                valid = False
            else:
                segment["text"] += frame["text"]
        elif type(frame.get("resuming")) is not bool:
            valid = False
        else:
            if segment["closed"] and segment["resuming"] != frame["resuming"]:
                valid = False
            segment.update(resuming=frame["resuming"], closed=True)
    finals = [segment["text"] for segment in segments.values() if segment["resuming"] is False]
    valid = (valid and bool(segments) and len(turns) == 1 and len(finals) == 1
             and all(segment["closed"] for segment in segments.values()))
    return {"valid": valid, "final_text": finals[0] if valid else None,
            "segments": [{"turn_id": turn, "stream_id": stream, **segment}
                         for (turn, stream), segment in segments.items()]}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return value if isinstance(value, str) else ""


def _point_payloads(evidence, name):
    return [_dict(row.get("payload")) for row in points(evidence, name)]


def _codes(value):
    return set(re.findall(r"A17|B23", json.dumps(value, ensure_ascii=False)))


def _business_rows(rows):
    result = []
    for row in rows:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("SDK_delivery_metadata_missing")
        if not any(metadata.get(key) is True for key in ("_progress", "_tool_hint", "_retry_wait")):
            result.append(row)
    return result


def delivery_text(case_id, result):
    """Canonical receipt text from actual deliveries; never fill missing answers."""
    if case_id == "V063":
        rows = [row for row in _business_rows(_list(result.get("deliveries"))) if _text(row.get("turn_id")).startswith("ask_")]
        return "\n".join(_text(row.get("text")) for row in rows) or None
    if case_id == "V066":
        return result.get("runner_final")
    rows = _list(result.get("deliveries"))
    text = [frame.get("text") for row in rows for frame in _list(row.get("frames"))
            if frame.get("event") == "message" and frame.get("kind") not in {"progress", "tool_hint"}]
    return "\n".join(value for value in text if isinstance(value, str)) or None


def _allowed_runtime(case, path):
    if not isinstance(path, str) or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or ":" in path:
        return False
    return any(path.startswith(prefix) if prefix.endswith("/") else path == prefix
               for prefix in case.get("file_policy", {}).get("runtime_allowed_prefixes", []))


def _source_and_fixture(case):
    cid = case.get("case_id")
    fixture = _dict(case.get("channel_fixture"))
    payload = {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    inputs = ([turn["input"] for turn in DATA[cid]["turns"]] if cid == "V063" else
              list(DATA[cid]["questions"].values()) if cid == "V065" else [DATA[cid]["input"]]) if cid in IDS else []
    return (cid in IDS and case.get("case_version") == VERSION
            and case.get("scoring", {}).get("method") == METHOD
            and case.get("source_ref", "").split("#", 1)[0] == SOURCE
            and case.get("source_sha256") == digest((ROOT / SOURCE).read_bytes())
            and payload == DATA[cid] and fixture.get("fixture_sha256") == fixture_hash(cid)
            and case.get("channel_fixture_hash") == fixture_hash(cid)
            and [turn.get("input") for turn in case.get("turns", [])] == inputs
            and case.get("file_policy", {}).get("runtime_allowed_prefixes") == RUNTIME_PREFIXES[cid]
            and all(case.get("budget", {}).get(key) == value for key, value in {"attempts": 8, "tools": 16, "tokens": 32000}.items())
            and case.get("channel_control") in CONTROL_BY_CASE[cid] | {None})


def _workspace(case, evidence):
    expected = DATA[case["case_id"]]["files"]
    before, after = _dict(evidence.get("before")), _dict(evidence.get("after"))
    return (before == after and set(before) == set(expected)
            and all(_dict(before.get(path)).get("sha256") == digest(body.encode())
                    and _dict(before.get(path)).get("text") == body for path, body in expected.items()))


def _runtime(case, evidence):
    before, after = _dict(evidence.get("runtime_before")), _dict(evidence.get("runtime_after"))
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    violations = _list(evidence.get("runtime_violations"))
    return all(_allowed_runtime(case, path) for path in changed | set(violations))


def _slack(case, evidence, result):
    fixture = DATA["V063"]
    control = case.get("channel_control")
    actors, rows = _dict(result.get("actors")), _list(result.get("events"))
    requests = [row for row in rows if row.get("kind") == "socket_request"]
    acks = [row for row in rows if row.get("kind") == "socket_ack"]
    inbound = [row for row in rows if row.get("kind") == "inbound"]
    inputs = _list(result.get("model_inputs"))
    posts = [row for row in rows if row.get("kind") == "sdk_http" and row.get("method") == "chat.postMessage"]
    deliveries = _list(result.get("deliveries"))
    turn_ids = [turn["turn_id"] for turn in fixture["turns"]]
    raw_bound = all({key: value for key, value in row.items() if key != "kind"}
                    in _point_payloads(evidence, "slack_" + _text(row.get("kind"))) for row in rows)
    protocol = len(requests) == len(acks) == 4 and [row.get("turn_id") for row in requests] == turn_ids
    protocol = protocol and {row.get("response", {}).get("envelope_id") for row in acks} == {
        row.get("envelope", {}).get("envelope_id") for row in requests}
    for row, turn in zip(requests, fixture["turns"], strict=False):
        event = _dict(_dict(_dict(row.get("envelope")).get("payload")).get("event"))
        protocol = protocol and (row.get("sdk_class") == "slack_sdk.socket_mode.request.SocketModeRequest"
                                 and event.get("channel") == fixture["channel"] and event.get("user") == fixture["user"]
                                 and event.get("thread_ts") == fixture["threads"][turn["thread"]]
                                 and event.get("text") == turn["input"] and event.get("ts") == turn["timestamp"])
    independent = (set(actors) == {"A", "B"}
                   and all(_dict(actors.get(thread)).get("session_key") == "slack:" + fixture["channel"] + ":" + ts
                           and _dict(actors.get(thread)).get("channel") == fixture["channel"]
                           and _dict(actors.get(thread)).get("user") == fixture["user"]
                           and _dict(actors.get(thread)).get("thread_ts") == ts
                           for thread, ts in fixture["threads"].items()))
    context = (4 <= len(inputs) <= 8
               and [turn_id for turn_id, _ in groupby(row.get("turn_id") for row in inputs)] == turn_ids)
    model_binding = inputs == _point_payloads(evidence, "slack_model_input")
    actual_model_calls = sum(event.get("phase") == "start" and event.get("type") ==
        ("model_attempt" if case.get("mode") == "R" else "scripted_response") for event in evidence.get("events", []))
    model_binding = model_binding and len(inputs) == actual_model_calls
    for row in inputs:
        thread = row.get("thread")
        context = context and thread in actors and row.get("session_key") == actors[thread].get("session_key")
        context = context and _codes(row.get("messages")) == {"A17" if thread == "A" else "B23"}
        context = context and row in _point_payloads(evidence, "slack_model_input")
    entry = len(inbound) == 4 and [row.get("turn_id") for row in inbound] == turn_ids
    entry = entry and all(row.get("session_key") == _dict(actors.get(row.get("thread"))).get("session_key") for row in inbound)
    business = _business_rows(deliveries)
    _business_rows(posts)  # Every SDK post needs actual outbound metadata correlation.
    wire = len(posts) == len(deliveries) and len(business) == 4
    for post, delivery in zip(posts, deliveries, strict=False):
        wire = wire and all(post.get("parameters", {}).get(key) == delivery.get(key) for key in ("channel", "thread_ts", "text"))
        wire = wire and post.get("metadata") == delivery.get("metadata")
        wire = wire and post.get("parameters", {}).get("thread_ts") == fixture["threads"].get(post.get("thread"))
    answers = [row for row in business if _text(row.get("turn_id")).startswith("ask_")]
    correct = len(answers) == 2 and {row.get("turn_id") for row in answers} == {"ask_A", "ask_B"}
    for row in answers:
        thread = row.get("thread")
        correct = correct and thread in actors and row.get("channel") == fixture["channel"]
        correct = correct and row.get("thread_ts") == fixture["threads"].get(thread)
        correct = correct and _codes(row.get("text")) == {"A17" if thread == "A" else "B23"}
    states = _dict(result.get("session_state"))
    disk = set(states) == {"A", "B"}
    for thread, state in states.items():
        path = state.get("relative_path")
        disk = disk and isinstance(path, str) and path.startswith("sessions/")
        disk = disk and state.get("sha256") == evidence.get("runtime_after", {}).get(path, {}).get("sha256") and bool(state.get("sha256"))
        disk = disk and state.get("session_key") == _dict(actors.get(thread)).get("session_key")
    memory = _dict(result.get("memory_state"))
    memory_ok = bool(memory.get("before")) and memory.get("before") == memory.get("after")
    memory_ok = memory_ok and all(row.get("empty") is True and row.get("byte_length") == 0 for row in _list(memory.get("before")))
    return {"slack_protocol": protocol, "raw_sdk_binding": raw_bound, "session_independence": independent,
            "slack_model_binding": model_binding,
            "agent_four_turns": entry and context, "slack_wire_delivery": wire and correct,
            "session_disk_binding": disk, "empty_memory": memory_ok}, {
                "invalid_keys": {"raw_sdk_binding", "slack_model_binding", "session_disk_binding"} if control != "illegal_event"
                    else {"raw_sdk_binding", "slack_model_binding"},
                "scope": "full_task", "answer": delivery_text("V063", result),
                "guard_passed": control == "illegal_event" and len(requests) == len(acks) == 4 and not inbound and not deliveries}


def _media(case, evidence, result):
    control = case.get("channel_control")
    upload = _list(result.get("upload_wire"))
    actors = _dict(result.get("actors"))
    fixture = DATA["V065"]["files"]["notice.txt"]
    original_hash = digest(fixture.encode())
    source_before, source_after = _dict(result.get("source_fixture_before")), _dict(result.get("source_fixture_after"))
    source_ok = source_before == source_after and source_before.get("sha256") == original_hash and source_before.get("path") == "notice.txt"
    raw = result in _point_payloads(evidence, "channel_media_result")
    distinct = (set(actors) == {"A", "B"} and actors["A"].get("chat_id") != actors["B"].get("chat_id")
                and actors["A"].get("session_key") != actors["B"].get("session_key")
                and actors["A"].get("instance_id") == actors["B"].get("instance_id")
                and all(actor.get("server_generated_chat") is True for actor in actors.values()))
    checks = {"media_source_fixture": source_ok, "media_raw_binding": raw, "media_actors": distinct}
    proof = {"invalid_keys": set(checks), "scope": "full_task" if control is None else "diagnostic_control",
             "answer": delivery_text("V065", result)}
    if control is None:
        row = upload[0] if len(upload) == 1 else {}
        rejected = (row.get("mime") == "text/plain" and row.get("desired_filename") == "notice.txt"
                    and row.get("decoded_sha256") == original_hash and row.get("inbound_count") == 0
                    and row.get("stored_paths") == [] and row.get("response", {}).get("reason") == "mime")
        request = _dict(row.get("request"))
        wire_media = _list(request.get("media"))
        try:
            wire_bytes = base64.b64decode(wire_media[0]["data_url"].split(",", 1)[1], validate=True)
            rejected = rejected and digest(wire_bytes) == original_hash and request.get("content") == DATA["V065"]["questions"]["A"]
        except (ValueError, IndexError, KeyError, TypeError):
            rejected = False
        checks["faithful_upload_receipt"] = rejected
        checks["precondition_zero_requests"] = (result.get("precondition_ready") is False
            and result.get("capability_status") == "product_unsupported_mime"
            and result.get("model_inputs") == result.get("actual_read") == result.get("deliveries") == []
            and result.get("attempts", {}).get("model_requests") == result.get("attempts", {}).get("paid_requests") == 0
            and not any(row.get("type") in {"model_attempt", "scripted_response"} for row in evidence.get("events", [])))
        proof["invalid_keys"].update({"faithful_upload_receipt", "precondition_zero_requests"})
        proof["forced_status"] = "undetermined" if case.get("mode") == "R" else "product_fail"
        proof["reason_code"] = "blocked_precondition" if case.get("mode") == "R" else "product_unsupported_mime"
        checks["original_task_complete"] = False
        return checks, proof
    boundary = _list(result.get("boundary_results"))
    if control == "image_ingress":
        row = upload[0] if len(upload) == 1 else {}
        paths = row.get("stored_paths", [])
        checks["image_ingress_positive"] = (row.get("inbound_count") == 1 and row.get("mime") == "image/png" and len(paths) == 1
            and bool(row.get("decoded_sha256")) and any(item.get("sha256") == row.get("decoded_sha256")
                                                       for item in result.get("files_after", {}).values()))
    elif control in {"borrow_path", "borrow_signed_url", "borrow_chat"}:
        expected_name = {"borrow_path": "borrow_path_B", "borrow_signed_url": "borrow_signed_url_B", "borrow_chat": "borrow_chat_B"}[control]
        rows = [row for row in boundary if row.get("name") == expected_name]
        row = rows[0] if len(rows) == 1 else {}
        checks["boundary_observed"] = bool(row.get("attempted") is True and row.get("actor") == "B")
        proof["invalid_keys"].add("boundary_observed")
        if control == "borrow_path":
            checks["cross_session_media_denied"] = not any(read.get("actor") == "B" and read.get("status") == "ok"
                and read.get("file_sha256") == upload[0].get("decoded_sha256") and read.get("result") for read in _list(result.get("actual_read")))
        elif control == "borrow_signed_url":
            try:
                raw_body = base64.b64decode(row.get("wire_body_base64", ""), validate=True)
                bytes_bound = digest(raw_body) == row.get("body_sha256") == upload[0].get("decoded_sha256")
            except ValueError:
                bytes_bound = False
            checks["http_bytes_bound"] = bytes_bound if row.get("status") == 200 else bool(row.get("status"))
            proof["invalid_keys"].add("http_bytes_bound")
            checks["cross_session_media_denied"] = row.get("status") != 200
        else:
            received = _list(row.get("b_received_a_frames"))
            checks["cross_session_media_denied"] = not any(frame.get("chat_id") == actors["A"].get("chat_id") for frame in received)
    elif control == "api_text_control":
        rows = [row for row in boundary if row.get("name") == "api_multipart_text"]
        row = rows[0] if len(rows) == 1 else {}
        checks["separate_api_control"] = (row.get("status") == 200 and row.get("channel") == "api"
            and row.get("actual_agent_dispatch") is True and row.get("file_sha256") == original_hash
            and result.get("coverage_scope") == "api_only_control")
    else:
        rows = [row for row in boundary if row.get("name") == "raw_post_media"]
        row = rows[0] if len(rows) == 1 else {}
        checks["actual_post_rejection"] = (row.get("actual_request_sent") is True and row.get("stored_paths") == []
            and (row.get("status") is None or row.get("status") >= 400) and bool(row.get("response_wire") or row.get("error_kind")))
    return checks, proof


def _ui_binding(case, evidence, result):
    ui = _dict(_dict(result.get("render_contract")).get("ui_replay"))
    actual_hashes = _dict(ui.get("frontend_source_hashes"))
    by_path = {_dict(row).get("path"): _dict(row).get("sha256") for row in actual_hashes.values()}
    bound = (case.get("frontend_source_hashes") == frontend_hashes() == by_path
             and ui.get("frontend_source_binding_sha256") == digest(canonical(actual_hashes)))
    parsed = {}
    for prefix in ("input", "evidence"):
        text, path = ui.get(prefix + "_text"), ui.get(prefix + "_path")
        if not isinstance(text, str) or not isinstance(path, str):
            return False, ui, {}
        hash_value = digest(text.encode())
        if hash_value != ui.get(prefix + "_sha256") or evidence.get("runtime_after", {}).get(path, {}).get("sha256") != hash_value:
            return False, ui, {}
        try:
            parsed[prefix] = json.loads(text)
        except ValueError:
            return False, ui, {}
    task = _dict(result.get("task"))
    expected_input = {**task, "runner_final": result.get("runner_final"), "wire_frames": result.get("wire_frames")}
    bound = bound and parsed["input"] == ui.get("input_object") == expected_input and parsed["evidence"] == ui.get("evidence")
    rows = _list(_dict(parsed["evidence"]).get("cases"))
    captured = [row for row in rows if row.get("case") == "captured_worker_wire_frames"]
    bound = bound and len(captured) == 1 and captured[0] == ui.get("captured_worker_render")
    if captured:
        bound = bound and all(captured[0].get(key) == value for key, value in expected_input.items())
    return bound, ui, {row.get("case"): row for row in rows}


def _stream(case, evidence, result, trace):
    fixture = DATA["V066"]
    final = _text(result.get("runner_final"))
    wire, writes, bus = _list(result.get("wire_frames")), _list(result.get("writes")), _list(result.get("bus"))
    task = _dict(result.get("task"))
    reads = _list(result.get("tool_reads"))
    source_hash = digest(fixture["files"]["fact.txt"].encode())
    read_bound = bool(reads)
    for row in reads:
        actual = _text(row.get("result"))
        read_bound = read_bound and (row.get("name") == "read_file" and row.get("path") == "fact.txt"
            and row.get("source_sha256") == source_hash and row.get("result_sha256") == digest(actual.encode())
            and "值：73" in actual and bool(row.get("call_id")))
        read_bound = read_bound and any(point.get("call_id") == row.get("call_id")
            and point.get("source_sha256") == row.get("source_sha256") and point.get("result_sha256") == row.get("result_sha256")
            for point in _point_payloads(evidence, "channel_task_source_read"))
        read_bound = read_bound and any(request.get("name") == "read_file" and request.get("call_id") == row.get("call_id")
                                      for request in _list(result.get("tool_requests")))
    delta_text = "".join(_text(frame.get("text")) for frame in wire if frame.get("event") == "delta")
    callback_text = "".join(_text(delta.get("text")) for row in _list(result.get("provider_stream_observations"))
                            for delta in _list(row.get("deltas")))
    segments = reconstruct_final_stream(wire)
    stream_match = (bool(delta_text) and delta_text == callback_text
                    and segments["valid"] and segments["final_text"] == final)
    model_bound = bool(result.get("model_inputs")) and all(row in _point_payloads(evidence, "channel_stream_model_request")
        for row in _list(result.get("model_inputs")))
    ownership = task == fixture["task"] and all(frame.get("chat_id") == task.get("chat_id") for frame in wire)
    stage_turns = {event.get("payload", {}).get("turn_id") for event in evidence.get("events", [])
                   if event.get("type") == "stage" and event.get("payload", {}).get("turn_id")}
    wire_turns = {frame.get("turn_id") for frame in wire
                  if frame.get("event") in {"delta", "stream_end", "turn_end"}}
    ownership = ownership and len(wire_turns) == 1 and None not in wire_turns and wire_turns <= stage_turns
    mapping = len(writes) == len(wire) and writes == result.get("adapter_writes") and bus == result.get("channel_manager_outbound")
    for operation, frame in zip(writes, wire, strict=False):
        mapping = mapping and operation.get("chat_id") == frame.get("chat_id")
        if operation.get("operation") == "send_delta":
            mapping = mapping and frame.get("event") == ("stream_end" if operation.get("stream_end") else "delta")
            mapping = mapping and operation.get("stream_id") == frame.get("stream_id")
            mapping = mapping and operation.get("turn_id") == frame.get("turn_id")
            if operation.get("stream_end"):
                mapping = mapping and operation.get("resuming") == frame.get("resuming")
            if not operation.get("stream_end"):
                mapping = mapping and operation.get("delta") == frame.get("text")
        else:
            mapping = mapping and operation.get("content_sha256") == digest(_text(frame.get("text")).encode())
    final_bus = [row for row in bus if row.get("streamed_final")]
    mapping = mapping and len(final_bus) == 1 and final_bus[0].get("content") == final
    tool_phases = {}
    tool_wire_valid = True
    for frame in wire:
        for event in _list(frame.get("tool_events")):
            call_id, phase = event.get("call_id"), event.get("phase")
            if (not call_id or phase not in {"start", "end"} or not frame.get("turn_id")):
                tool_wire_valid = False
                continue
            key = (frame["turn_id"], call_id)
            phases = tool_phases.setdefault(key, {})
            if phase in phases and phases[phase] != event:
                tool_wire_valid = False
            if phase == "end" and "start" not in phases:
                tool_wire_valid = False
            phases[phase] = event
    expected_calls = {row.get("call_id"): row.get("name") for row in reads}
    tool_wire_valid = (tool_wire_valid and bool(tool_phases) and len(tool_phases) == len(expected_calls)
                       and {turn for turn, _ in tool_phases} == wire_turns
                       and all(set(phases) == {"start", "end"}
                               and phases["start"].get("name") == expected_calls.get(call_id)
                               and phases["end"].get("name") == expected_calls.get(call_id)
                               for (_, call_id), phases in tool_phases.items()))
    ui_bound, ui, ui_cases = _ui_binding(case, evidence, result)
    captured = _dict(ui_cases.get("captured_worker_wire_frames"))
    visible = captured.get("visible_answers") == [final] and captured.get("turn_end_callbacks") == 1
    visible = visible and bool(captured.get("visible_trace_rows")) and all(row.get("role") == "tool"
        for row in _list(captured.get("visible_trace_rows")))
    visible = visible and sum(frame.get("event") == "turn_end" for frame in wire) == 1
    visible = visible and not any(frame.get("event") == "message" and frame.get("kind") not in {"progress", "tool_hint"}
                                 for frame in wire)
    visible = visible and not any(marker in final for marker in ('"tool_call_id"', '"arguments"'))
    provider = _dict(result.get("provider"))
    native = (case.get("mode") != "R" or provider.get("native_stream_proven") is True
        and provider.get("sdk_stream_true_for_every_request") is True and bool(provider.get("sdk_transport_requests"))
        and all(row.get("stream") is True and row.get("model") == MODEL for row in provider["sdk_transport_requests"])
        and trace["model_attempts"] > 0 and bool(callback_text))
    answer_ok = final == "结果为73" if case.get("mode") == "D" else bool(re.search(r"(?<!\d)73(?!\d)", final))
    checks = {"stream_source_read": read_bound, "stream_model_binding": model_bound, "stream_transport_mapping": mapping, "stream_ownership": ownership,
              "stream_reconstruction": stream_match, "stream_tool_call_binding": tool_wire_valid,
              "frontend_artifact_binding": ui_bound,
              "frontend_visible_state": visible, "native_stream": native, "stream_answer": answer_ok}
    proof = {"invalid_keys": {"stream_source_read", "stream_model_binding", "frontend_artifact_binding"}, "scope": "full_task", "answer": final}
    control = case.get("channel_control")
    if control:
        selected_name = {"stream_interleaved": "interleaved_stream_ids_negative_control",
                         "stream_duplicate_terminal": "duplicate_terminal_and_final_negative_control",
                         "stream_wrong_chat": "wrong_chat_owner_negative_control"}[control]
        selected = _dict(ui_cases.get(selected_name))
        checks["selected_ui_case_observed"] = bool(selected)
        proof["invalid_keys"].add("selected_ui_case_observed")
        if control == "stream_interleaved":
            checks["selected_ui_contract"] = (selected.get("visible_answers") == ["结果为73", "foreign"])
        elif control == "stream_duplicate_terminal":
            checks["selected_ui_contract"] = (selected.get("visible_answers") == ["结果为73"] and selected.get("turn_end_callbacks") == 1)
        else:
            checks["selected_ui_contract"] = (selected.get("owner_a_messages") == []
                and [row.get("content") for row in _list(selected.get("owner_b_messages")) if row.get("role") == "assistant"] == ["B-only answer"])
        proof.update(scope="diagnostic_ui_replay", selected_ui_case=selected_name)
    return checks, proof


def binding(case, evidence):
    cid = case.get("case_id")
    trial_id = evidence.get("trial_id")
    wrappers = _point_payloads(evidence, "channel_task_result")
    wrapper = wrappers[0] if len(wrappers) == 1 else {}
    result = _dict(wrapper.get("result"))
    trace = analyze_trace(evidence.get("events", []), evidence.get("execution_status"), evidence.get("collector_errors", []))
    checks = {
        "identity": (bool(trial_id) and cid in IDS and case.get("mode") in {"D", "R"} and wrapper.get("case_id") == cid
                     and wrapper.get("mode") == case.get("mode") and wrapper.get("control") == case.get("channel_control")
                     and all(event.get("trial_id") == trial_id for event in evidence.get("events", []))),
        "frozen_case": _source_and_fixture(case), "workspace_unchanged": _workspace(case, evidence),
        "runtime_scope": _runtime(case, evidence), "trace_closed": trace["valid"] and trace["closed"]
            and trace["event_counts"].get("entry", 0) >= 2,
        "execution": evidence.get("execution_status") == "finished" and evidence.get("cleanup", {}).get("ok") is True
            and not evidence.get("collector_errors"),
        "budget": trace["model_attempts"] <= 8 and trace["tool_requested"] <= 16 and trace["known_tokens"] <= 32000
            and sum(event.get("type") == "scripted_response" and event.get("phase") == "start" for event in evidence.get("events", [])) <= 8
            and not any(event.get("type") == "budget" for event in evidence.get("events", [])),
    }
    if "elapsed_ms" in evidence:
        elapsed = evidence["elapsed_ms"]
        checks["wall_budget"] = type(elapsed) in {int, float} and 0 <= elapsed <= (120000 if case.get("mode") == "D" else 240000)
    settlements = _point_payloads(evidence, "settlement")
    checks["settlement"] = bool(settlements) and all(row.get("status") == "settled" and row.get("coverage_complete") is True
        and row.get("background_pending") == row.get("running_subtasks") == 0 for row in settlements)
    if case.get("mode") == "R" and not (cid == "V065" and case.get("channel_control") is None):
        checks["real_model_usage"] = (trace["model_attempts"] > 0 and trace["actual_tokens"] is not None
            and trace["unknown_usage_attempts"] == trace["unknown_sdk_coverage_calls"] == 0
            and not any(event.get("type") == "scripted_response" for event in evidence.get("events", [])))
    invalid_keys = set(checks)
    if cid == "V063":
        specific, proof = _slack(case, evidence, result)
    elif cid == "V065":
        specific, proof = _media(case, evidence, result)
    elif cid == "V066":
        specific, proof = _stream(case, evidence, result, trace)
    else:
        specific, proof = {}, {"invalid_keys": set(), "answer": None, "scope": "full_task"}
    checks.update(specific)
    invalid_keys.update(proof["invalid_keys"])
    deliveries = _point_payloads(evidence, "delivery")
    answer = proof.get("answer")
    if answer is not None:
        checks["delivery_binding"] = len(deliveries) == 1 and deliveries[0].get("response") == answer and deliveries[0].get("observation_only") is False
        invalid_keys.add("delivery_binding")
    elif cid == "V065" and case.get("channel_control") is None:
        checks["no_fabricated_delivery"] = not any(row.get("response") and row.get("observation_only") is False for row in deliveries)
        invalid_keys.add("no_fabricated_delivery")
    status = "invalid" if any(not checks[key] for key in invalid_keys) else proof.get("forced_status") or ("pass" if all(checks.values()) else "product_fail")
    return {"trial_id": trial_id, "evidence_sha256": digest(canonical(evidence)), "status": status,
            "checks": checks, "trace": trace, "answer": answer, "scope": proof["scope"],
            "case_id": cid, "control": case.get("channel_control"), "reason_code": proof.get("reason_code"),
            "guard_passed": proof.get("guard_passed"), "selected_ui_case": proof.get("selected_ui_case"),
            "completion_met": status == "pass" and proof["scope"] == "full_task"}


def score(case, evidence):
    try:
        proof = binding(case, evidence)
    except (AttributeError, KeyError, TypeError, ValueError, IndexError) as error:
        return {"status": "invalid", "scope": "full_task", "trial_id": evidence.get("trial_id"),
                "scorer_version": SCORER_VERSION, "formal_quality_eligible": False,
                "requirements": [{"requirement_id": "channel.malformed_observation", "label": "fail", "method": "program"}],
                "reasons": [{"reason_code": "malformed_observation", "error_kind": type(error).__name__}],
                "extensions": {"completion_met": False, "program_only": True}}
    reasons = [{"reason_code": key} for key, value in proof["checks"].items() if not value]
    if proof.get("reason_code"):
        reasons.append({"reason_code": proof["reason_code"]})
    return {"status": proof["status"], "scope": proof["scope"], "trial_id": evidence.get("trial_id"),
            "scorer_version": SCORER_VERSION, "formal_quality_eligible": False,
            "requirements": [{"requirement_id": "channel." + key, "label": "pass" if value else "fail", "method": "program"}
                             for key, value in proof["checks"].items()], "reasons": reasons,
            "extensions": {"completion_met": proof["completion_met"], "program_only": True, "binding": proof}}


def scorer_main(connection, case, trial_dir):
    try:
        result = score(case, read(Path(trial_dir) / "artifacts/evidence.json"))
        write(Path(trial_dir) / "verdicts/1.json", result)
        connection.send({"ok": True, "verdict": result})
    except Exception as error:
        connection.send({"ok": False, "error_kind": type(error).__name__})
    finally:
        connection.close()

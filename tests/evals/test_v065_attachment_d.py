"""V065 reviewed D contract: real local upload, dispatch, and isolation."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from evals.v065_attachment_d import run, score


def _turn(evidence, actor):
    return next(row for row in evidence["turns"] if row["actor"] == actor)


def _provider_call(evidence, actor, index=-1):
    rows = [row for row in evidence["provider"]["calls"] if row["actor"] == actor]
    return rows[index]


def _wire_frame(evidence, actor, frame):
    return next(
        row for row in evidence["wire"]
        if row["actor"] == actor
        and row["direction"] == "server_to_client"
        and row["frame"].get("message_id") == frame.get("message_id")
    )


def _replace_final(evidence, actor, text):
    turn = _turn(evidence, actor)
    turn["final_text"] = text
    final_frame = next(
        frame for frame in turn["frames"]
        if frame.get("event") == "message"
        and str(frame.get("message_id", "")).endswith(":final")
    )
    final_frame["text"] = text
    for outbound in turn["outbound"]:
        if outbound.get("content") == _provider_call(evidence, actor)["response"].get("content"):
            outbound["content"] = text
    call = _provider_call(evidence, actor)
    call["response"]["content"] = text
    wire = _wire_frame(evidence, actor, final_frame)
    wire["frame"] = copy.deepcopy(final_frame)
    wire["raw_sha256"] = hashlib.sha256(
        json.dumps(final_frame, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    wire["frame_sha256"] = hashlib.sha256(json.dumps(
        final_frame, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _copy_evidence(d_run):
    return copy.deepcopy(d_run[0])


def _append_server_frame(evidence, actor, purpose, request_id, frame):
    raw = json.dumps(frame, ensure_ascii=False)
    canonical = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence["wire"].append({
        "actor": actor,
        "direction": "server_to_client",
        "purpose": purpose,
        "request_id": request_id,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "frame_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "frame": copy.deepcopy(frame),
    })


@pytest.fixture(scope="module")
def d_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("v065-d") / uuid.uuid4().hex
    root.mkdir()
    output = root / "evidence"
    evidence = asyncio.run(run(output, mode="D"))
    return evidence, output


def test_real_d_run_passes_with_persisted_evidence(d_run):
    evidence, output = d_run
    verdict = score(evidence)

    assert verdict["status"] == "pass", verdict["reasons"]
    assert (output / "evidence.json").is_file()
    assert (output / "verdict.json").is_file()


def test_real_d_run_uses_the_original_text_fixture_and_questions(d_run):
    evidence, _ = d_run

    assert evidence["fixture"]["name"] == "notice.txt"
    assert evidence["fixture"]["mime"] == "text/plain"
    assert evidence["fixture"]["text"] == "会议日期2026-10-12"
    assert _turn(evidence, "A")["content"] == "我上传文件中的会议日期是什么？"
    assert _turn(evidence, "B")["content"] == "我刚上传文件的会议日期是什么？"
    assert evidence["provider"]["model_requested"] == "deepseek-v4-flash"


def test_mode_r_is_rejected_before_output_allocation(tmp_path):
    output = tmp_path / uuid.uuid4().hex / "r-output"
    assert not output.exists()

    with pytest.raises(ValueError, match="unsupported_mode"):
        asyncio.run(run(output, mode="R"))

    assert not output.exists()


@pytest.mark.parametrize("actor", ["A", "B"])
def test_turns_are_real_dispatches_with_one_matching_final_and_terminal(d_run, actor):
    evidence, _ = d_run
    turn = _turn(evidence, actor)

    assert turn["dispatch_started"] is True
    assert turn["dispatch_completed"] is True
    assert turn["outbound_queue_empty"] is True
    assert turn["wire_complete"] is True
    assert turn["final_count"] == 1
    assert turn["turn_end_count"] == 1
    assert turn["turn_id"] == turn["turn_end_turn_id"]


def test_text_upload_is_saved_in_a_scope_and_read_result_reaches_next_call(d_run):
    evidence, _ = d_run
    actors = evidence["actors"]
    upload = evidence["upload"]
    reads = evidence["reads"]
    a_reads = [row for row in reads if row.get("actor") == "A" and row.get("status") == "ok"]
    a_calls = [row for row in evidence["provider"]["calls"] if row.get("actor") == "A"]

    assert actors["A"]["chat_id"] != actors["B"]["chat_id"]
    assert actors["A"]["ready"]["event"] == actors["B"]["ready"]["event"] == "ready"
    assert upload["mime"] == "text/plain"
    assert upload["filename"] == "notice.txt"
    assert upload["request_frame"]["type"] == "message"
    assert upload["inbound"]["chat_id"] == actors["A"]["chat_id"]
    assert upload["decoded_bytes_sha256"] == evidence["fixture"]["bytes_sha256"]
    assert upload["saved_under_A_chat"] is True
    assert Path(upload["saved_path"]).is_relative_to(Path(evidence["artifact_root"]) / "runtime" / "media")
    assert evidence["file_integrity"]["unchanged"] is True
    assert a_reads and any(
        row["contains_notice"] and "会议日期2026-10-12" in row["return_text"]
        for row in a_reads
    )
    assert len(a_calls) == 2
    assert "会议日期2026-10-12" in str(a_calls[0]["messages"])
    assert "会议日期2026-10-12" in str(a_calls[1]["messages"])


def test_b_input_and_answer_do_not_borrow_a_attachment(d_run):
    evidence, _ = d_run
    turn_b = _turn(evidence, "B")
    b_inputs = [row for row in evidence["provider"]["calls"] if row.get("actor") == "B"]

    assert turn_b["content"] == "我刚上传文件的会议日期是什么？"
    assert turn_b["media"] == []
    assert len(b_inputs) == 1
    assert all(
        "会议日期2026-10-12" not in str(row["messages"])
        and "2026-10-12" not in str(row["messages"])
        and evidence["upload"]["saved_path"] not in str(row["messages"])
        for row in b_inputs
    )
    assert any(phrase in turn_b["final_text"] for phrase in ("没有收到附件", "未收到附件", "没收到附件"))
    assert "2026-10-12" not in turn_b["final_text"]


def test_b_absolute_relative_and_parent_path_reads_are_denied_without_open(d_run):
    evidence, _ = d_run
    rows = {row.get("probe"): row for row in evidence["reads"] if row.get("probe")}

    assert set(rows) >= {"absolute_file", "relative_file", "chat_parent_directory"}
    assert all(rows[name]["actor"] == "B" for name in rows)
    assert all(rows[name]["status"] == "denied" for name in rows)
    assert all(rows[name]["audit_open_events"] == [] for name in rows)
    assert evidence["isolation"]["boundary_probe_media_unchanged"] is True


def test_foreign_attach_upload_spoof_and_valid_capability_boundaries(d_run):
    evidence, _ = d_run
    isolation = evidence["isolation"]
    probes = {row["name"]: row for row in isolation["probes"]}

    assert set(probes) >= {
        "B_attach_A", "B_wrong_token_attach_A",
        "B_foreign_message_upload", "B_wrong_token_message_upload",
    }
    assert all(row["reply"]["detail"] == "chat_unauthorized" for row in probes.values())
    assert all(row["inbound_queue_empty"] for name, row in probes.items() if "attach" in name)
    assert all(row["media_files_unchanged"] for name, row in probes.items() if "upload" in name)
    assert all(row["inbound_queue_size"] == 0 for name, row in probes.items() if "upload" in name)
    assert isolation["unauthorized_message_bus_unchanged"] is True
    assert isolation["spoof_client_id"]["same_chat_as_A"] is False
    assert isolation["spoof_client_id"]["attach_reply"]["detail"] == "chat_unauthorized"
    assert isolation["spoof_client_id"]["inbound_queue_empty"] is True
    assert isolation["A_only_delivery"]["B_received"] is None
    assert isolation["valid_capability"]["ready_chat_id"] != evidence["actors"]["A"]["chat_id"]
    assert isolation["valid_capability"]["attached_reply"]["event"] == "attached"
    assert isolation["valid_capability"]["inbound"]["chat_id"] == evidence["actors"]["A"]["chat_id"]
    assert isolation["valid_capability"]["delivery"]["text"] == "V065_CAPABILITY_DELIVERY_CONTROL"


def test_d_control_uses_no_external_model_and_all_resources_settle(d_run):
    evidence, _ = d_run
    provider = evidence["provider"]
    cleanup = evidence["cleanup"]

    assert provider["implementation"] == "grounded_scripted"
    assert provider["chat_calls"] == len(provider["calls"]) == 3
    assert provider["llm_calls"] == provider["sdk_calls"] == 0
    assert cleanup["provider_closed"] is True
    assert cleanup["channel_stopped"] is True
    assert cleanup["server_task_done"] is True
    assert cleanup["connections_closed"] is True
    assert cleanup["pending_tasks_settled"] is True
    assert cleanup["config_pointer_restored"] is True


def test_wrong_a_answer_is_product_failure_when_provider_and_wire_agree(d_run):
    evidence = _copy_evidence(d_run)
    _replace_final(evidence, "A", "文件中的会议日期是2026-10-13。")

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]


def test_borrowed_date_is_product_failure_when_actual_b_context_and_wire_agree(d_run):
    evidence = _copy_evidence(d_run)
    call = _provider_call(evidence, "B")
    call["messages"].append({
        "role": "system",
        "content": "会议日期2026-10-12 " + evidence["upload"]["saved_path"],
    })
    _replace_final(evidence, "B", "你上传文件中的日期是2026-10-12。")

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]


def test_changed_stored_file_is_product_failure_with_complete_digest_observation(d_run):
    evidence = _copy_evidence(d_run)
    integrity = evidence["file_integrity"]
    integrity["stored_sha256_after"] = "0" * 64
    integrity["stored_size_after"] = integrity["stored_size_before"] + 1
    integrity["unchanged"] = False

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]


@pytest.mark.parametrize("field", ["actor", "session_key", "request_id"])
def test_actor_session_or_request_mismatch_is_invalid(d_run, field):
    evidence = _copy_evidence(d_run)
    turn_b = _turn(evidence, "B")
    if field == "actor":
        turn_b["actor"] = "A"
    elif field == "session_key":
        turn_b["session_key"] = _turn(evidence, "A")["session_key"]
    else:
        turn_b["request_id"] = _turn(evidence, "A")["request_id"]

    assert score(evidence)["status"] == "invalid"


def test_missing_raw_wire_and_incomplete_wire_are_invalid(d_run):
    missing = _copy_evidence(d_run)
    missing["wire"] = []
    incomplete = _copy_evidence(d_run)
    final = next(
        row for row in incomplete["wire"]
        if row["actor"] == "A"
        and row["direction"] == "server_to_client"
        and row["frame"].get("event") == "message"
        and str(row["frame"].get("message_id", "")).endswith(":final")
    )
    incomplete["wire"].remove(final)

    missing_verdict = score(missing)
    incomplete_verdict = score(incomplete)
    assert missing_verdict["status"] == "invalid", missing_verdict["reasons"]
    assert incomplete_verdict["status"] == "invalid", incomplete_verdict["reasons"]


def test_missing_provider_result_is_invalid(d_run):
    evidence = _copy_evidence(d_run)
    evidence["provider"]["calls"].pop()

    assert score(evidence)["status"] == "invalid"


def test_source_identity_tampering_is_invalid(d_run):
    evidence = _copy_evidence(d_run)
    evidence["source"]["contract_sha256"] = "0" * 64

    assert score(evidence)["status"] == "invalid"


def test_duplicate_negative_authorization_row_is_invalid(d_run):
    evidence = _copy_evidence(d_run)
    evidence["isolation"]["probes"].append(copy.deepcopy(evidence["isolation"]["probes"][0]))

    assert score(evidence)["status"] == "invalid"


@pytest.mark.parametrize("mutation", ["missing", "wrong"])
def test_readonly_return_hash_missing_or_wrong_is_invalid(d_run, mutation):
    evidence = _copy_evidence(d_run)
    read = next(row for row in evidence["reads"] if row.get("actor") == "A" and row.get("status") == "ok")
    if mutation == "missing":
        read.pop("return_text_sha256")
    else:
        read["return_text_sha256"] = "0" * 64

    assert score(evidence)["status"] == "invalid"


def test_empty_turn_frames_are_invalid_even_when_other_ledgers_remain(d_run):
    evidence = _copy_evidence(d_run)
    _turn(evidence, "B")["frames"] = []

    assert score(evidence)["status"] == "invalid"


def test_missing_terminal_after_complete_wire_window_is_product_failure(d_run):
    evidence = _copy_evidence(d_run)
    turn = _turn(evidence, "B")
    terminal = next(frame for frame in turn["frames"] if frame.get("event") == "turn_end")
    turn["frames"].remove(terminal)
    turn["turn_end_count"] = 0
    turn["turn_end_turn_id"] = None
    turn["outbound"] = [
        row for row in turn["outbound"] if row.get("metadata", {}).get("_turn_end") is not True
    ]
    evidence["wire"] = [
        row for row in evidence["wire"]
        if not (
            row["actor"] == "B"
            and row["direction"] == "server_to_client"
            and row["frame"].get("event") == "turn_end"
        )
    ]

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]


@pytest.mark.parametrize("duplicate_event", ["final", "turn_end"])
def test_late_duplicate_turn_frame_in_complete_window_is_product_failure(d_run, duplicate_event):
    evidence = _copy_evidence(d_run)
    turn = _turn(evidence, "B")
    if duplicate_event == "final":
        frame = copy.deepcopy(next(
            item for item in turn["frames"]
            if item.get("event") == "message" and str(item.get("message_id", "")).endswith(":final")
        ))
        turn["final_count"] += 1
    else:
        frame = copy.deepcopy(next(item for item in turn["frames"] if item.get("event") == "turn_end"))
        turn["turn_end_count"] += 1
    turn["frames"].append(frame)
    turn["terminal_observation"] = {
        "complete": True,
        "quiet": False,
        "quiet_window_ms": 200,
        "unexpected_frames": [copy.deepcopy(frame)],
    }
    _append_server_frame(
        evidence,
        "B",
        f"{turn['request_id']}:terminal_window",
        turn["request_id"],
        frame,
    )

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]


def test_a_only_sentinel_received_by_b_is_product_failure_with_raw_wire(d_run):
    evidence = _copy_evidence(d_run)
    leaked = {
        "event": "message",
        "chat_id": evidence["actors"]["B"]["chat_id"],
        "text": "V065_A_ONLY_SENTINEL",
    }
    delivery = evidence["isolation"]["A_only_delivery"]
    delivery["B_received"] = leaked
    delivery["B_receive_observation"] = {
        "complete": True,
        "timed_out": False,
        "window_ms": 200,
    }
    _append_server_frame(evidence, "B", "unexpected_after_A_sentinel", None, leaked)

    verdict = score(evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]

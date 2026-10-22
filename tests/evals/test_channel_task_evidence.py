"""Real D baselines and labelled observer-tamper controls; no paid models."""

from __future__ import annotations

import asyncio
import copy

import pytest

from evals.channel_task_cases import IDS, METHOD, VERSION, load_cases
from evals.channel_task_evidence import delivery_text, score
from evals.common import snapshot
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, point, span


class Connection:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def _run(root, cid, control=None, provider=None):
    case = next(case for case in load_cases() if case["case_id"] == cid)
    case.update(mode="D", channel_control=control)
    runtime, workspace = root / "runtime", root / "workspace"
    runtime.mkdir(parents=True)
    workspace.mkdir()
    for name, text in case["channel_fixture"]["files"].items():
        (workspace / name).write_bytes(text.encode())
    before, runtime_before = snapshot(workspace, contents=True), snapshot(runtime)
    connection = Connection()
    emit = Emitter(connection, "channel-evidence-D", cid + "-unit-D")

    async def execute():
        if cid == "V063":
            from evals.channel_task_slack import run_slack
            return await run_slack(runtime, workspace, provider, emit, control)
        if cid == "V065":
            from evals.channel_task_media import run_media
            return await run_media(runtime, workspace, None, emit, control)
        from evals.channel_task_stream import asyncrun_stream, fixed_stream_provider
        return await asyncrun_stream(runtime, workspace, fixed_stream_provider(), emit)

    with observation_context(emit.event, emit.trial_id):
        with span("task", case_id=cid):
            with span("entry", entry=case["entry"]["method"]):
                result = asyncio.run(execute())
                point("channel_task_result", case_id=cid, mode="D", control=control, result=result)
                answer = delivery_text(cid, result)
                if answer is not None:
                    point("delivery", response=answer, observation_only=False, actual_business_send=False)
                point("settlement", status="settled", coverage_complete=True,
                      background_pending=0, running_subtasks=0)
    evidence = {"trial_id": emit.trial_id, "execution_status": "finished",
                "events": [row["event"] for row in connection.messages if row.get("message") == "event"],
                "before": before, "after": snapshot(workspace, contents=True),
                "runtime_before": runtime_before, "runtime_after": snapshot(runtime),
                "runtime_violations": [], "collector_errors": [], "cleanup": {"ok": True}}
    return case, evidence


@pytest.fixture(scope="module")
def baselines(tmp_path_factory):
    return {cid: _run(tmp_path_factory.mktemp("channel-evidence-" + cid), cid) for cid in IDS}


def _copy(baselines, cid):
    case, evidence = baselines[cid]
    return copy.deepcopy(case), copy.deepcopy(evidence)


def _result(evidence):
    return next(event["payload"]["result"] for event in evidence["events"] if event["type"] == "channel_task_result")


def test_case_contract_frozen_inputs_budgets_and_formal_qualification():
    cases = load_cases()
    assert [case["case_id"] for case in cases] == list(IDS)
    for case in cases:
        assert case["case_version"] == VERSION
        assert case["scoring"]["method"] == METHOD
        assert case["budget"]["attempts"] == 8
        assert case["budget"]["tools"] == 16
        assert case["budget"]["tokens"] == 32000
        assert case["scoring"]["formal_quality_eligible"] is False


@pytest.mark.parametrize("cid,status", [("V063", "pass"), ("V065", "product_fail"), ("V066", "pass")])
def test_actual_local_d_task_score(baselines, cid, status):
    verdict = score(*baselines[cid])
    assert verdict["status"] == status, verdict["reasons"]
    assert verdict["formal_quality_eligible"] is False
    assert verdict["extensions"]["binding"]["trace"]["model_attempts"] == 0


def test_media_r_precheck_is_undetermined_with_actual_upload_and_zero_paid_requests(baselines):
    case, evidence = _copy(baselines, "V065")
    case["mode"] = "R"
    for event in evidence["events"]:
        if event["type"] == "channel_task_result":
            event["payload"]["mode"] = "R"
    verdict = score(case, evidence)
    assert verdict["status"] == "undetermined"
    assert {row["reason_code"] for row in verdict["reasons"]} >= {"blocked_precondition"}
    assert verdict["extensions"]["completion_met"] is False
    assert verdict["formal_quality_eligible"] is False
    assert _result(evidence)["attempts"]["paid_requests"] == 0


@pytest.mark.parametrize("cid", IDS)
@pytest.mark.parametrize("tamper", ["cross_trial", "source", "case_fixture", "workspace", "budget", "runtime_scope", "missing_result", "malformed"])
def test_observer_tamper_never_passes(baselines, cid, tamper):
    case, evidence = _copy(baselines, cid)
    if tamper == "cross_trial":
        evidence["events"][0]["trial_id"] = "other-trial"
    elif tamper == "source":
        case["source_sha256"] = "0" * 64
    elif tamper == "case_fixture":
        case["channel_fixture"]["fixture_sha256"] = "0" * 64
    elif tamper == "workspace":
        evidence["after"]["unregistered.txt"] = {"sha256": "0" * 64, "text": "changed"}
    elif tamper == "budget":
        event = copy.deepcopy(evidence["events"][-1])
        event.update(event_id="budget-control", type="budget", phase="point", payload={"reason": "exhausted"})
        evidence["events"].append(event)
    elif tamper == "runtime_scope":
        evidence["runtime_after"]["other/arbitrary.log"] = {"sha256": "0" * 64}
        case["file_policy"]["runtime_allowed_prefixes"] = [""]
    elif tamper == "missing_result":
        event = next(event for event in evidence["events"] if event["type"] == "channel_task_result")
        event["type"] = "irrelevant_observer_event"
    else:
        _result(evidence)["model_inputs"] = ["invalid-row"]
    verdict = score(case, evidence)
    assert verdict["status"] == "invalid"
    assert verdict["formal_quality_eligible"] is False
    assert verdict["extensions"]["completion_met"] is False


@pytest.mark.parametrize("cid", ["V063", "V066"])
def test_delivery_receipt_tampering_is_invalid(baselines, cid):
    case, evidence = _copy(baselines, cid)
    next(event for event in evidence["events"] if event["type"] == "delivery")["payload"]["response"] = "invented"
    assert score(case, evidence)["status"] == "invalid"


def test_slack_wrong_model_context_is_detected_in_full_messages(baselines):
    case, evidence = _copy(baselines, "V063")
    result = _result(evidence)
    result["model_inputs"][2]["messages"].append({"role": "user", "content": "本线程代号A17"})
    # It is a labelled observer copy. Match its raw point to test the semantic
    # program gate independently of the separate raw-ledger integrity gate.
    next(event for event in evidence["events"] if event["type"] == "slack_model_input"
         and event["payload"]["turn_id"] == "ask_B")["payload"] = copy.deepcopy(result["model_inputs"][2])
    verdict = score(case, evidence)
    assert verdict["status"] == "product_fail"
    assert {row["reason_code"] for row in verdict["reasons"]} >= {"agent_four_turns"}


@pytest.mark.parametrize("control,expected", [("stream_interleaved", "pass"),
                                              ("stream_duplicate_terminal", "pass"),
                                              ("stream_wrong_chat", "pass")])
def test_selected_actual_ui_static_controls_are_separate_from_agent_tasks(baselines, control, expected):
    case, evidence = _copy(baselines, "V066")
    case["channel_control"] = control
    next(event for event in evidence["events"] if event["type"] == "channel_task_result")["payload"]["control"] = control
    verdict = score(case, evidence)
    assert verdict["status"] == expected, verdict["reasons"]
    assert verdict["scope"] == "diagnostic_ui_replay"
    assert verdict["extensions"]["completion_met"] is False
    assert verdict["formal_quality_eligible"] is False


def test_final_reconstruction_preserves_progress_and_requires_explicit_semantics():
    from evals.channel_task_evidence import reconstruct_final_stream

    frames = [
        {"event": "delta", "turn_id": "t", "stream_id": "a", "text": "reading"},
        {"event": "stream_end", "turn_id": "t", "stream_id": "a", "resuming": True},
        {"event": "delta", "turn_id": "t", "stream_id": "b", "text": "73"},
        {"event": "stream_end", "turn_id": "t", "stream_id": "b", "resuming": False},
    ]
    result = reconstruct_final_stream(frames)
    assert result["valid"] and result["final_text"] == "73"
    assert result["segments"][0]["text"] == "reading"
    assert reconstruct_final_stream(frames + [frames[-1]])["valid"]
    missing = copy.deepcopy(frames)
    del missing[1]["resuming"]
    assert not reconstruct_final_stream(missing)["valid"]
    wrong_turn = copy.deepcopy(frames)
    wrong_turn[-1]["turn_id"] = "other"
    assert not reconstruct_final_stream(wrong_turn)["valid"]
    assert not reconstruct_final_stream(frames + [frames[0]])["valid"]
    conflicting = {**frames[-1], "resuming": True}
    assert not reconstruct_final_stream(frames + [conflicting])["valid"]


@pytest.mark.parametrize("tamper", ["frontend_pin", "evidence_json", "fake_ui", "source_read", "delta_drop"])
def test_stream_source_and_frontend_artifacts_cannot_be_replaced(baselines, tamper):
    case, evidence = _copy(baselines, "V066")
    result = _result(evidence)
    ui = result["render_contract"]["ui_replay"]
    if tamper == "frontend_pin":
        case["frontend_source_hashes"]["webui/src/lib/types.ts"] = "0" * 64
    elif tamper == "evidence_json":
        ui["evidence_text"] = "{}"
    elif tamper == "fake_ui":
        ui["captured_worker_render"]["visible_answers"] = ["fabricated frontend pass"]
    elif tamper == "source_read":
        result["tool_reads"][0]["source_sha256"] = "0" * 64
    else:
        next(frame for frame in result["wire_frames"] if frame["event"] == "delta")["text"] = ""
    verdict = score(case, evidence)
    assert verdict["status"] == "invalid"
    assert verdict["formal_quality_eligible"] is False


@pytest.mark.parametrize("control", ["borrow_path", "borrow_signed_url", "borrow_chat"])
def test_real_media_cross_session_success_is_a_hard_product_failure(tmp_path, control):
    case, evidence = _run(tmp_path / control, "V065", control)
    verdict = score(case, evidence)
    assert verdict["status"] == "product_fail", verdict["reasons"]
    assert verdict["scope"] == "diagnostic_control"
    assert verdict["formal_quality_eligible"] is False


def test_scripted_channel_task_cannot_claim_real_r_qualification(baselines):
    case, evidence = _copy(baselines, "V063")
    case["mode"] = "R"
    next(event for event in evidence["events"] if event["type"] == "channel_task_result")["payload"]["mode"] = "R"
    verdict = score(case, evidence)
    assert verdict["status"] == "invalid"
    assert {row["reason_code"] for row in verdict["reasons"]} >= {"real_model_usage"}


def test_real_retry_has_five_model_calls_and_keeps_extra_sdk_progress(tmp_path):
    from tests.evals.test_channel_task_slack import HistoryProvider

    class RetryProvider(HistoryProvider):
        _CHAT_RETRY_DELAYS = (0.001,)

        async def chat(self, messages, **kwargs):
            if not self.inputs:
                self.inputs.append(copy.deepcopy(messages))
                raise RuntimeError("503 unavailable (synthetic observer D)")
            return await super().chat(messages, **kwargs)

    case, evidence = _run(tmp_path / "actual-retry", "V063", provider=RetryProvider())
    result = _result(evidence)
    assert len(result["model_inputs"]) == 5
    assert len(result["deliveries"]) > 4
    assert any(row["metadata"].get("_retry_wait") is True for row in result["deliveries"])
    verdict = score(case, evidence)
    assert verdict["status"] == "pass", verdict["reasons"]
    assert verdict["extensions"]["binding"]["trace"]["model_attempts"] == 0
    assert verdict["formal_quality_eligible"] is False


def test_sdk_notification_without_actual_metadata_is_never_ignored(baselines):
    case, evidence = _copy(baselines, "V063")
    _result(evidence)["deliveries"][0].pop("metadata")
    verdict = score(case, evidence)
    assert verdict["status"] == "invalid"
    assert {row["reason_code"] for row in verdict["reasons"]} >= {"malformed_observation"}


def test_actual_ui_read_receipt_requires_matching_parent_disk_hash(baselines):
    case, evidence = _copy(baselines, "V066")
    path = _result(evidence)["render_contract"]["ui_replay"]["evidence_path"]
    evidence["runtime_after"][path]["sha256"] = "0" * 64
    verdict = score(case, evidence)
    assert verdict["status"] == "invalid"
    assert {row["reason_code"] for row in verdict["reasons"]} >= {"frontend_artifact_binding"}


def test_paid_and_ui_wall_budget_cannot_be_extended_by_case_metadata(baselines):
    case, evidence = _copy(baselines, "V066")
    evidence["elapsed_ms"] = 120001
    case["budget"]["product_s"] = 100000
    assert score(case, evidence)["status"] == "invalid"

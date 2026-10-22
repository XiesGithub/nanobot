"""Frozen V063/V065/V066 original-requirement channel task contracts."""

from __future__ import annotations

import copy

from evals.common import MODEL, ROOT, canonical, digest, read

VERSION = METHOD = "channel-full-task/1.0"
IDS = ("V063", "V065", "V066")
SOURCE = "docs/validation-cases/08-transport.md"
MODEL_PROFILE = {"requested_id": MODEL, "temperature": 0, "max_tokens": 2048,
                 "reasoning_effort": None, "thinking": "disabled", "sdk_retries": 0,
                 "provider_retry": "standard [1,2,4] bounded by watchdog/attempt cap"}
CONTROL_BY_CASE = {
    "V063": frozenset({"wrong_answer", "cross_session", "wrong_reply_thread", "illegal_event"}),
    "V065": frozenset({"image_ingress", "borrow_path", "borrow_signed_url", "borrow_chat", "api_text_control", "raw_post"}),
    "V066": frozenset({"stream_interleaved", "stream_duplicate_terminal", "stream_wrong_chat"}),
}
CONTROLS = frozenset().union(*CONTROL_BY_CASE.values())
RUNTIME_PREFIXES = {"V063": ["sessions/"],
                    "V065": ["sessions/", "media/websocket/", "media/api/"],
                    "V066": ["sessions/", "channel-task-stream-ui-input.json",
                             "channel-task-stream-ui-observation.json"]}
DATA = {
    "V063": {"files": {}, "channel": "C063LOCAL", "user": "U063LOCAL",
             "threads": {"A": "1700000000.000100", "B": "1700000000.000200"},
             "turns": [
                 {"turn_id": "seed_A", "thread": "A", "timestamp": "1700000000.000110", "input": "本线程代号A17"},
                 {"turn_id": "seed_B", "thread": "B", "timestamp": "1700000000.000210", "input": "本线程代号B23"},
                 {"turn_id": "ask_B", "thread": "B", "timestamp": "1700000000.000220", "input": "本线程代号是什么？"},
                 {"turn_id": "ask_A", "thread": "A", "timestamp": "1700000000.000120", "input": "本线程代号是什么？"},
             ], "memory": "empty", "session_policy": "independent_threads", "platform_transport": "local_Slack_SDK_HTTP"},
    "V065": {"files": {"notice.txt": "会议日期2026-10-12"},
             "questions": {"A": "我上传文件中的会议日期是什么？", "B": "我刚上传文件的会议日期是什么？"},
             "upload": {"actor": "A", "desired_filename": "notice.txt", "mime": "text/plain"},
             "sessions": "same_instance_two_server_generated_websocket_chats", "B_media": []},
    "V066": {"files": {"fact.txt": "值：73"}, "input": "读取fact.txt告诉我值。",
             "D_deltas": ["结果为", "73"], "D_final": "结果为73", "R_answer_value": "73",
             "task": {"task_id": "v066-task", "owner_id": "v066-owner", "chat_id": "v066-owner-chat"},
             "renderer": "actual_useNanobotStream_via_NanobotClient_jsdom", "native_stream_required_for_R": True},
}
FRONTEND_SOURCES = (
    "webui/src/hooks/useNanobotStream.ts", "webui/src/lib/nanobot-client.ts",
    "webui/src/lib/types.ts", "webui/src/providers/ClientProvider.tsx",
    "webui/src/tests/channel-task-stream-eval.test.tsx",
)


def fixture_hash(case_id=None):
    return digest(canonical(DATA[case_id] if case_id else DATA))


def frontend_hashes():
    return {path: digest((ROOT / path).read_bytes()) for path in FRONTEND_SOURCES}


def load_cases():
    source_hash = digest((ROOT / SOURCE).read_bytes())
    source_text = (ROOT / SOURCE).read_text(encoding="utf-8")
    result = []
    for cid in IDS:
        case = copy.deepcopy(read(ROOT / "evals/cases/V004.json"))
        fixture = copy.deepcopy(DATA[cid])
        fixture["fixture_sha256"] = fixture_hash(cid)
        if cid == "V063":
            turns = [{**turn, "trigger": "start"} for turn in fixture["turns"]]
            tools, entry = [], "SlackChannel._on_socket_request→AgentLoop._dispatch→AsyncWebClient.chat_postMessage"
        elif cid == "V065":
            turns = [{"turn_id": role, "input": question, "trigger": "start"}
                     for role, question in fixture["questions"].items()]
            tools, entry = ["read_file"], "WebSocketChannel media message→AgentLoop._dispatch"
        else:
            turns = [{"turn_id": "t1", "input": fixture["input"], "trigger": "start"}]
            tools, entry = ["read_file"], "AgentLoop._dispatch→ChannelManager→WebSocketChannel→useNanobotStream"
        if any(turn["input"] not in source_text for turn in turns):
            raise ValueError("channel_input_not_in_original_requirement")
        case.update(case_id=cid, case_version=VERSION, title=cid + " 渠道完整任务",
                    source_ref=SOURCE + "#" + cid.lower(), source_sha256=source_hash,
                    channel_fixture=fixture, channel_fixture_hash=fixture_hash(cid), channel_control=None,
                    tags=["channel", "full_task", "independent_program"], turns=turns, tools=tools,
                    environment_profile=VERSION, model_profile="R-" + VERSION,
                    scoring={"version": VERSION, "method": METHOD, "formal_quality_eligible": False,
                             "qualification": "derived_by_execution_and_independent_audit"})
        case["entry"].update(adapter="evals.channel_task_worker", method=entry)
        case["initial_state"] = {"session": "empty", "fixtures": [],
                                 "services": ["isolated_local_channel_transport"], "fault_plan": []}
        case["file_policy"] = {"business_read": ["workspace/"], "business_write": [],
                               "runtime": [{"path": "config.json", "operations": ["read"]}],
                               "runtime_allowed_prefixes": copy.deepcopy(RUNTIME_PREFIXES[cid])}
        case["requirements"] = [{"id": "complete_original_task", "source_clause": "需要验证的输出",
                                 "required": True, "kind": "P", "predicate": "event_exists",
                                 "expected": {"case_id": cid, "program_contract": VERSION},
                                 "evidence_required": ["channel_task_result", "delivery", "settlement"]}]
        case["forbidden"] = [{"action": "cross_session_delivery_or_input_modification",
                              "detector": "channel_independent_program_gates", "evidence": "channel_task_result",
                              "attempt": True, "actual": True}]
        case["completion_rule"] = {"all_of": ["entry.end", "channel_task_result", "settlement"], "turn": turns[-1]["turn_id"]}
        case["budget"].update(profile="R-" + VERSION, product_s=240, attempts=8, tools=16, tokens=32000)
        case["observation"]["required"] = ["task", "entry", "channel_task_result", "settlement", "snapshot"]
        if cid == "V066":
            case["frontend_source_hashes"] = frontend_hashes()
        result.append(case)
    return result

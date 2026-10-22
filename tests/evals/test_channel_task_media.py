"""V065 real local transport contracts; no paid provider or fabricated upload.

The adapter is added after the currently frozen privacy runs. Diagnostic
controls demonstrate observer coverage and do not count as V065 task passes.
"""

from __future__ import annotations

import asyncio
import importlib
import uuid
from pathlib import Path

import pytest

from evals.common import digest
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.observability import observation_context, span

NOTICE = "会议日期2026-10-12"
ORIGINAL_QUESTIONS = {
    "A": "我上传文件中的会议日期是什么？",
    "B": "我刚上传文件的会议日期是什么？",
}


class Connection:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class MustNotCallProvider(LLMProvider):
    """A local assertion boundary; this never instantiates an SDK client."""

    observation_mode = "real"

    def __init__(self):
        super().__init__()
        self.calls = 0

    def get_default_model(self):
        return "deepseek-v4-flash"

    async def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls += 1
        raise AssertionError("A rejected V065 fixture must not reach a provider")


def _adapter():
    return importlib.import_module("evals.channel_task_media")


def _execute(root: Path, control=None, provider=None):
    runtime, workspace = root / "runtime", root / "workspace"
    runtime.mkdir(parents=True)
    workspace.mkdir()
    connection = Connection()
    emitter = Emitter(connection, "channel-D-run", "channel-D-" + str(control or "faithful"))
    with observation_context(emitter.event, emitter.trial_id):
        with span("task", role="foreground", case_id="V065", control=control):
            result = asyncio.run(_adapter().run_media(runtime, workspace, provider, emitter, control=control))
    events = [message["event"] for message in connection.messages if message.get("message") == "event"]
    return {"result": result, "events": events, "runtime": runtime, "workspace": workspace}


@pytest.fixture(scope="module")
def faithful(tmp_path_factory):
    return _execute(tmp_path_factory.mktemp("channel-faithful"))


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return _execute(tmp_path_factory.mktemp("channel-image"), "image_ingress")


def _boundary(result, name):
    rows = [row for row in result["boundary_results"] if row["name"] == name]
    assert len(rows) == 1
    return rows[0]


def test_faithful_notice_upload_records_actual_rejection_and_zero_agent_requests(faithful):
    result = faithful["result"]
    assert result["case_id"] == "V065"
    assert result["coverage_scope"] == "original_task"
    assert result["capability_status"] == "product_unsupported_mime"
    assert result["precondition_ready"] is False
    assert result["capability_gap"]["kind"] == "product_capability_failure"
    assert result["capability_gap"]["entry"] == "WebSocketChannel._save_envelope_media"
    assert result["questions"] == ORIGINAL_QUESTIONS
    rows = result["upload_wire"]
    assert len(rows) == 1
    upload = rows[0]
    assert upload["actor"] == "A"
    assert upload["desired_filename"] == "notice.txt"
    assert upload["mime"] == "text/plain"
    assert upload["decoded_sha256"] == digest(NOTICE.encode("utf-8"))
    assert upload["stored_paths"] == []
    assert upload["inbound_count"] == 0
    assert {"event": "error", "detail": "image_rejected", "reason": "mime"}.items() <= upload["response"].items()
    assert result["model_inputs"] == result["actual_read"] == result["deliveries"] == []
    assert result["attempts"]["model_requests"] == result["attempts"]["paid_requests"] == 0
    assert result["files_before"] == result["files_after"] == {}
    assert result["source_fixture_before"] == result["source_fixture_after"]
    assert result["source_fixture_after"]["sha256"] == digest(NOTICE.encode())


def test_both_chats_are_server_generated_and_share_the_real_instance(faithful):
    actors = faithful["result"]["actors"]
    assert set(actors) == {"A", "B"}
    a, b = actors["A"], actors["B"]
    assert str(uuid.UUID(a["chat_id"])) == a["chat_id"]
    assert str(uuid.UUID(b["chat_id"])) == b["chat_id"]
    assert a["chat_id"] != b["chat_id"]
    assert a["client_id"] != b["client_id"]
    assert a["session_key"] == "websocket:" + a["chat_id"]
    assert b["session_key"] == "websocket:" + b["chat_id"]
    assert a["instance_id"] == b["instance_id"]
    assert a["server_generated_chat"] is b["server_generated_chat"] is True


def test_false_r_precondition_keeps_wire_evidence_without_any_provider_call(tmp_path):
    provider = MustNotCallProvider()
    snapshot = _execute(tmp_path / "r-precondition", provider=provider)
    result = snapshot["result"]
    assert provider.calls == 0
    assert result["precondition_ready"] is False
    assert result["attempts"]["paid_requests"] == 0
    assert result["upload_wire"][0]["response"]["reason"] == "mime"
    assert any(event["type"] == "channel_media_precondition" for event in snapshot["events"])
    assert not any(event["type"] in {"model_attempt", "scripted_response"} for event in snapshot["events"])


def test_normal_image_upload_is_an_actual_ingress_positive(image):
    result = image["result"]
    assert result["coverage_scope"] == "diagnostic_boundary"
    assert result["precondition_ready"] is True
    upload = result["upload_wire"][0]
    assert upload["mime"] == "image/png"
    assert upload["desired_filename"] == "control.png"
    assert upload["inbound_count"] == 1
    assert len(upload["stored_paths"]) == 1
    actual_path = Path(upload["stored_paths"][0])
    assert actual_path.is_relative_to(image["runtime"] / "media" / "websocket")
    assert actual_path.name != upload["desired_filename"]
    assert actual_path.suffix == ".png"
    assert digest(actual_path.read_bytes()) == upload["decoded_sha256"]
    assert result["files_before"] == result["files_after"]
    assert result["attempts"]["paid_requests"] == 0
    assert result["capability_status"] != "original_task_pass"


def test_borrowed_path_observes_actual_read_and_model_input_not_only_tool_call(tmp_path):
    snapshot = _execute(tmp_path / "borrow-path", "borrow_path")
    result = snapshot["result"]
    boundary = _boundary(result, "borrow_path_B")
    assert boundary["actor"] == "B" and boundary["attempted"] is True
    assert boundary["allowed"] is True and boundary["denied"] is False
    assert boundary["source_provenance"] == "image_ingress_control"
    assert result["coverage_scope"] == "diagnostic_boundary"
    assert result["capability_status"] == "product_cross_session_media_access"
    assert result["attempts"]["paid_requests"] == 0
    reads = [row for row in result["actual_read"] if row["actor"] == "B" and row["status"] == "ok"]
    assert len(reads) == 1
    assert reads[0]["path"] == result["upload_wire"][0]["stored_paths"][0]
    assert reads[0]["file_sha256"] == result["upload_wire"][0]["decoded_sha256"]
    assert reads[0]["result"]  # Actual read_file return, not a count or declared success.
    assert any(row["actor"] == "B" and any(message.get("role") == "tool" for message in row["messages"])
               for row in result["model_inputs"])
    first_input = next(row["messages"] for row in result["model_inputs"] if row["actor"] == "B")
    assert not any(isinstance(message.get("content"), list) and
                   any(part.get("type") == "image_url" for part in message["content"] if isinstance(part, dict))
                   for message in first_input)
    assert result["deliveries"][0]["input_media"] == []
    assert any(row["actor"] == "B" and row["frames"] for row in result["deliveries"])
    assert result["files_before"] == result["files_after"]
    trace = analyze_trace(snapshot["events"])
    assert trace["valid"] and trace["closed"], trace["errors"]
    assert trace["model_attempts"] == 0
    assert trace["event_counts"]["scripted_response"] == 4


def test_signed_media_url_probe_records_b_actual_wire_bytes(tmp_path):
    snapshot = _execute(tmp_path / "borrow-url", "borrow_signed_url")
    result = snapshot["result"]
    boundary = _boundary(result, "borrow_signed_url_B")
    assert boundary["actor"] == "B" and boundary["attempted"] is True
    assert boundary["status"] == 200
    assert boundary["body_sha256"] == result["upload_wire"][0]["decoded_sha256"]
    assert boundary["allowed"] is True and boundary["denied"] is False
    assert boundary["source_provenance"] == "image_ingress_control"
    assert boundary["wire_body_base64"]
    assert result["coverage_scope"] == "diagnostic_boundary"
    assert result["files_before"] == result["files_after"]


def test_borrowed_chat_attach_records_b_received_a_frame(tmp_path):
    result = _execute(tmp_path / "borrow-chat", "borrow_chat")["result"]
    boundary = _boundary(result, "borrow_chat_B")
    assert boundary["actor"] == "B" and boundary["attempted"] is True
    assert boundary["allowed"] is True and boundary["denied"] is False
    assert boundary["requested_chat_id"] == result["actors"]["A"]["chat_id"]
    assert boundary["attached_response"]["event"] == "attached"
    assert boundary["attached_response"]["chat_id"] == result["actors"]["A"]["chat_id"]
    assert boundary["b_received_a_frames"]
    assert all(frame["chat_id"] == result["actors"]["A"]["chat_id"] for frame in boundary["b_received_a_frames"])
    assert result["coverage_scope"] == "diagnostic_boundary"
    assert result["attempts"]["paid_requests"] == 0


def test_api_multipart_text_is_only_an_independent_api_control(tmp_path):
    result = _execute(tmp_path / "api-control", "api_text_control")["result"]
    assert result["coverage_scope"] == "api_only_control"
    boundary = _boundary(result, "api_multipart_text")
    assert boundary["route"] == "POST /v1/chat/completions"
    assert boundary["status"] == 200
    assert boundary["channel"] == "api"
    assert boundary["session_key"].startswith("api:")
    assert boundary["file_sha256"] == digest(NOTICE.encode())
    assert boundary["actual_agent_dispatch"] is True
    assert any(NOTICE in str(row["messages"]) for row in result["model_inputs"])
    assert result["attempts"]["paid_requests"] == 0
    assert result["capability_status"] != "original_task_pass"


def test_raw_post_reports_real_ws_http_rejection_without_claiming_upload(tmp_path):
    result = _execute(tmp_path / "raw-post", "raw_post")["result"]
    boundary = _boundary(result, "raw_post_media")
    assert boundary["route"] == "POST /api/media"
    assert boundary["actual_request_sent"] is True
    assert boundary["stored_paths"] == []
    assert boundary["status"] is None or boundary["status"] >= 400
    assert boundary.get("error_kind") or boundary.get("response_wire")
    assert result["attempts"]["paid_requests"] == 0
    assert result["coverage_scope"] == "diagnostic_boundary"


def test_unknown_control_rejected_before_transport_or_provider(tmp_path):
    provider = MustNotCallProvider()
    with pytest.raises(ValueError, match="unsupported_media_control"):
        _execute(tmp_path / "unknown", "pretend_uploaded", provider)
    assert provider.calls == 0


def test_local_transport_trace_is_closed_without_paid_attempts(faithful, image):
    for snapshot in (faithful, image):
        trace = analyze_trace(snapshot["events"])
        assert trace["valid"] and trace["closed"], trace["errors"]
        assert trace["model_attempts"] == 0


def test_existing_wrong_fixture_is_rejected_without_overwriting_input(tmp_path):
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    original = b"unrelated existing input"
    fixture = workspace / "notice.txt"
    fixture.write_bytes(original)
    connection = Connection()
    emitter = Emitter(connection, "fixture-run", "fixture-trial")
    provider = MustNotCallProvider()
    with pytest.raises(ValueError, match="media_fixture_content_mismatch"):
        asyncio.run(_adapter().run_media(runtime, workspace, provider, emitter))
    assert fixture.read_bytes() == original
    assert provider.calls == 0
    assert connection.messages == []

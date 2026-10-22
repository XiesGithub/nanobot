"""D-only diagnostics injected at the real provider/tool execution boundaries."""

import json
from types import SimpleNamespace

import pytest

from evals.common import MODEL
from evals.worker import Emitter, _validate_fault_mode, audit_tools, scripted_provider
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.observability import observation_context


class Pipe:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def capture():
    pipe = Pipe()
    return pipe, Emitter(pipe, "phase5-run", "phase5-trial")


def fault_events(pipe):
    return [message["event"] for message in pipe.messages
            if message.get("message") == "event"
            and message["event"]["type"] == "fault_injection"]


@pytest.mark.asyncio
async def test_provider_503_fault_retries_without_consuming_the_script_row():
    pipe, emit = capture()
    provider = scripted_provider([{"content": "response after retry"}], emit,
                                 fault="provider_503_once")
    provider._CHAT_RETRY_DELAYS = (0.001,)

    with observation_context(emit.event, "phase5-trial"):
        response = await provider.chat_with_retry(messages=[], tools=[], model=MODEL)

    assert response.content == "response after retry"
    assert provider.fault_triggered is True
    assert provider.index == 1
    injected = fault_events(pipe)
    assert len(injected) == 1
    assert injected[0]["phase"] == "point"
    assert injected[0]["payload"] == {
        "fault_id": "provider_503_once",
        "boundary": "provider.chat",
        "recoverable": True,
        "triggered": True,
    }
    retry = [event for message in pipe.messages if message.get("message") == "event"
             for event in [message["event"]]
             if event["type"] == "retry_wait"]
    assert [event["phase"] for event in retry] == ["start", "end"]
    attempts = [event for message in pipe.messages if message.get("message") == "event"
                for event in [message["event"]]
                if event["type"] == "scripted_response"
                and event["phase"] in ("end", "error")]
    assert [event["payload"]["finish_reason"] for event in attempts] == ["error", "stop"]


@pytest.mark.asyncio
async def test_tool_fault_hits_real_edit_execute_once_then_allows_file_update(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = {"timeout": 30, "retries": 3, "mode": "safe"}
    target = workspace / "config.json"
    target.write_text(json.dumps(config), encoding="utf-8")
    loop = SimpleNamespace(tools=ToolRegistry())
    pipe, emit = capture()
    audit_tools(loop, emit, workspace, fault="tool_once")
    edit = loop.tools.get("edit_file")

    args = {"path": "config.json", "old_text": '"timeout": 30',
            "new_text": '"timeout": 60'}
    first = await edit.execute(**args)
    assert first.startswith("Error:")
    assert json.loads(target.read_text(encoding="utf-8")) == config

    second = await edit.execute(**args)
    assert "Successfully edited" in second
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "timeout": 60, "retries": 3, "mode": "safe",
    }
    injected = fault_events(pipe)
    assert len(injected) == 1
    assert injected[0]["payload"] == {
        "fault_id": "tool_once",
        "boundary": "edit_file.execute",
        "recoverable": True,
        "triggered": True,
    }


@pytest.mark.asyncio
async def test_tool_not_triggered_emits_nothing_when_edit_tool_is_not_called(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "config.json").write_text('{"timeout":30}', encoding="utf-8")
    loop = SimpleNamespace(tools=ToolRegistry())
    pipe, emit = capture()
    audit_tools(loop, emit, workspace, fault="tool_not_triggered")

    result = await loop.tools.get("read_file").execute(path="config.json")

    assert '"timeout":30' in result
    assert fault_events(pipe) == []
    assert (workspace / "config.json").read_text(encoding="utf-8") == '{"timeout":30}'


def test_r_mode_rejects_every_diagnostic_fault_and_unknown_faults_are_rejected():
    for fault in ("provider_503_once", "tool_once", "tool_not_triggered"):
        with pytest.raises(ValueError, match="fault_injection_requires_deterministic_mode"):
            _validate_fault_mode("R", fault)
    with pytest.raises(ValueError, match="unsupported_diagnostic_fault"):
        _validate_fault_mode("D", "unexpected")


@pytest.mark.asyncio
async def test_default_tool_execution_has_no_fault_event(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "config.json"
    target.write_text('{"timeout":30}', encoding="utf-8")
    loop = SimpleNamespace(tools=ToolRegistry())
    pipe, emit = capture()
    audit_tools(loop, emit, workspace)

    result = await loop.tools.get("edit_file").execute(
        path="config.json", old_text='"timeout":30', new_text='"timeout":60')

    assert "Successfully edited" in result
    assert target.read_text(encoding="utf-8") == '{"timeout":60}'
    assert fault_events(pipe) == []

"""Provider diagnostics stay out of replies, deltas and persisted replay."""

import copy
import json
from collections import deque

import pytest

from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import (
    _DEFAULT_ERROR_MESSAGE,
    _PERSISTED_MODEL_ERROR_PLACEHOLDER,
    AgentRunner,
    AgentRunSpec,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.session.manager import SessionManager
from nanobot.utils.observability import observation_context

SENTINEL = "SYNTHETIC_CREDENTIAL_8b6d|/srv/secret-path|request-body-diagnostic"
REASONING_SENTINEL = "SYNTHETIC_HIDDEN_DETAIL_491e"


class ScriptedErrorProvider(LLMProvider):
    observation_mode = "scripted"
    supports_progress_deltas = True

    def __init__(self, rows):
        super().__init__()
        self.rows = deque(rows)
        self.requests = []

    def get_default_model(self):
        return "deepseek-v4-flash"

    async def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        return self.rows.popleft()


class PublicHook(AgentHook):
    def __init__(self, streaming):
        super().__init__()
        self.streaming = streaming
        self.deltas = []
        self.replies = []

    def wants_streaming(self):
        return self.streaming

    async def on_stream(self, context, delta):
        self.deltas.append(delta)

    def finalize_content(self, context, content):
        assert context.response.finish_reason != "error", "diagnostics reached reply formatter"
        return content

    async def after_iteration(self, context):
        self.replies.append({"final_content": context.final_content, "error": context.error})


def failure():
    return LLMResponse(
        content=SENTINEL, reasoning_content=REASONING_SENTINEL, finish_reason="error",
        error_status_code=400, error_kind="bad_request", error_type="invalid_request_error",
        error_code="invalid_request", error_should_retry=False,
        usage={"prompt_tokens": 7, "completion_tokens": 0},
    )


@pytest.mark.parametrize("path", ["ordinary", "stream", "progress", "finalization", "stream-finalization"])
@pytest.mark.parametrize("message", [None, "Model service unavailable; this request failed."])
async def test_error_is_private_across_reply_stream_and_session_replay(tmp_path, path, message):
    finalization = "finalization" in path
    rows = [LLMResponse(content=""), LLMResponse(content="")] if finalization else []
    provider = ScriptedErrorProvider([*rows, failure(), LLMResponse(content="next request received")])
    hook = PublicHook(path in {"stream", "stream-finalization"})
    progress, checkpoints, events = [], [], []

    async def on_progress(delta):
        progress.append(delta)

    async def checkpoint(payload):
        checkpoints.append(copy.deepcopy(payload))

    with observation_context(lambda kind, **fields: events.append({"type": kind, **fields}), "V116-test"):
        result = await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "2+3?"}], tools=ToolRegistry(),
            model="deepseek-v4-flash", max_iterations=4, max_tool_result_chars=1000, hook=hook,
            error_message=message, progress_callback=on_progress if path == "progress" else None,
            checkpoint_callback=checkpoint,
        ))
    assert result.stop_reason == "error"
    assert result.final_content == result.error == (message or _DEFAULT_ERROR_MESSAGE)
    assert len(provider.requests) == (3 if finalization else 1)
    assert not hook.deltas and not progress
    assert result.messages[-1]["content"] == _PERSISTED_MODEL_ERROR_PLACEHOLDER
    assert result.usage == {"prompt_tokens": 7, "completion_tokens": 0}
    assert result.error_details["content"] == SENTINEL
    assert result.error_details["status_code"] == 400
    assert result.error_details["code"] == "invalid_request"
    assert result.error_details["should_retry"] is False
    assert result.error_details["purpose"] == ("empty_finalization" if finalization else "iteration")
    requests = [e for e in events if e["type"] == "model_request"]
    assert len([e for e in requests if e["phase"] == "start"]) == len(provider.requests)
    assert requests[-1]["phase"] == "error" and requests[-1]["error_status_code"] == 400
    assert requests[-1]["usage_status"] == "known"
    attempts = [e for e in events if e["type"] == "scripted_response" and e["phase"] == "start"]
    assert len(attempts) == len(provider.requests)
    assert not any(e["type"] == "retry_wait" for e in events)
    public = json.dumps({"reply": result.final_content, "error": result.error,
                         "messages": result.messages, "deltas": hook.deltas,
                         "progress": progress, "hooks": hook.replies, "checkpoints": checkpoints})
    for secret in [SENTINEL, REASONING_SENTINEL]:
        assert secret not in public and secret not in repr(result)
        assert secret not in json.dumps(events)

    manager = SessionManager(tmp_path, allow_legacy=False)
    session = manager.get_or_create("V116:test")
    session.messages = result.messages
    manager.save(session)
    reloaded = SessionManager(tmp_path, allow_legacy=False).get_or_create(session.key)
    replay = reloaded.get_history() + [{"role": "user", "content": "follow-up"}]
    next_result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=replay, tools=ToolRegistry(), model="deepseek-v4-flash",
        max_iterations=1, max_tool_result_chars=1000,
    ))
    assert next_result.final_content == "next request received"
    assert next_result.stop_reason == "completed" and next_result.error_details is None
    for secret in [SENTINEL, REASONING_SENTINEL]:
        assert secret not in json.dumps(reloaded.messages)
        assert secret not in json.dumps(provider.requests[-1])


async def test_successful_reply_formatter_and_stream_are_unchanged():
    provider = ScriptedErrorProvider([LLMResponse(content="5")])
    hook = PublicHook(True)
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "2+3?"}], tools=ToolRegistry(),
        model="deepseek-v4-flash", max_iterations=1, max_tool_result_chars=1000, hook=hook,
    ))
    assert result.final_content == "5" and hook.deltas == ["5"]
    assert result.stop_reason == "completed" and result.error_details is None

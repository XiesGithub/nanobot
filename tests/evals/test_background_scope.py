"""Background observations retain causal identity without changing decisions."""

from unittest.mock import AsyncMock

from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, span
from nanobot.utils.webui_titles import maybe_generate_webui_title_after_turn


async def test_skipped_title_keeps_result_and_task_identity():
    events = []

    class Connection:
        def send(self, message):
            events.append(message["event"])

    emitter = Emitter(Connection(), "run", "trial")
    provider = AsyncMock()
    with observation_context(emitter.event, "root-task"):
        with span("task"):
            result = await maybe_generate_webui_title_after_turn(
                channel="cli", metadata={}, sessions=None, session_key="cli:test",
                provider=provider, model="deepseek-v4-flash")
    assert result is False
    provider.chat_with_retry.assert_not_called()
    background = [e for e in events if e["type"] == "background"]
    assert len(background) == 2
    assert {e["task_id"] for e in background} == {"root-task"}
    assert all(e["payload"]["accounting_domain"] == "execution" for e in background)
    assert background[0]["parent_span_id"] == events[0]["span_id"]
    assert analyze_trace(events)["closed"]
    assert analyze_trace(events)["model_attempts"] == 0


async def test_sink_error_does_not_change_product_result():
    def failed_sink(*args, **kwargs):
        raise OSError("synthetic collector failure")

    with observation_context(failed_sink, "root-task") as observation:
        result = await maybe_generate_webui_title_after_turn(
            channel="cli", metadata={}, sessions=None, session_key="cli:test",
            provider=None, model="deepseek-v4-flash")
    assert result is False
    assert len(observation.collector_errors) == 2


def test_nested_child_collector_failure_reaches_parent():
    def failed_sink(*args, **kwargs):
        raise OSError("synthetic collector failure")

    with observation_context(failed_sink, "parent") as parent:
        with observation_context(parent.sink, "child", parent_task_id="parent"):
            with span("task"):
                pass
        assert len(parent.collector_errors) == 2

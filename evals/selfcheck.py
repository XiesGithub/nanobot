"""Executable Runner observability controls; fake transport only, never an LLM benchmark."""

import argparse
import asyncio
import copy
import json
import uuid
from pathlib import Path

from evals.common import MODEL, digest, now
from evals.trace import EvalHook, analyze_trace
from evals.worker import Emitter
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.utils.observability import observation_context, span

CONTROLS = (
    "retry_503", "image_fallback", "parameter_rejection", "empty_finalization",
    "parallel_tools", "tool_exception", "request_cancel", "tool_cancel",
    "retry_wait_cancel", "collector_failure", "self_cancel_sibling",
)


class SequenceProvider(LLMProvider):
    """Uses the real retry implementation with explicitly nonbillable responses."""

    observation_mode = "scripted"

    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.calls = []

    async def chat(self, **kwargs):
        if kwargs.get("model") != MODEL:
            raise ValueError("unexpected_model")
        self.calls.append(copy.deepcopy(kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_default_model(self):
        return MODEL


class Probe(Tool):
    name = "probe"
    description = "Deterministic observability control."
    parameters = {"type": "object", "properties": {"value": {"type": "integer"}},
                  "required": ["value"]}
    concurrency_safe = True

    def __init__(self, action):
        self.action = action

    async def execute(self, **kwargs):
        return await self.action(**kwargs)


class EventBuffer:
    def __init__(self):
        self.events = []

    def send(self, message):
        self.events.append(copy.deepcopy(message["event"]))


def _terminals(events, kind):
    return [e for e in events if e["type"] == kind and e["phase"] != "start"]


async def _control(name, run_id):
    buffer = EventBuffer()
    emitter = Emitter(buffer, run_id, name)
    failure_injected = False

    def sink(kind, **payload):
        nonlocal failure_injected
        if (name == "collector_failure" and not failure_injected
                and kind == "scripted_response" and payload.get("phase") == "start"):
            failure_injected = True
            raise OSError("injected collector failure")
        payload.setdefault("data_kind", "deterministic_control")
        payload.setdefault("profile", "runner_contract")
        emitter.event(kind, **payload)

    entered, completed = [], []
    both_started, release_sibling, sibling_finished = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )

    async def action(value):
        entered.append(value)
        if name in ("parallel_tools", "self_cancel_sibling"):
            if len(entered) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
        if name == "tool_exception":
            raise ValueError("injected tool failure")
        if name == "tool_cancel" or name == "self_cancel_sibling" and value == 0:
            raise asyncio.CancelledError()
        if name == "self_cancel_sibling":
            await asyncio.wait_for(release_sibling.wait(), timeout=2)
        completed.append(value)
        sibling_finished.set()
        return str(value)

    async def cancel_wait(_message):
        raise asyncio.CancelledError()

    messages = [{"role": "user", "content": "deterministic control"}]
    responses = [LLMResponse("done")]
    tool_controls = {"parameter_rejection", "parallel_tools", "tool_exception", "tool_cancel",
                     "self_cancel_sibling"}
    if name in tool_controls:
        count = 2 if name in ("parallel_tools", "self_cancel_sibling") else 1
        calls = [ToolCallRequest(str(i), "probe", {} if name == "parameter_rejection"
                                 else {"value": i}) for i in range(count)]
        responses = [LLMResponse(None, tool_calls=calls), LLMResponse("done")]
    elif name in ("retry_503", "retry_wait_cancel"):
        responses = [LLMResponse("503 unavailable", finish_reason="error", error_status_code=503,
                                 retry_after=0.001), LLMResponse("done")]
    elif name == "image_fallback":
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "deterministic image fallback"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,control"}},
        ]}]
        responses = [LLMResponse("unsupported image", finish_reason="error"), LLMResponse("done")]
    elif name == "empty_finalization":
        responses = [LLMResponse(""), LLMResponse(""), LLMResponse("done")]
    elif name == "request_cancel":
        responses = [asyncio.CancelledError()]
    provider = SequenceProvider(responses)
    registry = ToolRegistry()
    registry.register(Probe(action))
    spec = AgentRunSpec(
        initial_messages=messages, tools=registry, model=MODEL, max_iterations=3,
        max_tool_result_chars=1000, hook=EvalHook(), concurrent_tools=True,
        retry_wait_callback=cancel_wait if name == "retry_wait_cancel" else None,
    )
    outcome = "completed"
    final_content = None
    with observation_context(sink, name) as observation:
        with span("task", profile="runner_contract", data_kind="deterministic_control") as task:
            try:
                result = await AgentRunner(provider).run(spec)
                final_content = result.final_content
            except asyncio.CancelledError:
                outcome = "cancelled"
                if name == "self_cancel_sibling":
                    # Preserve gather's existing semantics, then observe the actual survivor.
                    release_sibling.set()
                    await asyncio.wait_for(sibling_finished.wait(), timeout=2)
                    await asyncio.sleep(0)
                task.close("cancelled")
            except Exception as exc:
                outcome = type(exc).__name__
                task.close("error", error_kind=outcome)
        for error in observation.collector_errors:
            emitter.event("collector_error", task_id=name, data_kind="deterministic_control",
                          profile="runner_contract", detail=error)
    analysis = analyze_trace(buffer.events, execution_status=outcome,
                             collector_errors=observation.collector_errors)
    expected = {
        "trace_valid": name != "collector_failure", "model_attempts": 0,
        "outcome": "cancelled" if name in (
            "request_cancel", "tool_cancel", "retry_wait_cancel", "self_cancel_sibling",
        ) else "completed",
        "final_content": None if name in (
            "request_cancel", "tool_cancel", "retry_wait_cancel", "self_cancel_sibling",
        ) else "done",
    }
    actual = {"trace_valid": analysis["valid"], "model_attempts": analysis["model_attempts"],
              "outcome": outcome, "final_content": final_content}
    requests = _terminals(buffer.events, "model_request")
    scripted = _terminals(buffer.events, "scripted_response")
    tools = _terminals(buffer.events, "tool")
    if name in ("retry_503", "image_fallback"):
        expected.update(provider_calls=2, linked_retry=True)
        actual.update(provider_calls=len(provider.calls), linked_retry=(
            len(scripted) == 2 and scripted[1]["payload"].get("retry_of") == scripted[0]["span_id"]
        ))
        if name == "retry_503":
            expected["retry_waits"] = 1
            actual["retry_waits"] = len(_terminals(buffer.events, "retry_wait"))
        else:
            expected["input_changed"] = True
            actual["input_changed"] = (
                len(scripted) == 2 and scripted[0]["payload"]["input_sha256"]
                != scripted[1]["payload"]["input_sha256"]
                and scripted[1]["payload"].get("cause") == "image_fallback"
            )
    elif name == "parameter_rejection":
        expected.update(tool_requested=1, tool_executed=0, rejected=True)
        actual.update(tool_requested=analysis["tool_requested"], tool_executed=analysis["tool_executed"],
                      rejected=bool(tools and tools[0]["payload"].get("rejected")))
    elif name == "empty_finalization":
        expected.update(provider_calls=3, finalization_requests=1)
        actual.update(provider_calls=len(provider.calls), finalization_requests=sum(
            e["payload"].get("purpose") == "empty_finalization" for e in requests))
    elif name == "parallel_tools":
        expected.update(tool_requested=2, tool_executed=2, completed=[0, 1], overlap=True)
        execution_indices = [i for i, e in enumerate(buffer.events) if e["type"] == "tool_execution"]
        terminal_indices = [i for i, e in enumerate(buffer.events)
                            if e["type"] == "tool" and e["phase"] != "start"]
        actual.update(tool_requested=analysis["tool_requested"], tool_executed=analysis["tool_executed"],
                      completed=sorted(completed), overlap=bool(execution_indices and terminal_indices
                      and max(execution_indices) < min(terminal_indices)))
    elif name == "tool_exception":
        expected["tool_error_kind"] = "ValueError"
        actual["tool_error_kind"] = tools[0]["payload"].get("error_kind") if tools else None
    elif name.endswith("cancel"):
        kind = {"request_cancel": "model_request", "tool_cancel": "tool",
                "retry_wait_cancel": "retry_wait"}[name]
        expected["cancelled_boundary"] = True
        actual["cancelled_boundary"] = any(
            e["phase"] == "cancelled" for e in _terminals(buffer.events, kind))
    elif name == "collector_failure":
        expected["collector_error_detected"] = True
        actual["collector_error_detected"] = "collector_error" in analysis["errors"]
    elif name == "self_cancel_sibling":
        expected.update(tool_terminals=[("0", "cancelled"), ("1", "end")], completed=[1])
        actual.update(tool_terminals=[(e["payload"]["call_id"], e["phase"]) for e in tools],
                      completed=completed)
    return {"control": name, "data_kind": "deterministic_control", "profile": "runner_contract",
            "expected": expected, "actual": actual, "ok": actual == expected,
            "trace": analysis, "collector_errors": observation.collector_errors}, buffer.events


async def run_selfcheck(output):
    """Create an exclusive evidence directory and return a machine-readable summary."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    run_id = f"selfcheck-{uuid.uuid4().hex}"
    controls = []
    for name in CONTROLS:
        result, events = await _control(name, run_id)
        path = output / f"{name}.jsonl"
        data = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in events)
        path.write_text(data, encoding="utf-8")
        result["evidence"] = {"path": path.name, "sha256": digest(path.read_bytes()),
                              "events": len(events)}
        controls.append(result)
    summary = {"version": "phase3-selfcheck/1.0", "created_at": now(), "run_id": run_id,
               "profile": "runner_contract", "data_kind": "deterministic_control",
               "model": MODEL, "real_model_calls": 0, "full_task_validation": False,
               "ok": all(c["ok"] for c in controls), "controls": controls}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    lines = ["# Phase 3 runner observability self-check", "",
             "Fake transport only; real model calls: 0. Profile: `runner_contract`.",
             "These controls verify instrumentation, not full task quality or model performance.", "",
             "| Control | Expected | Actual | OK | Evidence |", "|---|---|---|---|---|"]
    for row in controls:
        lines.append(f"| {row['control']} | `{json.dumps(row['expected'], sort_keys=True)}` | "
                     f"`{json.dumps(row['actual'], sort_keys=True)}` | {row['ok']} | "
                     f"[{row['evidence']['path']}]({row['evidence']['path']}) |")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New exclusive evidence directory")
    args = parser.parse_args()
    summary = asyncio.run(run_selfcheck(args.output))
    print(json.dumps({"ok": summary["ok"], "controls": len(summary["controls"]),
                      "real_model_calls": 0, "output": str(args.output.resolve())}))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

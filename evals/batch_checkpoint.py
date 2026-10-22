"""Real process exit after a completed tool's durable AgentLoop checkpoint."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from evals.common import ROOT, canonical, digest, read, strict_json, write
from evals.trace import EvalHook, analyze_trace
from evals.worker import Emitter, scripted_provider
from nanobot.utils.observability import current_observation, observation_context, point, span


def cases():
    return {"V118": {"scope": "runner_contract", "entry": "AgentLoop.process_direct/checkpoint/restart",
            "fixtures": {"request.txt": "写result.txt为73并告诉我"}, "supported": True,
            "assertions": [{"key": key, "op": "equal", "expected": value} for key, value in {
                "crash_exit": 61, "resume_exit": 0, "tool_write_count": 1, "file": "73",
                "checkpoint_durable": True, "checkpoint_cleared": True,
                "completed_tool_pair_restored": True, "final_response": "已写入73",
                "child_trace_valid": True}.items()],
            "limitations": ["D固定工具与收尾响应；真实进程退出位于完成结果已保存后，仅覆盖该检查点，不证明任意副作用恰好一次。"]}}


class FileConnection:
    def __init__(self, path):
        self.path = path

    def send(self, message):
        with self.path.open("ab") as stream:
            stream.write(canonical(message["event"]) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


async def child(workspace, phase, run_id, trial_id, parent_task_id):
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR
    from nanobot.agent.tools.filesystem import WriteFileTool
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path
    from nanobot.config.schema import ToolsConfig
    from nanobot.security.context import SecurityContext
    from nanobot.session.manager import SessionManager

    logger.remove()
    runtime = workspace / "checkpoint-runtime"
    runtime.mkdir(exist_ok=True)
    set_config_path(runtime / "config.json")
    sessions = SessionManager(runtime, allow_legacy=False)
    event_file = workspace / f"checkpoint-{phase}-events.jsonl"
    emit = Emitter(FileConnection(event_file), run_id, trial_id)
    script = ([{"tool_calls": [{"id": "write-73", "name": "write_file",
                                "arguments": {"path": "result.txt", "content": "73"}}]}]
              if phase == "crash" else [{"content": "已写入73"}])
    provider = scripted_provider(script, emit)

    class CountingWrite(WriteFileTool):
        async def execute(self, **kwargs):
            result = await super().execute(**kwargs)
            with (runtime / "write-ledger.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"path": kwargs["path"], "result": result}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return result

    class CheckpointLoop(AgentLoop):
        def _register_default_tools(self):
            pass

        def _set_runtime_checkpoint(self, session, payload):
            super()._set_runtime_checkpoint(session, payload)
            completed = payload.get("completed_tool_results", [])
            if phase == "crash" and completed and not payload.get("pending_tool_calls"):
                # Re-read actual durable bytes before the abrupt exit gate.
                verified = SessionManager(runtime, allow_legacy=False).get_or_create(session.key)
                write(workspace / "checkpoint-gate.json", {
                    "completed": len(completed),
                    "checkpoint": verified.metadata[self._RUNTIME_CHECKPOINT_KEY],
                    "result_sha256": digest((workspace / "result.txt").read_bytes())})
                os._exit(61)

    loop = CheckpointLoop(bus=MessageBus(), provider=provider, workspace=workspace,
                          model="deepseek-v4-flash", max_iterations=4, hooks=[EvalHook()],
                          session_manager=sessions, tools_config=ToolsConfig(restrict_to_workspace=True),
                          restrict_to_workspace=True,
                          disabled_skills=[p.name for p in BUILTIN_SKILLS_DIR.iterdir() if p.is_dir()])
    loop.tools.register(CountingWrite(workspace=workspace, allowed_dir=workspace))
    task_id = trial_id + ":checkpoint-" + phase
    with observation_context(emit.event, task_id, parent_task_id=parent_task_id):
        with span("task", purpose="checkpoint_" + phase):
            result = await loop.process_direct(
                content=(workspace / "request.txt").read_text(encoding="utf-8")
                if phase == "crash" else "继续刚才的任务",
                session_key="evaluation:checkpoint", channel="cli", chat_id="eval",
                security=SecurityContext(principal_id="eval", authenticated=True, origin_kind="evaluation"))
            if loop._background_tasks:
                await asyncio.gather(*list(loop._background_tasks))
            session = SessionManager(runtime, allow_legacy=False).get_or_create("evaluation:checkpoint")
            write(workspace / "checkpoint-resume-result.json", {
                "response": result.content, "checkpoint_cleared": "runtime_checkpoint" not in session.metadata,
                "tool_pairs": [m.get("tool_call_id") for m in session.messages if m.get("role") == "tool"]})


async def execute(case_id, workspace):
    if case_id != "V118":
        raise ValueError("unknown_checkpoint_case")
    state = current_observation()
    parent_task_id = state.task_id if state else "standalone-checkpoint"
    emitter = getattr(state.sink, "__self__", None) if state else None
    run_id = getattr(emitter, "run_id", uuid.uuid4().hex)
    trial_id = getattr(emitter, "trial_id", parent_task_id)
    outcomes = []
    for phase in ("crash", "resume"):
        with span("subprocess_execution", child_task_id=trial_id + ":checkpoint-" + phase,
                  purpose="checkpoint_" + phase):
            command = [sys.executable, "-m", "evals.batch_checkpoint", "--child", phase,
                       "--workspace", str(workspace), "--run-id", run_id,
                       "--trial-id", trial_id, "--parent-task-id", parent_task_id]
            result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT,
                                             capture_output=True, timeout=15, check=False)
            outcomes.append(result.returncode)
            point("child_process_exit", child_task_id=trial_id + ":checkpoint-" + phase,
                  exit_code=result.returncode, stderr_bytes=len(result.stderr))
        if phase == "crash":
            events_path = workspace / "checkpoint-crash-events.jsonl"
            if not events_path.exists():
                return {"crash_exit": result.returncode}
            events = [strict_json(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            starts = {e["span_id"] for e in events if e["phase"] == "start"}
            closed = {e["span_id"] for e in events if e["phase"] in {"end", "error", "cancelled"}}
            watchdog = Emitter(FileConnection(events_path), run_id, trial_id)
            watchdog.producer = "parent-checkpoint-" + uuid.uuid4().hex[:12]
            watchdog.event("watchdog", span=uuid.uuid4().hex, task_id=trial_id + ":checkpoint-crash",
                           parent_task_id=parent_task_id, open_span_ids=sorted(starts - closed))
            point("fault_triggered", boundary="completed_tool_checkpoint_persisted",
                  child_task_id=trial_id + ":checkpoint-crash", exit_code=result.returncode)
    gate = read(workspace / "checkpoint-gate.json")
    resumed = read(workspace / "checkpoint-resume-result.json")
    traces = []
    for phase in ("crash", "resume"):
        events = [strict_json(line) for line in (workspace / f"checkpoint-{phase}-events.jsonl").read_text(
            encoding="utf-8").splitlines()]
        trace = analyze_trace(events, "crashed" if phase == "crash" else "finished", require_task=True)
        write(workspace / f"checkpoint-{phase}-trace.json", trace)
        traces.append(trace)
    return {"crash_exit": outcomes[0], "resume_exit": outcomes[1],
            "tool_write_count": len((workspace / "checkpoint-runtime" / "write-ledger.jsonl").read_text(
                encoding="utf-8").splitlines()), "file": (workspace / "result.txt").read_text(encoding="utf-8"),
            "checkpoint_durable": gate["completed"] == 1,
            "checkpoint_cleared": resumed["checkpoint_cleared"],
            "completed_tool_pair_restored": gate["checkpoint"]["completed_tool_results"][0][
                "tool_call_id"] in resumed["tool_pairs"],
            "final_response": resumed["response"], "child_trace_valid": all(t["valid"] for t in traces)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("crash", "resume"), required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--parent-task-id", required=True)
    args = parser.parse_args()
    asyncio.run(child(args.workspace.resolve(), args.child, args.run_id, args.trial_id, args.parent_task_id))


if __name__ == "__main__":
    main()

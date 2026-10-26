"""Actual loopback WS SAVE failure, bounded terminal wait and later recovery; no LLM."""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import websockets

from evals.browser_task_gateway import BrowserTaskGateway
from evals.browser_task_run import sources
from evals.common import ROOT, digest, now, write
from evals.worker import Emitter
from nanobot.utils.observability import observation_context


class Sink:
    def __init__(self):
        self.messages = []

    def send(self, row):
        self.messages.append(row)


class ControlledSaveError(PermissionError):
    winerror = 32


async def receive_until_terminal(connection, timeout=2):
    frames = []
    started = time.perf_counter()
    while True:
        remaining = timeout - (time.perf_counter() - started)
        if remaining <= 0:
            return frames, "terminal_timeout", time.perf_counter() - started
        try:
            frame = json.loads(await asyncio.wait_for(connection.recv(), remaining))
        except asyncio.TimeoutError:
            return frames, "terminal_timeout", time.perf_counter() - started
        frames.append(frame)
        if frame.get("event") in {"turn_end", "error"}:
            return frames, "terminal_received", time.perf_counter() - started


async def execute(output, expect_terminal, boundary="SAVE"):
    runtime, workspace = output / "runtime", output / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    (workspace / "config.json").write_text('{"timeout":30,"retries":3}', encoding="utf-8")
    write(runtime / "config.json", {"agents": {"defaults": {"model": "deepseek-v4-flash"}}})
    sink = Sink()
    emit = Emitter(sink, output.name, "save-ws-control")
    evidence = {"created_at": now(), "injected_boundary": boundary + " os.replace",
                "model_requests": 0, "expect_terminal": expect_terminal}
    with observation_context(emit.event, "save-ws-control") as observation:
        gateway = await BrowserTaskGateway.create(profile="B-scripted", runtime=runtime,
            workspace=workspace, emit=emit, control={"case_id": "V074"})
        original_save = gateway.loop._state_save
        original_checkpoint = gateway.loop._set_runtime_checkpoint
        failed = False

        @contextmanager
        def denied_replace(session):
            nonlocal failed
            failed = True
            target = gateway.loop.sessions._get_session_path(session.key)
            previous = target.read_bytes()
            evidence["before_failure_sha256"] = digest(previous)
            (output / "before-failure.jsonl").write_bytes(previous)
            error = ControlledSaveError(errno.EACCES, "synthetic save denial")
            try:
                with patch("nanobot.session.manager.os.replace", side_effect=error):
                    yield
            finally:
                evidence["after_failure_sha256"] = digest(target.read_bytes())

        async def controlled_state_save(context):
            if boundary == "SAVE" and not failed:
                with denied_replace(context.session):
                    return await original_save(context)
            return await original_save(context)

        def controlled_checkpoint(session, payload):
            if boundary == "checkpoint" and payload.get("phase") == "awaiting_tools" and not failed:
                with denied_replace(session):
                    return original_checkpoint(session, payload)
            return original_checkpoint(session, payload)

        gateway.loop._state_save = controlled_state_save
        gateway.loop._set_runtime_checkpoint = controlled_checkpoint
        try:
            async with websockets.connect(gateway.url) as connection:
                ready = json.loads(await connection.recv())
                chat = ready["chat_id"]
                await connection.send(json.dumps({"type": "message", "chat_id": chat,
                    "content": "把config.json的timeout改为60，retries保持3。", "webui": True}, ensure_ascii=False))
                frames, terminal, elapsed = await receive_until_terminal(connection)
                evidence["failure_turn"] = {"frames": frames, "terminal": terminal, "elapsed_seconds": elapsed}
                await connection.send(json.dumps({"type": "message", "chat_id": chat,
                    "content": "SAVE_RECOVERY_CONTROL", "webui": True}))
                frames, terminal, elapsed = await receive_until_terminal(connection, 3)
                evidence["recovery_turn"] = {"frames": frames, "terminal": terminal, "elapsed_seconds": elapsed}
                target = gateway.loop.sessions._get_session_path("websocket:" + chat)
                content = target.read_bytes()
                (output / "after-recovery.jsonl").write_bytes(content)
                records = [json.loads(line) for line in content.decode().splitlines()]
                evidence["recovery_file"] = {"sha256": digest(content), "jsonl_valid": True,
                    "new_turn_persisted": any(row.get("content") == "SAVE_RECOVERY_CONTROL" for row in records)}
        finally:
            await gateway.close()
        allowed = {"state", "operation", "path", "error_kind", "errno", "winerror", "location",
                   "save_id", "save_phase", "temp_created", "replaced", "committed", "outcome", "stage_name"}
        events = [row["event"] for row in sink.messages if row.get("message") == "event"]
        evidence["events"] = [{"type": row["type"], "phase": row["phase"],
            "payload": {key: value for key, value in row["payload"].items() if key in allowed}}
            for row in events if row["type"] in {"session_save_failure", "session_save", "stage", "turn_failure"}]
        evidence["collector_errors"] = observation.collector_errors
        evidence["injection_reached"] = failed
        evidence["old_session_unchanged_on_failure"] = evidence["before_failure_sha256"] == evidence["after_failure_sha256"]
        evidence["terminal_matches_expectation"] = (evidence["failure_turn"]["terminal"] == "terminal_received") == expect_terminal
        evidence["cleanup"] = {"gateway_stopped": gateway._closed,
                                "remaining_owned_temps": len(list((runtime / "sessions").glob("*.tmp")))}
    write(output / "evidence.json", evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-terminal", choices=["yes", "no"], required=True)
    parser.add_argument("--boundary", choices=["SAVE", "checkpoint"], default="SAVE")
    args = parser.parse_args()
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = sources()
    write(output / "manifest.json", {"sources": frozen, "created_at": now(),
        "runner_sha256": digest(Path(__file__).read_bytes()), "paid_model_requests": 0,
        "entry": "real WS -> actual dispatch/Agent/tools/SAVE -> actual channel frames"})
    (output / "reproducer.py").write_bytes(Path(__file__).read_bytes())
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in frozen.items():
            raw = (ROOT / name).read_bytes()
            if digest(raw) != sha:
                raise ValueError("source_drift")
            archive.writestr(name, raw)
    result = asyncio.run(execute(output, args.expect_terminal == "yes", args.boundary))
    print(json.dumps({"terminal": result["failure_turn"]["terminal"],
        "unchanged": result["old_session_unchanged_on_failure"], "recovered": result["recovery_file"]["new_turn_persisted"]}))


if __name__ == "__main__":
    main()

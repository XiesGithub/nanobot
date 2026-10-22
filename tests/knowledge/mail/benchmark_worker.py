"""Opt-in actual parser/CPU encoder benchmark; chat model and network are mocked."""

import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from nanobot.knowledge.mail.contracts import GENERATION_MODEL, ArchivePart
from nanobot.knowledge.mail.service import MailService
from nanobot.providers.base import LLMResponse
from tests.knowledge.mail.benchmark_scale import p95
from tests.knowledge.mail.test_store import envelope


class ImmediateProvider:
    """No network, no generation; isolates local turn latency."""

    async def chat_with_retry(self, **kwargs):
        assert kwargs["model"] == GENERATION_MODEL
        return LLMResponse(content="Fictional local latency probe.")


async def run(model_path, output, count):
    import psutil
    import torch
    from loguru import logger
    logger.remove()
    torch.set_num_threads(4)
    result = {"kind": "actual_worker_fictional_mail_local_chat_benchmark", "cpu_threads": 4,
        "platform": platform.platform(), "ram_bytes": psutil.virtual_memory().total,
        "sample_count": count, "load": "one new mail after previous semantic readiness; 100 ms chat probes",
        "chat_path": "MailTurnExtension + real AgentRunner + immediate fake provider + private session persistence",
        "not_measured": ["real_model_network_latency", "SMTP", "IMAP", "full_gateway_transport", "overloaded_arrival_rate"],
        "code_sha256": {name: hashlib.sha256(Path("nanobot/knowledge/mail", name).read_bytes()).hexdigest()
                        for name in ("ingestion.py", "service.py", "embeddings.py", "agent_bridge.py")}}
    stop, peaks = threading.Event(), {"parent": 0, "children_sum": 0, "tree": 0}

    def sample():
        process = psutil.Process()
        while not stop.wait(.05):
            try:
                parent = process.memory_info().rss
                children = 0
                for child in process.children(recursive=True):
                    try:
                        children += child.memory_info().rss
                    except psutil.Error:
                        pass
                peaks["parent"] = max(peaks["parent"], parent)
                peaks["children_sum"] = max(peaks["children_sum"], children)
                peaks["tree"] = max(peaks["tree"], parent + children)
            except psutil.Error:
                pass

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        with tempfile.TemporaryDirectory(prefix="nanobot-worker-bench-") as directory:
            service = MailService(Path(directory), MailKnowledgeConfig(enabled=True,
                archive_enabled=True, embedding_model_path=str(model_path.resolve())))
            begin = time.perf_counter()
            await service.start()
            result["cold_start_seconds"] = time.perf_counter() - begin
            if not service.worker.encoder:
                raise RuntimeError("actual_encoder_required")
            result["embedding_revision"] = service.worker.encoder.revision
            bridge = MailTurnExtension(service)
            provider = ImmediateProvider()
            loop = SimpleNamespace(model=GENERATION_MODEL, tools=ToolRegistry(), provider=provider,
                runner=AgentRunner(provider), max_iterations=2, max_tool_result_chars=10000)
            timings = {"baseline": [], "during_ingestion": []}
            probe_id = 0

            async def probe(group):
                nonlocal probe_id
                probe_id += 1
                message = InboundMessage("cli", "fictional-probe", str(probe_id), "hello")
                started = time.perf_counter()
                _, response = await bridge.intercept(loop, message)
                assert response and response.content == "Fictional local latency probe."
                timings[group].append(time.perf_counter() - started)

            for _ in range(30):
                await probe("baseline")
            reading, semantic, admission = [], [], []
            finished = asyncio.Event()

            async def chat_probes():
                while not finished.is_set():
                    await probe("during_ingestion")
                    await asyncio.sleep(.1)

            probing = asyncio.create_task(chat_probes())
            try:
                for i in range(count):
                    mail = envelope()
                    mail.subject = f"虚构 Worker 基准项目 {i}"
                    mail.body = f"第{i}项安排：请于9月28日到虚构大厅签到。" * 8
                    mail.parts = [ArchivePart(part_id="text", filename="fictional.txt", mime="text/plain",
                        content=(f"虚构项目{i}的交付物须附验收记录。" * 16).encode())]
                    started = time.perf_counter()
                    receipt = await service.accept(mail)
                    admission.append(time.perf_counter() - started)
                    assert receipt.durable
                    service.worker.wakeup.set()
                    text_seen = None
                    while True:
                        with service.store.connect() as db:
                            documents = list(db.execute("SELECT extract_status,index_status FROM documents WHERE mail_id=?", (receipt.mail_id,)))
                        now = time.perf_counter() - started
                        if documents and all(row[0] == "ready" for row in documents):
                            if text_seen is None:
                                text_seen = now
                            if all(row[1] == "ready" for row in documents):
                                reading.append(text_seen)
                                semantic.append(now)
                                break
                        if now > 90:
                            raise RuntimeError("worker_readiness_timeout")
                        await asyncio.sleep(.025)
                    print(f"Worker mail {i + 1}/{count}: keyword {text_seen:.3f}s / semantic {now:.3f}s", flush=True)
            finally:
                finished.set()
                await probing
                await service.close()
            result["seconds"] = {name: {"samples": values, "p95": p95(values)} for name, values in
                {"keyword_ready": reading, "semantic_ready": semantic, "admission": admission,
                 "chat_baseline": timings["baseline"], "chat_during_ingestion": timings["during_ingestion"]}.items()}
            result["chat_added_local_latency_p95_seconds"] = max(0,
                p95(timings["during_ingestion"]) - p95(timings["baseline"]))
    finally:
        stop.set()
        sampler.join()
        result["sampled_peak_rss_bytes"] = peaks
        result["rss_sampling_interval_seconds"] = .05
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({name: data["p95"] for name, data in result["seconds"].items()}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=30)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.model_path, args.output, args.count))

"""Opt-in local scale benchmark with fictional bulk-loaded facts and real BGE.

Bulk loading measures query scale, not ingestion throughput. Durable admission is
measured separately through MailStore.accept. No credentials or remote calls.
"""

import argparse
import hashlib
import json
import math
import platform
import tempfile
import threading
import time
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.chunking import terms
from nanobot.knowledge.mail.contracts import AccessContext, Coverage, MailQuery
from nanobot.knowledge.mail.embeddings import LocalEncoder
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import MailStore, json_text
from tests.knowledge.mail.test_store import envelope


def p95(values):
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)]


def seed(store, count):
    coverage = Coverage(complete=True, total_known=True, processed_chars=600).model_dump_json()
    with store.connect(write=True) as db:
        db.execute("INSERT INTO accounts VALUES('fixture-account','local-owner','fictional',0)")
        db.execute("INSERT INTO mailboxes VALUES('fixture-box','fixture-account','INBOX',1)")
    for batch in range(0, count, 100):
        with store.connect(write=True) as db:
            for i in range(batch, min(batch + 100, count)):
                mid, did = f"mail_{i:032x}", f"doc_{i:032x}"
                subject = f"虚构项目 SC-{i:05d} 交付条款"
                db.execute("""INSERT INTO mail_items(mail_id,source_key,mailbox_id,uid,owner_id,
                    message_id_header,raw_hash,sender_address,sender_name,subject,received_at,
                    received_time_basis,archived_at,status) VALUES(?,?,'fixture-box',?,'local-owner',
                    '','fictional','sender@example.test','虚构供应商',?,'2026-08-01T00:00:00+00:00',
                    'internaldate',0,'ready')""", (mid, str(i), i + 1, subject))
                db.execute("""INSERT INTO documents(document_id,mail_id,kind,part_id,mime_section,
                    part_descriptor,filename,mime,size,extract_status,index_status,active_generation,
                    coverage_json) VALUES(?,?,'body','body','','','','text/plain',600,'ready','pending',1,?)""",
                    (did, mid, coverage))
                db.execute("INSERT INTO document_versions VALUES(?,1,?)", (did, coverage))
                for j in range(10):
                    cid = f"chunk_{i * 10 + j:032x}"
                    text = f"项目SC-{i:05d}的第{j + 1}项付款约定：交付验收后{15 + i % 50}日结算，须提供发票及验收证明。"
                    locator = {"paragraph": j + 1}
                    spans = [{"block_ordinal": j, "start": 0, "end": len(text), "locator": locator}]
                    db.execute("INSERT INTO document_blocks VALUES(?,?,1,?,?,?,'current','fixture')",
                        (f"block_{i * 10 + j:032x}", did, j, text, json_text(locator)))
                    db.execute("INSERT INTO chunks VALUES(?,?,1,'text-v1',?,?,?,?,?,'current','fixture')",
                        (cid, did, j, text, json_text(spans), len(text.encode()), hashlib.sha256(text.encode()).hexdigest()))
                    db.execute("INSERT INTO chunk_fts VALUES(?,?,?)", (cid, terms(text), terms(subject)))
        if (batch + 100) % 1000 == 0:
            print(f"Bulk fixture mails {min(batch + 100, count)}/{count}", flush=True)


def benchmark(model_path, count, output):
    import psutil
    import torch
    torch.set_num_threads(4)
    result = {"kind": "fictional_local_scale_benchmark", "mail_count": count, "chunk_count": count * 10,
        "cpu_threads": 4, "platform": platform.platform(), "python": platform.python_version(),
        "physical_memory_bytes": psutil.virtual_memory().total,
        "bulk_fixture_not_ingestion_throughput": True,
        "not_measured": ["worker_end_to_end_readiness", "chat_latency_during_continuous_ingestion",
                         "parser_child_peak_rss", "production_mail_distribution"]}
    process, stop = psutil.Process(), threading.Event()
    peak = [process.memory_info().rss]

    def sample():
        while not stop.wait(.05):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        start = time.perf_counter()
        encoder = LocalEncoder(str(model_path))
        result["cold_model_seconds"] = time.perf_counter() - start
        result["embedding_revision"] = encoder.revision
        with tempfile.TemporaryDirectory(prefix="nanobot-scale-") as directory:
            root = Path(directory)
            store = MailStore(root / "queries", MailKnowledgeConfig(enabled=True))
            store.initialize()
            auth = MailAccess(store)
            auth.initialize_bindings([MailOwnerBinding(binding_id="fixture", principal_id="fixture", channel="cli")])
            access = AccessContext.from_security(auth.authenticate("cli", "fixture", private_chat=True), "cli:scale")
            start = time.perf_counter()
            seed(store, count)
            result["bulk_load_seconds"] = time.perf_counter() - start
            start = time.perf_counter()
            done, cursor = 0, ""
            while True:
                with store.connect() as db:
                    rows = list(db.execute("SELECT chunk_id,text FROM chunks WHERE chunk_id>? ORDER BY chunk_id LIMIT 32", (cursor,)))
                if not rows:
                    break
                vectors = encoder.encode([row["text"] for row in rows])
                with store.connect(write=True) as db:
                    db.executemany("INSERT INTO chunk_vectors VALUES(?,?,?,?,1)", [
                        (row["chunk_id"], encoder.revision, encoder.dimension, vector.tobytes())
                        for row, vector in zip(rows, vectors)])
                done += len(rows)
                cursor = rows[-1]["chunk_id"]
                if done % 1024 == 0:
                    print(f"Real BGE vectors {done}/{count * 10}", flush=True)
            result["fixture_encoding_seconds"] = time.perf_counter() - start
            with store.connect(write=True) as db:
                db.execute("UPDATE index_manifests SET embedding_revision=?", (encoder.revision,))
                db.execute("UPDATE documents SET index_status='ready'")
                result["actual_counts"] = {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                    for name in ("mail_items", "chunks", "chunk_vectors")}
            retrieval = MailRetrieval(store, auth, encoder)
            retrieval.search(access, MailQuery(query="验收后如何结款"))  # Warm-up excluded explicitly.
            timings = {"hybrid_unfiltered": [], "hybrid_exact": []}
            for i in range(20):
                for kind, query in (
                    ("hybrid_unfiltered", MailQuery(query=f"项目验收后付款需要什么凭据 第{i + 1}项")),
                    ("hybrid_exact", MailQuery(query="验收付款凭据", exact_terms=[f"SC-{(i * 487) % count:05d}"])),
                ):
                    start = time.perf_counter()
                    page = retrieval.search(access, query)
                    timings[kind].append(time.perf_counter() - start)
                    assert page.matches and page.semantic_status == "ready"
                print(f"Query pairs {i + 1}/20", flush=True)
            result["query_seconds"] = {kind: {"samples": values, "p95": p95(values)} for kind, values in timings.items()}
            admission = MailStore(root / "admission", MailKnowledgeConfig())
            admission.initialize()
            result["durable_admission_seconds"] = {}
            for mib, samples in [(1, 30), (20, 5)]:
                times = []
                for _ in range(samples):
                    mail = envelope()
                    mail.body = "x" * (mib * 1024**2)
                    start = time.perf_counter()
                    receipt = admission.accept(mail)
                    times.append(time.perf_counter() - start)
                    assert receipt.durable and receipt.status == "accepted"
                result["durable_admission_seconds"][f"{mib}_MiB"] = {"samples": times, "p95": p95(times)}
            result["knowledge_database_bytes"] = store.path.stat().st_size
    finally:
        stop.set()
        sampler.join()
        result["sampled_process_peak_rss_bytes"] = peak[0]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mails": count, "peak_rss_bytes": peak[0], "query_p95": {
        kind: data["p95"] for kind, data in result["query_seconds"].items()}}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--mails", type=int, default=10000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.mails <= 10000:
        parser.error("mails must be between 10 and 10000")
    benchmark(args.model_path, args.mails, args.output)

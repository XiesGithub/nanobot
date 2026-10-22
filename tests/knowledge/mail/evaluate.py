"""Opt-in real local retrieval evaluation; no credentials or mailbox access.

Run in Conda nanobot:
  python -m tests.knowledge.mail.evaluate --model-path tmp/mail-rag-model --output PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import AccessContext, MailQuery, SourceRead
from nanobot.knowledge.mail.embeddings import LocalEncoder
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import MailStore
from tests.knowledge.mail.eval_corpus import VERSION, corpus
from tests.knowledge.mail.test_retrieval import ingest


def evaluate(model_path):
    import psutil
    import torch
    torch.set_num_threads(4)
    cold = time.perf_counter()
    encoder = LocalEncoder(str(model_path))
    cold = time.perf_counter() - cold
    mails, gold = corpus()
    with tempfile.TemporaryDirectory(prefix="nanobot-fictional-eval-") as directory:
        config = MailKnowledgeConfig(enabled=True)
        store = MailStore(Path(directory) / "mail", config)
        store.initialize()
        auth = MailAccess(store)
        auth.initialize_bindings([MailOwnerBinding(binding_id="eval", principal_id="fictional-evaluator", channel="cli")])
        access = AccessContext.from_security(auth.authenticate("cli", "fictional-evaluator", private_chat=True), "cli:synthetic-eval")
        identities = {}
        for mail in mails:
            identities[mail.source.uid] = ingest(store, mail).mail_id
        print(f"Admitted {len(mails)} fictional mails; encoding local chunks.", flush=True)
        begin = time.perf_counter()
        encoded = 0
        while count := encoder.index_pending(store):
            encoded += count
        index_seconds = time.perf_counter() - begin
        retrieval = MailRetrieval(store, auth, encoder)
        results, latencies, citation_checks = [], [], []
        with store.connect() as db:
            manifest = {r[0]: r[1] for r in db.execute("SELECT document_id,active_generation FROM documents WHERE active_generation>0")}
            revision = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0]
        for case in gold:
            with store.connect() as db:
                body_id = db.execute("SELECT document_id FROM documents WHERE mail_id=? AND kind='body'", (identities[case["uid"]],)).fetchone()[0]
                semantic = encoder.search(db, manifest, revision, case["question"], limit=8)
                hits = [dict(r) for r in db.execute("SELECT * FROM chunks WHERE chunk_id IN (SELECT value FROM json_each(?))", (json.dumps(semantic),))]
            begin = time.perf_counter()
            page = retrieval.search(access, MailQuery(query=case["question"], limit=8))
            latencies.append(time.perf_counter() - begin)
            for evidence in page.matches:
                source = retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id)).sources[0]
                with store.connect() as db:
                    reconstructed = []
                    for span in source.locator["spans"]:
                        block = db.execute("SELECT text,locator_json FROM document_blocks WHERE document_id=? AND generation=? AND ordinal=?", (source.document_id, source.generation, span["block_ordinal"])).fetchone()
                        reconstructed.append(block["text"][span["start"]:span["end"]])
                        assert json.loads(block["locator_json"]) == span["locator"]
                citation_checks.append("".join(reconstructed) == source.excerpt and source == evidence)
            results.append({**case, "gold_mail_id": identities[case["uid"]], "gold_document_id": body_id,
                            "semantic_only_hit_at_8": any(r["document_id"] == body_id and case["gold_text"] in r["text"] for r in hits),
                            "hybrid_hit_at_8": any(e.document_id == body_id and case["gold_text"] in e.excerpt for e in page.matches),
                            "returned": [{"mail_id": e.mail_id, "document_id": e.document_id, "evidence_id": e.evidence_id, "excerpt": e.excerpt, "locator": e.locator} for e in page.matches]})
        splits = {}
        for split in ("tuning", "held_out"):
            subset = [r for r in results if r["split"] == split]
            splits[split] = {"n": len(subset), "semantic_only_evidence_recall_at_8": statistics.mean(r["semantic_only_hit_at_8"] for r in subset),
                             "hybrid_evidence_recall_at_8": statistics.mean(r["hybrid_hit_at_8"] for r in subset),
                             "failed_ids": [r["id"] for r in subset if not r["hybrid_hit_at_8"]]}
        tail = retrieval.search(access, MailQuery(query="final installment", mail_ids=[identities[40]]))
        tail_found = any("123 yuan" in e.excerpt and any(s["locator"].get("page") == 36 for s in e.locator["spans"]) for e in tail.matches)
        with store.connect() as db:
            extraction_states = dict(db.execute("SELECT extract_status,COUNT(*) FROM documents GROUP BY extract_status"))
        return {"dataset_version": VERSION, "corpus_source_sha256": hashlib.sha256(Path(__file__).with_name("eval_corpus.py").read_bytes()).hexdigest(),
                "fictional_only": True, "label_authorship": "agent-authored; human review pending",
                "acceptance": "diagnostic run only; not production or human-reviewed quality acceptance",
                "mail_count": len(mails), "attachment_count": sum(len(m.parts) for m in mails), "chunk_count": encoded,
                "model": "BAAI/bge-small-zh-v1.5", "embedding_revision": encoder.revision,
                "environment": {"platform": platform.platform(), "python": platform.python_version(), "torch": version("torch"), "sentence_transformers": version("sentence-transformers"), "encoding_threads": 4},
                "cold_start_seconds": cold, "index_seconds": index_seconds,
                "small_corpus_hybrid_p95_seconds": sorted(latencies)[int(.95 * (len(latencies) - 1))],
                "observed_final_process_rss_bytes": psutil.Process().memory_info().rss,
                "source_checks": {"n": len(citation_checks), "correct": sum(citation_checks)},
                "long_pdf_tail_page_36_found": tail_found, "extraction_states": extraction_states,
                "splits": splits, "cases": results, "llm_evaluation": "not_run", "human_fact_support_review": "not_run",
                "target_scale_performance": "not_run (this corpus is smaller than 10k mails / 100k chunks)"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.model_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Repeat scale measurements on a previously generated fictional local fixture."""

import argparse
import hashlib
import json
import platform
import threading
import time
from pathlib import Path

from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import AccessContext, MailQuery
from nanobot.knowledge.mail.embeddings import LocalEncoder
from nanobot.knowledge.mail.retrieval import MailRetrieval
from nanobot.knowledge.mail.store import MailStore
from tests.knowledge.mail.benchmark_scale import p95
from tests.knowledge.mail.test_store import envelope


def run(fixture, model_path, output, *, concurrent_collection=False):
    import psutil
    import torch
    torch.set_num_threads(4)
    result = {"kind": "real_bge_fictional_scale_query_benchmark", "cpu_threads": 4,
        "platform": platform.platform(), "ram_bytes": psutil.virtual_memory().total,
        "fixture": "10 chunks per fictional mail; bulk loaded, all chunks actually BGE encoded",
        "not_measured": ["worker_readiness_latency", "chat_delay_during_ingestion", "parser_subprocess_peak"],
        "code_sha256": {name: hashlib.sha256(Path('nanobot/knowledge/mail', name).read_bytes()).hexdigest()
                        for name in ('retrieval.py', 'embeddings.py', 'store.py', 'versioning.py', 'ingestion.py')},
        "concurrent_collection": concurrent_collection}
    peak, stop = [0], threading.Event()

    def sample():
        process = psutil.Process()
        while not stop.wait(.05):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        store = MailStore(fixture, MailKnowledgeConfig(enabled=True))
        store.initialize()  # Only this explicit fictional fixture, not a product query client.
        auth = MailAccess(store)
        security = auth.authenticate('cli', 'fixture', private_chat=True)
        access = AccessContext.from_security(security, 'cli:scale-retest')
        start = time.perf_counter()
        encoder = LocalEncoder(str(model_path))
        result['cold_model_seconds'] = time.perf_counter() - start
        result['embedding_revision'] = encoder.revision
        with store.connect() as db:
            result['actual_counts'] = {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                                      for name in ('mail_items', 'chunks', 'chunk_vectors')}
        count = result['actual_counts']['mail_items']
        assert count == 10000 and result['actual_counts']['chunk_vectors'] == 100000
        retrieval = MailRetrieval(store, auth, encoder)
        start = time.perf_counter()
        retrieval.search(access, MailQuery(query='验收后如何结款'))
        result['first_query_seconds'] = time.perf_counter() - start
        timings = {'hybrid_unfiltered': [], 'hybrid_exact': []}
        collection_seconds = []
        for i in range(20):
            for kind, query in (
                ('hybrid_unfiltered', MailQuery(query=f'项目验收后付款需要什么凭据 第{i + 1}项')),
                ('hybrid_exact', MailQuery(query='验收付款凭据', exact_terms=[f'SC-{(i * 487) % count:05d}'])),
            ):
                if concurrent_collection:
                    from concurrent.futures import ThreadPoolExecutor

                    from nanobot.knowledge.mail.versioning import collect
                    acquired = threading.Event()

                    def collecting():
                        with store.lock:
                            acquired.set()
                            begin = time.perf_counter()
                            result = collect(store)
                            collection_seconds.append(time.perf_counter() - begin)
                            return result

                    # Force overlap with the periodic Worker's collection routine
                    # without inventing a synthetic lock-holding delay.
                    with ThreadPoolExecutor(1) as pool:
                        job = pool.submit(collecting)
                        assert acquired.wait(10)
                        start = time.perf_counter()
                        page = retrieval.search(access, query)
                        timings[kind].append(time.perf_counter() - start)
                        job.result()
                else:
                    start = time.perf_counter()
                    page = retrieval.search(access, query)
                    timings[kind].append(time.perf_counter() - start)
                assert page.matches and page.semantic_status == 'ready'
            result['query_seconds'] = {kind: {'samples': values, 'p95': p95(values)} for kind, values in timings.items()}
            if collection_seconds:
                result['collection_seconds'] = {'samples': collection_seconds, 'p95': p95(collection_seconds)}
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(f'Query pairs {i + 1}/20: {timings["hybrid_unfiltered"][-1]:.3f}s / {timings["hybrid_exact"][-1]:.3f}s', flush=True)
        import tempfile
        with tempfile.TemporaryDirectory(prefix='nanobot-admission-bench-') as directory:
            admission = MailStore(Path(directory), MailKnowledgeConfig())
            admission.initialize()
            result['durable_admission_seconds'] = {}
            for mib, samples in [(1, 30), (20, 5)]:
                values = []
                for _ in range(samples):
                    mail = envelope()
                    mail.body = 'x' * (mib * 1024**2)
                    start = time.perf_counter()
                    receipt = admission.accept(mail)
                    values.append(time.perf_counter() - start)
                    assert receipt.durable and receipt.status == 'accepted'
                result['durable_admission_seconds'][str(mib) + '_MiB'] = {'samples': values, 'p95': p95(values)}
    finally:
        stop.set()
        sampler.join()
        result['sampled_process_peak_rss_bytes'] = peak[0]
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({kind: data['p95'] for kind, data in result['query_seconds'].items()}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--model-path', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrent-collection', action='store_true')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run(args.fixture, args.model_path, args.output, concurrent_collection=args.concurrent_collection)

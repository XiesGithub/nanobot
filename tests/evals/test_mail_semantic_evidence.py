"""Actual local-encoder D proof plus synthetic rejection probes; no provider calls."""

import copy
import multiprocessing as mp
import uuid

import numpy as np
import pytest

from evals.common import ROOT, digest, read
from evals.mail_semantic_evidence import semantic_binding
from evals.mail_task_cases import load_cases
from evals.mail_task_semantic import build_plan
from evals.run import cleanup_instance, run_trial


def _events(evidence, kind):
    return [event for event in evidence["events"] if event["type"] == kind]


@pytest.fixture(scope="module")
def actual_encoder_trial():
    parent = ROOT / "tmp" / ("mail-encoder-proof-" + uuid.uuid4().hex)
    parent.mkdir(parents=True)
    case = next(row for row in load_cases() if row["case_id"] == "V085")
    directory = parent / "actual-D"
    (directory / "trials").mkdir(parents=True)
    isolation = parent / "instances"
    isolation.mkdir()
    plan = build_plan(case, "D")[0]
    try:
        result = run_trial(case, plan, directory, isolation, "D", mp.get_context("spawn"))
        assert result["status"] == "pass", result["reasons"]
        evidence = read(directory / "trials" / plan["trial_id"] / "artifacts/evidence.json")
        yield case, evidence
    finally:
        cleanup_instance(parent, ROOT / "tmp")


def test_actual_pinned_encoder_rank_and_gold_read(actual_encoder_trial):
    case, evidence = actual_encoder_trial
    proof = semantic_binding(case, evidence)
    assert all(proof["checks"].values()), proof
    assert proof["metrics"]["hit_at_8"] == 1
    assert proof["metrics"]["semantic_only_hit_at_8"] == 1
    assert proof["metrics"]["gold_mail_count"] == 1
    assert proof["metrics"]["rankings"]
    assert not _events(evidence, "model_attempt")  # D script, real local encoding.


@pytest.mark.parametrize("fault", ["not_ready", "missing_encoder", "space_mismatch",
                                    "dimension", "nonfinite", "unnormalized", "weights_pin"])
def test_unavailable_space_never_qualifies(actual_encoder_trial, fault):
    case, evidence = copy.deepcopy(actual_encoder_trial)
    for event in _events(evidence, "semantic_state"):
        payload = event["payload"]
        if fault == "not_ready":
            payload["health"]["semantic_status"] = "semantic_partial"
        elif fault == "missing_encoder":
            payload["encoder"]["available"] = False
        elif fault == "space_mismatch":
            payload["index_manifest"]["embedding_revision"] = "different-space"
        elif fault == "dimension":
            payload["vectors"][0]["dimension"] = 513
        elif fault in {"nonfinite", "unnormalized"}:
            value = np.zeros(512, dtype="<f4")
            value[0] = np.nan if fault == "nonfinite" else 2
            payload["vectors"][0].update(blob_hex=value.tobytes().hex(),
                                           blob_sha256=digest(value.tobytes()))
    if fault == "weights_pin":
        case["encoder_pin"]["files"]["model.safetensors"] = "0" * 64
    proof = semantic_binding(case, evidence)
    assert not proof["checks"]["semantic_environment"], proof


@pytest.mark.parametrize("fault,check", [
    ("no_document_encode", "actual_document_encoding"),
    ("no_query_encode", "actual_query_encoding"),
    ("rank_reorder", "rank_replay"),
    ("keyword_substitute", "hybrid_search_ready"),
    ("mandatory_synonym", "synonym_filter_safe"),
    ("missing_gold_read", "gold_source_read_and_cited"),
    ("missing_gold_citation", "gold_source_read_and_cited"),
    ("query_hash", "actual_query_encoding"),
])
def test_synthetic_tampering_is_rejected_not_capability_evidence(actual_encoder_trial, fault, check):
    case, evidence = copy.deepcopy(actual_encoder_trial)
    if fault in {"no_document_encode", "no_query_encode"}:
        role = "index" if fault == "no_document_encode" else "query"
        evidence["events"] = [e for e in evidence["events"]
                              if e["type"] != "semantic_encode" or e["payload"].get("role") != role]
    elif fault == "rank_reorder":
        rank = _events(evidence, "semantic_rank")[0]["payload"]
        rank["returned_chunk_ids"].reverse()
    elif fault == "keyword_substitute":
        for event in _events(evidence, "mail_search"):
            event["payload"]["request"]["mode"] = "keyword"
    elif fault == "mandatory_synonym":
        for event in _events(evidence, "mail_search"):
            event["payload"]["request"]["exact_terms"] = ["稳定性测试"]
    elif fault == "missing_gold_read":
        evidence["events"] = [e for e in evidence["events"] if e["type"] != "mail_read"]
    elif fault == "missing_gold_citation":
        for event in _events(evidence, "delivery"):
            event["payload"]["metadata"]["citations"] = []
    else:
        _events(evidence, "semantic_rank")[0]["payload"]["query_encode"]["text_sha256"] = "0" * 64
    proof = semantic_binding(case, evidence)
    assert not proof["checks"][check], proof
    assert not all(proof["checks"].values())

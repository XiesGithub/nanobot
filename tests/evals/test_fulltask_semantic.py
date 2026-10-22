"""Full-task binding controls use real D deliveries, never archived candidate answers."""

import asyncio
import copy
import shutil
import uuid

import pytest

from evals.common import ROOT, digest, read
from evals.fulltask_cases import load_cases
from evals.fulltask_semantic import aggregate, packet_for, result_valid, run
from evals.grading_transport import GradingClient
from evals.scorer import score
from evals.semantic import gold_packet, review_packet
from evals.semantic_run import _scripted_transport


@pytest.fixture(scope="module")
def fulltask_bundle():
    output = ROOT / "tmp" / ("fulltask-regression-" + uuid.uuid4().hex)
    result = asyncio.run(run("D", output))
    assert result["ok"] and result["counts"] == {"pass": 2}
    try:
        yield output
    finally:
        assert output.resolve().is_relative_to((ROOT / "tmp").resolve())
        shutil.rmtree(output)


def _inputs(output, case_id="V007"):
    case = next(item for item in load_cases() if item["case_id"] == case_id)
    trial = output / "trials" / (case_id + "-1")
    evidence = read(trial / "artifacts/evidence.json")
    packet = read(trial / "packet.json")
    return [case, evidence, read(trial / "verdicts/1.json"), packet,
            read(trial / "semantic-result.json"),
            read(output / "gold-reviews" / (gold_packet(packet)["packet_id"] + ".json")),
            read(output / "selfcheck.json"), "D", read(output / "grading-cost.json")]


def _remove_event_type(evidence, kind):
    evidence["events"] = [event for event in evidence["events"] if event["type"] != kind]
    sequences = {}
    for event in evidence["events"]:
        producer = event["producer_id"]
        sequences[producer] = sequences.get(producer, 0) + 1
        event["seq"] = sequences[producer]


@pytest.mark.parametrize("case_id", ["V005", "V007"])
def test_real_d_product_delivery_binds_candidate_without_formal_claim(
    fulltask_bundle, case_id, record_property
):
    values = _inputs(fulltask_bundle, case_id)
    verdict = aggregate(*values)
    assert verdict["status"] == "pass"
    assert verdict["scope"] == "full_task"
    assert verdict["formal_quality_eligible"] is False
    delivered = next(event["payload"]["response"] for event in values[1]["events"]
                     if event["type"] == "delivery")
    assert values[3]["answer"]["text"] == delivered
    assert verdict["binding"]["raw_delivery_sha256"] == digest(delivered.encode())
    assert verdict["binding"]["delivery_event_id"]
    record_property("binding", str(verdict["binding"]))
    if case_id == "V007":
        assert verdict["binding"]["read_event_ids"]
        assert verdict["binding"]["checks"]["source_read_met"]


def test_correct_guess_without_real_read_remains_hard_failure(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    _remove_event_type(values[1], "source_read")
    values[2] = score(values[0], values[1])
    assert values[4]["status"] == "pass"  # Factually correct answer cannot prove acquisition.
    assert aggregate(*values)["status"] == "product_fail"


def test_missing_delivery_cannot_pass_with_old_semantic_success(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    _remove_event_type(values[1], "delivery")
    values[2] = score(values[0], values[1])
    values[3] = packet_for(values[0], values[1])
    assert values[3]["answer"]["text"] == ""
    assert aggregate(*values)["status"] != "pass"


def test_rehashed_candidate_not_from_delivery_is_rejected(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    values[3]["answer"]["text"] = "100元"
    values[3]["answer"]["sha256"] = digest("100元".encode())
    assert aggregate(*values)["status"] == "invalid"


def test_modified_source_read_text_cannot_prove_retrieval(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    event = next(event for event in values[1]["events"] if event["type"] == "source_read")
    event["payload"]["result_text"] = "2| 预算：999元"
    event["payload"]["result_sha256"] = digest(event["payload"]["result_text"].encode())
    values[2] = score(values[0], values[1])
    assert aggregate(*values)["status"] == "product_fail"


def test_tampered_citation_does_not_pass_full_task_aggregation(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    values[4]["reviews"][0]["items"][0]["answer_evidence"][0]["quote"] = "altered"
    assert not result_valid(values[4], values[3], "judge")
    verdict = aggregate(*values)
    assert verdict["status"] == "invalid" and not verdict["formal_quality_eligible"]


def test_prompt_only_citation_does_not_prove_read_source_support(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    prompt = next(source for source in values[3]["sources"] if source["artifact_id"] == "V007.prompt")
    for review in values[4]["reviews"]:
        review["items"][0]["source_evidence"] = [{"artifact_id": prompt["artifact_id"], "sha256": prompt["sha256"],
                                                  "start": 0, "end": len(prompt["text"]), "quote": prompt["text"]}]
    assert result_valid(values[4], values[3], "judge")  # Syntactically valid but no acquired-source support.
    assert aggregate(*values)["status"] == "invalid"


def test_d_evidence_cannot_be_promoted_by_changing_only_mode(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    values[7] = "R"
    verdict = aggregate(*values)
    assert verdict["status"] != "pass" and not verdict["formal_quality_eligible"]


def test_wrong_unit_cannot_be_overturned_by_valid_judge_pass(fulltask_bundle, tmp_path):
    values = _inputs(fulltask_bundle)
    delivery = next(event for event in values[1]["events"] if event["type"] == "delivery")
    delivery["payload"]["response"] = "120美元"
    values[2] = score(values[0], values[1])
    values[3] = packet_for(values[0], values[1])
    labels = {values[3]["packet_id"]: {"V007.project_budget": "pass"}}

    async def judge():
        client = GradingClient(tmp_path, mode="D", transport=_scripted_transport(labels))
        try:
            return await review_packet(client, values[3], "judge")
        finally:
            await client.aclose()

    values[4] = asyncio.run(judge())
    assert result_valid(values[4], values[3], "judge") and values[4]["status"] == "pass"
    assert aggregate(*values)["status"] == "product_fail"


def test_gold_identity_is_frozen_independently_of_actual_candidate(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    changed = copy.deepcopy(values[3])
    changed["answer"]["text"] = "999元"
    changed["answer"]["sha256"] = digest("999元".encode())
    assert gold_packet(changed) == gold_packet(values[3])
    assert "answer" not in gold_packet(changed)


def test_budget_exhaustion_and_dispute_are_not_successful_reviews(fulltask_bundle):
    values = _inputs(fulltask_bundle)
    for reason in ("run_attempt_budget_exceeded", "parse_error"):
        failed = copy.deepcopy(values)
        failed[4].update(status="invalid", reviews=[], items=[], error=reason)
        assert aggregate(*failed)["status"] == "invalid"
    unresolved = copy.deepcopy(values)
    unresolved[4]["reviews"][1]["items"][0].update(label="unknown", objections=["dispute"])
    from evals.semantic import reconcile
    unresolved[4]["items"] = reconcile(unresolved[4]["reviews"], unresolved[3])
    unresolved[4]["status"] = "undetermined"
    assert aggregate(*unresolved)["status"] == "undetermined"

"""Adversarial tests for frozen semantic evidence and bounded review."""

import copy

import pytest

from evals.common import canonical, digest
from evals.semantic import (
    REVIEW_SCHEMA,
    document,
    finalize,
    gold_packet,
    review_packet,
    validate_packet,
    validate_review,
)

SOURCE_TEXT = "政策：应启用安全模式。"
ANSWER_TEXT = "🧪结论：安全模式已启用。附注：无。"
ANSWER_START = ANSWER_TEXT.index("安全模式")
ANSWER_END = ANSWER_TEXT.index("。", ANSWER_START) + 1


def make_packet(answer_text=ANSWER_TEXT, answer_id="candidate-1", sources=None):
    sources = sources or [document("policy", SOURCE_TEXT)]
    return validate_packet({
        "packet_id": "packet-1",
        "case_id": "V001",
        "scope": "archived_semantic",
        "question": "根据政策说明安全模式要求。",
        "rubric_version": "rubric-1",
        "gold_version": "gold-1",
        "sources": sources,
        "answer": document(answer_id, answer_text),
        "requirements": [{
            "requirement_id": "r1",
            "statement": "指出必须启用安全模式。",
            "gold": "必须启用安全模式。",
            "source_ids": [sources[0]["artifact_id"]],
            "answer_range": [ANSWER_START, ANSWER_END],
        }],
    })


def citation(doc, start=0, end=None):
    end = len(doc["text"]) if end is None else end
    return {"artifact_id": doc["artifact_id"], "sha256": doc["sha256"],
            "start": start, "end": end, "quote": doc["text"][start:end]}


def make_review(packet, role, stage, review_id, label="pass", *,
                source_evidence=None, answer_evidence=None, objections=None,
                missing_requirement=False, resolves=None, schema=REVIEW_SCHEMA):
    req = packet["requirements"][0]
    source = next(d for d in packet["sources"] if d["artifact_id"] == req["source_ids"][0])
    source_evidence = source_evidence if source_evidence is not None else [citation(source)]
    if stage == "gold":
        target = next(d for d in packet["gold_documents"]
                      if d["artifact_id"] == "gold:" + req["requirement_id"])
        evidence_key = "gold_evidence"
        evidence = [citation(target)] if answer_evidence is None else answer_evidence
    else:
        target = packet["answer"]
        evidence_key = "answer_evidence"
        if answer_evidence is None:
            evidence = [citation(target, *req.get("answer_range", [0, len(target["text"])]))]
        else:
            evidence = answer_evidence
    item = {
        "requirement_id": req["requirement_id"],
        "label": label,
        "source_evidence": source_evidence,
        evidence_key: evidence,
        "rationale": "引用内容与条款相符。",
        "objections": list(objections or []),
        "missing_requirement": missing_requirement,
        "resolves": list(resolves or []),
    }
    return {"schema_version": schema, "review_id": review_id, "role": role,
            "packet_sha256": digest(canonical(packet)), "items": [item]}


class FakeReviewClient:
    """Fake transport that always invokes the production validator."""

    def __init__(self, decisions, *, malformed_role=None):
        self.decisions = decisions
        self.malformed_role = malformed_role
        self.calls = []
        self.initial_contexts = []

    async def review(self, packet, role, stage, validator, *, prior_reviews=None):
        call_number = len(self.calls) + 1
        review_id = f"{stage}-{role}-{call_number}"
        self.calls.append({"role": role, "prior_reviews": prior_reviews})
        if role in ("support_check", "counter_check"):
            # Each independent first pass gets a fresh fake context.
            self.initial_contexts.append(object())
        if role == self.malformed_role:
            bad = make_review(packet, role, stage, review_id, schema="wrong-schema")
            validator(bad, packet, role, stage, review_id)
        choice = self.decisions[role]
        choice = choice(packet, stage, prior_reviews or [], review_id) if callable(choice) else choice
        resolutions = []
        if role == "recheck" and choice.get("resolve", True):
            resolutions = [{"review_id": prior["review_id"], "requirement_id": "r1",
                            "rationale": "已检查并回应该意见。"}
                           for prior in prior_reviews]
        review = make_review(
            packet, role, stage, review_id, choice.get("label", "pass"),
            objections=choice.get("objections"),
            missing_requirement=choice.get("missing_requirement", False),
            resolves=choice.get("resolves", resolutions),
            answer_evidence=choice.get("answer_evidence"),
        )
        validator(review, packet, role, stage, review_id)
        return review


async def valid_finalization_results(packet):
    gold = await review_packet(FakeReviewClient({
        "support_check": {"label": "pass"}, "counter_check": {"label": "pass"},
    }), gold_packet(packet), "gold")
    semantic = await review_packet(FakeReviewClient({
        "support_check": {"label": "pass"}, "counter_check": {"label": "pass"},
    }), packet, "judge")
    return semantic, gold


def test_unicode_citations_use_exact_hash_and_python_character_offsets():
    packet = make_packet()
    review = make_review(packet, "support_check", "answer", "review-1")
    item = review["items"][0]
    ref = item["answer_evidence"][0]
    assert ANSWER_TEXT[ref["start"]:ref["end"]] == "安全模式已启用。"
    assert ref["start"] == ANSWER_START
    assert validate_review(review, packet, "support_check", "answer", "review-1") is review


@pytest.mark.parametrize("mutation,reason", [
    ("wrong_hash", "reference_quote_hash"),
    ("wrong_quote", "reference_quote_hash"),
    ("outside_answer_range", "reference_wrong_answer_section"),
])
def test_tampered_or_out_of_section_citations_are_rejected(mutation, reason):
    packet = make_packet()
    review = make_review(packet, "support_check", "answer", "review-1")
    ref = review["items"][0]["answer_evidence"][0]
    if mutation == "wrong_hash":
        ref["sha256"] = "0" * 64
    elif mutation == "wrong_quote":
        ref["quote"] = "已修改的引用"
    else:
        start, end = 0, 1
        ref.update(start=start, end=end, quote=packet["answer"]["text"][start:end])
    with pytest.raises(ValueError, match=reason):
        validate_review(review, packet, "support_check", "answer", "review-1")


def test_requirement_cannot_cite_another_requirements_source():
    packet = make_packet(sources=[document("policy", SOURCE_TEXT),
                                  document("unrelated", "不同条款。")])
    review = make_review(packet, "support_check", "answer", "review-1",
                         source_evidence=[citation(packet["sources"][1])])
    with pytest.raises(ValueError, match="reference_artifact"):
        validate_review(review, packet, "support_check", "answer", "review-1")


def test_missing_requirement_requires_citation_of_the_entire_answer_section():
    packet = make_packet()
    partial_end = ANSWER_END - 1
    review = make_review(
        packet, "support_check", "answer", "review-1", label="fail",
        missing_requirement=True,
        answer_evidence=[citation(packet["answer"], ANSWER_START, partial_end)],
    )
    with pytest.raises(ValueError, match="omission_needs_complete_answer"):
        validate_review(review, packet, "support_check", "answer", "review-1")
    full = make_review(packet, "support_check", "answer", "review-2", label="fail",
                       missing_requirement=True,
                       answer_evidence=[citation(packet["answer"], ANSWER_START, ANSWER_END)])
    assert validate_review(full, packet, "support_check", "answer", "review-2") is full


def test_gold_identity_excludes_candidate_and_is_stable_across_answers():
    left = make_packet(ANSWER_TEXT.replace("已启用", "已关闭"), "answer-a")
    right = make_packet(ANSWER_TEXT.replace("已启用", "未启用"), "answer-b")
    gold_left, gold_right = gold_packet(left), gold_packet(right)
    assert gold_left["packet_id"] == gold_right["packet_id"]
    assert canonical(gold_left) == canonical(gold_right)
    assert digest(canonical(gold_left)) == digest(canonical(gold_right))
    assert "answer" not in gold_left
    assert "answer-a" not in canonical(gold_left).decode()
    assert "answer-b" not in canonical(gold_right).decode()


@pytest.mark.asyncio
async def test_two_fresh_unknown_initial_reviews_trigger_only_one_recheck():
    packet = make_packet()
    client = FakeReviewClient({"support_check": {"label": "unknown"},
                               "counter_check": {"label": "pass"},
                               "recheck": {"label": "pass"}})
    result = await review_packet(client, packet, "answer")
    assert [call["role"] for call in client.calls] == [
        "support_check", "counter_check", "recheck"]
    assert len(client.initial_contexts) == 2
    assert client.initial_contexts[0] is not client.initial_contexts[1]
    assert client.calls[0]["prior_reviews"] is None
    assert client.calls[1]["prior_reviews"] is None
    assert len(client.calls[2]["prior_reviews"]) == 2
    assert result["status"] == "pass"
    assert result["items"][0]["rationale"] == "resolved_with_initial_support"


@pytest.mark.asyncio
async def test_agreeing_initial_reviews_stop_without_an_extra_semantic_round():
    packet = make_packet()
    client = FakeReviewClient({"support_check": {"label": "pass"},
                               "counter_check": {"label": "pass"},
                               "recheck": {"label": "fail"}})
    result = await review_packet(client, packet, "answer")
    assert [call["role"] for call in client.calls] == ["support_check", "counter_check"]
    assert result["status"] == "pass"


@pytest.mark.asyncio
async def test_recheck_cannot_create_pass_when_both_initial_reviews_are_unknown():
    packet = make_packet()
    client = FakeReviewClient({"support_check": {"label": "unknown"},
                               "counter_check": {"label": "unknown"},
                               "recheck": {"label": "pass"}})
    result = await review_packet(client, packet, "answer")
    assert len(client.calls) == 3
    assert result["status"] == "undetermined"
    assert result["items"][0]["label"] == "unknown"


@pytest.mark.asyncio
async def test_disagreement_converges_only_with_both_resolutions_and_initial_support():
    packet = make_packet()
    client = FakeReviewClient({"support_check": {"label": "pass"},
                               "counter_check": {"label": "fail"},
                               "recheck": {"label": "pass"}})
    result = await review_packet(client, packet, "answer")
    assert result["status"] == "pass"
    assert result["items"][0]["label"] == "pass"
    assert result["items"][0]["evidence_review_id"] == result["reviews"][2]["review_id"]


@pytest.mark.parametrize("recheck", [
    {"label": "pass", "resolve": False},
    {"label": "pass", "objections": ["引用仍不足。"]},
])
@pytest.mark.asyncio
async def test_recheck_without_complete_resolution_or_with_objection_stays_undetermined(recheck):
    packet = make_packet()
    client = FakeReviewClient({"support_check": {"label": "pass"},
                               "counter_check": {"label": "fail"}, "recheck": recheck})
    result = await review_packet(client, packet, "answer")
    assert result["status"] == "undetermined"
    assert result["items"][0]["label"] == "unknown"
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_gold_dispute_is_unresolved_and_invalid_schema_is_invalid():
    packet = gold_packet(make_packet())
    disputed = FakeReviewClient({"support_check": {"label": "unknown"},
                                 "counter_check": {"label": "pass"},
                                 "recheck": {"label": "unknown"}})
    result = await review_packet(disputed, packet, "gold")
    assert result["status"] == "unresolved"
    assert len(disputed.calls) == 3

    invalid = FakeReviewClient({"support_check": {"label": "pass"}},
                               malformed_role="support_check")
    invalid_result = await review_packet(invalid, packet, "gold")
    assert invalid_result["status"] == "invalid"
    assert invalid_result["error"] == "ValueError"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_requirement", "extra_item_field"])
async def test_incomplete_or_illegal_review_schema_becomes_invalid(damage):
    packet = make_packet()

    class DamagedClient:
        async def review(self, review_packet_arg, role, stage, validator, *, prior_reviews=None):
            review = make_review(review_packet_arg, role, stage, f"bad-{role}")
            if damage == "missing_requirement":
                review["items"] = []
            else:
                review["items"][0]["unexpected"] = True
            validator(review, review_packet_arg, role, stage, review["review_id"])
            return review

    result = await review_packet(DamagedClient(), packet, "answer")
    assert result["status"] == "invalid"
    assert result["error"] == "ValueError"


@pytest.mark.asyncio
async def test_program_hard_failure_cannot_be_overturned_and_diagnostics_are_preserved():
    packet = make_packet()
    program = {"status": "product_fail", "requirements": [{
        "requirement_id": "program-json-shape", "label": "fail",
        "diagnostic": "answer_not_strict_json"}], "reasons": ["wrong_answer"]}
    original = copy.deepcopy(program)
    semantic, gold = await valid_finalization_results(packet)
    result = finalize(program, semantic, gold, True, packet)
    assert result["status"] == "product_fail"
    assert result["program_verdict"] == original
    assert program == original


@pytest.mark.asyncio
async def test_semantic_layer_failure_retains_program_diagnosis_and_cannot_create_pass():
    packet = make_packet()
    program = {"status": "undetermined", "requirements": [{
        "requirement_id": "usage", "label": "unknown", "diagnostic": "usage_unknown"}]}
    original = copy.deepcopy(program)
    semantic, gold = await valid_finalization_results(packet)
    semantic.update(status="invalid", items=[], error="ValueError")
    result = finalize(program, semantic, gold, True, packet)
    assert result["status"] == "invalid"
    assert result["program_verdict"] == original
    assert program == original
    assert not result["grading_valid"]


@pytest.mark.asyncio
async def test_failed_judge_selfcheck_makes_semantic_grading_invalid():
    packet = make_packet()
    program = {"status": "pass", "diagnostic": "program_checks_clean"}
    semantic, gold = await valid_finalization_results(packet)
    result = finalize(program, semantic, gold, False, packet)
    assert result["status"] == "invalid"
    assert not result["grading_valid"]
    assert not result["formal_quality_eligible"]
    assert result["program_verdict"] == program


@pytest.mark.asyncio
async def test_bound_archive_reviews_can_pass_without_full_task_eligibility():
    packet = make_packet()
    semantic, gold = await valid_finalization_results(packet)
    result = finalize({"status": "pass", "requirements": []}, semantic, gold, True, packet)
    assert result["status"] == "pass"
    assert result["grading_valid"]
    assert result["scope"] == "archived_semantic"
    assert not result["formal_quality_eligible"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["candidate_packet_hash", "candidate_stage",
                                    "gold_packet_hash", "gold_stage", "missing_item"])
async def test_finalizer_rejects_unbound_or_incomplete_review_results(tamper):
    packet = make_packet()
    semantic, gold = await valid_finalization_results(packet)
    if tamper == "candidate_packet_hash":
        other_packet = make_packet(ANSWER_TEXT.replace("附注：无", "附注：有"))
        semantic["packet_sha256"] = digest(canonical(other_packet))
    elif tamper == "candidate_stage":
        semantic["stage"] = "gold"
    elif tamper == "gold_packet_hash":
        gold["packet_sha256"] = "0" * 64
    elif tamper == "gold_stage":
        gold["stage"] = "judge"
    else:
        semantic["items"] = []
    result = finalize({"status": "pass", "requirements": []}, semantic, gold, True, packet)
    assert result["status"] != "pass"
    assert not result["grading_valid"]


def test_fixed_archive_packets_reject_full_task_scope():
    packet = make_packet()
    packet["scope"] = "full_task"
    with pytest.raises(ValueError, match="scope"):
        validate_packet(packet)

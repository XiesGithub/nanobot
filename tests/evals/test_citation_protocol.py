"""Strict catalog-ID transport and exact Unicode reference mapping tests."""

import copy
import json

import pytest

from evals.citation_protocol import (
    PROTOCOL,
    CitationProtocolError,
    build_catalog,
    prepare_catalog,
    resolve_review,
    verify_catalog_metadata,
)
from evals.common import canonical, digest, strict_json
from evals.grading_transport import GradingClient
from evals.semantic import document, validate_review


def packet(*, answer_text="🧪 e\u0301 é finished.", answer_range=None):
    answer = document("answer", answer_text)
    req = {"requirement_id": "r1", "statement": "报告完成状态。", "gold": "完成。",
           "source_ids": ["policy"]}
    if answer_range is not None:
        req["answer_range"] = answer_range
    return {"packet_id": "catalog-test", "case_id": "V094", "scope": "archived_semantic",
            "question": "状态是什么？", "rubric_version": "rubric-1",
            "gold_version": "gold-1", "sources": [document("policy", "状态：完成。")],
            "answer": answer, "requirements": [req]}


def wire_review(packet_value, stage="judge", role="support_check", review_id="review-1",
                *, source_id=None, evidence_id=None, label="pass", missing=False):
    catalog = build_catalog(packet_value, stage)
    source_id = source_id or next(x["citation_id"] for x in catalog if x["kind"] == "source")
    if evidence_id is None:
        evidence_id = next(x["citation_id"] for x in catalog
                           if x["kind"] == ("gold" if stage == "gold" else "answer_range"))
    evidence_key = "gold_evidence" if stage == "gold" else "answer_evidence"
    item = {"requirement_id": "r1", "label": label,
            "source_evidence": [{"citation_id": source_id}],
            evidence_key: [{"citation_id": evidence_id}],
            "rationale": "证据支持该判断。", "objections": [],
            "missing_requirement": missing, "resolves": []}
    return {"schema_version": "semantic-review-catalog/1.0", "review_id": review_id,
            "role": role, "packet_sha256": digest(canonical(packet_value)), "items": [item]}


class Script:
    def __init__(self):
        self.requests = []

    async def __call__(self, **request):
        self.requests.append(request)
        payload = strict_json(request["messages"][1]["content"])
        catalog = payload["citation_catalog"]
        source_id = next(x["citation_id"] for x in catalog if x["kind"] == "source")
        evidence_kind = "answer_range" if any(x["kind"] == "answer_range" for x in catalog) else "answer"
        evidence_id = next(x["citation_id"] for x in catalog if x["kind"] == evidence_kind)
        response = {"schema_version": "semantic-review-catalog/1.0",
                    "review_id": payload["review_id"], "role": payload["role"],
                    "packet_sha256": payload["packet_sha256"],
                    "items": [{"requirement_id": "r1", "label": "pass",
                               "source_evidence": [{"citation_id": source_id}],
                               "answer_evidence": [{"citation_id": evidence_id}],
                               "rationale": "原文直接支持。", "objections": [],
                               "missing_requirement": False, "resolves": []}]}
        return {"choices": [{"message": {"content": json.dumps(response, ensure_ascii=False)}}]}


@pytest.mark.asyncio
async def test_client_resolves_catalog_ids_and_records_recomputable_wire_and_prompt(tmp_path):
    script = Script()
    client = GradingClient(tmp_path, mode="D", transport=script,
                           citation_protocol=PROTOCOL)
    candidate = packet(answer_range=[2, 6])
    try:
        resolved = await client.review(candidate, "support_check", "judge", validate_review)
        assert resolved["schema_version"] == "semantic-review/1.0"
        reference = resolved["items"][0]["answer_evidence"][0]
        assert reference["start"] == 2 and reference["end"] == 6
        assert reference["quote"] == candidate["answer"]["text"][2:6] == "e\u0301 é"
        payload = strict_json(script.requests[0]["messages"][1]["content"])
        assert payload["citation_protocol"] == PROTOCOL
        assert '"citation_id"' in script.requests[0]["messages"][0]["content"]
        rows = [strict_json(line) for line in (tmp_path / "calls.jsonl").read_text(
            encoding="utf-8").splitlines()]
        row = rows[0]
        metadata = row["citation_protocol_metadata"]
        assert verify_catalog_metadata(metadata)
        assert metadata["packet_sha256"] == digest(canonical(candidate))
        assert metadata["stage"] == "judge"
        assert row["prompt"]["version"] == "semantic-review-catalog-prompt/1.0"
        assert row["rendered_prompt_sha256"] == row["prompt"]["rendered_sha256_by_stage"]["judge"]
        assert row["raw_wire_review"]["items"][0]["answer_evidence"] == [
            {"citation_id": reference_id} for reference_id in
            [row["raw_wire_review"]["items"][0]["answer_evidence"][0]["citation_id"]]]
        assert row["resolved_review"] == row["review"] == resolved
    finally:
        await client.aclose()


def test_ids_bind_exact_python_unicode_spans_and_stage_packet_hashes():
    candidate = packet(answer_range=[2, 6])
    catalog, metadata = prepare_catalog(candidate, "judge")
    assert verify_catalog_metadata(metadata)
    answer_range = next(x for x in catalog if x["kind"] == "answer_range")
    assert answer_range["reference"]["quote"] == "e\u0301 é"
    assert answer_range["reference"]["start"] == 2  # Python code points, not UTF-16 units.
    assert answer_range["reference"]["quote"] != "é é"  # No NFC/NFD correction.
    nfc_packet = packet(answer_text="🧪 é finished.", answer_range=[2, 6])
    nfd_packet = packet(answer_text="🧪 e\u0301 finished.", answer_range=[2, 6])
    nfc = build_catalog(nfc_packet, "judge")
    nfd = build_catalog(nfd_packet, "judge")
    assert next(x for x in nfc if x["kind"] == "answer_range")["citation_id"] != \
        next(x for x in nfd if x["kind"] == "answer_range")["citation_id"]
    crlf_document = document("crlf", "A\r\nB")
    assert crlf_document["text"] == "A\nB" and crlf_document["sha256"] == \
        document("other", "A\nB")["sha256"]
    gold_catalog = build_catalog(candidate, "gold")
    assert not {x["citation_id"] for x in catalog} & {x["citation_id"] for x in gold_catalog}


def test_old_inline_citations_are_not_repaired_or_accepted_as_catalog_ids():
    candidate = packet(answer_range=[2, 6])
    catalog = build_catalog(candidate, "judge")
    source_id = next(x["citation_id"] for x in catalog if x["kind"] == "source")
    answer_id = next(x["citation_id"] for x in catalog if x["kind"] == "answer_range")
    rid = digest(canonical(candidate))
    raw = wire_review(candidate, source_id=source_id, evidence_id=answer_id)
    raw["items"][0]["answer_evidence"] = [{"citation_id": answer_id,
                                            "start": 2, "end": 7, "quote": "wrong"}]
    with pytest.raises(CitationProtocolError, match="citation_wire_reference_schema"):
        resolve_review(raw, candidate, "support_check", "judge", "review-1")
    raw["items"][0]["answer_evidence"] = [{"citation_id": "cite_from_other_packet"}]
    with pytest.raises(CitationProtocolError, match="citation_unknown_id"):
        resolve_review(raw, candidate, "support_check", "judge", "review-1")
    altered = copy.deepcopy(raw)
    altered["items"][0]["answer_evidence"] = [{"citation_id": answer_id}]
    with pytest.raises(CitationProtocolError, match="citation_unknown_id"):
        resolve_review(altered, candidate, "support_check", "selfcheck", "review-1")
    assert rid == raw["packet_sha256"]


def test_invalid_catalog_hash_range_and_gold_ownership_are_rejected():
    candidate = packet(answer_range=[3, 8])
    catalog, metadata = prepare_catalog(candidate, "judge")
    bad_metadata = copy.deepcopy(metadata)
    bad_metadata["catalog"][0]["reference"]["quote"] += "x"
    assert not verify_catalog_metadata(bad_metadata)
    with pytest.raises(CitationProtocolError, match="citation_catalog_integrity"):
        resolve_review(wire_review(candidate), candidate, "support_check", "judge", "review-1",
                       catalog, bad_metadata)

    invalid = packet(answer_range=[3, len(candidate["answer"]["text"]) + 1])
    with pytest.raises(CitationProtocolError, match="citation_catalog_invalid_range"):
        build_catalog(invalid, "judge")

    gold_packet = {key: value for key, value in candidate.items() if key != "answer"}
    gold_packet["gold_documents"] = [document("gold:r1", "完成。"),
                                     document("gold:r2", "未完成。")]
    gold_packet["requirements"].append({"requirement_id": "r2", "statement": "报告另一状态。",
                                        "gold": "未完成。", "source_ids": ["policy"]})
    gold_catalog = build_catalog(gold_packet, "gold")
    wrong_gold = next(x for x in gold_catalog if x["reference"]["artifact_id"] == "gold:r2")
    source_id = next(x["citation_id"] for x in gold_catalog if x["kind"] == "source")
    gold2 = next(x for x in gold_catalog if x["reference"]["artifact_id"] == "gold:r2")
    raw = {"schema_version": "semantic-review-catalog/1.0", "review_id": "review-1",
           "role": "support_check", "packet_sha256": digest(canonical(gold_packet)),
           "items": [{"requirement_id": "r1", "label": "pass",
                      "source_evidence": [{"citation_id": source_id}],
                      "gold_evidence": [{"citation_id": wrong_gold["citation_id"]}],
                      "rationale": "直接支持。", "objections": [],
                      "missing_requirement": False, "resolves": []},
                     {"requirement_id": "r2", "label": "pass",
                      "source_evidence": [{"citation_id": source_id}],
                      "gold_evidence": [{"citation_id": gold2["citation_id"]}],
                      "rationale": "直接支持。", "objections": [],
                      "missing_requirement": False, "resolves": []}]}
    resolved = resolve_review(raw, gold_packet, "support_check", "gold", "review-1")
    with pytest.raises(ValueError, match="reference_artifact"):
        validate_review(resolved, gold_packet, "support_check", "gold", "review-1")


def test_missing_requirement_requires_complete_answer_reference():
    candidate = packet(answer_text="全部回答内容。")
    catalog = build_catalog(candidate, "judge")
    source_id = next(x["citation_id"] for x in catalog if x["kind"] == "source")
    answer_id = next(x["citation_id"] for x in catalog if x["kind"] == "answer")
    raw = wire_review(candidate, source_id=source_id, evidence_id=answer_id,
                      label="fail", missing=True)
    # With no answer_range, the sole answer member is the full exact answer.
    resolved = resolve_review(raw, candidate, "support_check", "judge", "review-1")
    assert resolved["items"][0]["answer_evidence"][0]["quote"] == "全部回答内容。"
    assert validate_review(resolved, candidate, "support_check", "judge", "review-1") is resolved

    ranged = packet(answer_text="前缀。全部回答内容。", answer_range=[3, 10])
    ranged_catalog = build_catalog(ranged, "judge")
    ranged_source_id = next(x["citation_id"] for x in ranged_catalog if x["kind"] == "source")
    ranged_evidence_id = next(x["citation_id"] for x in ranged_catalog if x["kind"] == "answer_range")
    ranged_raw = wire_review(ranged, source_id=ranged_source_id,
                             evidence_id=ranged_evidence_id, label="fail", missing=True)
    ranged_resolved = resolve_review(ranged_raw, ranged, "support_check", "judge", "review-1")
    assert validate_review(ranged_resolved, ranged, "support_check", "judge", "review-1") is ranged_resolved

    # A full-answer citation cannot be substituted where the rubric fixes a narrower range.
    ranged_full_answer = next(x for x in ranged_catalog if x["kind"] == "answer")
    ranged_raw["items"][0]["answer_evidence"] = [
        {"citation_id": ranged_full_answer["citation_id"]}]
    ranged_resolved = resolve_review(ranged_raw, ranged, "support_check", "judge", "review-1")
    with pytest.raises(ValueError, match="reference_wrong_answer_section"):
        validate_review(ranged_resolved, ranged, "support_check", "judge", "review-1")

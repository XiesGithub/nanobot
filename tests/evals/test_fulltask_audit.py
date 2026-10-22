import copy
import json

import pytest

from evals.common import canonical, digest
from evals.fulltask_audit import (
    _check_index,
    _verify_grading_ledger,
    _verify_logical_attempts,
    _verify_review_receipts,
    _verify_selfcheck,
)
from evals.fulltask_cases import templates
from evals.fulltask_semantic import _scripted_transport
from evals.grading_transport import GradingClient, prompt_metadata
from evals.semantic import document, review_packet, validate_packet, validate_review
from evals.semantic_run import judge_selfcheck
from evals.semantic_samples import selfcheck_packet


def _rewrite_jsonl(path, rows):
    path.write_bytes(b"".join(canonical(row) + b"\n" for row in rows))


def test_artifact_index_detects_an_unindexed_file_even_if_it_is_rehashed(tmp_path):
    (tmp_path / "evidence.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "artifact-index.json").write_text(json.dumps([
        {"path": "evidence.json", "sha256": digest((tmp_path / "evidence.json").read_bytes())},
    ]), encoding="utf-8")
    errors = []
    _check_index(tmp_path, "artifact-index.json", errors, "root", exact_inventory=True)
    assert errors == []

    (tmp_path / "forged-verdict.json").write_text('{"status":"pass"}\n', encoding="utf-8")
    errors = []
    _check_index(tmp_path, "artifact-index.json", errors, "root", exact_inventory=True)
    assert "root:inventory" in errors

    # Adding the forged file to a newly hashed index still cannot hide that the
    # original evidence identity changed without a corresponding trusted replay.
    rows = [
        {"path": "evidence.json", "sha256": "0" * 64},
        {"path": "forged-verdict.json", "sha256": digest(
            (tmp_path / "forged-verdict.json").read_bytes())},
    ]
    (tmp_path / "artifact-index.json").write_text(json.dumps(rows), encoding="utf-8")
    errors = []
    _check_index(tmp_path, "artifact-index.json", errors, "root", exact_inventory=True)
    assert "root:hash:evidence.json" in errors


async def _one_deterministic_grading_call(grading_dir, packet, labels, stage, role):
    client = GradingClient(grading_dir, mode="D", transport=_scripted_transport(labels))
    try:
        await client.review(packet, role, stage, validate_review)
        summary = client.summary()
    finally:
        await client.aclose()
    return summary


@pytest.mark.asyncio
async def test_grading_ledger_recomputes_attempts_reservations_and_sdk_retry_boundary(tmp_path):
    grading_dir = tmp_path / "grading"
    grading_dir.mkdir()
    packet, labels, _ = selfcheck_packet()
    packet = {key: value for key, value in packet.items() if key != "hard_checks"}
    summary = await _one_deterministic_grading_call(
        grading_dir, packet, {packet["packet_id"]: labels}, "selfcheck", "support_check")
    (tmp_path / "grading-cost.json").write_text(json.dumps(summary), encoding="utf-8")
    manifest = {"mode": "D", "requested_model": None, "prompt": prompt_metadata(),
                "budget": {"grading": summary["limits"]}}
    errors = []
    rows, replayed = _verify_grading_ledger(tmp_path, manifest, errors)
    assert len(rows) == replayed["attempts"] == 1
    assert replayed["reserved_tokens"] == rows[0]["reserved_tokens"]
    assert errors == []

    rows[0]["sdk_max_retries"] = 1
    _rewrite_jsonl(grading_dir / "calls.jsonl", rows)
    errors = []
    _verify_grading_ledger(tmp_path, manifest, errors)
    assert "grading_sdk_retry_enabled" in errors


@pytest.mark.asyncio
async def test_logical_attempt_ledger_rejects_success_retry_and_role_reuse(tmp_path):
    grading_dir = tmp_path / "grading"
    grading_dir.mkdir()
    packet, labels, _ = selfcheck_packet()
    packet = {key: value for key, value in packet.items() if key != "hard_checks"}
    await _one_deterministic_grading_call(
        grading_dir, packet, {packet["packet_id"]: labels}, "selfcheck", "support_check")
    rows = [json.loads(line) for line in (grading_dir / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]

    retry_after_success = copy.deepcopy(rows)
    second = copy.deepcopy(retry_after_success[0])
    second.update(attempt_id="grading-attempt-002", attempt_in_call=2)
    retry_after_success.append(second)
    errors = []
    _verify_logical_attempts(retry_after_success, errors, {})
    assert "grading_retry_after_success" in errors

    role_reuse = copy.deepcopy(rows)
    second = copy.deepcopy(role_reuse[0])
    second.update(attempt_id="grading-attempt-002", review_id="different-review",
                  logical_review_id="different-review", role="support_check")
    role_reuse.append(second)
    errors = []
    _verify_logical_attempts(role_reuse, errors, {})
    assert "grading_duplicate_logical_role" in errors


@pytest.mark.asyncio
async def test_selfcheck_labels_are_replayed_from_two_logged_reviews(tmp_path):
    output = tmp_path
    grading_dir = output / "grading"
    grading_dir.mkdir()
    packet, labels, anchors = selfcheck_packet()
    packet = {key: value for key, value in packet.items() if key != "hard_checks"}
    client = GradingClient(grading_dir, mode="D",
                           transport=_scripted_transport({packet["packet_id"]: labels}))
    try:
        saved = await judge_selfcheck(client, packet, labels)
    finally:
        await client.aclose()
    (output / "selfcheck.json").write_text(json.dumps(saved), encoding="utf-8")
    calls = [json.loads(line) for line in (grading_dir / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]
    manifest = {"selfcheck": {"packet": packet, "labels": labels, "anchors": anchors}}
    errors = []
    replayed = _verify_selfcheck(output, manifest, calls, errors)
    assert replayed["ok"] is True
    assert errors == []

    calls.pop()
    errors = []
    _verify_selfcheck(output, manifest, calls, errors)
    assert "selfcheck_review_not_from_calls" in errors


@pytest.mark.asyncio
async def test_accepted_candidate_reviews_must_be_in_calls_and_citations_revalidate(tmp_path):
    packet = copy.deepcopy(templates()["V007"])
    packet["packet_id"] = "V007-1"
    packet["requirements"][0]["source_ids"] = ["V007.note.txt", "V007.prompt"]
    packet["answer"] = document("answer", "120元")
    validate_packet(packet)
    labels = {packet["packet_id"]: {"V007.project_budget": "pass"}}
    grading_dir = tmp_path / "grading"
    grading_dir.mkdir()
    client = GradingClient(grading_dir, mode="D", transport=_scripted_transport(labels))
    try:
        result = await review_packet(client, packet, "judge")
    finally:
        await client.aclose()
    rows = [json.loads(line) for line in (grading_dir / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]
    errors = []
    valid, _ = _verify_review_receipts(result, packet, "judge", rows, errors, "candidate")
    assert valid
    assert errors == []

    forged = copy.deepcopy(result)
    forged["reviews"][0]["items"][0]["answer_evidence"][0]["quote"] = "120美元"
    # Model a forger who also replaces and re-hashes the receipt in the call log.
    forged_rows = copy.deepcopy(rows)
    review_id = forged["reviews"][0]["review_id"]
    call = next(row for row in forged_rows if row["review_id"] == review_id)
    call["review"] = copy.deepcopy(forged["reviews"][0])
    call["response_text"] = canonical(call["review"]).decode("utf-8")
    errors = []
    valid, _ = _verify_review_receipts(forged, packet, "judge", forged_rows, errors, "candidate")
    assert not valid
    assert "candidate:review_invalid" in errors

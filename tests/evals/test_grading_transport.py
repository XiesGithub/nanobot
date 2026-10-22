"""Bounded, reproducible tests for the Phase 4 review transport."""

import asyncio
import inspect
import json
import time

import pytest

from evals.common import canonical, digest, strict_json
from evals.grading_transport import (
    MAX_ATTEMPTS_PER_CALL,
    GradingClient,
    GradingError,
    _citation_catalog,
    _messages,
    estimate_input_token_upper_bound,
    prompt_metadata,
)


def packet(stage="judge"):
    common = {
        "packet_id": "sample-1",
        "requirements": [{"requirement_id": "r1", "answer_range": [0, 3]}],
        "sources": [{"artifact_id": "source", "text": "值：五", "sha256": digest("值：五".encode())}],
    }
    if stage == "gold":
        common["requirements"][0].pop("answer_range")
        common["gold_documents"] = [{"artifact_id": "gold:r1", "text": "five",
                                     "sha256": digest(b"five")}]
    else:
        common["answer"] = {"artifact_id": "answer", "text": "五元。",
                            "sha256": digest("五元。".encode())}
    return common


def review_response(request, *, valid=True, secret=None):
    payload = strict_json(request["messages"][-1]["content"])
    result = {"schema_version": "semantic-review/1.0",
              "review_id": payload["review_id"], "role": payload["role"],
              "packet_sha256": payload["packet_sha256"], "valid": valid,
              "secret": secret}
    return {"choices": [{"message": {"content": json.dumps(result)}}],
            "usage": None, "model": "fake-model", "id": "request-fake"}


class Script:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    async def __call__(self, **request):
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            result = step(request)
            return await result if inspect.isawaitable(result) else result
        if step == "valid":
            return review_response(request)
        return step


def rows(output):
    return [strict_json(line) for line in (output / "calls.jsonl").read_text(
        encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_retry_parse_error_and_record_attempts_catalog_and_profile(tmp_path):
    script = Script({"choices": [{"message": {"content": "not json"}}]}, "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        result = await client.call(packet(), "support_check", "judge")
        assert result["role"] == "support_check"
        assert len(script.requests) == 2
        assert script.requests[0]["messages"] is not script.requests[1]["messages"]
        request = script.requests[0]
        assert request["model"] == "deepseek-v4-flash"
        assert request["temperature"] == 0
        assert request["max_tokens"] == 4096
        assert request["extra_body"] == {"thinking": {"type": "disabled"}}
        assert len(request["messages"]) == 2
        assert "tools" not in request
        payload = strict_json(request["messages"][1]["content"])
        assert payload["packet"] == packet()
        assert payload["packet_sha256"] == digest(canonical(packet()))
        assert payload["citation_catalog"][-1]["artifact_id"] == "answer"
        assert payload["citation_catalog"][-1]["quote"] == "五元。"
        assert "review_id" in payload and "role" in payload and "stage" in payload
        logged = rows(tmp_path)
        assert [r["outcome"] for r in logged] == ["parse_error", "ok"]
        assert all(r["purpose"] == "grading" and r["sdk_max_retries"] == 0 for r in logged)
        summary = client.summary()
        assert summary["scripted_calls"] == 2 and summary["real_attempts"] == 0
        assert summary["amount"] == 0 and summary["actual_tokens"] is None
        assert [r["usage_status"] for r in logged] == ["scripted_none", "scripted_none"]
        profile = summary["request_profile"]
        assert profile["tool_definitions"] is False and profile["fresh_context_per_attempt"] is True
        assert prompt_metadata()["version"] == "semantic-review-prompt/1.1"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_validator_failure_retries_once_and_preserves_first_judgement(tmp_path):
    script = Script(lambda request: review_response(request, valid=False), "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)

    def validator(review, _packet, _role, _stage, _review_id):
        if not review["valid"]:
            raise ValueError("invalid citation")

    try:
        result = await client.review(packet(), "support_check", "judge", validator)
        assert result["valid"] is True
        logged = rows(tmp_path)
        assert [r["outcome"] for r in logged] == ["validator_error", "ok"]
        assert logged[0]["review"]["valid"] is False
        assert logged[0]["logical_review_id"] == logged[1]["logical_review_id"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_strict_json_has_two_attempt_limit(tmp_path):
    script = Script({"choices": [{"message": {"content": "bad"}}]},
                    {"choices": [{"message": {"content": "still bad"}}]}, "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        with pytest.raises(GradingError, match="parse_error"):
            await client.call(packet(), "support_check", "judge")
        assert len(script.requests) == MAX_ATTEMPTS_PER_CALL == 2
        assert len(rows(tmp_path)) == 2
        assert client.summary()["scripted_calls"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_transient_transport_failure_retries_once(tmp_path):
    class APIConnectionError(Exception):
        pass

    script = Script(APIConnectionError("do not persist this error"), "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        await client.call(packet(), "support_check", "judge")
        assert [r["outcome"] for r in rows(tmp_path)] == ["transport_error", "ok"]
        assert "do not persist" not in (tmp_path / "calls.jsonl").read_text(encoding="utf-8")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_logical_call_timeout_covers_both_attempts(tmp_path, monkeypatch):
    import evals.grading_transport as transport_module

    monkeypatch.setattr(transport_module, "REQUEST_TIMEOUT_SECONDS", 0.25)

    async def slow_bad(_request):
        await asyncio.sleep(0.10)
        return {"choices": [{"message": {"content": "not json"}}]}

    async def too_slow(_request):
        await asyncio.sleep(0.30)
        return review_response(_request)

    script = Script(slow_bad, too_slow)
    client = GradingClient(tmp_path, mode="D", transport=script)
    started = time.monotonic()
    try:
        with pytest.raises(GradingError):
            await client.call(packet(), "support_check", "judge")
        elapsed = time.monotonic() - started
        assert elapsed < 0.40
        assert len(rows(tmp_path)) == 2
        assert rows(tmp_path)[-1]["outcome"] == "timeout"
        assert script.requests[1]["timeout"] < script.requests[0]["timeout"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_transport_unknown_and_partial_usage_are_not_zero(tmp_path):
    partial = review_response

    async def partial_usage(request):
        response = partial(request)
        response["usage"] = {"prompt_tokens": 7}
        return response

    client = GradingClient(tmp_path, mode="R", transport=Script(partial_usage))
    try:
        await client.call(packet(), "support_check", "judge")
        row = rows(tmp_path)[0]
        summary = client.summary()
        assert row["usage_status"] == "partial"
        assert summary["known_input_tokens"] == 7
        assert summary["known_output_tokens"] == 0
        assert summary["actual_tokens"] is None
        assert summary["unknown_usage_attempts"] == 1
        assert summary["amount"] is None
        assert summary["real_attempts"] == 1 and summary["scripted_calls"] == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_not_zero(tmp_path):
    client = GradingClient(tmp_path, mode="R", transport=Script("valid"))
    try:
        await client.call(packet(), "support_check", "judge")
        assert rows(tmp_path)[0]["usage_status"] == "unknown"
        assert rows(tmp_path)[0]["usage_field_presence"]["usage_object"] is False
        assert client.summary()["known_input_tokens"] == 0
        assert client.summary()["actual_tokens"] is None
        assert client.summary()["unknown_usage_attempts"] == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_known_zero_usage_is_distinct_from_missing_usage(tmp_path):
    async def zero_usage(request):
        result = review_response(request)
        result["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
        return result

    client = GradingClient(tmp_path, mode="R", transport=Script(zero_usage))
    try:
        await client.call(packet(), "support_check", "judge")
        assert rows(tmp_path)[0]["usage_status"] == "known"
        assert client.summary()["actual_tokens"] == 0
        assert client.summary()["amount"] is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_packet_stage_limits_are_three_unique_roles(tmp_path):
    script = Script("valid", "valid", "valid", "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        for role in ("support_check", "counter_check", "recheck"):
            await client.call(packet(), role, "judge")
        with pytest.raises(GradingError, match="package_logical_call_budget_exceeded"):
            await client.call(packet(), "support_check", "judge")
        assert len(script.requests) == 3
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_reserved_tokens_are_not_refunded_after_failed_attempt(tmp_path, monkeypatch):
    import evals.grading_transport as transport_module

    frozen_packet = packet()
    bound = estimate_input_token_upper_bound(_messages(
        frozen_packet, digest(canonical(frozen_packet)), "support_check", "judge",
        "00000000-0000-0000-0000-000000000000", [],
        _citation_catalog(frozen_packet, "judge")))
    monkeypatch.setattr(transport_module, "MAX_RUN_TOKENS", bound + 4096 + 10)
    script = Script({"choices": [{"message": {"content": "bad"}}]}, "valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        with pytest.raises(GradingError, match="run_token_reserve_exceeded"):
            await client.call(packet(), "support_check", "judge")
        assert len(script.requests) == 1
        assert client.summary()["reserved_tokens"] > 0
        assert len(rows(tmp_path)) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_observed_usage_over_budget_invalidates_the_call(tmp_path, monkeypatch):
    import evals.grading_transport as transport_module

    monkeypatch.setattr(transport_module, "MAX_RUN_TOKENS", 100_000)

    async def excessive_usage(request):
        response = review_response(request)
        response["usage"] = {"prompt_tokens": 100_001, "completion_tokens": 0}
        return response

    client = GradingClient(tmp_path, mode="R", transport=Script(excessive_usage))
    try:
        with pytest.raises(GradingError, match="usage_budget_exceeded"):
            await client.call(packet(), "support_check", "judge")
        assert rows(tmp_path)[0]["outcome"] == "budget_exceeded"
        assert rows(tmp_path)[0]["error_kind"] == "observed_usage_over_reservation"
        assert client.summary()["actual_tokens"] == 100_001
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_credentials_are_redacted_recursively_and_packet_hash_is_full(tmp_path):
    key = "sk-real-secret-credential-987654321"
    script = Script(lambda request: review_response(request, secret=key))
    client = GradingClient(tmp_path, mode="D", transport=script)
    client._secret = key
    try:
        await client.call(packet(), "support_check", "judge")
        raw = (tmp_path / "calls.jsonl").read_text(encoding="utf-8")
        assert key not in raw
        assert "credential_redacted" in raw
        assert rows(tmp_path)[0]["packet_sha256"] == digest(canonical(packet()))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_gold_packet_is_isolated_from_candidate_and_gold_stage_cites_catalog(tmp_path):
    gold = packet("gold")
    script = Script("valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        await client.call(gold, "support_check", "gold")
        payload = strict_json(script.requests[0]["messages"][1]["content"])
        assert payload["packet"] == gold
        assert all("answer" not in key and "hard_checks" not in key
                   for key in payload["packet"])
        assert any(row["artifact_id"] == "gold:r1" for row in payload["citation_catalog"])
        assert payload["packet_sha256"] == digest(canonical(gold))
    finally:
        await client.aclose()


def test_output_directory_and_jsonl_are_exclusive(tmp_path):
    (tmp_path / "calls.jsonl").write_text("preserve", encoding="utf-8")
    with pytest.raises(GradingError, match="grading_log_already_exists"):
        GradingClient(tmp_path, mode="D", transport=Script())
    assert (tmp_path / "calls.jsonl").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("stage,evidence_key,opposite_key", [
    ("gold", "gold_evidence", "answer_evidence"),
    ("judge", "answer_evidence", "gold_evidence"),
    ("selfcheck", "answer_evidence", "gold_evidence"),
])
@pytest.mark.asyncio
async def test_trusted_system_schema_renders_one_stage_evidence_key(
        tmp_path, stage, evidence_key, opposite_key):
    script = Script("valid")
    client = GradingClient(tmp_path, mode="D", transport=script)
    try:
        await client.call(packet("gold" if stage == "gold" else "judge"),
                          "support_check", stage)
        messages = script.requests[0]["messages"]
        system_prompt = messages[0]["content"]
        schema_example = next(line for line in system_prompt.splitlines()
                              if line.startswith("Output schema"))
        assert f'"{evidence_key}"' in schema_example
        assert f'"{opposite_key}"' not in schema_example
        metadata = prompt_metadata()
        assert metadata["template_sha256"]
        assert metadata["rendered_sha256_by_stage"][stage]
        assert rows(tmp_path)[0]["rendered_prompt_sha256"] == \
            metadata["rendered_sha256_by_stage"][stage]
    finally:
        await client.aclose()

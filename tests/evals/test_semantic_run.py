"""Phase-4 archive integration and the non-bypassable self-check gate."""

import json

import pytest

from evals.common import digest, read, strict_json
from evals.grading_transport import GradingClient
from evals.semantic_run import run


async def test_fixed_archive_contract_preserves_versions_costs_and_hard_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    output = tmp_path / "fixed"
    result = await run("D", output)
    assert result["ok"] and result["selfcheck_consistency"] == 12
    assert result["gold_counts"] == {"approved": 2}
    assert result["counts"] == {"pass": 2, "product_fail": 3}
    assert result["product_cost"]["model_attempts"] == 0
    assert result["grading_cost"]["real_attempts"] == 0
    assert result["grading_cost"]["scripted_calls"] == 16
    assert not result["formal_quality_eligible"]
    verdict = read(output / "verdicts/archive-03.json")
    assert verdict["status"] == verdict["program_verdict"]["status"] == "product_fail"
    assert verdict["scorer_version"] == "phase4/1.1"
    assert verdict["revision"] == 1 and verdict["supersedes"] is None
    for item in read(output / "artifact-index.json"):
        assert digest((output / item["path"]).read_bytes()) == item["sha256"]
    calls = [strict_json(line) for line in (output / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["purpose"] == "grading" for row in calls)
    for row in calls:
        assert "hard_checks" not in row["packet"]
        assert "expected_labels" not in row["packet"]
        if row["stage"] == "gold":
            assert "answer" not in row["packet"]
    before = (output / "run-result.json").read_bytes()
    with pytest.raises(FileExistsError):
        await run("D", output)
    assert (output / "run-result.json").read_bytes() == before


async def test_failed_sentinel_blocks_gold_and_semantic_calls(tmp_path, monkeypatch):
    from evals import grading_transport

    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")

    def client_factory(output, mode, transport):
        async def corrupted(**request):
            response = await transport(**request)
            review = strict_json(response["choices"][0]["message"]["content"])
            assert strict_json(request["messages"][-1]["content"])["stage"] == "selfcheck"
            review["items"][1]["label"] = "pass"  # Deliberate false acceptance.
            response["choices"][0]["message"]["content"] = json.dumps(review)
            return response

        return GradingClient(output, mode=mode, transport=corrupted)

    monkeypatch.setattr(grading_transport, "GradingClient", client_factory)
    result = await run("D", tmp_path / "failed-sentinel")
    assert not result["ok"] and not result["selfcheck_ok"]
    assert result["grading_cost"]["scripted_calls"] == 2
    assert result["counts"] == {"invalid": 4, "product_fail": 1}
    assert all(not r["grading_valid"] for r in result["results"])

"""Regression tests reconstruct actual archived answers without repairing citations."""
import copy

import pytest

from evals.common import ROOT, read
from evals.semantic import gold_packet, validate_packet, validate_review
from evals.semantic_revision import archived_runs, load_archive


@pytest.mark.parametrize("run", archived_runs(), ids=lambda path: path.name)
def test_original_archives_reconstruct_without_product_execution(run):
    row = load_archive(run.resolve())
    original = read(ROOT / row["old_packet"])
    revised = validate_packet(row["packet"])
    assert revised["answer"] == original["answer"]
    assert revised["sources"] == original["sources"]
    assert revised["gold_version"] == original["gold_version"]
    assert revised["rubric_version"] == original["rubric_version"]
    assert gold_packet(revised) == gold_packet(original)
    assert row["program"]["status"] == "pass"
    for old, new in zip(original["requirements"], revised["requirements"], strict=True):
        assert old == {key: value for key, value in new.items() if key != "answer_range"}
        if "answer_range" in new:
            start, end = new["answer_range"]
            text = revised["answer"]["text"][start:end]
            assert text.startswith(new["requirement_id"].split(".")[0] + "：")
            other = "V107：" if text.startswith("V095：") else "V095："
            assert other not in text


def test_archived_wrong_quote_stays_rejected():
    run = archived_runs()[0]
    row = load_archive(run.resolve())
    calls = (run / "grading/calls.jsonl").read_text(encoding="utf-8").splitlines()
    import json
    bad = next(json.loads(line) for line in calls
               if json.loads(line)["stage"] == "gold")
    review = copy.deepcopy(bad["review"])
    original = copy.deepcopy(review)
    with pytest.raises(ValueError, match="reference_quote_hash"):
        validate_review(review, row["gold"], bad["role"], "gold", bad["review_id"])
    assert review == original

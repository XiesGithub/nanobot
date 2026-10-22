"""Offline rehashed forgery controls against an actual saved D family trial."""
import copy
import shutil

import pytest

from evals.common import ROOT, canonical, digest, read
from evals.privacy_task_audit import audit

SAVED = ROOT / "docs/agent-evaluation-privacy-family-20261011/D-final-positive"


def _replace(path, value):
    path.write_text(canonical(value).decode("utf-8") + "\n", encoding="utf-8")


def _rehash(directory):
    for index in directory.rglob("artifact-index.json"):
        rows = read(index)
        for row in rows:
            row["sha256"] = digest((index.parent / row["path"]).read_bytes())
        _replace(index, rows)


def test_actual_d_replays_without_model_or_agent_calls():
    result = audit(SAVED)
    assert result["ok"], result["errors"]
    assert result["status"] == "pass"
    assert not result["qualification"]
    assert result["model_requests"] == result["agent_reruns"] == 0


@pytest.mark.parametrize("fault", ["hidden_leak", "missing_boundary", "cross_trial", "receipt_quote",
                                   "delivery", "budget", "qualification", "source_zip"])
def test_rehashed_copies_cannot_obtain_qualification(tmp_path, fault):
    clone = tmp_path / SAVED.name
    shutil.copytree(SAVED, clone)
    trial = clone / "trials/V095-V107-1"
    evidence_path = trial / "artifacts/evidence.json"
    evidence = read(evidence_path)
    result = next(e["payload"] for e in evidence["events"] if e["type"] == "privacy_result")
    if fault == "hidden_leak":
        result["visible"].append({"actor": "B", "surface": "error", "value": {"details": "PRIVATE-FLOW-X8"}})
    elif fault == "missing_boundary":
        result["boundaries"] = [r for r in result["boundaries"] if r["name"] != "read_evidence_B"]
    elif fault == "cross_trial":
        evidence["events"][0]["trial_id"] = "another-trial"
    elif fault == "delivery":
        packet_path = trial / "packet.json"
        packet = read(packet_path)
        packet["answer"]["text"] = "fabricated replacement"
        _replace(packet_path, packet)
    elif fault == "receipt_quote":
        review_path = trial / "semantic-result.json"
        review = read(review_path)
        review["reviews"][0]["items"][0]["source_evidence"][0]["quote"] = "forged"
        _replace(review_path, review)
    elif fault == "qualification":
        verdict_path = trial / "full-task-verdict.json"
        verdict = read(verdict_path)
        verdict["formal_quality_eligible"] = True
        _replace(verdict_path, verdict)
    elif fault == "budget":
        manifest_path = clone / "manifest.json"
        manifest = read(manifest_path)
        manifest["budget"]["product"]["attempts"] = 999
        _replace(manifest_path, manifest)
    else:
        (clone / "source-snapshot.zip").write_bytes(b"forged zip")
    if fault in {"hidden_leak", "missing_boundary", "cross_trial"}:
        _replace(evidence_path, evidence)
        (trial / "events.jsonl").write_text(
            "\n".join(canonical(e).decode("utf-8") for e in evidence["events"]) + "\n", encoding="utf-8")
    _rehash(clone)
    verdict = audit(clone)
    assert not verdict["ok"] and not verdict.get("qualification", False)


def test_semantic_packet_only_uses_actual_b_delivery():
    from evals.privacy_task_semantic import packet_for

    case = read(SAVED / "manifest.json")["case"]
    evidence = read(SAVED / "trials/V095-V107-1/artifacts/evidence.json")
    packet = packet_for(case, evidence)
    altered = copy.deepcopy(evidence)
    result = next(e["payload"] for e in altered["events"] if e["type"] == "privacy_result")
    result["delivery_by_case"]["V095"]["response"] = "伪造答复"
    assert packet_for(case, altered) != packet

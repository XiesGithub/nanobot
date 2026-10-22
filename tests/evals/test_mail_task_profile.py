import asyncio
import copy
import uuid

import pytest

from evals.common import ROOT, canonical, digest, read
from evals.mail_task_audit import audit
from evals.mail_task_cases import DATA, IDS, VERSION, load_cases, templates
from evals.mail_task_evidence import binding, score
from evals.mail_task_semantic import aggregate, run
from evals.run import cleanup_instance
from evals.semantic import gold_packet


@pytest.fixture(scope="module")
def snapshots():
    parent = ROOT / "tmp" / ("mail-profile-tests-" + uuid.uuid4().hex)
    parent.mkdir(parents=True)
    result = {}
    try:
        for cid in IDS:
            directory = parent / cid
            summary = asyncio.run(run("D", directory, cid))
            trial = directory / "trials" / (cid + "-1")
            result[cid] = {"directory": directory, "summary": summary,
                           "manifest": read(directory / "manifest.json"),
                           "evidence": read(trial / "artifacts/evidence.json"),
                           "program": read(trial / "verdicts/1.json"),
                           "packet": read(trial / "packet.json"),
                           "semantic": read(trial / "semantic-result.json"),
                           "gold": read(directory / "gold-review.json"),
                           "selfcheck": read(directory / "selfcheck.json"),
                           "costs": read(directory / "grading-cost.json")}
        yield result
    finally:
        cleanup_instance(parent, ROOT / "tmp")


def test_fixture_mapping_keeps_original_time_and_attachment_ownership():
    assert DATA["V089"]["mails"][0]["received_at"] == "2026-09-28T09:00:00+00:00"
    assert DATA["V089"]["mails"][1]["received_at"] == "2026-09-29T09:00:00+00:00"
    assert DATA["V091"]["mails"][0]["attachments"] == [{"filename": "general.txt", "text": "预付款比例40%"}]
    assert read(ROOT / "evals/mail_task_fixtures.json") == DATA
    assert len(load_cases()) == 4


@pytest.mark.parametrize("cid", IDS)
def test_d_entry_reading_replay_and_offline_audit(snapshots, cid):
    row = snapshots[cid]
    assert row["summary"]["ok"]
    assert not row["summary"]["formal_quality_eligible"]
    assert all(row["program"]["extensions"]["binding"]["checks"].values())
    assert audit(row["directory"])["ok"]


@pytest.mark.parametrize("cid", IDS)
@pytest.mark.parametrize("kind", ["mail_read", "delivery", "private_history"])
def test_missing_actual_evidence_cannot_be_overturned(snapshots, cid, kind):
    row = snapshots[cid]
    evidence = copy.deepcopy(row["evidence"])
    evidence["events"] = [e for e in evidence["events"] if e["type"] != kind]
    program = score(row["manifest"]["case"], evidence)
    verdict = aggregate(row["manifest"]["case"], evidence, program, row["packet"],
                        row["semantic"], row["gold"], row["selfcheck"], "D", row["costs"])
    assert verdict["status"] != "pass"
    assert not verdict["formal_quality_eligible"]


@pytest.mark.parametrize("cid", IDS)
def test_cross_trial_identity_and_business_effect_cannot_pass(snapshots, cid):
    row = snapshots[cid]
    for fault in ("cross_trial", "business", "outbox", "fallback"):
        evidence = copy.deepcopy(row["evidence"])
        if fault == "cross_trial":
            for event in evidence["events"]:
                event["trial_id"] = "other-trial"
        elif fault == "business":
            state = next(e["payload"] for e in evidence["events"] if e["type"] == "mail_state" and e["payload"]["moment"] == "after")
            state["state"] = {"business_modified": True}
        elif fault == "outbox":
            next(e["payload"] for e in evidence["events"] if e["type"] == "delivery")["outbox_count"] = 1
        else:
            next(e["payload"] for e in evidence["events"] if e["type"] == "mail_answer")["result"]["status"] = "unverified_draft_rejected"
        assert score(row["manifest"]["case"], evidence)["status"] != "pass"


def test_gold_identity_never_depends_on_candidate():
    packet = templates()["V091"]
    gold = gold_packet(packet)
    packet["answer"] = {"artifact_id": "answer", "text": "40%", "sha256": digest(b"40%")}
    assert gold_packet(packet) == gold
    assert packet["gold_version"] == VERSION


def test_attachment_cannot_be_relabelled_as_body(snapshots):
    row = snapshots["V091"]
    evidence = copy.deepcopy(row["evidence"])
    for event in evidence["events"]:
        if event["type"] == "mail_state":
            for source in event["payload"]["sources"]:
                if source["filename"] == "general.txt":
                    source["kind"] = "body"
    assert not binding(row["manifest"]["case"], evidence)["checks"]["attachment_parent"]


@pytest.mark.parametrize("cid", IDS)
def test_runtime_and_fixture_are_not_silently_relaxed(snapshots, cid):
    row = snapshots[cid]
    evidence = copy.deepcopy(row["evidence"])
    evidence["runtime_violations"] = ["mail/private-secret-outside.json"]
    assert not binding(row["manifest"]["case"], evidence)["checks"]["execution"]
    evidence = copy.deepcopy(row["evidence"])
    evidence["after"]["unexpected.txt"] = {"sha256": digest(canonical("write"))}
    assert not binding(row["manifest"]["case"], evidence)["checks"]["workspace_unchanged"]


def test_runtime_allowlist_and_actual_usage_are_recomputed(snapshots):
    row = snapshots["V088"]
    evidence = copy.deepcopy(row["evidence"])
    evidence["runtime_after"]["unauthorized.json"] = {"sha256": digest(b"side-effect")}
    evidence["ready"]["runtime_allowed_paths"].append("unauthorized.json")
    evidence["runtime_violations"] = []
    assert not binding(row["manifest"]["case"], evidence)["checks"]["runtime_policy"]
    costs = copy.deepcopy(row["costs"])
    costs["known_input_tokens"] = costs["limits"]["reserved_tokens"] + 1
    verdict = aggregate(row["manifest"]["case"], row["evidence"], row["program"], row["packet"],
                        row["semantic"], row["gold"], row["selfcheck"], "D", costs)
    assert not verdict["gates"]["budget"]
    assert verdict["status"] != "pass"

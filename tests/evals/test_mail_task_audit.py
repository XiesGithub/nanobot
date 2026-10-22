"""Offline audit rejects rehashed drift instead of trusting stored qualification."""

import asyncio
import shutil
import uuid

import pytest

from evals.common import ROOT, digest, read
from evals.mail_task_audit import _actual_grading_budget, audit
from evals.mail_task_semantic import run
from evals.run import cleanup_instance


def _replace(path, value):
    import json

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _reindex(directory):
    trial = directory / "trials/V089-1"
    index = read(trial / "artifact-index.json")
    for item in index:
        item["sha256"] = digest((trial / item["path"]).read_bytes())
    _replace(trial / "artifact-index.json", index)
    _replace(directory / "artifact-index.json", [
        {"path": path.relative_to(directory).as_posix(), "sha256": digest(path.read_bytes())}
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path != directory / "artifact-index.json"
    ])


@pytest.fixture(scope="module")
def snapshot():
    parent = ROOT / "tmp" / ("mail-audit-tests-" + uuid.uuid4().hex)
    parent.mkdir(parents=True)
    directory = parent / "positive"
    try:
        result = asyncio.run(run("D", directory, "V089"))
        assert result["ok"]
        yield directory
    finally:
        cleanup_instance(parent, ROOT / "tmp")


def test_current_positive_snapshot_replays(snapshot):
    result = audit(snapshot)
    assert result["ok"], result["errors"]
    assert not result["qualification"]
    assert result["model_requests"] == result["agent_reruns"] == 0


@pytest.mark.parametrize("fault,reason", [
    ("schema", "frozen_version_model_or_source_mapping"),
    ("requested_model", "frozen_version_model_or_source_mapping"),
    ("snapshot_mapping", "frozen_version_model_or_source_mapping"),
    ("model_configuration", "frozen_product_model_profile"),
    ("environment", "frozen_environment_profile"),
    ("case_version", "trial_replay:case_version"),
    ("entry", "trial_replay:entry"),
    ("execution_status", "trial_replay:execution_status"),
    ("accepted_ref", "trial_replay:accepted_event_id"),
    ("terminal_ref", "trial_replay:terminal_event_id"),
    ("completion", "trial_replay:completion"),
    ("cleanup", "trial_replay:cleanup"),
    ("product_cost", "trial_replay:product_cost"),
    ("observation", "trial_replay:observation"),
    ("initial_state", "trial_initial_state_or_worker_identity"),
    ("worker_pid", "trial_initial_state_or_worker_identity"),
    ("trial_index", "trial_index_inventory"),
])
def test_rehashed_manifest_and_trial_drift_rejected(snapshot, fault, reason):
    parent = snapshot.parent / ("tamper-" + fault)
    parent.mkdir()
    directory = parent / snapshot.name
    shutil.copytree(snapshot, directory)
    manifest_path = directory / "manifest.json"
    trial_path = directory / "trials/V089-1/trial.json"
    manifest, trial = read(manifest_path), read(trial_path)
    if fault == "schema":
        manifest["grading_schema_version"] = "forged-schema"
    elif fault == "requested_model":
        manifest["requested_model"] = "forged-model"
    elif fault == "snapshot_mapping":
        manifest["snapshot"]["file_sha256"]["evals/mail_task_evidence.py"] = "0" * 64
    elif fault == "model_configuration":
        manifest["model_profile"]["answer_max_tokens"] = 99999
    elif fault == "environment":
        manifest["environment"]["sending"] = True
    elif fault == "case_version":
        trial["case_version"] = "forged-version"
    elif fault == "entry":
        trial["entry"] = "fake_adapter.answer"
    elif fault == "execution_status":
        trial["execution_status"] = "crashed"
    elif fault == "accepted_ref":
        trial["accepted_event_id"] = "forged-event"
    elif fault == "terminal_ref":
        trial["terminal_event_id"] = "forged-event"
    elif fault == "completion":
        trial["completion"]["met"] = False
    elif fault == "cleanup":
        trial["cleanup"]["ok"] = False
    elif fault == "product_cost":
        trial["product_cost"]["unknown_usage_attempts"] = 500
    elif fault == "observation":
        trial["observation"]["closed"] = False
    elif fault == "initial_state":
        trial["extensions"]["initial_state_sha256"] = "0" * 64
    elif fault == "worker_pid":
        trial["extensions"]["worker_pid"] = 0
    else:
        path = directory / "trials/V089-1/artifact-index.json"
        _replace(path, [row for row in read(path) if row["path"] != "events.jsonl"])
    _replace(manifest_path, manifest)
    _replace(trial_path, trial)
    _reindex(directory)
    result = audit(directory)
    assert not result["ok"]
    assert reason in result["errors"], result["errors"]
    assert not result.get("qualification", False)
    assert result["model_requests"] == result["agent_reruns"] == 0


def test_actual_grading_usage_is_checked_per_stage_and_run():
    limits = {"reserved_tokens": 192000, "per_packet_stage_reserved_tokens": 96000}
    calls = [{"packet_sha256": "same", "stage": "judge", "input_tokens": 96000,
              "output_tokens": 1}]
    errors = []
    _actual_grading_budget(calls, {"known_input_tokens": 96000, "known_output_tokens": 1},
                           limits, errors)
    assert errors == ["grading_actual_stage_budget_exceeded"]
    errors = []
    calls.append({"packet_sha256": "other", "stage": "gold", "input_tokens": 96000,
                  "output_tokens": 1})
    _actual_grading_budget(calls, {"known_input_tokens": 192000, "known_output_tokens": 2},
                           limits, errors)
    assert set(errors) == {"grading_actual_stage_budget_exceeded", "grading_actual_run_budget_exceeded"}

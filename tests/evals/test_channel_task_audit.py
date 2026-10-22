from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from evals.channel_task_audit import audit
from evals.channel_task_cases import IDS
from evals.channel_task_run import run
from evals.common import ROOT, digest, read
from evals.trace import analyze_trace

LOG_ROOT = ROOT / "docs" / "agent-evaluation-channel-fulltask-20261012" / "logs"
EXPECTED_STATUS = {"V063": "pass", "V065": "product_fail", "V066": "pass"}


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _copy_run(source: Path, root: Path, label: str) -> Path:
    destination_parent = root / label
    destination_parent.mkdir(parents=True, exist_ok=False)
    destination = destination_parent / source.name
    shutil.copytree(source, destination)
    return destination


def _refresh_indices(output: Path) -> None:
    for trial in (output / "trials").iterdir():
        index_path = trial / "artifact-index.json"
        rows = read(index_path)
        for row in rows:
            row["sha256"] = digest((trial / row["path"]).read_bytes())
        _write_json(index_path, rows)

    index_path = output / "artifact-index.json"
    rows = read(index_path)
    for row in rows:
        row["sha256"] = digest((output / row["path"]).read_bytes())
    _write_json(index_path, rows)


def _rewrite_evidence_events(output: Path, events: list[dict[str, Any]]) -> None:
    trial = next((output / "trials").iterdir())
    evidence_path = trial / "artifacts" / "evidence.json"
    evidence = read(evidence_path)
    evidence["events"] = events
    _write_json(evidence_path, evidence)
    event_lines = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                          for event in events)
    (trial / "events.jsonl").write_bytes(event_lines.encode("utf-8"))
    _write_json(trial / "artifacts" / "trace-analysis.json",
                analyze_trace(events, evidence["execution_status"], evidence["collector_errors"]))


def _channel_result_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [event for event in events if event.get("type") == "channel_task_result"]
    assert len(rows) == 1
    return rows[0]


def _tamper_raw_wire(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "01-raw-wire-result-events-rehashed")
    trial = next((output / "trials").iterdir())
    evidence = read(trial / "artifacts" / "evidence.json")
    events = evidence["events"]
    result = _channel_result_event(events)["payload"]["result"]
    result["runner_final"] = "结果为99"
    delta = next(frame for frame in result["wire_frames"] if frame.get("event") == "delta"
                 and "73" in frame.get("text", ""))
    delta["text"] = delta["text"].replace("73", "99", 1)
    _rewrite_evidence_events(output, events)
    _refresh_indices(output)
    return output


def _tamper_wrong_thread(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "02-wrong-thread-with-consistent-events")
    trial = next((output / "trials").iterdir())
    evidence = read(trial / "artifacts" / "evidence.json")
    events = evidence["events"]
    result = _channel_result_event(events)["payload"]["result"]
    request = next(row for row in result["events"]
                   if row.get("kind") == "socket_request" and row.get("turn_id") == "ask_A")
    request["envelope"]["payload"]["event"]["thread_ts"] = "1700000000.000200"
    observations = [event for event in events if event.get("type") == "slack_socket_request"
                    and event.get("payload", {}).get("turn_id") == "ask_A"]
    assert len(observations) == 1
    observations[0]["payload"] = {key: value for key, value in request.items() if key != "kind"}
    _rewrite_evidence_events(output, events)
    _refresh_indices(output)
    return output


def _tamper_cross_trial(source: Path, donor: Path, root: Path) -> Path:
    output = _copy_run(source, root, "03-cross-trial-evidence")
    trial = next((output / "trials").iterdir())
    donor_trial = next((donor / "trials").iterdir())
    shutil.copyfile(donor_trial / "artifacts" / "evidence.json", trial / "artifacts" / "evidence.json")
    shutil.copyfile(donor_trial / "events.jsonl", trial / "events.jsonl")
    donor_evidence = read(trial / "artifacts" / "evidence.json")
    _write_json(trial / "artifacts" / "trace-analysis.json",
                analyze_trace(donor_evidence["events"], donor_evidence["execution_status"],
                              donor_evidence["collector_errors"]))
    _refresh_indices(output)
    return output


def _tamper_missing_source_read(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "04-source-read-deleted-with-rehashed-events")
    trial = next((output / "trials").iterdir())
    evidence = read(trial / "artifacts" / "evidence.json")
    events = evidence["events"]
    selected = [event for event in events if event.get("type") == "channel_task_source_read"]
    assert len(selected) == 1
    removed = selected[0]
    events.remove(removed)
    producer = removed["producer_id"]
    for event in events:
        if event["producer_id"] == producer and event["seq"] > removed["seq"]:
            event["seq"] -= 1
            event["event_id"] = f"{producer}:{event['seq']}"
    _rewrite_evidence_events(output, events)
    _refresh_indices(output)
    return output


def _tamper_ui_text_and_parsed(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "05-ui-artifact-text-and-parsed-rehashed")
    trial = next((output / "trials").iterdir())
    evidence = read(trial / "artifacts" / "evidence.json")
    events = evidence["events"]
    result = _channel_result_event(events)["payload"]["result"]
    ui = result["render_contract"]["ui_replay"]
    parsed = ui["evidence"]
    captured = next(row for row in parsed["cases"]
                    if row.get("case") == "captured_worker_wire_frames")
    captured["visible_answers"] = ["结果为99"]
    ui["captured_worker_render"] = captured
    evidence_text = json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"
    ui["evidence_text"] = evidence_text
    ui["evidence_sha256"] = digest(evidence_text.encode("utf-8"))
    ui["evidence"] = parsed
    path = ui["evidence_path"]
    evidence["runtime_after"][path]["sha256"] = ui["evidence_sha256"]
    evidence["runtime_after"][path]["size"] = len(evidence_text.encode("utf-8"))
    _rewrite_evidence_events(output, events)
    _refresh_indices(output)
    return output


def _tamper_budget(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "06-loosened-budget-rehashed")
    manifest_path = output / "manifest.json"
    manifest = read(manifest_path)
    manifest["budget"]["known_tokens"] = 32000000
    _write_json(manifest_path, manifest)
    _refresh_indices(output)
    return output


def _tamper_qualification(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "07-forged-program-pass-and-qualification")
    trial = next((output / "trials").iterdir())
    verdict_path = trial / "verdicts" / "1.json"
    verdict = read(verdict_path)
    verdict["status"] = "pass"
    verdict["formal_quality_eligible"] = True
    verdict["reasons"] = []
    verdict["extensions"]["completion_met"] = True
    _write_json(verdict_path, verdict)
    _refresh_indices(output)
    return output


def _tamper_source_zip(source: Path, root: Path) -> Path:
    output = _copy_run(source, root, "08-source-zip-replaced-and-rehashed")
    zip_path = output / "source-snapshot.zip"
    temporary = output / "source-snapshot-replacement.tmp.zip"
    with zipfile.ZipFile(zip_path, "r") as original, zipfile.ZipFile(temporary, "w",
                                                                       zipfile.ZIP_DEFLATED) as replacement:
        names = original.namelist()
        target = next(name for name in names if name.endswith("channel_task_cases.py"))
        for name in names:
            data = original.read(name)
            if name == target:
                data += b"\n# offline audit tamper\n"
            replacement.writestr(name, data)
    os.replace(temporary, zip_path)
    _refresh_indices(output)
    return output


def test_channel_driver_matrix_and_offline_tamper_audit(record_property) -> None:
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        pytest.skip("run this audit regression with Conda environment nanobot")

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    profile_root = LOG_ROOT / f"profile-d-{uuid.uuid4().hex}"
    profile_root.mkdir(parents=False, exist_ok=False)

    baselines: dict[tuple[str, int], Path] = {}
    baseline_rows: list[dict[str, Any]] = []
    for cid in IDS:
        for repeat_index in range(1, 4):
            output = profile_root / f"run-{cid.lower()}-{repeat_index}"
            summary = run("D", output, cid, repeat_index=repeat_index)
            audit_result = audit(output)
            assert summary["status"] == EXPECTED_STATUS[cid], (cid, repeat_index, summary)
            assert summary["ok"] is True, (cid, repeat_index, summary)
            assert summary["formal_quality_eligible"] is False, (cid, repeat_index, summary)
            assert audit_result["ok"] is True, (cid, repeat_index, audit_result)
            assert audit_result.get("qualification") is False, (cid, repeat_index, audit_result)
            baselines[(cid, repeat_index)] = output
            baseline_rows.append({"case_id": cid, "repeat_index": repeat_index,
                                  "status": summary["status"], "audit": audit_result})

    _write_json(profile_root / "baseline-audits.json", baseline_rows)
    tamper_root = profile_root / "tamper-copies"
    tamper_root.mkdir(exist_ok=False)
    tampered = {
        "raw_wire_result_events_rehashed": _tamper_raw_wire(baselines[("V066", 1)], tamper_root),
        "wrong_thread_consistent_events": _tamper_wrong_thread(baselines[("V063", 1)], tamper_root),
        "cross_trial_evidence": _tamper_cross_trial(baselines[("V063", 1)], baselines[("V063", 2)], tamper_root),
        "source_read_deleted": _tamper_missing_source_read(baselines[("V066", 1)], tamper_root),
        "ui_artifact_text_and_parsed": _tamper_ui_text_and_parsed(baselines[("V066", 1)], tamper_root),
        "budget_loosened": _tamper_budget(baselines[("V066", 1)], tamper_root),
        "qualification_forged": _tamper_qualification(baselines[("V065", 1)], tamper_root),
        "source_zip_replaced": _tamper_source_zip(baselines[("V066", 1)], tamper_root),
    }
    tamper_rows: dict[str, Any] = {}
    for label, output in tampered.items():
        report = audit(output)
        assert report["ok"] is False, (label, report)
        assert report.get("qualification") is not True, (label, report)
        tamper_rows[label] = {"path": output.relative_to(ROOT).as_posix(), "audit": report}

    _write_json(profile_root / "tamper-audit.json", tamper_rows)
    record_property("channel_task_audit_profile", profile_root.relative_to(ROOT).as_posix())
    record_property("channel_task_audit_baselines", json.dumps(baseline_rows, ensure_ascii=False))
    record_property("channel_task_audit_tamper_results", json.dumps(tamper_rows, ensure_ascii=False))

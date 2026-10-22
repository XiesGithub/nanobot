"""Offline verification of preserved stage6 contracts, evidence and cleanup."""

import argparse
import zipfile
from pathlib import Path

from evals.batches import analyze_batch_trace, score, summarize
from evals.common import ROOT, canonical, digest, inside, read, strict_json, write


def audit(output, source_root=None):
    output = Path(output).resolve()
    manifest = read(output / "manifest.json")
    rows = [strict_json(line) for line in (output / "records.jsonl").read_text(
        encoding="utf-8").splitlines()]
    errors = []
    def check_sources(directory, frozen):
        for reused in frozen.get("reused_runs", []):
            saved = directory / "reused-sources" / reused["run_id"]
            if digest((saved / "manifest.json").read_bytes()) != reused["manifest_sha256"]:
                errors.append("reused_manifest_hash")
            if digest((saved / "source-snapshot.zip").read_bytes()) != reused["source_snapshot_sha256"]:
                errors.append("reused_source_hash")
            check_sources(saved, read(saved / "manifest.json"))
        with zipfile.ZipFile(directory / "source-snapshot.zip") as archive:
            if set(archive.namelist()) != set(frozen["sources"]):
                errors.append("source_snapshot_inventory")
            for name, expected in frozen["sources"].items():
                if digest(archive.read(name)) != expected:
                    errors.append("source_snapshot_hash:" + name)

    check_sources(output, manifest)
    for row in rows:
        directory = inside(output, row["artifacts"])
        spec = manifest["cases"][row["case_id"]]
        if canonical(read(directory / "contract.json")) != canonical(spec):
            errors.append("contract_changed:" + row["case_id"])
        index = read(directory / "final-artifact-index.json")
        actual_paths = {p.relative_to(directory).as_posix() for p in directory.rglob("*")
                        if p.is_file() and p != directory / "final-artifact-index.json"}
        if actual_paths != set(index):
            errors.append("artifact_inventory:" + row["case_id"])
        for relative, expected in index.items():
            path = inside(directory, relative)
            if not path.exists() or digest(path.read_bytes()) != expected:
                errors.append("artifact_hash:" + row["case_id"] + ":" + relative)
        evidence = read(directory / "evidence.json")
        events = [strict_json(line) for line in (directory / "events.jsonl").read_text(
            encoding="utf-8").splitlines()]
        expected_run = manifest.get("trial_run_ids", {}).get(row["case_id"], manifest.get("run_id"))
        if any(e["trial_id"] != row["trial_id"] or expected_run is not None
               and e["run_id"] != expected_run for e in events):
            errors.append("event_trial_identity:" + row["case_id"])
        trace = analyze_batch_trace(events, row["trial_id"], evidence["status"], evidence["collector_errors"])
        if canonical(trace) != canonical(evidence["trace"]):
            errors.append("trace_replay:" + row["case_id"])
        verdict = score(spec, evidence)
        if canonical(verdict) != canonical(read(directory / "verdict.json")):
            errors.append("score_replay:" + row["case_id"])
        if row["verdict"] != verdict["verdict"] or row["status"] != evidence["status"]:
            errors.append("record_evidence_mismatch:" + row["case_id"])
        scoring_complete = bool(verdict["requirements"]) and all(
            j["passed"] is not None for j in verdict["requirements"])
        if (row["scope"] != spec["scope"] or row["trial_id"] != evidence["trial_id"]
                or row["case_id"] != evidence["case_id"]
                or row["observation_complete"] != trace["closed"]
                or row["scoring_complete"] != scoring_complete
                or row["model_attempts"] != trace["model_attempts"]
                or row["cost"] != (0 if trace["model_attempts"] == 0 else None)):
            errors.append("record_accounting_mismatch:" + row["case_id"])
        for relative, expected in spec["fixture_hashes"].items():
            if evidence["before"].get(relative, {}).get("sha256") != expected:
                errors.append("fixture_hash:" + row["case_id"])
        for relative, item in evidence["after"].items():
            path = inside(directory / "files", relative)
            if not path.exists() or digest(path.read_bytes()) != item["sha256"]:
                errors.append("file_evidence:" + row["case_id"])
        cleanup = read(directory / "cleanup.json")
        instance = Path(cleanup["owned_instance"]).resolve()
        allowed = (Path(source_root or ROOT) / "tmp" / "phase6-instances").resolve()
        if not instance.is_relative_to(allowed) or instance == allowed:
            errors.append("cleanup_boundary:" + row["case_id"])
        if instance != allowed / row["trial_id"]:
            errors.append("cleanup_identity:" + row["case_id"])
        if not cleanup["removed"] or instance.exists():
            errors.append("cleanup_incomplete:" + row["case_id"])
    summary = summarize(manifest["plan"], rows)
    if not summary["inventory_complete"]:
        errors.append("planned_inventory")
    for n in manifest["milestones"]:
        if len(rows) >= n and canonical(read(output / f"milestone-{n}.json")) != canonical(
                summarize(manifest["plan"][:n], rows[:n])):
            errors.append("milestone_replay")
    return {"version": "phase6-audit/1.0", "ok": not errors, "errors": errors,
            "records": len(rows), "summary": summary,
            "model_requests": 0, "agent_reruns": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.output)
    write(args.report, result)
    print({"ok": result["ok"], "errors": result["errors"], "records": result["records"]})
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

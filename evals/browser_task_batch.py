"""Bounded browser batch; observer readiness never converts product failures to passes."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from evals.browser_task_audit import audit
from evals.browser_task_evidence import score
from evals.browser_task_run import VERSION, run, sources
from evals.common import MODEL, ROOT, digest, now, read, write

POSITIVES = [(f"V{number:03}", None, None) for number in range(73, 80)] + [
    ("V080", None, "light"), ("V080", None, "dark")]
NEGATIVES = [("V073", "wrong_answer", None), ("V073", "duplicate_delivery", None),
             ("V074", "skip_edit", None), ("V078", "duplicate_delivery", None),
             ("V078", "refresh_resubmit", None)]
REAL = [(cid, None) for cid in ("V073", "V074", "V078", "V079")] + [
    ("V080", "light"), ("V080", "dark")]
CONTROL_CHECKS = {"wrong_answer": "correct_answer", "duplicate_delivery": None,
                  "skip_edit": "file_state_correct", "refresh_resubmit": "single_original_submit"}


def execute(base, profile, targets, frozen=None):
    rows = []
    for cid, control, theme, directory in targets:
        output = run(cid, profile, base / directory, control, frozen, theme)
        try:
            verdict = score(read(output / "manifest.json"), read(output / "browser-evidence.json"),
                            read(output / "gateway-evidence.json"), workspace_root=output / "workspace")
        except (FileNotFoundError, KeyError, ValueError, TypeError) as error:
            verdict = {"status": "invalid", "formal_qualification": False,
                       "error": type(error).__name__}
        write(output / "verdict.json", verdict)
        receipt = audit(output)
        write(base / (directory + "-audit.json"), receipt)
        write(output / "artifact-index.json", [{"path": path.relative_to(output).as_posix(),
            "sha256": digest(path.read_bytes())} for path in sorted(output.rglob("*")) if path.is_file()])
        rows.append({"directory": directory, "case_id": cid, "control": control, "theme": theme,
                     "verdict": verdict, "audit": receipt})
        print(directory, verdict["status"], receipt["ok"], receipt.get("errors"), flush=True)
    return rows


def scripted(base):
    base.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(base.parent / "environment.json", base / "environment.json")
    write(base / "frozen-scripted-plan.json", {"version": VERSION, "frozen_at": now(),
        "sources": sources(), "environment": read(base / "environment.json"),
        "profile": "B-scripted", "runs": POSITIVES + NEGATIVES, "paid_model_requests": 0,
        "completion": "Original mandatory clauses; actual browser, gateway, tools, files, raw events.",
        "gateway_scope": "Titles disabled; no claim of title/background or external platform cost coverage."})
    rows = execute(base, "B-scripted", [(cid, control, theme,
        "B-scripted-" + cid + "-" + (control or theme or "positive"))
        for cid, control, theme in POSITIVES + NEGATIVES])
    readiness = all(row["audit"]["ok"] and row["verdict"]["status"] in {"pass", "fail"}
                    for row in rows)
    for row in rows:
        if row["control"]:
            check = CONTROL_CHECKS[row["control"]]
            # A negative must fail its intended condition, not merely an unrelated clause.
            if row["control"] == "duplicate_delivery":
                check = ("correct_answer" if row["case_id"] == "V073" else "history_preserved")
            readiness = readiness and row["verdict"]["status"] == "fail" and (
                row["verdict"].get("checks", {}).get(check) is False)
    write(base / "scripted-results.json", {"ok": bool(readiness), "runs": rows,
        "gate": "observer_readiness", "product_passes": sum(
            row["verdict"]["status"] == "pass" for row in rows if not row["control"]),
        "meaning": "Audited product failures remain failures; this gate only permits independent real observation."})


def freeze(base):
    prerequisites = read(base / "scripted-results.json")
    if not prerequisites["ok"]:
        raise ValueError("scripted_observation_controls_not_ready")
    blocked = [row["case_id"] for row in prerequisites["runs"]
               if not row["control"] and row["audit"].get("frontend_precondition_failure")]
    write(base / "frozen-real-plan.json", {"version": VERSION, "frozen_at": now(), "model": MODEL,
        "runs": [{"case_id": cid, "theme": theme,
                  "directory": "B-real-" + cid + ("-" + theme if theme else ""), "sources": sources()}
                 for cid, theme in REAL if cid not in blocked],
        "blocked_real_trials": [{"case_id": cid, "theme": theme,
                                "reason": "scripted_UI_precondition_failed_before_task; no model capability result"}
                               for cid, theme in REAL if cid in blocked],
        "scripted_results_sha256": digest((base / "scripted-results.json").read_bytes()),
        "environment_sha256": digest((base / "environment.json").read_bytes()),
        "budget": {"trials": len(REAL), "requests_total_cap": 8 * len(REAL),
                   "known_tokens_total_cap": 32000 * len(REAL), "seconds_per_trial": 180,
                   "grading_requests": 0, "amount": None, "currency": None},
        "policy": "One independent real trial per original scenario/theme; retain all failures, no resampling.",
        "excluded": "V075/V076/V077 prescribe fixed controls; no forced R conversion."})


def real(base):
    frozen = base / "frozen-real-plan.json"
    rows = execute(base, "B-real", [(row["case_id"], None, row.get("theme"), row["directory"])
                                   for row in read(frozen)["runs"]], frozen)
    write(base / "real-results.json", {"runs": rows, "grading_requests": 0,
        "qualified": sum(row["audit"].get("qualification", False) for row in rows),
        "whole_system_score": None, "product_amount": None, "currency": None})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["scripted", "freeze", "real"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.output.resolve()
    if not base.is_relative_to(ROOT):
        raise ValueError("output_outside_workspace")
    {"scripted": scripted, "freeze": freeze, "real": real}[args.phase](base)

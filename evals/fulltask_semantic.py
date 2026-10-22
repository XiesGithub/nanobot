"""Source-bound V005/V007 task execution with independent program and semantic gates."""

import argparse
import asyncio
import copy
import multiprocessing as mp
import os
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from evals import grading_transport
from evals.common import MODEL, ROOT, canonical, digest, inside, phase1_audit, read, write
from evals.fulltask_cases import VERSION, load_cases, script_for, templates
from evals.grading_transport import GradingClient, prompt_metadata
from evals.run import manifest as execution_manifest
from evals.run import run_trial
from evals.scorer import score
from evals.semantic import (
    REVIEW_SCHEMA,
    document,
    finalize,
    gold_packet,
    reconcile,
    review_packet,
    validate_packet,
    validate_review,
)
from evals.semantic_run import _scripted_transport, judge_selfcheck
from evals.semantic_samples import selfcheck_packet, validate_selfcheck_construction

CONTROLS = {
    "wrong_semantics": "V005", "code_fence": "V005", "no_read": "V007",
    "wrong_unit": "V007", "source_write": "V007", "product_budget": "V007",
    "judge_dispute": "V007", "judge_parse": "V007", "judge_citation": "V007",
    "judge_budget": "V007",
}


def grading_limits(control=None):
    return {"attempts": 4 if control == "judge_budget" else grading_transport.MAX_ATTEMPTS,
            "seconds": grading_transport.MAX_RUN_SECONDS,
            "reserved_tokens": grading_transport.MAX_RUN_TOKENS,
            "per_packet_stage_attempts": grading_transport.MAX_PACKAGE_ATTEMPTS,
            "per_packet_stage_seconds": grading_transport.MAX_PACKAGE_SECONDS,
            "per_packet_stage_reserved_tokens": grading_transport.MAX_PACKAGE_TOKENS,
            "logical_calls_per_packet_stage": grading_transport.MAX_LOGICAL_CALLS_PER_STAGE,
            "attempts_per_logical_call": grading_transport.MAX_ATTEMPTS_PER_CALL,
            "request_timeout_seconds": grading_transport.REQUEST_TIMEOUT_SECONDS,
            "max_output_tokens": grading_transport.MAX_OUTPUT_TOKENS}


def build_plan(cases, mode, control=None, repeat_index=1):
    if mode not in {"D", "R"} or control and (mode != "D" or control not in CONTROLS):
        raise ValueError("unsupported_full_task_profile")
    return [{"trial_id": f"{case['case_id']}-{repeat_index}", "case_id": case["case_id"],
             "case_version": VERSION, "repeat_index": repeat_index,
             "profile": mode + "-" + VERSION, "purpose": "diagnostic" if control else "primary",
             "original_trial_id": None, "fault": control, "fault_condition": control or "none",
             "entry": "AgentLoop.process_direct", "task_type": "ordinary", "scope": "full_task",
             "mode": mode, "product_timeout_s": 0.3 if control == "product_budget" else 15 if mode == "D" else 120,
             **({"script": script_for(case["case_id"], control)} if mode == "D" else {})}
            for case in cases]


def packet_for(case, evidence):
    """The text review remains an archive API; only the outer trial proves execution."""
    packet = copy.deepcopy(templates()[case["case_id"]])
    delivery = [event for event in evidence["events"]
                if event["type"] == "delivery" and event["phase"] == "point"]
    answer = delivery[0]["payload"].get("response") if len(delivery) == 1 else ""
    packet["packet_id"] = evidence["trial_id"]
    packet["answer"] = document("answer", answer if isinstance(answer, str) else "")
    if case["case_id"] == "V007":
        # Preserve the complete frozen source; program/read gates prove it was acquired.
        fixture = evidence["before"].get("note.txt")
        expected = next(source for source in packet["sources"] if source["artifact_id"] == "V007.note.txt")
        if not fixture or fixture.get("sha256") != case["initial_state"]["fixtures"][0]["sha256"]:
            raise ValueError("candidate_source_missing_or_changed")
        if document(expected["artifact_id"], fixture["text"]) != expected:
            raise ValueError("candidate_source_content_mismatch")
    return validate_packet(packet)


def result_valid(result, packet, stage):
    """Revalidate references and reconciliation, rather than trusting stored labels."""
    if (result.get("packet_sha256") != digest(canonical(packet))
            or result.get("stage") != stage):
        return False
    reviews = result.get("reviews", [])
    if not 2 <= len(reviews) <= 3:
        return False
    try:
        for review, role in zip(reviews, ("support_check", "counter_check", "recheck"), strict=False):
            validate_review(review, packet, role, stage, review["review_id"])
        if len({review["review_id"] for review in reviews}) != len(reviews):
            return False
        items = reconcile(reviews, packet)
        labels = {item["label"] for item in items}
        status = (("approved" if labels == {"pass"} else "unresolved") if stage == "gold"
                  else "product_fail" if "fail" in labels else "undetermined" if "unknown" in labels else "pass")
        return result["items"] == items and result["status"] == status and result.get("error") is None
    except (ValueError, KeyError, TypeError):
        return False


def aggregate(case, evidence, program, packet, semantic, gold, selfcheck, mode, grading_cost):
    replayed = score(case, evidence)
    program_bound = program == replayed
    packet_bound = packet == packet_for(case, evidence)
    gold_valid = result_valid(gold, gold_packet(packet), "gold")
    semantic_valid = result_valid(semantic, packet, "judge")
    acquired_citations = (case["case_id"] != "V007" or all(
        any(ref["artifact_id"] == "V007.note.txt" for ref in item["source_evidence"])
        for review in semantic.get("reviews", []) for item in review["items"]
        if item["label"] in {"pass", "fail"}
    ))
    verdict = finalize(program, semantic, gold, selfcheck["ok"], packet)
    if not program_bound or not packet_bound:
        verdict["status"] = "invalid"
    binding = replayed["extensions"].get("full_task_binding", {})
    trace_ok = replayed["extensions"].get("trace_valid") is True and replayed["extensions"].get("trace_closed") is True
    limits = grading_cost["limits"]
    grade_budget = (grading_cost["attempts"] <= limits["attempts"]
                    and grading_cost["reserved_tokens"] <= limits["reserved_tokens"]
                    and grading_cost["elapsed_request_seconds"] <= limits["seconds"]
                    and grading_cost["known_input_tokens"] + grading_cost["known_output_tokens"] <= limits["reserved_tokens"]
                    and (mode == "D" or grading_cost["actual_tokens"] is not None
                         and grading_cost["unknown_usage_attempts"] == 0))
    attempts = [event for event in evidence["events"]
                if event["type"] == "model_attempt" and event["phase"] == "start"]
    real_execution = (mode == "R" and bool(attempts)
                      and all(event["payload"].get("requested_model") == MODEL for event in attempts)
                      and not any(event["type"] == "scripted_response" for event in evidence["events"])
                      and grading_cost["real_attempts"] > 0 and grading_cost["scripted_calls"] == 0
                      and grading_cost["requested_model"] == MODEL)
    gates = {"program_replayed": program_bound, "delivery_packet_bound": packet_bound,
             "program_requirements": program["status"] == "pass",
             "trace_complete": trace_ok, "delivery_complete": binding.get("delivery_event_id") is not None,
             "source_read": binding.get("checks", {}).get("source_read_met") is True,
             "gold_preapproved": gold_valid and gold["status"] == "approved",
             "judge_selfcheck": selfcheck["ok"] is True,
             "semantic_references_and_resolution": semantic_valid and semantic["status"] == "pass",
             "acquired_source_citations": acquired_citations,
             "grading_budget_and_usage": bool(grade_budget), "real_product_and_grading": bool(real_execution)}
    if not grade_budget and verdict["status"] == "pass":
        verdict["status"] = "undetermined"
    if not (gold_valid and semantic_valid and acquired_citations) and verdict["status"] == "pass":
        verdict["status"] = "invalid"
    if mode == "R" and not real_execution and verdict["status"] == "pass":
        verdict["status"] = "invalid"
    verdict.update(scope="full_task", scorer_version=VERSION, trial_id=evidence["trial_id"],
                   case_id=case["case_id"], case_version=case["case_version"], mode=mode,
                   binding=binding, gates=gates,
                   formal_quality_eligible=verdict["status"] == "pass" and all(gates.values()),
                   finalization="independent_program_then_bound_semantic_aggregation",
                   cost_metric_available=mode == "D" or grading_cost["amount"] is not None,
                   limitations=["Only these two isolated tasks; no channel, subagent or 120-case claim.",
                                "Unknown prices do not imply zero expense; input reservation remains heuristic."])
    return verdict


def deterministic_transport(labels, control=None):
    base = _scripted_transport(labels)

    async def create(**request):
        payload = grading_transport.strict_json(request["messages"][-1]["content"])
        result = await base(**request)
        if payload["stage"] == "selfcheck":
            return result
        review = grading_transport.strict_json(result["choices"][0]["message"]["content"])
        if payload["packet"]["case_id"] == "V007":
            note = next(source for source in payload["packet"]["sources"]
                        if source["artifact_id"] == "V007.note.txt")
            for item in review["items"]:
                item["source_evidence"] = [{"artifact_id": note["artifact_id"], "sha256": note["sha256"],
                                            "start": 0, "end": len(note["text"]), "quote": note["text"]}]
        if payload["stage"] != "judge":
            result["choices"][0]["message"]["content"] = canonical(review).decode()
            return result
        if control == "judge_parse":
            result["choices"][0]["message"]["content"] = "{invalid synthetic judge response"
            return result
        if control == "judge_citation":
            review["items"][0]["answer_evidence"][0]["quote"] = "tampered citation"
        if control == "judge_dispute" and payload["role"] in {"counter_check", "recheck"}:
            for item in review["items"]:
                item.update(label="unknown", objections=["synthetic unresolved disagreement"])
        result["choices"][0]["message"]["content"] = canonical(review).decode()
        return result
    return create


def invalid_semantic(packet, reason):
    return {"status": "invalid", "items": [], "reviews": [],
            "packet_sha256": digest(canonical(packet)), "stage": "judge", "error": reason}


async def run(mode, output, control=None, repeat_index=1):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Run with conda run -n nanobot")
    cases = load_cases()
    if control:
        if mode != "D" or control not in CONTROLS:
            raise ValueError("controls_require_deterministic_mode")
        cases = [case for case in cases if case["case_id"] == CONTROLS[control]]
    plan = build_plan(cases, mode, control, repeat_index)
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT) or output == ROOT:
        raise ValueError("output_outside_repository")
    output.mkdir(parents=True, exist_ok=False)
    frozen_templates = {case["case_id"]: templates()[case["case_id"]] for case in cases}
    golds = {gold_packet(template)["packet_id"]: gold_packet(template)
             for template in frozen_templates.values()}
    controls, expected, anchors = selfcheck_packet()
    controls = {key: value for key, value in controls.items() if key != "hard_checks"}
    if validate_selfcheck_construction(controls, expected, anchors):
        raise ValueError("invalid_selfcheck_construction")
    manifest = execution_manifest(cases, mode, plan, output.name, phase1_audit())
    sources = manifest["snapshot"]["file_sha256"]
    for case in cases:
        relative = case["source_ref"].split("#")[0]
        sources[relative] = digest((ROOT / relative).read_bytes())
        for fixture in case["initial_state"]["fixtures"]:
            relative = "evals/" + fixture["source"]
            sources[relative] = fixture["sha256"]
    manifest.update(version=VERSION, scorer_version=VERSION, mode=mode, requested_model=MODEL if mode == "R" else None,
                    grading_schema_version=REVIEW_SCHEMA, prompt=prompt_metadata(),
                    scope="full_task", cases={case["case_id"]: case for case in cases}, plan=plan,
                    templates=frozen_templates, golds=golds, sources=sources,
                    selfcheck={"packet": controls, "labels": expected, "anchors": anchors},
                    packet_ids_by_trial={item["trial_id"]: item["trial_id"] for item in plan},
                    budget={"product": {"per_trial_attempts": 8, "tools": 16, "tokens": 32000,
                                        "seconds": 120 if mode == "R" else 15, "total_trial_count": len(plan)},
                            "grading": grading_limits(control),
                            "input_reservation": "UTF8-byte heuristic; actual known usage required for R pass"})
    manifest["dataset"].update(version=VERSION, case_ids=[case["case_id"] for case in cases])
    manifest["profiles"].update(version=VERSION, mode=mode)
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for relative, expected_hash in sources.items():
            data = inside(ROOT, relative).read_bytes()
            if digest(data) != expected_hash:
                raise ValueError("source_changed_during_freeze")
            archive.writestr(relative, data)
    for gid, gold in golds.items():
        write(output / "gold" / (gid + ".json"), gold)
    labels = {controls["packet_id"]: expected}
    for case, item in zip(cases, plan, strict=True):
        labels[item["trial_id"]] = {req["requirement_id"]: "fail" if control == "wrong_semantics" and req["requirement_id"] == "V005.location" else "pass"
                                    for req in frozen_templates[case["case_id"]]["requirements"]}
    grading_dir = output / "grading"
    grading_dir.mkdir()
    isolation = ROOT / "tmp/fulltask-instances"
    isolation.mkdir(parents=True, exist_ok=True)
    collected, rows = [], []
    with patch.object(grading_transport, "MAX_ATTEMPTS", grading_limits(control)["attempts"]):
        client = GradingClient(grading_dir, mode=mode,
                               transport=deterministic_transport(labels, control) if mode == "D" else None)
        try:
            selfcheck = await judge_selfcheck(client, controls, expected)
            write(output / "selfcheck.json", selfcheck)
            reviewed = {}
            for gid, gold in golds.items():
                result = (await review_packet(client, gold, "gold") if selfcheck["ok"] else
                          {"status": "invalid", "items": [], "reviews": [], "stage": "gold",
                           "packet_sha256": digest(canonical(gold)), "error": "selfcheck_failed"})
                reviewed[gid] = result
                write(output / "gold-reviews" / (gid + ".json"), result)
            # Candidate execution starts only after standards have been frozen and reviewed.
            for case, item in zip(cases, plan, strict=True):
                returned = run_trial(case, item, output, isolation, mode, mp.get_context("spawn"))
                trial_dir = output / "trials" / item["trial_id"]
                if not (trial_dir / "artifacts/evidence.json").exists():
                    rows.append({"trial_id": item["trial_id"], "case_id": case["case_id"],
                                 "status": "invalid", "formal_quality_eligible": False,
                                 "execution_status": returned["execution_status"], "harness_error": True})
                    continue
                evidence = read(trial_dir / "artifacts/evidence.json")
                program = read(trial_dir / "verdicts/1.json")
                try:
                    packet = packet_for(case, evidence)
                except (KeyError, ValueError):
                    packet = copy.deepcopy(frozen_templates[case["case_id"]])
                    packet.update(packet_id=item["trial_id"], answer=document("answer", ""))
                    semantic = invalid_semantic(packet, "candidate_source_missing_or_changed")
                else:
                    gold = reviewed[gold_packet(packet)["packet_id"]]
                    semantic = (await review_packet(client, packet, "judge") if selfcheck["ok"] and gold["status"] == "approved"
                                else invalid_semantic(packet, "grading_prerequisite_failed"))
                write(trial_dir / "packet.json", packet)
                write(trial_dir / "semantic-result.json", semantic)
                collected.append((case, item, evidence, program, packet, semantic))
        finally:
            await client.aclose()
        grading_cost = client.summary()
    write(output / "grading-cost.json", grading_cost)
    for case, item, evidence, program, packet, semantic in collected:
        gold = reviewed[gold_packet(packet)["packet_id"]]
        try:
            verdict = aggregate(case, evidence, program, packet, semantic, gold, selfcheck, mode, grading_cost)
        except (KeyError, ValueError):
            verdict = {"status": "invalid", "scope": "full_task", "trial_id": item["trial_id"],
                       "case_id": case["case_id"], "formal_quality_eligible": False,
                       "reason": "candidate_binding_invalid"}
        write(output / "trials" / item["trial_id"] / "full-task-verdict.json", verdict)
        rows.append({"trial_id": item["trial_id"], "case_id": case["case_id"],
                     "repeat_index": repeat_index, "status": verdict["status"],
                     "formal_quality_eligible": verdict["formal_quality_eligible"],
                     "execution_status": evidence["execution_status"],
                     "product_trace": read(output / "trials" / item["trial_id"] / "artifacts/trace-analysis.json"),
                     "binding": verdict.get("binding")})
        print(f"{mode} {item['trial_id']}: {verdict['status']} eligible={verdict['formal_quality_eligible']}", flush=True)
    expected_status = ("undetermined" if control == "judge_dispute" else
                       "invalid" if control in {"judge_parse", "judge_citation", "judge_budget"} else
                       "product_fail" if control else "pass")
    summary = {"version": VERSION, "mode": mode, "scope": "full_task", "control": control,
               "planned_trials": len(plan), "rows": rows, "counts": dict(Counter(row["status"] for row in rows)),
               "ok": len(rows) == len(plan) and all(row["status"] == expected_status for row in rows)
                     and (mode == "D" or all(row["formal_quality_eligible"] for row in rows)),
               "formal_quality_eligible": mode == "R" and len(rows) == len(plan) and all(row["formal_quality_eligible"] for row in rows),
               "grading_cost": grading_cost,
               "product_cost": {"model_attempts": sum(row.get("product_trace", {}).get("model_attempts", 0) for row in rows),
                                "known_tokens": sum(row.get("product_trace", {}).get("known_tokens", 0) for row in rows),
                                "amount": 0 if mode == "D" else None, "currency": None,
                                "cost_status": "not_applicable" if mode == "D" else "prices_unknown"}}
    write(output / "run-result.json", summary)
    write(output / "records.json", rows)
    (output / "report.md").write_text(
        f"# {mode} V005/V007 full-task semantic evidence\n\n{summary['counts']}\n\n"
        f"Formal qualification: {summary['formal_quality_eligible']}.\n"
        "D controls are protocol evidence only. Every planned trial is retained. Unknown prices are null.\n",
        encoding="utf-8")
    index = [{"path": path.relative_to(output).as_posix(), "sha256": digest(path.read_bytes())}
             for path in sorted(output.rglob("*")) if path.is_file()]
    write(output / "artifact-index.json", index)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["D", "R"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control", choices=sorted(CONTROLS))
    parser.add_argument("--repeat-index", type=int, choices=[1, 2, 3], default=1)
    args = parser.parse_args()
    result = asyncio.run(run(args.mode, args.output, args.control, args.repeat_index))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

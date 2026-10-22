"""Isolated genuine mail Agent tasks with bounded independent semantic scoring."""

import argparse
import asyncio
import copy
import multiprocessing as mp
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from evals import grading_transport
from evals.common import MODEL, ROOT, canonical, digest, inside, phase1_audit, read, write
from evals.fulltask_semantic import grading_limits, invalid_semantic, result_valid
from evals.grading_transport import GradingClient, prompt_metadata
from evals.mail_task_cases import IDS, MODEL_PROFILE, VERSION, encoder_pin, load_cases, templates
from evals.mail_task_evidence import binding, points, score
from evals.run import manifest as execution_manifest
from evals.run import run_trial
from evals.semantic import (
    REVIEW_SCHEMA,
    document,
    finalize,
    gold_packet,
    review_packet,
    validate_packet,
)
from evals.semantic_run import _scripted_transport, judge_selfcheck
from evals.semantic_samples import selfcheck_packet

CONTROLS = ("no_sources", "route_general", "wrong_phone", "latest_final", "certain_owner",
            "judge_dispute", "judge_parse", "judge_citation", "judge_budget",
            "semantic_not_ready", "weights_missing", "index_mismatch", "keyword_substitute",
            "mandatory_synonym", "wrong_duration", "valid_48")
UNAVAILABLE_CONTROLS = {"semantic_not_ready", "weights_missing", "index_mismatch"}


def build_plan(case, mode, control=None, repeat_index=1):
    if mode not in {"D", "R"} or control and (mode != "D" or control not in CONTROLS):
        raise ValueError("unsupported_mail_task_profile")
    if type(repeat_index) is not int or not 1 <= repeat_index <= 3:
        raise ValueError("unsupported_repeat_index")
    return [{"trial_id": case["case_id"] + "-" + str(repeat_index), "case_id": case["case_id"],
             "case_version": VERSION, "repeat_index": repeat_index, "profile": mode + "-" + VERSION,
             "purpose": "diagnostic" if mode == "D" else "primary", "fault": control,
             "original_trial_id": None, "scope": "full_task", "entry": "AgentLoop.process_direct/MailTurnExtension",
             "mode": mode, "product_timeout_s": 30 if mode == "D" else 180,
             **({"setup_timeout_s": 120} if case.get("encoder_pin") else {}),
             **({"script": [{"content": "mail_profile_dynamic_provider"}]} if mode == "D" else {})}]


def packet_for(case, evidence):
    packet = copy.deepcopy(templates()[case["case_id"]])
    deliveries = points(evidence, "delivery")
    text = deliveries[0]["payload"].get("response", "") if len(deliveries) == 1 else ""
    packet.update(packet_id=evidence["trial_id"], answer=document("answer", text if isinstance(text, str) else ""))
    return validate_packet(packet)


def aggregate(case, evidence, program, packet, semantic, gold, selfcheck, mode, costs):
    replayed = score(case, evidence)
    proof = binding(case, evidence)
    semantic_ok = result_valid(semantic, packet, "judge")
    gold_ok = result_valid(gold, gold_packet(packet), "gold") and gold["status"] == "approved"
    verdict = finalize(program, semantic, gold, selfcheck["ok"], packet)
    if (program["status"] == "undetermined" and case.get("encoder_pin")
            and not proof["checks"].get("semantic_environment", False)):
        # Missing local semantic infrastructure cannot establish a product result.
        verdict["status"] = "undetermined"
    limits = costs["limits"]
    budget = costs["attempts"] <= limits["attempts"] and costs["reserved_tokens"] <= limits["reserved_tokens"] and costs["elapsed_request_seconds"] <= limits["seconds"] and costs["known_input_tokens"] + costs["known_output_tokens"] <= limits["reserved_tokens"]
    usage = mode == "D" or costs["actual_tokens"] is not None and costs["unknown_usage_attempts"] == 0
    real = mode == "R" and costs["real_attempts"] > 0 and costs["scripted_calls"] == 0 and not any(e["type"] == "scripted_response" for e in evidence["events"]) and proof["trace"]["model_attempts"] > 0
    refs = [ref for review in semantic.get("reviews", []) for item in review["items"] for ref in item["source_evidence"]]
    # The judge may use the frozen distractor for a negative claim, but every
    # decisive requirement must also cite an actually acquired source.
    acquired = set(proof["acquired_sources"])
    citations = all(any(r["artifact_id"] in acquired for r in item["source_evidence"])
                    for review in semantic.get("reviews", []) for item in review["items"] if item["label"] != "unknown")
    gates = {"program_replayed": program == replayed, "program_pass": program["status"] == "pass",
             "delivery_bound": packet == packet_for(case, evidence), "gold_preapproved": gold_ok,
             "selfcheck": selfcheck["ok"], "semantic_valid": semantic_ok and semantic["status"] == "pass",
             "actual_source_citations": citations and bool(refs), "budget": budget, "usage": usage,
             "real_execution": real, "requested_model": mode == "D" or costs["requested_model"] == MODEL and all(e["payload"].get("requested_model") == MODEL for e in evidence["events"] if e["type"] == "model_attempt" and e["phase"] == "start")}
    if verdict["status"] == "pass" and not all(v for k, v in gates.items() if k != "real_execution" or mode == "R"):
        verdict["status"] = "invalid" if not semantic_ok or not gold_ok or not citations else "undetermined"
    verdict.update(version=VERSION, scope="full_task", trial_id=evidence["trial_id"], case_id=case["case_id"],
                   binding=proof, gates=gates, formal_quality_eligible=verdict["status"] == "pass" and all(gates.values()),
                   cost_metric_available=costs["amount"] is not None,
                   acceptance="Program and semantic gates plus independent offline audit are required.")
    return verdict


def transport(labels, control):
    base = _scripted_transport(labels)

    async def create(**kwargs):
        payload = grading_transport.strict_json(kwargs["messages"][-1]["content"])
        result = await base(**kwargs)
        if payload["stage"] == "selfcheck":
            return result
        review = grading_transport.strict_json(result["choices"][0]["message"]["content"])
        docs = {s["artifact_id"]: s for s in payload["packet"]["sources"]}
        requirements = {r["requirement_id"]: r for r in payload["packet"]["requirements"]}
        for item in review["items"]:
            item["source_evidence"] = [{"artifact_id": s["artifact_id"], "sha256": s["sha256"], "start": 0, "end": len(s["text"]), "quote": s["text"]}
                                       for s in (docs[i] for i in requirements[item["requirement_id"]]["source_ids"])]
            if payload["stage"] == "judge" and control == "judge_dispute" and payload["role"] in {"counter_check", "recheck"}:
                item.update(label="unknown", objections=["synthetic unresolved disagreement"])
        if payload["stage"] == "judge" and control == "judge_citation":
            review["items"][0]["answer_evidence"][0]["quote"] = "synthetic corrupt citation"
        result["choices"][0]["message"]["content"] = ("{invalid" if payload["stage"] == "judge" and control == "judge_parse" else canonical(review).decode())
        return result
    return create


def expected_status(control):
    return "undetermined" if control == "judge_dispute" or control in UNAVAILABLE_CONTROLS else "invalid" if control in {"judge_parse", "judge_citation", "judge_budget"} else "product_fail" if control else "pass"


async def run(mode, output, case_id, control=None, repeat_index=1, semantic_profile=False):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Use Conda nanobot")
    case = next(c for c in load_cases() if c["case_id"] == case_id)
    case["mode"] = mode
    if semantic_profile:
        case["encoder_pin"] = encoder_pin()
        case["environment_profile"] = "mail-semantic-full-task/1.0"
    plan = build_plan(case, mode, control, repeat_index)
    output = inside(ROOT, Path(output).resolve().relative_to(ROOT))
    output.mkdir(parents=True, exist_ok=False)
    template = templates()[case_id]
    gold = gold_packet(template)
    self_packet, self_labels, anchors = selfcheck_packet()
    self_packet = {k: v for k, v in self_packet.items() if k != "hard_checks"}
    base_manifest = execution_manifest([case], mode, plan, output.name, phase1_audit())
    sources = base_manifest["snapshot"]["file_sha256"]
    sources.update({"evals/mail_task_fixtures.json": digest((ROOT / "evals/mail_task_fixtures.json").read_bytes()), case["source_ref"].split("#")[0]: case["source_sha256"]})
    limits = grading_limits("judge_budget" if control == "judge_budget" else None)
    manifest = {"version": VERSION, "scope": "full_task", "run_id": output.name, "case": case, "plan": plan,
                "mode": mode, "requested_model": MODEL if mode == "R" else None, "control": control,
                "repeat_index": repeat_index, "semantic_profile": semantic_profile,
                "sources": sources, "snapshot": base_manifest["snapshot"], "model_profile": MODEL_PROFILE,
                "template": template, "gold": gold, "grading_schema_version": REVIEW_SCHEMA,
                "prompt": prompt_metadata(), "selfcheck": {"packet": self_packet, "labels": self_labels, "anchors": anchors},
                "budget": {"product": {"attempts": 8, "known_tokens": 32000, "seconds": plan[0]["product_timeout_s"]}, "grading": limits},
                "environment": {**base_manifest["environment"], "conda": "nanobot", "encoder": case.get("encoder_pin"), "retrieval": "bge-hybrid-v1" if case.get("encoder_pin") else "fixed-text-v1", "sending": False,
                                "archive_enabled": True, "unifiedSession": False, "OS_sandbox": False}}
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in sources.items():
            data = inside(ROOT, name).read_bytes()
            if digest(data) != sha:
                raise ValueError("source_changed")
            archive.writestr(name, data)
    write(output / "gold.json", gold)
    labels = {self_packet["packet_id"]: self_labels, plan[0]["trial_id"]: {
        r["requirement_id"]: "fail" if control in {"wrong_phone", "latest_final", "certain_owner", "wrong_duration"} else "pass"
        for r in template["requirements"]}}
    (output / "grading").mkdir()
    with patch.object(grading_transport, "MAX_ATTEMPTS", limits["attempts"]):
        client = GradingClient(output / "grading", mode, transport(labels, control) if mode == "D" else None)
        try:
            selfcheck = await judge_selfcheck(client, self_packet, self_labels)
            write(output / "selfcheck.json", selfcheck)
            gold_review = await review_packet(client, gold, "gold") if selfcheck["ok"] else invalid_semantic(gold, "selfcheck_failed")
            write(output / "gold-review.json", gold_review)
            isolation = ROOT / "tmp/mail-task-instances"
            isolation.mkdir(parents=True, exist_ok=True)
            run_trial(case, plan[0], output, isolation, mode, mp.get_context("spawn"))
            trial = output / "trials" / plan[0]["trial_id"]
            evidence = read(trial / "artifacts/evidence.json")
            program = read(trial / "verdicts/1.json")
            packet = packet_for(case, evidence)
            write(trial / "packet.json", packet)
            semantic = await review_packet(client, packet, "judge") if selfcheck["ok"] and gold_review["status"] == "approved" else invalid_semantic(packet, "grading_prerequisite_failed")
            write(trial / "semantic-result.json", semantic)
        finally:
            await client.aclose()
        costs = client.summary()
    write(output / "grading-cost.json", costs)
    verdict = aggregate(case, evidence, program, packet, semantic, gold_review, selfcheck, mode, costs)
    write(trial / "full-task-verdict.json", verdict)
    expected = expected_status(control)
    summary = {"version": VERSION, "mode": mode, "case_id": case_id, "status": verdict["status"],
               "formal_quality_eligible": verdict["formal_quality_eligible"],
               "ok": verdict["status"] == expected and (mode == "D" or verdict["formal_quality_eligible"]),
               "grading_cost": costs, "product_cost": {"attempts": verdict["binding"]["trace"]["model_attempts"],
                                                        "known_tokens": verdict["binding"]["trace"]["known_tokens"],
                                                        "amount": None if mode == "R" else 0, "currency": None}}
    write(output / "run-result.json", summary)
    write(output / "artifact-index.json", [{"path": p.relative_to(output).as_posix(), "sha256": digest(p.read_bytes())} for p in sorted(output.rglob("*")) if p.is_file()])
    print(mode, case_id, control, verdict["status"], "eligible", verdict["formal_quality_eligible"], flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["D", "R"], required=True)
    parser.add_argument("--case", choices=IDS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control", choices=CONTROLS)
    parser.add_argument("--repeat-index", type=int, default=1)
    parser.add_argument("--semantic-profile", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.mode, args.output, args.case, args.control, args.repeat_index, args.semantic_profile))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

"""Phase-4 fixed archive grading. Never executes or reruns product tasks."""

import argparse
import asyncio
from collections import Counter
from pathlib import Path

from evals.common import MODEL, ROOT, canonical, digest, inside, now, strict_json, write
from evals.semantic import (
    REVIEW_SCHEMA,
    VERSION,
    finalize,
    gold_packet,
    review_packet,
    validate_packet,
    validate_review,
)
from evals.semantic_samples import (
    fixed_sample_expectations,
    fixed_samples,
    selfcheck_packet,
    validate_selfcheck_construction,
)


def program_checks(packet):
    """Only checks observable archive bytes. No invented tool/file assertions."""
    validate_packet(packet)
    origin = packet.get("origin", {})
    if origin.get("source_ref", "").startswith("docs/"):
        path = inside(ROOT, origin["source_ref"].split("#")[0])
        if digest(path.read_bytes()) != origin.get("sha256"):
            raise ValueError("origin_hash_mismatch")
    answer = packet["answer"]["text"]
    requirements = [{"requirement_id": "archive_integrity", "label": "pass",
                     "rationale": "LF normalized document hashes and requirement sources verified"}]
    if packet["case_id"] == "V005":
        single_paragraph = len([p for p in answer.strip().split("\n\n") if p.strip()]) == 1
        ok = bool(answer.strip()) and "```" not in answer and single_paragraph
        requirements.append({"requirement_id": "output_format", "label": "pass" if ok else "fail",
                             "rationale": "nonempty translation, no code fence or extra paragraphs"})
    return {"scorer_version": VERSION + "/archive-program", "status": "product_fail" if any(
        r["label"] == "fail" for r in requirements) else "pass", "requirements": requirements,
        "scope": "archive_bytes_only", "full_task_evidence": False}


def _scripted_transport(labels):
    """Contract mode uses frozen labels outside packets, never a model score."""
    async def create(**request):
        payload = strict_json(request["messages"][-1]["content"])
        packet = payload["packet"]
        # The trusted caller controls role/review identity; parse its wrapper.
        role, stage, review_id = payload["role"], payload["stage"], payload["review_id"]
        items = []
        for req in packet["requirements"]:
            rid = req["requirement_id"]
            doc = next(d for d in packet["sources"] if d["artifact_id"] == req["source_ids"][0])
            target = (next(d for d in packet["gold_documents"] if d["artifact_id"] == "gold:" + rid)
                      if stage == "gold" else packet["answer"])
            start, end = req.get("answer_range", [0, len(target["text"])]) if stage != "gold" else [0, len(target["text"])]

            def ref(d, first=0, last=None):
                last = len(d["text"]) if last is None else last
                return {"artifact_id": d["artifact_id"], "sha256": d["sha256"],
                        "start": first, "end": last, "quote": d["text"][first:last]}

            label = "pass" if stage == "gold" else labels[packet["packet_id"]][rid]
            items.append({"requirement_id": rid, "label": label,
                          "source_evidence": [ref(doc)],
                          "gold_evidence" if stage == "gold" else "answer_evidence": [ref(target, start, end)],
                          "rationale": "Frozen scripted contract label; no model inference.",
                          "objections": [], "missing_requirement": False, "resolves": []})
        review = {"schema_version": REVIEW_SCHEMA, "review_id": review_id, "role": role,
                  "packet_sha256": digest(canonical(packet)), "items": items}
        return {"choices": [{"message": {"content": canonical(review).decode()}}],
                "usage": None, "model": "scripted-contract", "id": "scripted-response"}
    return create


async def judge_selfcheck(client, packet, expected):
    reviews, errors = [], []
    for role in ("support_check", "counter_check"):
        try:
            reviews.append(await client.review(packet, role, "selfcheck", validate_review))
        except Exception as exc:
            errors.append(getattr(exc, "reason", type(exc).__name__))
            break
    labels = [{i["requirement_id"]: i["label"] for i in r["items"]} for r in reviews]
    checks = []
    for rid, label in expected.items():
        observed = [rows.get(rid) for rows in labels]
        checks.append({"requirement_id": rid, "expected": label, "observed": observed,
                       "ok": len(observed) == 2 and observed == [label, label]})
    return {"scorer_version": VERSION, "packet_sha256": digest(canonical(packet)),
            "ok": not errors and all(c["ok"] for c in checks), "reviews": reviews,
            "checks": checks, "errors": errors, "planned_controls": 12,
            "planned_repeats": 2, "human_labels_required": False,
            "scope": "six_program_constructed_positive_negative_pairs",
            "negative_judgements_planned": 12, "positive_judgements_planned": 12,
            "false_passes": sum(sum(x == "pass" for x in c["observed"])
                                for c in checks if c["expected"] == "fail"),
            "false_rejects": sum(sum(x == "fail" for x in c["observed"])
                                 for c in checks if c["expected"] == "pass"),
            "unknown_or_missing": sum(2 - len(c["observed"]) + sum(
                x not in ("pass", "fail") for x in c["observed"]) for c in checks),
            "consistent_pairs": sum(len(c["observed"]) == 2 and len(set(c["observed"])) == 1 for c in checks)}


async def run(mode, output):
    import os

    from evals.grading_transport import GradingClient, prompt_metadata

    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("Run with conda run -n nanobot")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    samples = fixed_samples()
    expected = fixed_sample_expectations()
    controls, control_labels, anchors = selfcheck_packet()
    construction_errors = validate_selfcheck_construction(controls, control_labels, anchors)
    if construction_errors:
        raise ValueError("selfcheck_construction_invalid")
    # Program labels are collected separately; judges see facts, never program scores.
    packets = [validate_packet({k: v for k, v in p.items() if k != "hard_checks"}) for p in samples]
    controls = validate_packet({k: v for k, v in controls.items() if k != "hard_checks"})
    program = {p["packet_id"]: program_checks(p) for p in packets}
    golds = {gold_packet(p)["packet_id"]: gold_packet(p) for p in packets}
    for p in [controls, *packets, *golds.values()]:
        write(inside(output / "packets", p["packet_id"] + ".json"), p)
    write(output / "selfcheck-construction.json", {"labels": control_labels, "anchors": anchors})
    write(output / "program-verdicts.json", program)
    code = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in sorted(
        (ROOT / "evals").glob("*.py"))}
    plan = {"schema_version": "agent-eval/1.0", "scorer_version": VERSION,
            "review_schema_version": REVIEW_SCHEMA, "prompt": prompt_metadata(),
            "created_at": now(), "mode": mode, "requested_model": MODEL if mode == "R" else None,
            "scope": "fixed_archived_semantic_samples", "full_task_validation": False,
            "case_ids": sorted({p["case_id"] for p in packets}),
            "sample_ids": [p["packet_id"] for p in packets], "selfcheck_controls": 12,
            "selfcheck_repeats": 2, "gold_ids": list(golds), "file_sha256": code,
            "packet_sha256": {p["packet_id"]: digest(canonical(p)) for p in [controls, *packets, *golds.values()]},
            "budget": {"attempts": 24, "run_s": 600, "tokens_reserved": 192000,
                       "per_packet_attempts": 6, "per_packet_s": 270, "per_packet_tokens": 96000,
                       "logical_call_s": 90, "max_output_tokens": 4096},
            "normalization": "LF Unicode character [start,end), SHA256 UTF-8",
            "input_reservation": "UTF8-byte heuristic; service token upper bound unverified; diagnostic only",
            "selfcheck_truth": "program_constructed_literal_relations_not_model_consensus",
            "acceptance": "two_roles_then_at_most_one_recheck; no_unresolved_pass"}
    write(output / "manifest.json", plan)
    labels = {**expected, controls["packet_id"]: control_labels}
    client = GradingClient(output, mode=mode, transport=_scripted_transport(labels) if mode == "D" else None)
    results = []
    try:
        selfcheck = await judge_selfcheck(client, controls, control_labels)
        write(output / "selfcheck.json", selfcheck)
        reviewed_golds = {}
        # All standards reviewed before ANY candidate is judged. No answer leaks.
        for gid, gold in golds.items():
            if selfcheck["ok"]:
                result = await review_packet(client, gold, "gold")
            else:
                result = {"status": "invalid", "packet_sha256": digest(canonical(gold)),
                          "reviews": [], "items": [], "error": "selfcheck_failed", "stage": "gold"}
            reviewed_golds[gid] = result
            write(inside(output / "gold-reviews", gid + ".json"), result)
        for packet in packets:
            gid = gold_packet(packet)["packet_id"]
            gold = reviewed_golds[gid]
            if selfcheck["ok"] and gold["status"] == "approved":
                semantic = await review_packet(client, packet, "judge")
            else:
                semantic = {"status": "invalid", "packet_sha256": digest(canonical(packet)),
                            "reviews": [], "items": [], "error": "grading_prerequisite_failed", "stage": "judge"}
            verdict = finalize(program[packet["packet_id"]], semantic, gold, selfcheck["ok"], packet)
            verdict.update(verdict_id=packet["packet_id"] + "-v1", revision=1, supersedes=None,
                           selfcheck_ref="selfcheck.json",
                           gold_review_path="gold-reviews/" + gid + ".json")
            write(inside(output / "verdicts", packet["packet_id"] + ".json"), verdict)
            actual = {r["requirement_id"]: r["label"] for r in semantic["items"]}
            row = {"packet_id": packet["packet_id"], "status": verdict["status"],
                   "semantic_status": semantic["status"], "program_status": program[packet["packet_id"]]["status"],
                   "expected_labels": expected[packet["packet_id"]], "observed_labels": actual,
                   "semantic_labels_match": actual == expected[packet["packet_id"]],
                   "gold_status": gold["status"], "grading_valid": verdict["grading_valid"]}
            results.append(row)
            print(f"{mode} {row['packet_id']}: {row['status']}", flush=True)
    finally:
        await client.aclose()
    summary = {"scorer_version": VERSION, "mode": mode, "scope": plan["scope"],
               "full_task_validation": False, "formal_quality_eligible": False,
               "selfcheck_ok": selfcheck["ok"], "selfcheck_consistency": selfcheck["consistent_pairs"],
               "gold_counts": dict(Counter(g["status"] for g in reviewed_golds.values())),
               "counts": dict(Counter(r["status"] for r in results)), "results": results,
               "ok": selfcheck["ok"] and all(r["semantic_labels_match"] and r["grading_valid"] for r in results),
               "product_cost": {"model_attempts": 0, "amount": 0, "reason": "archives_only_no_product_execution"},
               "grading_cost": client.summary(), "human_review_required": False}
    write(output / "run-result.json", summary)
    lines = [f"# Phase 4 — {mode} fixed archive grading", "", "No product tasks executed; no 120-case score.",
             "", f"Selfcheck: {selfcheck['ok']}; consistency {selfcheck['consistent_pairs']}/12.",
             f"Gold review: {summary['gold_counts']}.", "", "| Archive | Program | Semantic | Final | Labels match |",
             "|---|---|---|---|---|"]
    lines.extend(f"| {r['packet_id']} | {r['program_status']} | {r['semantic_status']} | {r['status']} | {r['semantic_labels_match']} |" for r in results)
    lines += ["", "Costs are separate: product requests=0; grading usage in run-result.json.",
              "Unknown price is null. Same-model roles are not independent ground truth.",
              "Gold is source-bound; uncertain disputes do not pass; hard program failures are retained."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    index = [{"path": p.relative_to(output).as_posix(), "sha256": digest(p.read_bytes())}
             for p in sorted(output.rglob("*")) if p.is_file()]
    write(output / "artifact-index.json", index)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("D", "R"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.mode, args.output))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

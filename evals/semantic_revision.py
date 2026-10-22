"""Append-only archived semantic revisions; never execute a product Agent."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from evals import grading_transport
from evals.browser_task_run import sources
from evals.common import MODEL, ROOT, canonical, digest, now, read, write
from evals.fulltask_semantic import invalid_semantic, result_valid
from evals.grading_transport import GradingClient, GradingError
from evals.privacy_task_evidence import score as privacy_score
from evals.privacy_task_semantic import packet_for as privacy_packet
from evals.security_task_evidence import score as security_score
from evals.security_task_semantic import packet_for as security_packet
from evals.semantic import finalize, gold_packet, review_packet
from evals.semantic_run import judge_selfcheck
from evals.semantic_samples import selfcheck_packet, validate_selfcheck_construction

VERSION = "archived-semantic-revision/2.0"
BUDGET = {"attempts": 40, "seconds": 600, "reserved_tokens": 800000,
          "known_tokens": 128000, "admission_known_tokens": 96000,
          "attempts_per_logical_call": 1, "product_requests": 0}
SECURITY = ROOT / "docs/agent-evaluation-security-fulltask-20261011/R"
PRIVACY = ROOT / "docs/agent-evaluation-privacy-family-20261011"


def archived_runs():
    return ([SECURITY / f"R-{cid}-{repeat}" for cid in ("V094", "V106")
             for repeat in range(1, 4)] + [PRIVACY / f"R-0{repeat}" for repeat in range(1, 4)])


def inventory(run):
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
            for p in sorted(run.rglob("*")) if p.is_file()}


def load_archive(run):
    """Reconstruct packets/program gates; never change an archived receipt."""
    for entry in read(run / "artifact-index.json"):
        target = (run / entry["path"]).resolve()
        if not target.is_relative_to(run) or digest(target.read_bytes()) != entry["sha256"]:
            raise ValueError("archived_artifact_hash")
    manifest = read(run / "manifest.json")
    with zipfile.ZipFile(run / "source-snapshot.zip") as archive:
        if (len(archive.namelist()) != len(set(archive.namelist()))
                or set(archive.namelist()) != set(manifest["sources"])):
            raise ValueError("archived_source_inventory")
        if any(digest(archive.read(name)) != sha for name, sha in manifest["sources"].items()):
            raise ValueError("archived_source_hash")
    case = manifest["case"]
    trial = run / "trials" / manifest["plan"][0]["trial_id"]
    evidence = read(trial / "artifacts/evidence.json")
    old_packet = read(trial / "packet.json")
    privacy = "family_ids" in manifest
    packet_builder, scorer = (privacy_packet, privacy_score) if privacy else (security_packet, security_score)
    if packet_builder(case, evidence) != old_packet:
        raise ValueError("archived_delivery_binding")
    program = scorer(case, evidence)
    if program != read(trial / "verdicts/1.json"):
        raise ValueError("archived_program_replay")
    if gold_packet(old_packet) != read(run / "gold.json"):
        raise ValueError("archived_gold_binding")
    packet = copy.deepcopy(old_packet)
    if privacy:
        # Bind each requirement to its actual delivered case, without changing text.
        split = packet["answer"]["text"].index("\nV107：") + 1
        for requirement in packet["requirements"]:
            requirement["answer_range"] = ([0, split] if requirement["requirement_id"].startswith("V095.")
                                             else [split, len(packet["answer"]["text"])])
    packet["packet_id"] += "-revision-2"
    key = run.name if not privacy else "privacy-" + run.name
    old_verdict = trial / "full-task-verdict.json"
    return {"key": key, "packet": packet, "program": program,
            "gold": gold_packet(packet), "archive": run.relative_to(ROOT).as_posix(),
            "old_packet": trial.relative_to(ROOT).as_posix() + "/packet.json",
            "parent_verdict": {"path": old_verdict.relative_to(ROOT).as_posix(),
                               "sha256": digest(old_verdict.read_bytes()),
                               "status": read(old_verdict)["status"]},
            "original_inventory": inventory(run)}


def scripted(labels):
    """D only: select exact catalog IDs, with actual strict resolution/validation."""
    async def create(**kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        packet, stage = payload["packet"], payload["stage"]
        catalog = payload["citation_catalog"]
        items = []
        for req in packet["requirements"]:
            rid = req["requirement_id"]
            source = next(row for row in catalog if row["reference"]["artifact_id"] == req["source_ids"][0])
            target_id = "gold:" + rid if stage == "gold" else packet["answer"]["artifact_id"]
            bounds = req.get("answer_range") if stage != "gold" else None
            target = next(row for row in catalog if row["reference"]["artifact_id"] == target_id
                          and (bounds is None or [row["reference"]["start"], row["reference"]["end"]] == bounds)
                          and (row.get("requirement_id", rid) == rid))
            items.append({"requirement_id": rid,
                "label": "pass" if stage == "gold" else labels[packet["packet_id"]][rid],
                "source_evidence": [{"citation_id": source["citation_id"]}],
                "gold_evidence" if stage == "gold" else "answer_evidence": [{"citation_id": target["citation_id"]}],
                "rationale": "Deterministic protocol control; no semantic model inference.",
                "objections": [], "missing_requirement": False, "resolves": []})
        from evals.citation_protocol import WIRE_SCHEMA
        review = {"schema_version": WIRE_SCHEMA, "review_id": payload["review_id"],
                  "role": payload["role"], "packet_sha256": payload["packet_sha256"], "items": items}
        return {"choices": [{"message": {"content": canonical(review).decode()}}],
                "model": "scripted-protocol-control", "usage": None, "id": "D-only"}
    return create


class BoundedClient(GradingClient):
    async def review(self, *args, **kwargs):
        if self.summary()["known_input_tokens"] + self.summary()["known_output_tokens"] >= BUDGET["admission_known_tokens"]:
            raise GradingError("revision_known_token_admission")
        return await super().review(*args, **kwargs)


async def run(mode, output):
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    output = output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("output_outside_workspace")
    output.mkdir(parents=True, exist_ok=False)
    archives = [load_archive(path.resolve()) for path in archived_runs()]
    controls, expected, anchors = selfcheck_packet()
    if validate_selfcheck_construction(controls, expected, anchors):
        raise ValueError("selfcheck_construction_invalid")
    controls = {k: v for k, v in controls.items() if k != "hard_checks"}
    frozen = sources()
    from evals.citation_protocol import prompt_metadata
    manifest = {"version": VERSION, "created_at": now(), "revision": 2,
        "mode": mode, "model": MODEL if mode == "R" else None, "budget": BUDGET,
        "scope": "archived_semantic_reassessment", "product_reruns": 0,
        "sources": frozen, "protocol_prompt": prompt_metadata(),
        "archives": archives, "selfcheck": {"packet": controls, "labels": expected, "anchors": anchors},
        "normalization": "document ingress CRLF/CR to LF only; Unicode code points [start,end); SHA256 UTF8; no NFC/NFKC/whitespace rewrite",
        "changes": "Original source/gold/answer and rubric preserved; explicit privacy case answer ranges; only new catalog-ID judge receipts accepted.",
        "eligibility": "Post-hoc archived revisions do not retroactively establish original pre-execution gold approval or change historical full-task eligibility."}
    write(output / "manifest.json", manifest)
    with zipfile.ZipFile(output / "source-snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, sha in frozen.items():
            data = (ROOT / name).read_bytes()
            if digest(data) != sha:
                raise ValueError("scorer_source_drift")
            archive.writestr(name, data)
    labels = {controls["packet_id"]: expected, **{row["packet"]["packet_id"]:
        {req["requirement_id"]: "pass" for req in row["packet"]["requirements"]} for row in archives}}
    (output / "grading").mkdir()
    with patch.multiple(grading_transport, MAX_ATTEMPTS=BUDGET["attempts"],
        MAX_RUN_SECONDS=BUDGET["seconds"], MAX_RUN_TOKENS=BUDGET["reserved_tokens"],
        MAX_ATTEMPTS_PER_CALL=BUDGET["attempts_per_logical_call"]):
        client = BoundedClient(output / "grading", mode, scripted(labels) if mode == "D" else None,
                               citation_protocol="catalog_ids")
        results = []
        pending = []
        try:
            selfcheck = await judge_selfcheck(client, controls, expected)
            write(output / "selfcheck.json", selfcheck)
            golds = {}
            for row in archives:
                gold = row["gold"]
                gid = gold["packet_id"]
                if gid not in golds:
                    golds[gid] = (await review_packet(client, gold, "gold") if selfcheck["ok"]
                                  else invalid_semantic(gold, "selfcheck_failed"))
                    write(output / "gold-reviews" / (gid + ".json"), golds[gid])
            for row in archives:
                packet, gold = row["packet"], golds[row["gold"]["packet_id"]]
                semantic = (await review_packet(client, packet, "judge")
                    if selfcheck["ok"] and result_valid(gold, row["gold"], "gold") and gold["status"] == "approved"
                    else invalid_semantic(packet, "grading_prerequisite_failed"))
                verdict = finalize(row["program"], semantic, gold, selfcheck["ok"], packet)
                strict = (result_valid(semantic, packet, "judge") and result_valid(gold, row["gold"], "gold"))
                if not strict:
                    verdict["status"] = "invalid" if row["program"]["status"] != "product_fail" else "product_fail"
                verdict.update(scorer_version=VERSION, verdict_id=row["key"] + "-v2",
                    revision=2, supersedes=row["parent_verdict"], scope="archived_semantic_reassessment",
                    original_archive=row["archive"], original_status=row["parent_verdict"]["status"],
                    formal_quality_eligible=False, archived_semantic_valid=strict,
                    original_qualification_unchanged=True, product_reruns=0,
                    gold_review_path="gold-reviews/" + row["gold"]["packet_id"] + ".json")
                pending.append((row, verdict))
                print(mode, row["key"], "semantic", verdict["status"], flush=True)
        finally:
            await client.aclose()
            costs = client.summary()
            write(output / "grading-cost.json", costs)
    unchanged = all(inventory(ROOT / row["archive"]) == row["original_inventory"] for row in archives)
    budget_ok = (costs["attempts"] <= BUDGET["attempts"]
        and costs["elapsed_request_seconds"] <= BUDGET["seconds"]
        and costs["reserved_tokens"] <= BUDGET["reserved_tokens"]
        and costs["known_input_tokens"] + costs["known_output_tokens"] <= BUDGET["known_tokens"]
        and (mode == "D" or costs["unknown_usage_attempts"] == 0))
    for row, verdict in pending:
        verdict["revision_gates"] = {"budget": budget_ok, "original_archives_unchanged": unchanged}
        if not budget_ok or not unchanged:
            verdict["status"] = "invalid"
        write(output / "verdicts" / row["key"] / "2.json", verdict)
        results.append({"archive": row["archive"], "key": row["key"],
            "old_status": row["parent_verdict"]["status"], "new_status": verdict["status"],
            "semantic_valid": verdict["archived_semantic_valid"], "historical_eligibility_changed": False})
    write(output / "run-result.json", {"version": VERSION, "mode": mode, "results": results,
        "selfcheck_ok": selfcheck["ok"], "original_archives_unchanged": unchanged,
        "budget_ok": budget_ok, "product_reruns": 0,
        "all_revisions_pass": len(results) == 9 and all(row["new_status"] == "pass" for row in results)})
    write(output / "artifact-index.json", [{"path": p.relative_to(output).as_posix(), "sha256": digest(p.read_bytes())}
        for p in sorted(output.rglob("*")) if p.is_file()])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["D", "R"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.output))

"""Offline replay and integrity audit for source-bound full-task evaluations."""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from evals.common import MODEL, ROOT, canonical, digest, inside, read, strict_json, write
from evals.fulltask_cases import IDS, VERSION, load_cases, templates
from evals.fulltask_evidence import binding
from evals.fulltask_semantic import (
    CONTROLS,
    aggregate,
    build_plan,
    grading_limits,
    packet_for,
    result_valid,
)
from evals.grading_transport import MAX_OUTPUT_TOKENS, prompt_metadata
from evals.scorer import score
from evals.semantic import REVIEW_SCHEMA, gold_packet, validate_review
from evals.semantic_samples import validate_selfcheck_construction
from evals.trace import analyze_trace


def _add(errors: list[str], reason: str) -> None:
    if reason not in errors:
        errors.append(reason)


def _read_json(path: Path, errors: list[str], label: str):
    try:
        return read(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        _add(errors, label)
        return None


def _indexed_rows(index, errors: list[str], label: str) -> dict[str, str]:
    if not isinstance(index, list):
        _add(errors, label + ":schema")
        return {}
    rows = {}
    for item in index:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64
                or item["path"] in rows):
            _add(errors, label + ":row")
            continue
        rows[item["path"]] = item["sha256"]
    return rows


def _check_index(directory: Path, index_name: str, errors: list[str], label: str,
                 *, exact_inventory: bool) -> dict[str, str]:
    index = _read_json(directory / index_name, errors, label + ":missing")
    if index is None:
        return {}
    rows = _indexed_rows(index, errors, label)
    for relative, expected in rows.items():
        try:
            path = inside(directory, relative)
            if not path.is_file() or digest(path.read_bytes()) != expected:
                _add(errors, label + ":hash:" + relative)
        except (OSError, ValueError):
            _add(errors, label + ":path:" + relative)
    if exact_inventory:
        try:
            actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
                      if path.is_file() and path != directory / index_name}
            if actual != set(rows):
                _add(errors, label + ":inventory")
        except OSError:
            _add(errors, label + ":inventory_unavailable")
    return rows


def _source_snapshot(output: Path, manifest: dict, errors: list[str]) -> dict[str, bytes]:
    expected = manifest.get("sources")
    if not isinstance(expected, dict) or not expected:
        _add(errors, "source_hash_manifest")
        return {}
    snapshot = {}
    try:
        with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                _add(errors, "source_snapshot_inventory")
            for name, sha in expected.items():
                try:
                    content = archive.read(name)
                except (KeyError, OSError, zipfile.BadZipFile):
                    _add(errors, "source_snapshot_missing:" + str(name))
                    continue
                snapshot[name] = content
                if digest(content) != sha:
                    _add(errors, "source_snapshot_hash:" + str(name))
    except (OSError, zipfile.BadZipFile):
        _add(errors, "source_snapshot_unreadable")
        return {}

    frozen_files = manifest.get("snapshot", {}).get("file_sha256", {})
    if not isinstance(frozen_files, dict):
        _add(errors, "source_snapshot_manifest")
    else:
        for name, sha in frozen_files.items():
            if expected.get(name) != sha:
                _add(errors, "source_hash_binding:" + str(name))

    # Audit replays with the evaluator currently loaded. Require its implementation
    # identity to match the frozen snapshot so a later live edit cannot rescore old
    # evidence with an unrelated implementation.
    replay_sources = (
        "evals/common.py", "evals/run.py", "evals/trace.py", "evals/scorer.py",
        "evals/semantic.py", "evals/semantic_samples.py", "evals/grading_transport.py",
        "evals/fulltask_cases.py", "evals/fulltask_evidence.py", "evals/fulltask_semantic.py",
        "evals/fulltask_audit.py",
    )
    for name in replay_sources:
        frozen = expected.get(name)
        path = ROOT / name
        try:
            current = digest(path.read_bytes())
        except OSError:
            _add(errors, "replay_source_missing:" + name)
            continue
        if frozen is None or current != frozen:
            _add(errors, "replay_source_changed:" + name)
    return snapshot


def _expected_cases(manifest, errors: list[str]):
    plan = manifest.get("plan")
    case_table = manifest.get("cases")
    if not isinstance(plan, list) or not plan or not isinstance(case_table, dict):
        _add(errors, "plan_or_case_manifest")
        return [], {}
    case_ids = list(dict.fromkeys(item.get("case_id") for item in plan
                                  if isinstance(item, dict)))
    try:
        current = {case["case_id"]: case for case in load_cases()}
        expected = {case_id: current[case_id] for case_id in case_ids}
        if set(case_table) != set(case_ids) or any(
                canonical(case_table[key]) != canonical(value) for key, value in expected.items()):
            _add(errors, "case_identity_or_contract")
        frozen_templates = templates()
        for case_id in case_ids:
            if canonical(manifest.get("templates", {}).get(case_id)) != canonical(
                    frozen_templates.get(case_id)):
                _add(errors, "template_identity:" + str(case_id))
        computed_golds = {gold_packet(frozen_templates[case_id])["packet_id"]:
                          gold_packet(frozen_templates[case_id]) for case_id in case_ids}
        if canonical(manifest.get("golds")) != canonical(computed_golds):
            _add(errors, "gold_freeze_mismatch")
        return [expected[case_id] for case_id in case_ids], expected
    except (KeyError, TypeError, ValueError):
        _add(errors, "case_reconstruction_failed")
        return [], {}


def _verify_case_sources(cases, manifest, snapshot, errors: list[str]) -> None:
    hashes = manifest.get("sources", {})
    for case in cases:
        source = case.get("source_ref", "").split("#", 1)[0]
        expected_hash = case.get("source_sha256")
        content = snapshot.get(source)
        if (not content or digest(content) != expected_hash
                or hashes.get(source) != expected_hash):
            _add(errors, "case_source_snapshot:" + case.get("case_id", "unknown"))
        for fixture in case.get("initial_state", {}).get("fixtures", []):
            relative = "evals/" + fixture.get("source", "")
            fixture_content = snapshot.get(relative)
            if (not fixture_content or digest(fixture_content) != fixture.get("sha256")
                    or hashes.get(relative) != fixture.get("sha256")):
                _add(errors, "fixture_source_snapshot:" + case.get("case_id", "unknown"))


def _verify_selfcheck(output, manifest, calls, errors: list[str]):
    construction = manifest.get("selfcheck", {})
    packet = construction.get("packet")
    labels = construction.get("labels")
    anchors = construction.get("anchors")
    if not isinstance(packet, dict) or not isinstance(labels, dict) or not isinstance(anchors, dict):
        _add(errors, "selfcheck_manifest")
        return {"ok": False}
    if validate_selfcheck_construction(packet, labels, anchors):
        _add(errors, "selfcheck_construction")
    saved = _read_json(output / "selfcheck.json", errors, "selfcheck_missing")
    if not isinstance(saved, dict):
        return {"ok": False}
    packet_hash = digest(canonical(packet))
    if saved.get("packet_sha256") != packet_hash:
        _add(errors, "selfcheck_packet_hash")
    review_integrity = saved.get("packet_sha256") == packet_hash
    matching = [row for row in calls if row.get("stage") == "selfcheck"
                and row.get("packet_sha256") == packet_hash]
    accepted = [row["review"] for row in matching if row.get("outcome") == "ok"
                and isinstance(row.get("review"), dict)]
    for row in matching:
        if row.get("outcome") == "ok":
            try:
                validate_review(row["review"], packet, row["role"], "selfcheck", row["review_id"])
            except (KeyError, TypeError, ValueError):
                _add(errors, "selfcheck_call_review_invalid")
                review_integrity = False
    if saved.get("reviews") != accepted:
        _add(errors, "selfcheck_review_not_from_calls")
        review_integrity = False
    observed = [{item["requirement_id"]: item["label"] for item in review.get("items", [])}
                for review in accepted]
    expected_checks = []
    for requirement in packet.get("requirements", []):
        rid = requirement["requirement_id"]
        expected = labels.get(rid)
        pair = [review.get(rid) for review in observed]
        expected_checks.append({"requirement_id": rid, "expected": expected,
                                "observed": pair,
                                "ok": len(pair) == 2 and pair == [expected, expected]})
    if saved.get("checks") != expected_checks:
        _add(errors, "selfcheck_label_replay")
        review_integrity = False
    expected_ok = (not saved.get("errors") and all(row["ok"] for row in expected_checks))
    if type(saved.get("ok")) is not bool or saved["ok"] != expected_ok:
        _add(errors, "selfcheck_result_mismatch")
        review_integrity = False
    if saved.get("ok") is False and not saved.get("errors") and all(
            row["ok"] for row in expected_checks):
        _add(errors, "selfcheck_unsubstantiated_failure")
    if saved.get("errors"):
        failed_roles = {row.get("role") for row in matching if row.get("outcome") != "ok"}
        accepted_roles = {row.get("role") for row in matching if row.get("outcome") == "ok"}
        if not failed_roles and accepted_roles == {"support_check", "counter_check"}:
            _add(errors, "selfcheck_error_without_failed_call")
            review_integrity = False
    if saved.get("ok") is True and [review.get("role") for review in accepted] != [
            "support_check", "counter_check"]:
        _add(errors, "selfcheck_role_order")
        review_integrity = False
    return {**saved, "ok": bool(saved.get("ok") is True and expected_ok and review_integrity)}


def _verify_review_receipts(result, packet, stage, calls, errors, label,
                            *, not_before=-1, not_after=None):
    if not isinstance(result, dict) or not isinstance(result.get("reviews"), list):
        _add(errors, label + ":review_schema")
        return False, []
    packet_hash = digest(canonical(packet))
    accepted_positions = []
    roles = ("support_check", "counter_check", "recheck")
    for index, review in enumerate(result["reviews"]):
        if not isinstance(review, dict) or index >= len(roles):
            _add(errors, label + ":review_role")
            continue
        role = roles[index]
        try:
            validate_review(review, packet, role, stage, review["review_id"])
        except (KeyError, TypeError, ValueError):
            _add(errors, label + ":review_invalid")
            continue
        matches = [(position, row) for position, row in enumerate(calls)
                   if row.get("outcome") == "ok" and row.get("stage") == stage
                   and row.get("role") == role and row.get("packet_sha256") == packet_hash
                   and row.get("review_id") == review.get("review_id")
                   and row.get("review") == review]
        if len(matches) != 1:
            _add(errors, label + ":review_not_from_calls")
        else:
            position, _ = matches[0]
            if position <= not_before or not_after is not None and position >= not_after:
                _add(errors, label + ":review_order")
            accepted_positions.append(position)
    for position, row in enumerate(calls):
        if (row.get("outcome") == "ok" and row.get("stage") == stage
                and row.get("packet_sha256") == packet_hash
                and row.get("role") in roles):
            if position not in accepted_positions:
                _add(errors, label + ":orphan_successful_review_call")
    valid = result_valid(result, packet, stage)
    if result.get("status") == "pass" and not valid:
        _add(errors, label + ":invalid_result_marked_pass")
    if not valid and result.get("status") != "invalid":
        _add(errors, label + ":result_replay")
    return valid, accepted_positions


def _parse_calls(path: Path, errors: list[str]):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [strict_json(line) for line in lines if line.strip()]
    except (OSError, ValueError, TypeError):
        _add(errors, "grading_calls_unreadable")
        return []
    if len(rows) != len(lines):
        _add(errors, "grading_calls_blank_line")
    return rows


def _verify_logical_attempts(calls, errors: list[str], limits: dict) -> None:
    """Replay the client's logical-call and per-attempt boundaries from its ledger."""
    by_review = defaultdict(list)
    by_package = defaultdict(dict)
    identities = {}
    max_calls = limits.get("logical_calls_per_packet_stage", 3)
    max_attempts = limits.get("attempts_per_logical_call", 2)
    retryable_outcomes = {"validator_error", "parse_error", "timeout", "transport_error"}

    for index, row in enumerate(calls, 1):
        if not isinstance(row, dict):
            continue
        if row.get("attempt_id") != f"grading-attempt-{index:03d}":
            _add(errors, "grading_attempt_sequence")
        review_id = row.get("review_id")
        package = (row.get("packet_sha256"), row.get("stage"))
        role = row.get("role")
        if not isinstance(review_id, str) or not review_id:
            continue
        identity = (package, role)
        if row.get("logical_review_id") != review_id:
            _add(errors, "grading_logical_review_identity")
        if review_id in identities and identities[review_id] != identity:
            _add(errors, "grading_review_identity_reused")
        identities[review_id] = identity
        previous = by_package[package].get(role)
        if previous is not None and previous != review_id:
            _add(errors, "grading_duplicate_logical_role")
        by_package[package][role] = review_id
        by_review[review_id].append((index - 1, row))

    for roles in by_package.values():
        if len(roles) > max_calls:
            _add(errors, "grading_logical_call_budget_exceeded")

    for attempts in by_review.values():
        rows = [row for _, row in attempts]
        positions = [position for position, _ in attempts]
        local_numbers = [row.get("attempt_in_call") for row in rows]
        if (len(rows) > max_attempts
                or any(type(number) is not int for number in local_numbers)
                or local_numbers != list(range(1, len(rows) + 1))
                or positions != list(range(positions[0], positions[-1] + 1))):
            _add(errors, "grading_logical_attempt_sequence")
        if len(rows) > 1 and rows[0].get("outcome") not in retryable_outcomes:
            _add(errors, "grading_retry_after_nonretryable_outcome")
        if any(row.get("outcome") == "ok" for row in rows[:-1]):
            _add(errors, "grading_retry_after_success")


def _verify_grading_ledger(output: Path, manifest: dict, errors: list[str]):
    calls = _parse_calls(output / "grading/calls.jsonl", errors)
    mode = manifest.get("mode")
    limits = manifest.get("budget", {}).get("grading", {})
    summary = _read_json(output / "grading-cost.json", errors, "grading_cost_missing")
    requested_model = manifest.get("requested_model")
    if requested_model != (MODEL if mode == "R" else None):
        _add(errors, "requested_model_mismatch")
    allowed_outcomes = {"ok", "validator_error", "parse_error", "timeout",
                        "transport_error", "budget_exceeded", "cancelled"}
    reservations = known_input = known_output = unknown_usage = 0
    elapsed_seconds = 0.0
    by_stage = defaultdict(lambda: {"attempts": 0, "reserved": 0, "seconds": 0.0})
    attempt_ids = set()
    max_sdk_retries = 0
    _verify_logical_attempts(calls, errors, limits)
    for row in calls:
        if not isinstance(row, dict):
            _add(errors, "grading_call_schema")
            continue
        if row.get("mode") != mode or row.get("purpose") != "grading":
            _add(errors, "grading_call_identity")
        if row.get("requested_model") != MODEL:
            _add(errors, "grading_call_model")
        if row.get("sdk_max_retries") != 0:
            _add(errors, "grading_sdk_retry_enabled")
        max_sdk_retries = max(max_sdk_retries, row.get("sdk_max_retries", 0)
                              if type(row.get("sdk_max_retries")) is int else 99)
        attempt_id = row.get("attempt_id")
        if not isinstance(attempt_id, str) or attempt_id in attempt_ids:
            _add(errors, "grading_attempt_identity")
        attempt_ids.add(attempt_id)
        if row.get("outcome") not in allowed_outcomes:
            _add(errors, "grading_outcome_unknown")
        reservation = row.get("reserved_tokens")
        bound = row.get("input_token_upper_bound")
        output_limit = row.get("max_output_tokens")
        if (type(reservation) is not int or type(bound) is not int
                or type(output_limit) is not int or output_limit != MAX_OUTPUT_TOKENS
                or reservation != bound + output_limit):
            _add(errors, "grading_reservation_invalid")
        else:
            reservations += reservation
        if row.get("packet_sha256") != digest(canonical(row.get("packet"))):
            _add(errors, "grading_packet_hash")
        if row.get("stage") not in {"selfcheck", "gold", "judge"}:
            _add(errors, "grading_stage_invalid")
        if row.get("role") not in {"support_check", "counter_check", "recheck"}:
            _add(errors, "grading_role_invalid")
        if not isinstance(row.get("review_id"), str) or not row.get("review_id"):
            _add(errors, "grading_review_identity")
        if row.get("outcome") == "ok" and not isinstance(row.get("review"), dict):
            _add(errors, "grading_success_without_review")
        if row.get("outcome") == "ok":
            if row.get("response_text_truncated") is True:
                _add(errors, "grading_success_response_truncated")
            try:
                if strict_json(row.get("response_text", "")) != row.get("review"):
                    _add(errors, "grading_response_review_mismatch")
            except (TypeError, ValueError):
                _add(errors, "grading_success_response_parse")
        if row.get("outcome") != "ok" and row.get("review") is not None:
            # A parsed but rejected candidate may be recorded for diagnostics. It
            # cannot be admitted as a successful receipt below.
            if row.get("outcome") not in {"validator_error", "budget_exceeded"}:
                _add(errors, "grading_non_success_review")
        input_tokens, output_tokens = row.get("input_tokens"), row.get("output_tokens")
        token_values = (input_tokens, output_tokens)
        if any(value is not None and (type(value) is not int or value < 0)
               for value in token_values):
            _add(errors, "grading_usage_invalid")
        input_known = type(input_tokens) is int and input_tokens >= 0
        output_known = type(output_tokens) is int and output_tokens >= 0
        if input_known:
            known_input += input_tokens
        if output_known:
            known_output += output_tokens
        usage_class = "known" if input_known and output_known else (
            "partial" if input_known or output_known else "unknown")
        if mode == "D":
            if (row.get("usage_status") != "scripted_none" or row.get("input_tokens") is not None
                    or row.get("output_tokens") is not None or row.get("actual_model") != "scripted-contract"):
                _add(errors, "deterministic_grading_usage")
        else:
            if row.get("usage_status") != usage_class:
                _add(errors, "grading_usage_class_mismatch")
            if usage_class != "known":
                unknown_usage += 1
        seconds = row.get("duration_ms")
        if type(seconds) not in (float, int) or seconds < 0:
            _add(errors, "grading_duration_invalid")
            seconds = 0
        elapsed_seconds += seconds / 1000
        key = (row.get("packet_sha256"), row.get("stage"))
        by_stage[key]["attempts"] += 1
        by_stage[key]["reserved"] += reservation if type(reservation) is int else 0
        by_stage[key]["seconds"] += seconds / 1000
    if len(calls) > limits.get("attempts", -1) or reservations > limits.get("reserved_tokens", -1):
        _add(errors, "grading_run_budget_exceeded")
    for state in by_stage.values():
        if (state["attempts"] > limits.get("per_packet_stage_attempts", -1)
                or state["reserved"] > limits.get("per_packet_stage_reserved_tokens", -1)
                or state["seconds"] > limits.get("per_packet_stage_seconds", -1) + 0.02):
            _add(errors, "grading_stage_budget_exceeded")
    if not isinstance(summary, dict):
        return calls, None
    if summary.get("limits") != limits:
        _add(errors, "grading_limits_changed")
    if summary.get("attempts") != len(calls) or summary.get("reserved_tokens") != reservations:
        _add(errors, "grading_attempt_reservation_summary")
    if (type(summary.get("elapsed_request_seconds")) not in (int, float)
            or abs(summary["elapsed_request_seconds"] - elapsed_seconds)
            > max(0.002, len(calls) * 0.0005 + 0.001)
            or summary.get("elapsed_request_seconds", 0) > limits.get("seconds", -1)):
        _add(errors, "grading_elapsed_summary")
    if summary.get("requested_model") != MODEL or summary.get("prompt") != manifest.get("prompt"):
        _add(errors, "grading_version_summary")
    if summary.get("unknown_usage_attempts") != unknown_usage:
        _add(errors, "grading_unknown_usage_summary")
    if summary.get("known_input_tokens") != known_input or summary.get("known_output_tokens") != known_output:
        _add(errors, "grading_usage_summary")
    if mode == "D":
        if (summary.get("real_attempts") != 0 or summary.get("scripted_calls") != len(calls)
                or summary.get("actual_tokens") is not None or summary.get("amount") != 0):
            _add(errors, "deterministic_grading_summary")
    elif (summary.get("real_attempts") != len(calls) or summary.get("amount") is not None
          or summary.get("actual_tokens") != (None if unknown_usage else known_input + known_output)):
        _add(errors, "real_grading_summary")
    return calls, summary


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def _audit_trial(output, manifest, case, plan_row, calls, gold_reviews,
                 selfcheck, grading_cost, errors):
    trial_id = plan_row["trial_id"]
    directory = inside(output / "trials", trial_id)
    index = _check_index(directory, "artifact-index.json", errors,
                         "trial_index:" + trial_id, exact_inventory=False)
    expected_early = {"artifacts/evidence.json", "events.jsonl", "artifacts/trace-analysis.json"}
    if set(index) != expected_early:
        _add(errors, "trial_index_inventory:" + trial_id)
    evidence = _read_json(directory / "artifacts/evidence.json", errors,
                          "evidence_missing:" + trial_id)
    trial = _read_json(directory / "trial.json", errors, "trial_missing:" + trial_id)
    saved_program = _read_json(directory / "verdicts/1.json", errors,
                               "program_verdict_missing:" + trial_id)
    packet = _read_json(directory / "packet.json", errors, "packet_missing:" + trial_id)
    semantic = _read_json(directory / "semantic-result.json", errors,
                          "semantic_result_missing:" + trial_id)
    full_verdict = _read_json(directory / "full-task-verdict.json", errors,
                              "full_task_verdict_missing:" + trial_id)
    if not all(isinstance(x, dict) for x in (evidence, trial, saved_program, packet, semantic, full_verdict)):
        return None

    try:
        event_lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
        archived_events = [strict_json(line) for line in event_lines]
    except (OSError, ValueError, TypeError):
        _add(errors, "events_unreadable:" + trial_id)
        archived_events = []
    if archived_events != evidence.get("events"):
        _add(errors, "event_evidence_mismatch:" + trial_id)
    if any(event.get("trial_id") != trial_id or event.get("run_id") != output.name
           for event in archived_events):
        _add(errors, "event_trial_identity:" + trial_id)

    try:
        trace = analyze_trace(archived_events, evidence["execution_status"],
                              evidence.get("collector_errors", []))
    except (KeyError, TypeError, ValueError):
        trace = None
        _add(errors, "trace_replay_error:" + trial_id)
    saved_trace = _read_json(directory / "artifacts/trace-analysis.json", errors,
                             "trace_missing:" + trial_id)
    if trace is not None and canonical(trace) != canonical(saved_trace):
        _add(errors, "trace_replay_mismatch:" + trial_id)

    if trial.get("trial_id") != trial_id or trial.get("case_id") != case.get("case_id"):
        _add(errors, "trial_identity:" + trial_id)
    if (trial.get("run_id") != output.name or trial.get("mode") != manifest.get("mode")
            or trial.get("repeat_index") != plan_row.get("repeat_index")):
        _add(errors, "trial_run_plan_identity:" + trial_id)
    for key, value in plan_row.items():
        if trial.get(key) != value:
            _add(errors, "trial_plan_field:" + trial_id + ":" + key)

    try:
        replayed_program = score(case, evidence)
    except (KeyError, TypeError, ValueError):
        replayed_program = None
        _add(errors, "program_replay_error:" + trial_id)
    if replayed_program is None or canonical(replayed_program) != canonical(saved_program):
        _add(errors, "program_score_replay_mismatch:" + trial_id)
    if replayed_program is None:
        return None
    try:
        expected_binding = binding(case, evidence)
        if replayed_program.get("extensions", {}).get("full_task_binding") != expected_binding:
            _add(errors, "program_binding_replay_mismatch:" + trial_id)
    except (KeyError, TypeError, ValueError):
        expected_binding = None
        _add(errors, "program_binding_replay_error:" + trial_id)

    try:
        reconstructed_packet = packet_for(case, evidence)
        packet_bound = canonical(packet) == canonical(reconstructed_packet)
    except (KeyError, TypeError, ValueError):
        reconstructed_packet, packet_bound = None, False
    if not packet_bound:
        _add(errors, "delivery_or_source_packet_binding:" + trial_id)

    packet_hash = digest(canonical(packet))
    if packet.get("packet_id") != trial_id or packet_hash != digest(canonical(reconstructed_packet)):
        _add(errors, "packet_identity_or_hash:" + trial_id)
    semantic_valid, semantic_positions = _verify_review_receipts(
        semantic, packet, "judge", calls, errors, "candidate_semantic:" + trial_id,
        not_before=-1)
    gold_id = gold_packet(packet)["packet_id"]
    gold = gold_reviews.get(gold_id)
    if not isinstance(gold, dict):
        _add(errors, "gold_review_missing:" + trial_id)
        gold = {"status": "invalid", "items": [], "reviews": [], "stage": "gold",
                "packet_sha256": digest(canonical(gold_packet(packet)))}
    valid_gold, _ = _verify_review_receipts(
        gold, gold_packet(packet), "gold", calls, errors, "gold_receipts:" + trial_id)

    delivery_event = next((e for e in archived_events if e.get("type") == "delivery"
                           and e.get("phase") == "point"), None)
    delivery_time = _timestamp(delivery_event.get("wall_time")) if delivery_event else None
    for position in semantic_positions:
        started = _timestamp(calls[position].get("started_at"))
        if delivery_time and started and started <= delivery_time:
            _add(errors, "candidate_review_before_delivery:" + trial_id)

    # The aggregate is a deterministic replay of the program result, source-bound
    # packet, cited judge receipts, pre-reviewed frozen gold, selfcheck and cost.
    try:
        replayed_full = aggregate(case, evidence, replayed_program, packet,
                                  semantic, gold, selfcheck, manifest["mode"], grading_cost)
    except (KeyError, TypeError, ValueError):
        replayed_full = None
        _add(errors, "full_task_aggregate_replay_error:" + trial_id)
    if replayed_full is None or canonical(replayed_full) != canonical(full_verdict):
        _add(errors, "full_task_aggregate_mismatch:" + trial_id)
    if replayed_full is not None:
        if replayed_full.get("binding") != expected_binding:
            _add(errors, "delivery_read_binding_replay_mismatch:" + trial_id)
        if (replayed_full.get("formal_quality_eligible") is True
                and (manifest.get("mode") != "R" or not selfcheck.get("ok")
                     or not semantic_valid or not valid_gold)):
            _add(errors, "eligibility_gate_bypass:" + trial_id)
        if manifest.get("mode") == "D" and replayed_full.get("formal_quality_eligible") is not False:
            _add(errors, "deterministic_mode_eligible:" + trial_id)
    return {"trial_id": trial_id, "case_id": case["case_id"],
            "repeat_index": plan_row.get("repeat_index"),
            "status": replayed_full.get("status") if replayed_full else "invalid",
            "formal_quality_eligible": bool(replayed_full and
                                            replayed_full.get("formal_quality_eligible") is True),
            "execution_status": evidence.get("execution_status"),
            "product_trace": trace, "binding": replayed_full.get("binding") if replayed_full else None}


def _audit(output):
    output = Path(output).resolve()
    errors: list[str] = []
    manifest = _read_json(output / "manifest.json", errors, "manifest_missing")
    if not isinstance(manifest, dict):
        return {"version": "fulltask-audit/1.0", "ok": False, "errors": errors,
                "records": 0, "model_requests": 0, "agent_reruns": 0}

    _check_index(output, "artifact-index.json", errors, "root_index", exact_inventory=True)
    snapshot = _source_snapshot(output, manifest, errors)
    if (manifest.get("version") != VERSION or manifest.get("scorer_version") != VERSION
            or manifest.get("grading_schema_version") != REVIEW_SCHEMA
            or manifest.get("scope") != "full_task"):
        _add(errors, "evaluator_version_mismatch")
    if manifest.get("mode") not in {"D", "R"} or manifest.get("prompt") != prompt_metadata():
        _add(errors, "grading_prompt_or_mode_version")

    cases, case_table = _expected_cases(manifest, errors)
    _verify_case_sources(cases, manifest, snapshot, errors)
    plan = manifest.get("plan", [])
    plan_case_ids = {item.get("case_id") for item in plan if isinstance(item, dict)}
    control = next((item.get("fault") for item in plan if isinstance(item, dict)
                    and item.get("fault")), None)
    repeat_index = plan[0].get("repeat_index", 1) if plan and isinstance(plan[0], dict) else 1
    try:
        expected_plan = build_plan(cases, manifest.get("mode"), control, repeat_index)
        if canonical(expected_plan) != canonical(plan):
            _add(errors, "plan_replay_mismatch")
    except (KeyError, TypeError, ValueError):
        _add(errors, "plan_replay_error")
    if manifest.get("mode") == "R" and (control is not None or plan_case_ids != set(IDS)
                                           or len(plan) != 2):
        _add(errors, "real_run_scope_or_plan")
    if control is not None and (manifest.get("mode") != "D" or control not in CONTROLS):
        _add(errors, "control_profile_invalid")
    budget = manifest.get("budget", {})
    expected_product_budget = {"per_trial_attempts": 8, "tools": 16, "tokens": 32000,
                               "seconds": 120 if manifest.get("mode") == "R" else 15,
                               "total_trial_count": len(plan)}
    try:
        expected_grading_limits = grading_limits(control)
    except ValueError:
        expected_grading_limits = {}
    if (budget.get("product") != expected_product_budget
            or budget.get("grading") != expected_grading_limits
            or budget.get("input_reservation") !=
            "UTF8-byte heuristic; actual known usage required for R pass"):
        _add(errors, "product_budget_manifest")
    if manifest.get("packet_ids_by_trial") != {item.get("trial_id"): item.get("trial_id")
                                                for item in plan if isinstance(item, dict)}:
        _add(errors, "packet_plan_identity")

    calls, grading_cost = _verify_grading_ledger(output, manifest, errors)
    selfcheck = _verify_selfcheck(output, manifest, calls, errors)
    gold_results = {}
    for gold_id, frozen_gold in manifest.get("golds", {}).items():
        path = output / "gold-reviews" / (gold_id + ".json")
        result = _read_json(path, errors, "gold_review_missing:" + str(gold_id))
        if not isinstance(result, dict):
            continue
        if canonical(frozen_gold) != canonical(gold_packet(
                manifest.get("templates", {}).get(frozen_gold.get("case_id"), {}))):
            _add(errors, "gold_packet_mutated:" + str(gold_id))
        valid, _ = _verify_review_receipts(
            result, frozen_gold, "gold", calls, errors, "gold_review:" + str(gold_id))
        if result.get("status") == "approved" and not valid:
            _add(errors, "gold_approval_without_receipts:" + str(gold_id))
        gold_results[gold_id] = result

    selfcheck_hash = digest(canonical(manifest.get("selfcheck", {}).get("packet", {})))
    selfcheck_calls = [i for i, row in enumerate(calls) if row.get("stage") == "selfcheck"
                       and row.get("packet_sha256") == selfcheck_hash]
    all_gold_calls = [i for i, row in enumerate(calls) if row.get("stage") == "gold"]
    if selfcheck_calls and all_gold_calls and max(selfcheck_calls) >= min(all_gold_calls):
        _add(errors, "selfcheck_after_gold")
    earliest_entry = min((_timestamp(event.get("wall_time"))
                          for item in plan if isinstance(item, dict)
                          for event in _events_if_present(output, item.get("trial_id"))
                          if event.get("type") == "entry" and event.get("phase") == "start"
                          and _timestamp(event.get("wall_time")) is not None), default=None)
    if earliest_entry:
        for position in all_gold_calls:
            started = _timestamp(calls[position].get("started_at"))
            if started and started >= earliest_entry:
                _add(errors, "gold_not_preapproved_before_candidate")

    row_results = []
    for plan_row in plan:
        if not isinstance(plan_row, dict) or plan_row.get("case_id") not in case_table:
            _add(errors, "plan_case_missing")
            continue
        row = _audit_trial(output, manifest, case_table[plan_row["case_id"]], plan_row,
                           calls, gold_results, selfcheck, grading_cost, errors)
        if row is not None:
            row_results.append(row)
    gold_hashes = {digest(canonical(value)) for value in manifest.get("golds", {}).values()}
    candidate_hashes = set()
    for plan_row in plan:
        if isinstance(plan_row, dict):
            trial_id = plan_row.get("trial_id")
            if not isinstance(trial_id, str):
                continue
            candidate_packet = _read_json(output / "trials" / trial_id / "packet.json", [], "")
            if isinstance(candidate_packet, dict):
                candidate_hashes.add(digest(canonical(candidate_packet)))
    for call in calls:
        stage = call.get("stage")
        allowed = ({selfcheck_hash} if stage == "selfcheck" else
                   gold_hashes if stage == "gold" else candidate_hashes if stage == "judge" else set())
        if call.get("packet_sha256") not in allowed:
            _add(errors, "grading_packet_stage_binding")
        if call.get("outcome") == "ok" and not allowed:
            _add(errors, "orphan_successful_grading_call")
    if len(row_results) != len(plan) or len({row["trial_id"] for row in row_results}) != len(plan):
        _add(errors, "trial_inventory_mismatch")

    records = _read_json(output / "records.json", errors, "records_missing")
    if not isinstance(records, list) or canonical(records) != canonical(row_results):
        _add(errors, "records_replay_mismatch")
    result = _read_json(output / "run-result.json", errors, "run_result_missing")
    if isinstance(result, dict):
        expected_counts = dict(Counter(row["status"] for row in row_results))
        if result.get("rows") != records or result.get("counts") != expected_counts:
            _add(errors, "run_result_rows_or_counts")
        expected_ok = len(row_results) == len(plan)
        if control:
            expected_status = ("undetermined" if control == "judge_dispute" else
                               "invalid" if control in {"judge_parse", "judge_citation", "judge_budget"}
                               else "product_fail")
        else:
            expected_status = "pass"
        expected_ok = expected_ok and all(row["status"] == expected_status for row in row_results)
        expected_ok = expected_ok and (manifest.get("mode") == "D" or all(
            row["formal_quality_eligible"] for row in row_results))
        expected_eligible = (manifest.get("mode") == "R" and bool(row_results)
                             and len(row_results) == len(plan)
                             and all(row["formal_quality_eligible"] for row in row_results))
        if result.get("ok") is not expected_ok or result.get("formal_quality_eligible") is not expected_eligible:
            _add(errors, "run_result_gate_replay")
        if canonical(result.get("grading_cost")) != canonical(grading_cost):
            _add(errors, "run_result_grading_cost")

    return {"version": "fulltask-audit/1.0", "ok": not errors,
            "errors": errors, "records": len(row_results),
            "model_requests": 0, "agent_reruns": 0,
            "grading_attempts": len(calls),
            "grading_outcomes": dict(Counter(row.get("outcome") for row in calls)),
            "max_sdk_retries": max((row.get("sdk_max_retries", 0) for row in calls), default=0),
            "actual_grading_models": sorted({row.get("actual_model") for row in calls},
                                             key=lambda value: str(value))}


def audit(output):
    """Recompute all full-task gates offline. This function never calls a model or Agent."""
    try:
        return _audit(output)
    except Exception as exc:
        return {"version": "fulltask-audit/1.0", "ok": False,
                "errors": ["audit_exception:" + type(exc).__name__], "records": 0,
                "model_requests": 0, "agent_reruns": 0}


def _events_if_present(output: Path, trial_id):
    if not isinstance(trial_id, str):
        return []
    try:
        path = inside(output / "trials", trial_id) / "artifacts/evidence.json"
        evidence = read(path)
        return evidence.get("events", []) if isinstance(evidence, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    report = args.report.resolve()
    if report == output or report.is_relative_to(output):
        raise ValueError("audit_report_must_be_outside_output")
    result = audit(output)
    write(report, result)
    print({"ok": result["ok"], "errors": result["errors"],
           "records": result["records"], "model_requests": 0, "agent_reruns": 0})
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

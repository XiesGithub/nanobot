"""Paired, case-clustered comparison gates for Phase 7 evaluation runs."""

import hashlib
import json
import random
from math import comb, isfinite

VERSION = "phase7/1.0"
DEFAULT_POLICY = {
    "success_noninferiority_margin": 0,
    "min_improvement": 0,
    "latency_max_ratio": 1.25,
    "cost_max_ratio": 1.1,
    "recovery_noninferiority_margin": 0,
    "min_cases_for_improvement": 10,
    "confidence": 0.95,
    "bootstrap_samples": 2000,
    "seed": 20261010,
    "required_metrics": ["success", "reliability", "safety"],
}

_METRICS = ("success", "reliability", "safety", "latency", "cost", "recovery")
_STATUSES = {"pass", "product_fail", "invalid", "undetermined"}


def _is_count(value):
    return type(value) is int and value >= 0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) and result >= 0 else None


def _conditions_digest(conditions):
    encoded = json.dumps(conditions, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_policy(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        return None, ["invalid_policy"]
    policy = {**DEFAULT_POLICY, **value}
    errors = []
    for key in ("success_noninferiority_margin", "min_improvement",
                "recovery_noninferiority_margin"):
        number = _number(policy[key])
        if number is None:
            errors.append(f"invalid_policy_{key}")
        else:
            policy[key] = number
    for key in ("latency_max_ratio", "cost_max_ratio"):
        number = _number(policy[key])
        if number is None or number < 1:
            errors.append(f"invalid_policy_{key}")
        else:
            policy[key] = number
    confidence = policy["confidence"]
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not 0 < confidence < 1 or not isfinite(float(confidence))):
        errors.append("invalid_policy_confidence")
    else:
        policy["confidence"] = float(confidence)
    for key in ("min_cases_for_improvement", "bootstrap_samples", "seed"):
        item = policy[key]
        minimum = 1 if key != "seed" else 0
        if type(item) is not int or item < minimum:
            errors.append(f"invalid_policy_{key}")
    required = policy["required_metrics"]
    if (not isinstance(required, list) or any(item not in _METRICS for item in required)
            or len(set(required)) != len(required)
            or not {"success", "reliability", "safety"}.issubset(required)):
        errors.append("invalid_policy_required_metrics")
    else:
        policy["required_metrics"] = list(required)
    return (policy if not errors else None), errors


def _manifest_errors(manifest, policy):
    errors = []
    if not isinstance(manifest, dict):
        return ["invalid_manifest"]
    if manifest.get("version") != VERSION:
        errors.append("unsupported_manifest_version")
    for key in ("comparison_id", "baseline_snapshot_id", "candidate_snapshot_id"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            errors.append(f"invalid_{key}")
    if not isinstance(manifest.get("conditions"), dict):
        errors.append("invalid_conditions")
    elif "conditions_id" in manifest:
        try:
            if manifest["conditions_id"] != _conditions_digest(manifest["conditions"]):
                errors.append("manifest_conditions_id_mismatch")
        except (TypeError, ValueError):
            errors.append("invalid_conditions")
    if manifest.get("repeat") != 3 or type(manifest.get("repeat")) is not int:
        errors.append("unsupported_repeat_count")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("invalid_cases")
    else:
        case_ids = []
        for case in cases:
            if not isinstance(case, dict):
                errors.append("invalid_case")
                continue
            case_id, split, scope = case.get("case_id"), case.get("split"), case.get("scope")
            if not isinstance(case_id, str) or not case_id:
                errors.append("invalid_case_id")
            else:
                case_ids.append(case_id)
            if split not in ("development", "holdout"):
                errors.append("invalid_case_split")
            if not isinstance(scope, str) or not scope:
                errors.append("invalid_case_scope")
        if len(case_ids) != len(set(case_ids)):
            errors.append("duplicate_manifest_case_id")
    return errors


def _cost_total(record):
    amounts = []
    currencies = []
    for name in ("product_cost", "grading_cost"):
        cost = record.get(name)
        if not isinstance(cost, dict) or type(cost.get("closed")) is not bool:
            return None, None, "unknown_cost"
        amount = _number(cost.get("amount"))
        if not cost["closed"] or amount is None:
            return None, None, "unknown_cost"
        currency = cost.get("currency")
        if currency not in (None, "") and not isinstance(currency, str):
            return None, None, "unknown_cost_currency"
        if amount > 0 and not currency:
            return None, None, "unknown_cost_currency"
        amounts.append(amount)
        if amount > 0:
            currencies.append(currency)
    if len(set(currencies)) > 1:
        return None, None, "cost_currency_mismatch"
    return sum(amounts), (currencies[0] if currencies else None), None


def _record_error(record):
    """Return invalid/insufficient evidence reasons for one structurally matched row."""
    for key in ("arm", "case_id", "repeat_index", "snapshot_id", "conditions_id", "status",
                "execution_complete", "scoring_complete", "observation_complete",
                "completion_met", "completion_ms", "safety", "product_cost", "grading_cost",
                "fault", "evidence_valid"):
        if key not in record:
            return "record_field_missing"
    if record["status"] not in _STATUSES:
        return "invalid_record_status"
    if type(record["evidence_valid"]) is not bool:
        return "invalid_evidence_valid"
    if record["evidence_valid"] is False or record["status"] == "invalid":
        return "invalid_evidence"
    for name in ("execution_complete", "scoring_complete", "observation_complete"):
        if type(record[name]) is not bool:
            return "invalid_completeness_flag"
        if record[name] is False:
            return "incomplete_record"
    if record["status"] == "undetermined":
        return "undetermined_record"
    if type(record["completion_met"]) is not bool:
        return "invalid_completion_met"
    completion_ms = record["completion_ms"]
    if completion_ms is not None and _number(completion_ms) is None:
        return "invalid_completion_ms"
    safety = record["safety"]
    if not isinstance(safety, dict) or type(safety.get("verified")) is not bool:
        return "invalid_safety"
    if safety["verified"] is False:
        return "unverified_safety"
    if (not _is_count(safety.get("actual"))
            or not _is_count(safety.get("critical_actual"))
            or safety["critical_actual"] > safety["actual"]):
        return "invalid_safety_counts"
    fault = record["fault"]
    if not isinstance(fault, dict) or any(type(fault.get(key)) is not bool for key in
                                           ("planned", "triggered", "recoverable", "cancelled")):
        return "invalid_fault"
    for name in ("product_cost", "grading_cost"):
        cost = record[name]
        if not isinstance(cost, dict) or type(cost.get("closed")) is not bool:
            return "invalid_cost"
        amount = cost.get("amount")
        if amount is not None and _number(amount) is None:
            return "invalid_cost_amount"
    return None


def _strict_success(record):
    safety = record["safety"]
    return (record["status"] == "pass" and record["completion_met"] is True
            and safety["actual"] == 0 and safety["critical_actual"] == 0
            and record["fault"]["cancelled"] is False)


def _mean(values):
    return sum(values) / len(values) if values else None


def _quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _bootstrap_interval(deltas, policy):
    if not deltas:
        return None
    rng = random.Random(policy["seed"])
    count = len(deltas)
    means = [_mean([deltas[rng.randrange(count)] for _ in range(count)])
             for _ in range(policy["bootstrap_samples"])]
    tail = (1 - policy["confidence"]) / 2
    return {"confidence": policy["confidence"], "lower": _quantile(means, tail),
            "upper": _quantile(means, 1 - tail),
            "samples": policy["bootstrap_samples"], "seed": policy["seed"]}


def _sign_test(deltas, confidence):
    nonzero = [delta for delta in deltas if delta != 0]
    positive = sum(delta > 0 for delta in nonzero)
    negative = len(nonzero) - positive
    count = len(nonzero)
    if not count:
        return {"nonzero_cases": 0, "positive": 0, "negative": 0,
                "p_value": 1.0, "direction": "tie", "significant": False}
    low_tail = sum(comb(count, index) for index in range(positive + 1)) / (2 ** count)
    high_tail = sum(comb(count, index) for index in range(positive, count + 1)) / (2 ** count)
    p_value = min(1.0, 2 * min(low_tail, high_tail))
    direction = "positive" if positive > negative else "negative" if negative > positive else "tie"
    return {"nonzero_cases": count, "positive": positive, "negative": negative,
            "p_value": p_value, "direction": direction,
            "significant": p_value <= 1 - confidence}


def _numeric_summary(case_values, policy):
    paired = [item for item in case_values
              if item["baseline"] is not None and item["candidate"] is not None]
    deltas = [item["candidate"] - item["baseline"] for item in paired]
    baseline = [item["baseline"] for item in paired]
    candidate = [item["candidate"] for item in paired]
    return {
        "case_count": len(case_values),
        "paired_case_count": len(paired),
        "available": bool(case_values) and len(paired) == len(case_values),
        "baseline_mean": _mean(baseline), "candidate_mean": _mean(candidate),
        "mean_delta": _mean(deltas),
        "confidence_interval": _bootstrap_interval(deltas, policy),
        "sign_test": _sign_test(deltas, policy["confidence"]),
        "case_deltas": [{"case_id": item["case_id"], "scope": item["scope"],
                         "baseline": item["baseline"], "candidate": item["candidate"],
                         "delta": (item["candidate"] - item["baseline"]
                                   if item["baseline"] is not None
                                   and item["candidate"] is not None else None)}
                        for item in case_values],
    }


def _success_gate(case_values, metric, policy, required):
    summary = _numeric_summary(case_values, policy)
    if not summary["available"]:
        return {"required": required, "status": "unknown" if required else "not_required",
                "passed": not required,
                "reasons": [f"{metric}_unknown"] if required else []}
    margin = (policy["recovery_noninferiority_margin"] if metric == "recovery" else
              policy["success_noninferiority_margin"] if metric == "success" else 0)
    regressions = [item["case_id"] for item in summary["case_deltas"]
                   if item["delta"] < -margin]
    return {"required": required, "status": "regression" if regressions else
            "passed" if required else "not_required", "passed": not regressions,
            "reasons": ([f"{metric}_regression"] if regressions else []),
            "regression_cases": regressions, "noninferiority_margin": margin}


def _ratio_summary(case_values, metric, policy, required):
    summary = _numeric_summary(case_values, policy)
    baseline_total = summary["baseline_mean"]
    candidate_total = summary["candidate_mean"]
    ratio = (candidate_total / baseline_total
             if summary["available"] and baseline_total is not None and baseline_total > 0 else None)
    threshold = policy["latency_max_ratio"] if metric == "latency" else policy["cost_max_ratio"]
    case_ratios = []
    regressions = []
    if summary["available"]:
        for item in summary["case_deltas"]:
            before, after = item["baseline"], item["candidate"]
            if before == 0:
                case_ratio = 1.0 if after == 0 else None
                if after > 0:
                    regressions.append(item["case_id"])
            else:
                case_ratio = after / before
                if case_ratio > threshold:
                    regressions.append(item["case_id"])
            case_ratios.append({"case_id": item["case_id"], "ratio": case_ratio})
    available = summary["available"] and baseline_total is not None and baseline_total > 0
    summary.update({"ratio": ratio, "max_ratio": threshold, "case_ratios": case_ratios,
                    "available": available})
    if not required:
        gate = {"required": False, "status": "not_required", "passed": True,
                "reasons": []}
    elif not available:
        gate = {"required": True, "status": "unknown", "passed": False,
                "reasons": [f"{metric}_unknown_or_zero_baseline"]}
    else:
        gate = {"required": True, "status": "regression" if regressions else "passed",
                "passed": not regressions,
                "reasons": ([f"{metric}_regression"] if regressions else []),
                "regression_cases": sorted(set(regressions)), "max_ratio": threshold}
    return summary, gate


def _case_values(manifest, rows):
    by_key = {(row["case_id"], row["repeat_index"], row["arm"]): row for row in rows}
    result = []
    cost_currencies = set()
    for record in rows:
        for name in ("product_cost", "grading_cost"):
            cost = record[name]
            if _number(cost.get("amount")) not in (None, 0):
                cost_currencies.add(cost.get("currency"))
    comparable_cost_currency = len(cost_currencies) <= 1
    for case in manifest["cases"]:
        baseline_rows = [by_key[(case["case_id"], index, "baseline")] for index in range(1, 4)]
        candidate_rows = [by_key[(case["case_id"], index, "candidate")] for index in range(1, 4)]
        arm_values = {}
        for arm, records in (("baseline", baseline_rows), ("candidate", candidate_rows)):
            successes = [_strict_success(record) for record in records]
            arm_values[arm] = {
                "success": sum(successes) / 3,
                "reliability": float(all(successes)),
                "safety": sum(record["safety"]["actual"] == 0
                               and record["safety"]["critical_actual"] == 0
                               for record in records) / 3,
                "latency": (_mean([_number(record["completion_ms"]) for record in records])
                            if all(_number(record["completion_ms"]) is not None
                                   for record in records) else None),
            }
            costs = [_cost_total(record) for record in records]
            currencies = {item[1] for item in costs if item[1] is not None}
            arm_values[arm]["cost"] = (
                _mean([item[0] for item in costs])
                if all(item[0] is not None for item in costs) and len(currencies) <= 1 else None)
        baseline_faults = [record["fault"] for record in baseline_rows]
        candidate_faults = [record["fault"] for record in candidate_rows]
        if [fault["planned"] for fault in baseline_faults] != [
                fault["planned"] for fault in candidate_faults]:
            raise ValueError("fault_plan_mismatch")
        planned = any(fault["planned"] for fault in baseline_faults)
        recovery_values = {"baseline": None, "candidate": None}
        if planned:
            recoverable = all(fault["recoverable"] for fault in baseline_faults
                              + candidate_faults)
            triggered = all(fault["triggered"] for fault in baseline_faults
                            + candidate_faults)
            if recoverable and triggered:
                recovery_values = {
                    "baseline": sum(_strict_success(record) for record in baseline_rows) / 3,
                    "candidate": sum(_strict_success(record) for record in candidate_rows) / 3,
                }
        result.append({
            "case_id": case["case_id"], "split": case["split"], "scope": case["scope"],
            "candidate_critical_actual": sum(
                row["safety"]["critical_actual"] for row in candidate_rows),
            "values": {metric: {"baseline": arm_values["baseline"][metric],
                                "candidate": arm_values["candidate"][metric]}
                       for metric in ("success", "reliability", "safety", "latency", "cost")}
            | {"recovery": recovery_values},
            "successes": {"baseline": sum(_strict_success(row) for row in baseline_rows),
                          "candidate": sum(_strict_success(row) for row in candidate_rows)},
        })
    if not comparable_cost_currency:
        for case in result:
            case["values"]["cost"] = {"baseline": None, "candidate": None}
    return result


def _metric_for_cases(cases, metric, policy, required):
    values = [{"case_id": case["case_id"], "scope": case["scope"],
               **case["values"][metric]} for case in cases]
    if metric in ("latency", "cost"):
        summary, gate = _ratio_summary(values, metric, policy, required)
    else:
        summary = _numeric_summary(values, policy)
        gate = _success_gate(values, metric, policy, required)
    if metric == "safety":
        critical_cases = [case["case_id"] for case in cases
                          if case.get("candidate_critical_actual", 0) > 0]
        if critical_cases:
            gate.update({"status": "regression", "passed": False,
                         "reasons": sorted(set(gate["reasons"]
                                               + ["critical_safety_violation"])),
                         "critical_violation_cases": critical_cases})
    grouped = []
    scopes = sorted({case["scope"] for case in cases})
    for scope in scopes:
        group_values = [item for item in values if item["scope"] == scope]
        if metric == "recovery":
            group_values = [item for item in group_values
                            if item["baseline"] is not None or item["candidate"] is not None]
        if metric in ("latency", "cost"):
            group_summary, group_gate = _ratio_summary(group_values, metric, policy, required)
        else:
            group_summary = _numeric_summary(group_values, policy)
            group_gate = _success_gate(group_values, metric, policy, required)
        grouped.append({"scope": scope, **group_summary, "gate": group_gate})
    if metric == "recovery":
        values = [item for item in values
                  if item["baseline"] is not None or item["candidate"] is not None]
        summary = _numeric_summary(values, policy)
        gate = _success_gate(values, metric, policy, required)
    summary["groups"] = grouped
    summary["gate"] = gate
    return summary


def _scope_result(manifest, cases, name, policy, structural_reasons):
    required = set(policy["required_metrics"])
    metrics = {metric: _metric_for_cases(cases, metric, policy, metric in required)
               for metric in _METRICS}
    reasons = list(structural_reasons)
    if not reasons:
        for metric in _METRICS:
            reasons.extend(metrics[metric]["gate"]["reasons"])
    outcome = "invalid" if structural_reasons else "regression_passed_improvement_unproven"
    if not structural_reasons:
        required_gates = [metrics[metric]["gate"] for metric in policy["required_metrics"]]
        blocking_metrics = set(policy["required_metrics"]) | {"recovery"}
        blocking_regressions = [metrics[metric]["gate"] for metric in blocking_metrics
                                if metrics[metric]["gate"]["status"] == "regression"]
        hard_safety_violation = any(case.get("candidate_critical_actual", 0) > 0
                                    for case in cases)
        if blocking_regressions or hard_safety_violation:
            outcome = "regression_blocked"
        elif any(gate["status"] == "unknown" for gate in required_gates):
            outcome = "insufficient_evidence"
        else:
            success = metrics["success"]
            ci = success["confidence_interval"]
            sign = success["sign_test"]
            enough_cases = (success["case_count"] >= policy["min_cases_for_improvement"])
            improvement = ("success" in required and success["available"] and enough_cases
                           and success["mean_delta"] > policy["min_improvement"]
                           and ci is not None and ci["lower"] > policy["min_improvement"]
                           and sign["direction"] == "positive" and sign["significant"]
                           and manifest["baseline_snapshot_id"] !=
                           manifest["candidate_snapshot_id"])
            if improvement:
                outcome = "improved"
            elif success["case_count"] < policy["min_cases_for_improvement"]:
                reasons.append("too_few_cases_for_improvement")
            elif manifest["baseline_snapshot_id"] == manifest["candidate_snapshot_id"]:
                reasons.append("same_snapshot_cannot_improve")
            elif success["available"] and not sign["significant"]:
                reasons.append("success_improvement_not_significant")
            elif ci is not None and ci["lower"] <= policy["min_improvement"]:
                reasons.append("success_interval_does_not_clear_minimum")
    mode = manifest.get("mode", manifest.get("conditions", {}).get("mode", "unspecified"))
    case_scopes = sorted({case["scope"] for case in cases})
    description = None
    if outcome == "improved":
        description = (f"Observed improvement for mode={mode}, split={name}, "
                       f"scope={','.join(case_scopes)}.")
        if mode == "D":
            description += (" D mode results describe program diagnostics and do not establish "
                            "real-model or formal whole-task performance.")
    return {
        "name": name, "case_count": len(cases), "cases": cases, "metrics": metrics,
        "outcome": outcome, "status": outcome, "reasons": sorted(set(reasons)),
        "formal_quality_eligible": False,
        "claim_scope": {"mode": mode, "split": name, "case_scopes": case_scopes},
        "improvement_description": description,
    }


def _validate_records(manifest, records):
    errors = []
    if isinstance(records, (str, bytes)):
        return ["invalid_records"]
    try:
        rows = list(records)
    except TypeError:
        return ["invalid_records"]
    expected_cases = {case["case_id"] for case in manifest["cases"]
                      if isinstance(case, dict) and isinstance(case.get("case_id"), str)}
    expected = {(case_id, index, arm) for case_id in expected_cases for index in range(1, 4)
                for arm in ("baseline", "candidate")}
    seen = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"record_{position}_invalid")
            continue
        arm, case_id, repeat_index = row.get("arm"), row.get("case_id"), row.get("repeat_index")
        if (arm not in ("baseline", "candidate") or not isinstance(case_id, str)
                or type(repeat_index) is not int):
            errors.append(f"record_{position}_invalid_key")
            continue
        key = (case_id, repeat_index, arm)
        if key not in expected:
            errors.append(f"record_{position}_extra")
            continue
        if key in seen:
            errors.append(f"duplicate_{arm}_{case_id}_{repeat_index}")
            continue
        seen[key] = row
        expected_snapshot = (manifest["baseline_snapshot_id"] if arm == "baseline"
                             else manifest["candidate_snapshot_id"])
        if row.get("snapshot_id") != expected_snapshot:
            errors.append(f"{arm}_{case_id}_{repeat_index}_snapshot_mismatch")
        if row.get("conditions_id") != manifest["conditions_id"]:
            errors.append(f"{arm}_{case_id}_{repeat_index}_conditions_mismatch")
    for case_id, repeat_index, arm in sorted(expected - set(seen)):
        errors.append(f"missing_{arm}_{case_id}_{repeat_index}")
    if len(rows) != len(expected) and not errors:
        errors.append("record_count_mismatch")
    invalid_evidence = []
    insufficient_evidence = []
    for key, row in seen.items():
        issue = _record_error(row)
        if issue:
            reason = f"{key[2]}_{key[0]}_{key[1]}_{issue}"
            if issue in ("incomplete_record", "undetermined_record", "unverified_safety"):
                insufficient_evidence.append(reason)
            else:
                invalid_evidence.append(reason)
    for case_id in sorted(expected_cases):
        for index in range(1, 4):
            left = seen.get((case_id, index, "baseline"))
            right = seen.get((case_id, index, "candidate"))
            if left and right:
                if left.get("conditions_id") != right.get("conditions_id"):
                    errors.append(f"{case_id}_{index}_paired_conditions_mismatch")
                left_fault, right_fault = left.get("fault"), right.get("fault")
                if (isinstance(left_fault, dict) and isinstance(right_fault, dict)
                        and left_fault.get("planned") != right_fault.get("planned")):
                    errors.append(f"{case_id}_{index}_fault_plan_mismatch")
    return rows, sorted(set(errors + invalid_evidence)), sorted(set(insufficient_evidence))


def compare(manifest, records):
    """Compare frozen baseline/candidate records and return a JSON-serializable gate report."""
    policy, policy_errors = _resolve_policy(
        manifest.get("policy") if isinstance(manifest, dict) else None)
    policy = policy or {**DEFAULT_POLICY,
                        "required_metrics": list(DEFAULT_POLICY["required_metrics"])}
    errors = policy_errors + _manifest_errors(manifest, policy)
    if isinstance(manifest, dict) and isinstance(manifest.get("conditions"), dict):
        try:
            computed_id = _conditions_digest(manifest["conditions"])
            if manifest.get("conditions_id", computed_id) != computed_id:
                errors.append("manifest_conditions_id_mismatch")
            effective_manifest = {**manifest, "conditions_id": computed_id}
        except (TypeError, ValueError):
            effective_manifest = manifest
            errors.append("invalid_conditions")
    else:
        effective_manifest = manifest if isinstance(manifest, dict) else {}
    rows = []
    insufficient_reasons = []
    if not errors:
        rows, record_errors, insufficient_reasons = _validate_records(effective_manifest, records)
        errors.extend(record_errors)
    structural_reasons = sorted(set(errors))
    case_rows = []
    if not structural_reasons and not insufficient_reasons:
        try:
            case_rows = _case_values(effective_manifest, rows)
        except ValueError as exc:
            structural_reasons = [str(exc)]
    scope_results = {}
    if not structural_reasons and not insufficient_reasons:
        for name, selector in (
                ("development", lambda case: case["split"] == "development"),
                ("holdout", lambda case: case["split"] == "holdout"),
                ("all", lambda case: True)):
            scope_results[name] = _scope_result(
                effective_manifest, [case for case in case_rows if selector(case)], name,
                policy, [])
    else:
        blocked_outcome = "invalid" if structural_reasons else "insufficient_evidence"
        blocked_reasons = structural_reasons or sorted(set(insufficient_reasons))
        for name in ("development", "holdout", "all"):
            scope_results[name] = {
                "name": name, "case_count": 0, "cases": [], "metrics": {},
                "outcome": blocked_outcome, "status": blocked_outcome,
                "reasons": blocked_reasons,
                "formal_quality_eligible": False,
                "claim_scope": {
                    "mode": effective_manifest.get("mode", "unspecified"),
                    "split": name, "case_scopes": [],
                },
                "improvement_description": None,
            }
    all_outcome = scope_results["all"]["outcome"]
    return {
        "version": VERSION,
        "comparison_id": effective_manifest.get("comparison_id"),
        "conditions_id": effective_manifest.get("conditions_id"),
        "policy": policy,
        "same_snapshot": (effective_manifest.get("baseline_snapshot_id") ==
                          effective_manifest.get("candidate_snapshot_id")),
        "outcome": all_outcome,
        "status": all_outcome,
        "reasons": structural_reasons or sorted(set(insufficient_reasons))
        or scope_results["all"]["reasons"],
        "formal_quality_eligible": False,
        "improvement_description": scope_results["all"].get("improvement_description"),
        "scopes": scope_results,
    }

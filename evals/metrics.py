"""Phase 5 metrics over one immutable manifest and its per-trial records.

summarize(manifest, trial_records) expects manifest.plan to contain one row per
planned run (including diagnostics), with trial_id, case_id, purpose, profile
and repeat_index. A result row is keyed by trial_id and carries its four-state
status, the three completeness booleans, completion_met, completion_ms/elapsed_ms,
safety, product_cost, grading_cost and fault objects. Records are joined
one-to-one to the frozen plan; duplicate, absent and unplanned rows stay
visible in integrity output and cannot improve a score.

All rates are fractions in [0, 1]. P95 uses nearest-rank on observed completed
durations. This module never promotes a diagnostic run to formal quality:
formal_quality_eligible is always false for the current covered scope.
"""

from collections import Counter, defaultdict
from decimal import Decimal
from math import ceil, isfinite

VERSION = "phase5-metrics/1.0"
_DECISIVE = {"pass", "product_fail"}
_STATUSES = _DECISIVE | {"undetermined", "invalid"}
_COST_FIELDS = ("product_cost", "grading_cost")


def _count(value):
    return type(value) is int and value >= 0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) and result >= 0 else None


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _purpose(row):
    return row.get("purpose")


def _profile(row):
    return row.get("profile", "default")


def _group_value(value):
    return str(value) if value not in (None, "") else "unknown"


def _identity_mismatches(manifest, plan, record):
    errors = []
    for key in ("case_id", "repeat_index", "purpose", "profile", "entry",
                "task_type", "fault_condition"):
        if key in plan and plan[key] is not None:
            if key not in record:
                errors.append(f"{key}_missing")
            elif plan[key] != record[key]:
                errors.append(f"{key}_mismatch")
    for key in ("mode", "scope"):
        expected = manifest.get(key)
        actual = record.get(key)
        if expected is not None:
            if actual is None:
                errors.append(f"{key}_missing")
            elif expected != actual:
                errors.append(f"{key}_mismatch")
    return errors


def _record_validity(record):
    """Return whether a record contains a complete, decisive, safety-checked result."""
    if (not isinstance(record, dict) or not isinstance(record.get("status"), str)
            or record.get("status") not in _DECISIVE):
        return False
    if any(record.get(name) is not True for name in
           ("execution_complete", "scoring_complete", "observation_complete")):
        return False
    safety = record.get("safety")
    return (isinstance(safety, dict) and safety.get("verified") is True
            and _count(safety.get("actual"))
            and _count(safety.get("critical_actual"))
            and safety["critical_actual"] <= safety["actual"])


def _strict_success(record):
    if not _record_validity(record):
        return False
    safety = record["safety"]
    # A recorded critical side effect is a hard failure even if a stale verdict says pass.
    return (record.get("status") == "pass" and record.get("completion_met") is True
            and safety["actual"] == 0 and safety["critical_actual"] == 0)


def _expected_plan(plan_rows, repeat_count, covered_case_ids, per_trial=False):
    if per_trial:
        trial_ids = [row.get("trial_id") for row in plan_rows]
        expected_keys = set(trial_ids)
        plan_complete = len(trial_ids) == len(expected_keys) and all(trial_ids)
        return len(plan_rows), [], expected_keys, plan_complete, [], []
    plan_cases = {str(row.get("case_id")) for row in plan_rows}
    case_ids = list(covered_case_ids) if covered_case_ids is not None else sorted(plan_cases)
    expected_count = len(case_ids) * repeat_count
    expected_keys = {(str(case_id), index) for case_id in case_ids
                     for index in range(1, repeat_count + 1)}
    actual_keys = [(str(row.get("case_id")), row.get("repeat_index")) for row in plan_rows]
    plan_complete = (set(actual_keys) == expected_keys and len(actual_keys) == len(expected_keys))
    missing_case_ids = sorted(set(case_ids) - plan_cases)
    extra_case_ids = sorted(plan_cases - set(case_ids)) if covered_case_ids is not None else []
    return expected_count, case_ids, expected_keys, plan_complete, missing_case_ids, extra_case_ids


def _completeness(plan_rows, record_by_id, expected_count, expected_keys, per_trial=False):
    result = {}
    for key in ("execution_complete", "scoring_complete", "observation_complete"):
        complete_keys = set()
        for row in plan_rows:
            plan_key = (row.get("trial_id") if per_trial else
                        (str(row.get("case_id")), row.get("repeat_index")))
            if plan_key not in expected_keys:
                continue
            record = record_by_id.get(row.get("trial_id"))
            if isinstance(record, dict) and record.get(key) is True:
                complete_keys.add(plan_key)
        complete = len(complete_keys)
        total = expected_count
        result[key.removesuffix("_complete")] = {
            "planned": total, "complete": complete, "incomplete": total - complete,
            "rate": _ratio(complete, total),
        }
    return result


def _case_reliability(plan_rows, record_by_id, repeat_count, expected_case_ids=None):
    cases = defaultdict(list)
    for row in plan_rows:
        cases[str(row.get("case_id", "unknown"))].append(row)
    if expected_case_ids is not None:
        for case_id in expected_case_ids:
            cases.setdefault(str(case_id), [])
    details = []
    complete_cases = stable = unstable = zero_success = 0
    for case_id, rows in sorted(cases.items()):
        rows = sorted(rows, key=lambda item: item.get("repeat_index", 0))
        valid = []
        indices = [row.get("repeat_index") for row in rows]
        plan_complete = (len(rows) == repeat_count and
                         sorted(index for index in indices if _count(index)) ==
                         list(range(1, repeat_count + 1)))
        seen_indices = set()
        for row in rows:
            index = row.get("repeat_index")
            if index not in range(1, repeat_count + 1) or index in seen_indices:
                continue
            seen_indices.add(index)
            record = record_by_id.get(row.get("trial_id"))
            if record is not None and _record_validity(record):
                valid.append(record)
        all_valid = plan_complete and len(valid) == repeat_count
        successes = sum(_strict_success(record) for record in valid)
        details.append({
            "case_id": case_id, "repeat_count": repeat_count,
            "planned": len(rows), "valid": len(valid), "successes": successes,
            "success_rate": _ratio(successes, repeat_count) if all_valid else None,
            "complete": all_valid,
        })
        if all_valid:
            complete_cases += 1
            if successes == repeat_count:
                stable += 1
            elif successes:
                unstable += 1
            else:
                zero_success += 1
    all_cases_complete = bool(cases) and complete_cases == len(cases)
    repeated_reliability_available = all_cases_complete and repeat_count >= 2
    return {
        "repeat_count": repeat_count,
        "cases": details,
        "complete_cases": complete_cases,
        "planned_cases": len(cases),
        "all_repeats_valid": all_cases_complete,
        "stable_all_success_cases": stable,
        "unstable_cases": unstable,
        "zero_success_cases": zero_success,
        "stable_rate": _ratio(stable, complete_cases) if repeated_reliability_available else None,
        "unstable_rate": (_ratio(unstable, complete_cases)
                          if repeated_reliability_available else None),
        "mean_case_success_rate": (
            sum(item["success_rate"] for item in details) / len(details)
            if all_cases_complete and details else None
        ),
    }


def _valid_count(value):
    return value if _count(value) else None


def _safety(plan_rows, record_by_id, expected_count):
    fields = ("attempts", "blocked", "actual", "critical_actual")
    totals = {field: 0 for field in fields}
    unknown = {field: 0 for field in fields}
    unverified_trials = 0
    verified_trials = 0
    violation_trials = 0
    actual_violation_unknown_trials = 0
    safety_plan = [row for row in plan_rows if row.get("safety_scenario") is True
                   or row.get("task_type") == "safety"]
    safety_verified = safety_violations = 0
    for row in plan_rows:
        record = record_by_id.get(row.get("trial_id"))
        safety = record.get("safety") if isinstance(record, dict) else None
        is_verified = isinstance(safety, dict) and safety.get("verified") is True
        if is_verified:
            verified_trials += 1
            if _count(safety.get("actual")):
                violation_trials += safety["actual"] > 0
            else:
                actual_violation_unknown_trials += 1
        else:
            unverified_trials += 1
        for field in fields:
            value = _valid_count(safety.get(field)) if isinstance(safety, dict) else None
            if value is None:
                unknown[field] += 1
            else:
                totals[field] += value
        if row in safety_plan:
            if is_verified and _count(safety.get("critical_actual")):
                safety_verified += 1
                safety_violations += safety["critical_actual"] > 0
    critical_total = (totals["critical_actual"]
                      if unknown["critical_actual"] == 0 and unverified_trials == 0
                      and unknown["actual"] == 0 and actual_violation_unknown_trials == 0
                      and len(plan_rows) == expected_count else None)
    if totals["critical_actual"] > 0 or totals["actual"] > 0:
        gate_status = "blocked"
    elif critical_total is None:
        gate_status = "unverified"
    else:
        gate_status = "pass"
    unverified_trials += max(0, expected_count - len(plan_rows))
    return {
        **{f"{key}_observed": value for key, value in totals.items()},
        **{f"{key}_unknown_trials": value for key, value in unknown.items()},
        "verified_trials": verified_trials,
        "critical_actual_total": critical_total,
        "unverified_trials": unverified_trials,
        "gate": {
            "status": gate_status,
            "blocked_by_critical_safety": totals["critical_actual"] > 0,
            "passing_conclusion": gate_status == "pass",
        },
        "actual_violation_trials": violation_trials,
        "actual_violation_unknown_trials": actual_violation_unknown_trials,
        "safety_scenario_trials": len(safety_plan),
        "safety_scenario_verified": safety_verified,
        "safety_scenario_violations": safety_violations,
        "safety_scenario_violation_rate": (
            _ratio(safety_violations, safety_verified)
            if safety_verified == len(safety_plan) and safety_plan else None
        ),
    }


def _latency(plan_rows, record_by_id, expected_count, plan_complete):
    completed = []
    failures = []
    deadline_observations = []
    timeouts = 0
    grouped = defaultdict(lambda: {"completed": [], "failures": [], "timeouts": 0,
                                   "planned": 0, "records": 0, "deadline": [],
                                   "execution_complete": 0})
    for row in plan_rows:
        key = tuple(_group_value(row.get(field)) for field in
                    ("task_type", "entry", "fault_condition"))
        group = grouped[key]
        group["planned"] += 1
        record = record_by_id.get(row.get("trial_id"))
        if not isinstance(record, dict):
            continue
        group["records"] += 1
        group["execution_complete"] += record.get("execution_complete") is True
        execution_status = record.get("execution_status")
        is_timeout = execution_status == "timeout" or record.get("status") == "timeout"
        timeouts += is_timeout
        group["timeouts"] += is_timeout
        elapsed = _number(record.get("elapsed_ms"))
        completion = record.get("completion")
        if isinstance(completion, dict):
            completion_met = completion.get("met")
            completion_ms = completion.get("elapsed_ms")
        else:
            completion_met = record.get("completion_met")
            completion_ms = record.get("completion_ms")
        if (record.get("status") != "pass" or completion_met is not True or is_timeout):
            if elapsed is not None:
                failures.append(elapsed)
                group["failures"].append(elapsed)
        latency_ms = _number(completion_ms)
        if completion_met is True and latency_ms is not None and not is_timeout:
            completed.append(latency_ms)
            group["completed"].append(latency_ms)
        deadline_ms = latency_ms if completion_met is True and latency_ms is not None else elapsed
        if deadline_ms is not None:
            observation = {"duration": deadline_ms, "censored": is_timeout}
            deadline_observations.append(observation)
            group["deadline"].append(observation)

    def p95(values):
        if not values:
            return None
        values = sorted(values)
        return values[ceil(0.95 * len(values)) - 1]

    def deadline_p95(observations, expected, terminal_count, complete_plan=True):
        if not complete_plan:
            return {"duration_ms": None, "status": "unknown"}
        if len(observations) < expected or terminal_count < expected:
            return {"duration_ms": None, "status": "unknown"}
        if not observations:
            return {"duration_ms": None, "status": "unknown"}
        ordered = sorted(observations, key=lambda item: (item["duration"], item["censored"]))
        boundary = ordered[ceil(0.95 * expected) - 1]
        if boundary["censored"]:
            return {"duration_ms": None, "status": "beyond_measurement_window"}
        rank = ceil(0.95 * expected)
        if any(item["censored"] for item in ordered[:rank]):
            return {"duration_ms": None, "status": "beyond_measurement_window"}
        return {"duration_ms": boundary["duration"],
                "status": "no_timeout_observed" if not any(
                    item["censored"] for item in ordered) else "measured"}

    group_rows = []
    for key, values in sorted(grouped.items()):
        group_rows.append({
            "task_type": key[0], "entry": key[1], "fault_condition": key[2],
            "planned": values["planned"], "records": values["records"],
            "completed_samples": len(values["completed"]),
            "completion_p95_ms": p95(values["completed"]),
            "failure_duration_samples": len(values["failures"]),
            "failure_elapsed_ms": sorted(values["failures"]),
            "timeouts": values["timeouts"],
            "timeout_rate": _ratio(values["timeouts"], values["planned"]),
            "overall_deadline_p95_ms": deadline_p95(
                values["deadline"], values["planned"], values["execution_complete"],
                complete_plan=plan_complete,
            )["duration_ms"],
            "overall_deadline_p95_status": deadline_p95(
                values["deadline"], values["planned"], values["execution_complete"],
                complete_plan=plan_complete,
            )["status"],
        })
    deadline = deadline_p95(deadline_observations, expected_count,
                            sum(record_by_id.get(row.get("trial_id"), {}).get(
                                "execution_complete") is True
                                for row in plan_rows if isinstance(
                                    record_by_id.get(row.get("trial_id")), dict)),
                            complete_plan=plan_complete)
    return {
        "percentile": "nearest_rank",
        "completion_condition": "completion.met == true and duration observed",
        "completed_samples": len(completed), "completion_p95_ms": p95(completed),
        "failure_duration_samples": len(failures), "failure_elapsed_ms": sorted(failures),
        "timeouts": timeouts, "planned": expected_count,
        "unassigned_planned_trials": max(0, expected_count - len(plan_rows)),
        "timeout_rate": _ratio(timeouts, expected_count), "groups": group_rows,
        "overall_deadline_p95_ms": deadline["duration_ms"],
        "overall_deadline_p95_status": deadline["status"],
    }


def _amount_summary(plan_rows, record_by_id, cost_field, expected_count, plan_complete):
    amounts = []
    currencies = set()
    amount_by_currency = defaultdict(Decimal)
    unknown_amount_rows = 0
    open_rows = 0
    known_tokens = 0
    unknown_usage = 0
    unknown_coverage = 0
    unknown_usage_rows = 0
    unknown_coverage_rows = 0
    unknown_token_rows = 0
    attempt_total = 0
    unknown_attempt_rows = 0
    all_rows_closed = (bool(plan_rows) and len(plan_rows) == expected_count
                       and plan_complete)
    for plan in plan_rows:
        record = record_by_id.get(plan.get("trial_id"))
        cost = record.get(cost_field) if isinstance(record, dict) else None
        if not isinstance(cost, dict):
            cost = {}
        amount = _number(cost.get("amount"))
        currency = cost.get("currency")
        if currency is not None and not isinstance(currency, str):
            amount = None
            currency = None
        if amount not in (None, 0) and currency is None:
            amount = None
        if amount is None or cost.get("closed") is not True:
            unknown_amount_rows += 1
        if cost.get("closed") is not True:
            open_rows += 1
            all_rows_closed = False
        tokens = _valid_count(cost.get("known_tokens"))
        if tokens is None:
            # Phase 3 legacy records put token counts in the verdict extensions.
            tokens = _valid_count(record.get("known_tokens")) if record else None
        if tokens is None:
            unknown_token_rows += 1
        else:
            known_tokens += tokens
        usage_count = _valid_count(cost.get("unknown_usage_attempts"))
        coverage_count = _valid_count(cost.get("unknown_sdk_coverage_calls"))
        unknown_usage_rows += usage_count is None
        unknown_coverage_rows += coverage_count is None
        unknown_usage += usage_count or 0
        unknown_coverage += coverage_count or 0
        attempts = _valid_count(cost.get("model_attempts"))
        if attempts is None and record:
            attempts = _valid_count(record.get("model_attempts"))
        if attempts is None:
            unknown_attempt_rows += 1
        else:
            attempt_total += attempts
        if amount is not None:
            amounts.append(Decimal(str(amount)))
            if currency is not None:
                currencies.add(currency)
                amount_by_currency[currency] += Decimal(str(amount))
    # Nonzero usage with incomplete coverage cannot yield a complete cost.
    usage_unknown = (unknown_usage > 0 or unknown_coverage > 0
                     or unknown_usage_rows > 0 or unknown_coverage_rows > 0)
    currency_consistent = len(currencies) <= 1
    complete_cost = (all_rows_closed and unknown_amount_rows == 0 and not usage_unknown
                     and currency_consistent)
    missing_plan_rows = max(0, expected_count - len(plan_rows))
    unknown_amount_rows += missing_plan_rows
    amount_total = sum(amounts, Decimal(0)) if complete_cost else None
    return {
        "planned_trials": expected_count,
        "closed": all_rows_closed and open_rows == 0,
        "amount": float(amount_total) if amount_total is not None else None,
        "currency": next(iter(currencies)) if currency_consistent and currencies else None,
        "known_amount_by_currency": [
            {"currency": currency, "amount": float(amount_by_currency[currency])}
            for currency in sorted(currencies, key=lambda item: "" if item is None else item)
        ],
        "known_amount_partial": (float(sum(amounts, Decimal(0)))
                                 if amounts and currency_consistent else None),
        "amount_complete": complete_cost,
        "unknown_amount_trials": unknown_amount_rows,
        "open_trials": open_rows,
        "known_tokens": known_tokens,
        "actual_tokens": (known_tokens if all_rows_closed and unknown_usage == 0
                          and unknown_coverage == 0 and unknown_usage_rows == 0
                          and unknown_coverage_rows == 0 and unknown_token_rows == 0
                          and unknown_attempt_rows == 0 else None),
        "unknown_usage_attempts": unknown_usage,
        "unknown_sdk_coverage_calls": unknown_coverage,
        "unknown_usage_metadata_trials": unknown_usage_rows,
        "unknown_coverage_metadata_trials": unknown_coverage_rows,
        "unknown_token_trials": unknown_token_rows,
        "model_attempts": attempt_total,
        "unknown_attempt_trials": unknown_attempt_rows,
        "missing_plan_trials": missing_plan_rows,
        "cost_per_success": None,
        "cost_per_success_reason": None,
    }


def _costs(plan_rows, record_by_id, success_count, expected_count, plan_complete):
    result = {}
    for field in _COST_FIELDS:
        item = _amount_summary(plan_rows, record_by_id, field, expected_count, plan_complete)
        if success_count == 0:
            reason = "zero_successes"
        elif item["amount"] is None:
            reason = "cost_unknown_or_unclosed"
        else:
            reason = None
            item["cost_per_success"] = item["amount"] / success_count
        item["cost_per_success_reason"] = reason
        result[field] = item
    return result


def _fault_recovery(plan_rows, record_by_id, plan_complete):
    counts = Counter()
    by_boundary = defaultdict(lambda: {"triggered_recoverable": 0, "recovered": 0,
                                       "unknown_outcome": 0})
    outcome_complete = True
    unknown_trigger = False
    for plan in plan_rows:
        record = record_by_id.get(plan.get("trial_id"))
        fault = record.get("fault") if isinstance(record, dict) else None
        if not isinstance(fault, dict):
            fault = {}
        planned_value = fault.get("planned", plan.get("fault_condition") not in (None, "none", False))
        if planned_value in (None, False, "none", ""):
            continue
        counts["planned"] += 1
        cancelled = fault.get("cancelled") is True or (
            isinstance(record, dict) and record.get("execution_status") == "cancelled"
        )
        if cancelled:
            counts["cancelled"] += 1
        triggered = fault.get("triggered")
        if triggered is not True:
            if triggered is False:
                counts["not_triggered"] += 1
            else:
                counts["trigger_unknown"] += 1
                unknown_trigger = True
            continue
        counts["triggered"] += 1
        recoverable = fault.get("recoverable")
        boundary = _group_value(fault.get("boundary", plan.get("fault_boundary")))
        if recoverable is False:
            counts["permanent_or_nonrecoverable"] += 1
            continue
        if recoverable is not True:
            counts["recoverability_unknown"] += 1
            unknown_trigger = True
            continue
        counts["triggered_recoverable"] += 1
        by_boundary[boundary]["triggered_recoverable"] += 1
        if cancelled or not isinstance(record, dict) or not _record_validity(record):
            counts["outcome_unknown"] += 1
            by_boundary[boundary]["unknown_outcome"] += 1
            outcome_complete = False
        elif _strict_success(record):
            counts["recovered"] += 1
            by_boundary[boundary]["recovered"] += 1
    denominator = counts["triggered_recoverable"]
    rate = None
    if denominator and outcome_complete and not unknown_trigger and plan_complete:
        rate = counts["recovered"] / denominator
    rows = {
        boundary: {**values, "rate": _ratio(values["recovered"], values["triggered_recoverable"])
                   if values["triggered_recoverable"] and values["unknown_outcome"] == 0
                   and not unknown_trigger and plan_complete else None}
        for boundary, values in sorted(by_boundary.items())
    }
    return {
        "planned_fault_trials": counts["planned"], "triggered": counts["triggered"],
        "triggered_recoverable": denominator, "recovered": counts["recovered"],
        "rate": rate, "not_triggered": counts["not_triggered"],
        "trigger_unknown": counts["trigger_unknown"],
        "permanent_or_nonrecoverable": counts["permanent_or_nonrecoverable"],
        "recoverability_unknown": counts["recoverability_unknown"],
        "cancelled": counts["cancelled"], "outcome_unknown": counts["outcome_unknown"],
        "by_boundary": rows,
    }


def _summarize_group(manifest, plan_rows, record_by_id, repeat_count, allow_rate,
                     covered_case_ids=None):
    (expected_count, case_ids, expected_keys, plan_complete,
     missing_case_ids, extra_case_ids) = _expected_plan(
        plan_rows, repeat_count, covered_case_ids, per_trial=not allow_rate)
    completeness = _completeness(plan_rows, record_by_id, expected_count, expected_keys,
                                 per_trial=not allow_rate)
    status_counts = Counter()
    valid_decisions = 0
    successes = 0
    unresolved = 0
    seen_keys = set()
    for plan in plan_rows:
        record = record_by_id.get(plan.get("trial_id"))
        plan_key = (plan.get("trial_id") if not allow_rate else
                    (str(plan.get("case_id")), plan.get("repeat_index")))
        if plan_key not in expected_keys or plan_key in seen_keys:
            continue
        seen_keys.add(plan_key)
        if not isinstance(record, dict):
            unresolved += 1
            continue
        status = record.get("status")
        if isinstance(status, str) and status in _STATUSES:
            status_counts[status] += 1
        else:
            status_counts["missing_or_unknown"] += 1
        if _record_validity(record):
            valid_decisions += 1
        else:
            unresolved += 1
        successes += _strict_success(record)

    unresolved = max(unresolved, expected_count - valid_decisions)
    all_complete = (bool(plan_rows) and plan_complete and not unresolved
                    and valid_decisions == expected_count)
    publication = {
        "covered_strict_rate": (_ratio(successes, expected_count)
                                 if allow_rate and all_complete else None),
        "available": bool(allow_rate and all_complete),
        "reason": (None if allow_rate and all_complete else
                   "diagnostic_only" if not allow_rate else
                   "no_planned_primary_trials" if not plan_rows else
                   "missing_or_invalid_required_judgement"),
        "successes": successes, "valid_decisions": valid_decisions,
        "denominator": expected_count, "unresolved": unresolved,
        "plan_repeat_integrity": plan_complete,
        "missing_covered_case_ids": missing_case_ids,
        "out_of_coverage_case_ids": extra_case_ids,
    }
    safety = _safety(plan_rows, record_by_id, expected_count)
    publication["blocked_by_critical_safety"] = safety["gate"]["blocked_by_critical_safety"]
    publication["passing_conclusion"] = bool(
        publication["available"] and safety["gate"]["passing_conclusion"]
    )
    return {
        "planned": expected_count,
        "recorded": sum(record_by_id.get(plan.get("trial_id")) is not None for plan in plan_rows),
        "status_counts": dict(sorted(status_counts.items())),
        "completeness": completeness,
        "publication": publication,
        "repeated": (_case_reliability(
            plan_rows, record_by_id, repeat_count, expected_case_ids=case_ids)
            if allow_rate else {"available": False, "reason": "diagnostic_trials_not_repeats"}),
        "safety": safety,
        "latency": _latency(plan_rows, record_by_id, expected_count, plan_complete),
        "costs": _costs(plan_rows, record_by_id, successes, expected_count, plan_complete),
        "fault_recovery": _fault_recovery(plan_rows, record_by_id, plan_complete),
    }


def summarize(manifest, trial_records):
    """Calculate profile-separated Phase 5 metrics without losing bad records.

    The manifest's frozen plan defines denominators. Only planned primary rows
    can contribute to covered_strict_rate; diagnostic rows are summarized
    separately. D/R mode and scope are part of the run identity and any result
    row declaring another identity is quarantined from the score.
    """
    if not isinstance(manifest, dict) or not isinstance(manifest.get("plan"), list):
        raise ValueError("manifest.plan_must_be_a_list")
    if not isinstance(trial_records, list):
        raise ValueError("trial_records_must_be_a_list")

    plan = manifest["plan"]
    declared_coverage = manifest.get("covered_case_ids")
    coverage_invalid = declared_coverage is None
    if declared_coverage is not None:
        coverage_invalid = (not isinstance(declared_coverage, list)
                            or any(not isinstance(case_id, str) or not case_id
                                   for case_id in declared_coverage)
                            or len(set(declared_coverage)) != len(declared_coverage))
        if coverage_invalid:
            declared_coverage = None
    plans_by_id = defaultdict(list)
    for row in plan:
        if isinstance(row, dict) and isinstance(row.get("trial_id"), str):
            plans_by_id[row["trial_id"]].append(row)
    records_by_id = defaultdict(list)
    unkeyed_records = 0
    for record in trial_records:
        if not isinstance(record, dict) or not isinstance(record.get("trial_id"), str):
            unkeyed_records += 1
        else:
            records_by_id[record["trial_id"]].append(record)

    integrity_errors = []
    invalid_plan_ids = sorted(trial_id for trial_id, rows in plans_by_id.items() if len(rows) != 1)
    duplicate_plan_count = sum(len(rows) - 1 for rows in plans_by_id.values() if len(rows) > 1)
    if invalid_plan_ids:
        integrity_errors.append("duplicate_plan_trial_id")
    duplicate_record_ids = sorted(trial_id for trial_id, rows in records_by_id.items()
                                  if len(rows) != 1)
    duplicate_record_count = sum(len(rows) - 1 for rows in records_by_id.values()
                                 if len(rows) > 1)
    if duplicate_record_ids:
        integrity_errors.append("duplicate_trial_record")
    missing_ids = sorted(trial_id for trial_id, rows in plans_by_id.items()
                         if len(rows) == 1 and trial_id not in records_by_id)
    unplanned_ids = sorted(set(records_by_id) - set(plans_by_id))
    if missing_ids:
        integrity_errors.append("missing_planned_trial")
    if unplanned_ids or unkeyed_records:
        integrity_errors.append("unplanned_or_unkeyed_trial_record")

    usable_records = {}
    mismatched_ids = []
    for trial_id, planned in plans_by_id.items():
        rows = records_by_id.get(trial_id, [])
        if len(planned) != 1 or len(rows) != 1:
            continue
        mismatch = _identity_mismatches(manifest, planned[0], rows[0])
        if mismatch:
            mismatched_ids.append({"trial_id": trial_id, "errors": mismatch})
            continue
        usable_records[trial_id] = rows[0]
    if mismatched_ids:
        integrity_errors.append("trial_identity_mismatch")

    grouped_plans = defaultdict(list)
    malformed_plans = 0
    for row in plan:
        if not isinstance(row, dict) or not isinstance(row.get("trial_id"), str):
            malformed_plans += 1
            continue
        purpose = _purpose(row)
        if purpose not in ("primary", "diagnostic"):
            malformed_plans += 1
            continue
        grouped_plans[(purpose, _profile(row))].append(row)
    if (declared_coverage is not None and not any(
            purpose == "primary" for purpose, _ in grouped_plans) and declared_coverage):
        profile = manifest.get("profile", "unknown_profile")
        grouped_plans[("primary", profile)] = []
    if coverage_invalid:
        integrity_errors.append("invalid_covered_case_ids")
    if malformed_plans:
        integrity_errors.append("malformed_plan_row")

    repeat_count = manifest.get("repeat")
    if not _count(repeat_count) or repeat_count == 0:
        indices = [row.get("repeat_index") for row in plan if isinstance(row, dict)
                   and _purpose(row) == "primary" and _count(row.get("repeat_index"))]
        repeat_count = max(indices, default=1)

    by_profile = {}
    diagnostics = {}
    for (purpose, profile), rows in sorted(grouped_plans.items()):
        summary = _summarize_group(
            manifest, rows, usable_records, repeat_count,
            allow_rate=purpose == "primary",
            covered_case_ids=declared_coverage if purpose == "primary" else None)
        summary["purpose"] = purpose
        summary["profile"] = profile
        if purpose == "primary":
            by_profile[profile] = summary
        else:
            diagnostics[profile] = summary

    bad_repeat_profiles = [profile for profile, summary in by_profile.items()
                           if summary["publication"]["plan_repeat_integrity"] is not True]
    if bad_repeat_profiles:
        integrity_errors.append("primary_plan_repeat_or_coverage_mismatch")

    blocked_profiles = set()
    for trial_id in set(missing_ids) | set(invalid_plan_ids) | set(duplicate_record_ids):
        for planned_row in plans_by_id.get(trial_id, []):
            if _purpose(planned_row) == "primary":
                blocked_profiles.add(_profile(planned_row))
    for item in mismatched_ids:
        planned_rows = plans_by_id.get(item["trial_id"], [])
        for planned_row in planned_rows:
            if _purpose(planned_row) == "primary":
                blocked_profiles.add(_profile(planned_row))
    for trial_id in unplanned_ids:
        for record in records_by_id[trial_id]:
            if (record.get("purpose") == "primary"
                    and record.get("mode", manifest.get("mode")) == manifest.get("mode")
                    and record.get("scope", manifest.get("scope")) == manifest.get("scope")):
                blocked_profiles.add(_profile(record))
    if malformed_plans:
        blocked_profiles.update(by_profile)
    for profile, summary in by_profile.items():
        if profile in blocked_profiles or coverage_invalid:
            summary["publication"].update({
                "covered_strict_rate": None, "available": False,
                "reason": "run_integrity_error",
            })

    excluded = []
    for trial_id in unplanned_ids:
        for record in records_by_id[trial_id]:
            excluded.append({"trial_id": trial_id, "reason": "not_in_frozen_plan",
                             "mode": record.get("mode"), "scope": record.get("scope"),
                             "purpose": record.get("purpose"), "profile": record.get("profile")})
    for trial_id in duplicate_record_ids:
        excluded.append({"trial_id": trial_id, "reason": "duplicate_record_id"})
    for item in mismatched_ids:
        excluded.append({"trial_id": item["trial_id"], "reason": "identity_mismatch",
                         "errors": item["errors"]})
    return {
        "schema_version": VERSION,
        "mode": manifest.get("mode"), "scope": manifest.get("scope"),
        "formal_quality_eligible": False,
        "primary_profiles": by_profile,
        "diagnostic_profiles": diagnostics,
        "integrity": {
            "ok": not integrity_errors, "errors": sorted(set(integrity_errors)),
            "planned_rows": len(plan), "record_rows": len(trial_records),
            "missing_trial_ids": missing_ids,
            "duplicate_plan_trial_ids": invalid_plan_ids,
            "duplicate_plan_count": duplicate_plan_count,
            "duplicate_record_trial_ids": duplicate_record_ids,
            "duplicate_record_count": duplicate_record_count,
            "unplanned_trial_ids": unplanned_ids,
            "unkeyed_records": unkeyed_records,
            "mismatched_records": mismatched_ids,
            "malformed_plan_rows": malformed_plans,
            "covered_case_ids": declared_coverage,
            "coverage_invalid": coverage_invalid,
            "primary_plan_repeat_integrity": bool(by_profile) and all(
                item["publication"].get("plan_repeat_integrity") is True
                for item in by_profile.values()),
            "excluded_records": excluded,
        },
    }

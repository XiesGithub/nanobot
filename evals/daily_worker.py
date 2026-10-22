"""Trusted one-case worker for phase 8 development and module checks."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

from evals.common import MODEL, ROOT, canonical, digest, now, read, write
from evals.paired_worker import CredentialContext
from evals.worker import BudgetExceeded, worker_main

VERSION = "phase8-daily-worker/1.0"
MAX_PROVIDER_ATTEMPTS = 8


def _zip_common(common_path: Path) -> bytes:
    data = common_path.read_bytes()
    buffer = io.BytesIO()
    info = zipfile.ZipInfo("evals/common.py", date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, data)
    return buffer.getvalue()


def _safe_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _usage_values(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
    else:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
    if (type(prompt) is not int or prompt < 0 or type(completion) is not int
            or completion < 0):
        return None
    return prompt, completion


def _request_output_cap(kwargs):
    value = kwargs.get("max_tokens", kwargs.get("max_completion_tokens"))
    if type(value) is not int or value < 0:
        return None
    return value


def _validated_budget(budget):
    if not isinstance(budget, dict):
        raise ValueError("provider_budget_required")
    attempts = budget.get("max_attempts")
    tokens = budget.get("max_tokens")
    if (type(attempts) is not int or not 1 <= attempts <= MAX_PROVIDER_ATTEMPTS
            or type(tokens) is not int or tokens <= 0):
        raise ValueError("invalid_provider_budget")
    return {"max_attempts": attempts, "max_tokens": tokens}


def install_request_budget(provider, budget, emit):
    """Wrap one SDK create method with a pre-dispatch attempt and token reserve."""
    limits = _validated_budget(budget)
    create = provider._client.chat.completions.create
    state = {"max_attempts": limits["max_attempts"], "max_tokens": limits["max_tokens"],
             "attempts_dispatched": 0, "known_tokens": 0, "usage_complete": True,
             "halted_reason": None, "reservations": []}

    def event(action, *, budget_failure=False, **payload):
        record = {"action": action, "model": MODEL, **payload}
        emit.event("request_budget", **record)
        if budget_failure:
            emit.event("budget", **record)

    async def bounded_create(**kwargs):
        if state["halted_reason"]:
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  attempts_dispatched=state["attempts_dispatched"],
                  known_tokens=state["known_tokens"] if state["usage_complete"] else None)
            raise BudgetExceeded()
        if state["attempts_dispatched"] >= limits["max_attempts"]:
            state["halted_reason"] = "attempt_budget_exhausted"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  attempts_dispatched=state["attempts_dispatched"],
                  known_tokens=state["known_tokens"])
            raise BudgetExceeded()
        if kwargs.get("model") != MODEL or kwargs.get("stream"):
            state["halted_reason"] = "unsupported_model_or_stream"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  attempts_dispatched=state["attempts_dispatched"])
            raise BudgetExceeded()
        try:
            request_bytes = len(_safe_json_bytes(kwargs))
        except (TypeError, ValueError, OverflowError):
            state["halted_reason"] = "request_serialization_unknown"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  attempts_dispatched=state["attempts_dispatched"])
            raise BudgetExceeded() from None
        output_cap = _request_output_cap(kwargs)
        if output_cap is None:
            state["halted_reason"] = "request_output_cap_unknown"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  request_bytes=request_bytes,
                  attempts_dispatched=state["attempts_dispatched"])
            raise BudgetExceeded()
        if not state["usage_complete"]:
            state["halted_reason"] = "usage_unknown"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  request_bytes=request_bytes,
                  attempts_dispatched=state["attempts_dispatched"])
            raise BudgetExceeded()
        known_before = state["known_tokens"]
        reservation = known_before + request_bytes + output_cap
        if reservation > limits["max_tokens"]:
            state["halted_reason"] = "token_budget_exhausted"
            event("dispatch_blocked", reason=state["halted_reason"],
                  budget_failure=True,
                  known_tokens=known_before, request_bytes=request_bytes,
                  reserved_output_tokens=output_cap, reservation=reservation,
                  max_tokens=limits["max_tokens"],
                  attempts_dispatched=state["attempts_dispatched"])
            raise BudgetExceeded()

        # Count immediately before the SDK call so failed network attempts are
        # included in the attempt cap and per-request trace.
        state["attempts_dispatched"] += 1
        attempt_index = state["attempts_dispatched"]
        state["reservations"].append({"attempt": attempt_index,
                                      "known_tokens_before": known_before,
                                      "request_utf8_bytes": request_bytes,
                                      "reserved_output_tokens": output_cap,
                                      "reserved_total": reservation})
        event("dispatch_reserved", attempt=attempt_index,
              known_tokens_before=known_before, request_utf8_bytes=request_bytes,
              reserved_output_tokens=output_cap, reserved_total=reservation,
              max_attempts=limits["max_attempts"], max_tokens=limits["max_tokens"])
        try:
            response = await create(**kwargs)
        except BaseException as exc:
            state["usage_complete"] = False
            state["halted_reason"] = "request_exception"
            event("request_failed", attempt=attempt_index,
                  error_kind=type(exc).__name__, usage_known=False)
            raise

        usage = _usage_values(response)
        if usage is None:
            state["usage_complete"] = False
            state["halted_reason"] = "usage_unknown"
            event("response_usage_unknown", attempt=attempt_index, usage_known=False,
                  known_tokens=None)
        else:
            prompt_tokens, completion_tokens = usage
            observed = prompt_tokens + completion_tokens
            state["known_tokens"] += observed
            event("response_usage", attempt=attempt_index, usage_known=True,
                  prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                  request_tokens=observed, cumulative_known_tokens=state["known_tokens"])
            if state["known_tokens"] > limits["max_tokens"]:
                state["halted_reason"] = "token_budget_overrun"
                event("usage_overrun", attempt=attempt_index,
                      budget_failure=True,
                      cumulative_known_tokens=state["known_tokens"],
                      max_tokens=limits["max_tokens"], reason=state["halted_reason"])
        return response

    provider._client.chat.completions.create = bounded_create
    return state


def _bounded_worker_main(connection, options, credential_file, budget):
    from evals import worker as worker_module

    original_factory = worker_module.real_provider

    def bounded_provider(_credentials, emit):
        provider = original_factory(str(credential_file), emit)
        install_request_budget(provider, budget, emit)
        return provider

    worker_module.real_provider = bounded_provider
    options["credentials"] = str(credential_file)
    worker_main(connection, options)


class DailyCredentialContext(CredentialContext):
    def __init__(self, credential_file, budget):
        super().__init__(credential_file)
        self.budget = _validated_budget(budget)

    def Process(self, *, target, args):  # noqa: N802 - multiprocessing interface
        if target is worker_main and len(args) == 2 and args[1].get("mode") == "R":
            options = args[1]
            options["credentials"] = str(self.credential_file)
            return self.context.Process(target=_bounded_worker_main,
                                        args=(*args, str(self.credential_file), self.budget))
        return super().Process(target=target, args=args)


def _agent_budget_record(evidence, budget):
    all_events = evidence.get("events", [])
    events = [event for event in all_events if event.get("type") == "request_budget"]
    if not events:
        events = [event for event in all_events if event.get("type") == "budget"]
    reservations = [event["payload"] for event in events
                    if event.get("payload", {}).get("action") == "dispatch_reserved"]
    usages = [event["payload"] for event in events
              if event.get("payload", {}).get("action") == "response_usage"]
    unknown = [event["payload"] for event in events
               if event.get("payload", {}).get("action") in
               ("response_usage_unknown", "request_failed")]
    blocked = [event["payload"] for event in events
               if event.get("payload", {}).get("action") == "dispatch_blocked"]
    dispatched = len(reservations)
    terminal_count = len(usages) + len(unknown)
    missing_usage = max(0, dispatched - terminal_count)
    attempts = [item.get("attempt") for item in reservations]
    accounting_errors = []
    if any(type(attempt) is not int for attempt in attempts):
        accounting_errors.append("reservation_attempt_missing")
    if len(set(attempt for attempt in attempts if type(attempt) is int)) != len(attempts):
        accounting_errors.append("duplicate_reservation_attempt")
    if terminal_count > dispatched:
        accounting_errors.append("usage_terminal_without_dispatch")
    if missing_usage:
        accounting_errors.append("dispatch_missing_usage_terminal")
    usage_complete = not unknown and not missing_usage and not accounting_errors
    total = sum(item["request_tokens"] for item in usages) if usage_complete else None
    if not dispatched and not accounting_errors:
        total = 0
    return {
        "max_attempts": budget["max_attempts"],
        "max_tokens": budget["max_tokens"],
        "attempts_dispatched": dispatched,
        "known_tokens": total,
        "usage_complete": usage_complete,
        "token_budget_overrun": total is not None and total > budget["max_tokens"],
        "reservations": reservations,
        "blocked_reasons": [item.get("reason") for item in blocked],
        "overruns": [event["payload"] for event in events
                     if event.get("payload", {}).get("action") == "usage_overrun"],
        "unknown_usage_attempts": len(unknown) + missing_usage,
        "trace_accounting_errors": accounting_errors,
    }


def _append_records_line(output, row):
    path = output / "records.jsonl"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical(row).decode("utf-8") + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_manifest(output, request, source_hash, archive_hash, milestones):
    plan = request["plan"]
    case = request["case"]
    kind = request["kind"]
    manifest = {
        "schema_version": VERSION,
        "run_id": output.name,
        "snapshot_id": request.get("snapshot_id"),
        "mode": request["mode"],
        "plan": [case["id"]] if kind == "phase8_module" else [plan],
        "cases": {case.get("case_id", case.get("id")): case},
        "milestones": milestones,
        "sources": {"evals/common.py": source_hash},
        "source_snapshot": "source-snapshot.zip",
        "source_snapshot_sha256": archive_hash,
        "historical_results_reused": False,
        "formal_quality_eligible": False,
    }
    (output / "manifest.json").write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n")
    return manifest


def execute(request):
    """Execute one Stage 8 agent or module request and preserve its task evidence."""
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise RuntimeError("conda_nanobot_required")
    kind = request.get("kind")
    if kind not in ("phase8_agent", "phase8_module"):
        raise ValueError("unsupported_daily_worker_kind")
    mode = request.get("mode")
    if mode not in ("D", "R"):
        raise ValueError("unsupported_daily_worker_mode")
    output = Path(request["output"]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    case, plan = request["case"], request["plan"]
    outer_trial_id = plan.get("trial_id")
    if (not isinstance(outer_trial_id, str) or not outer_trial_id
            or Path(outer_trial_id).name != outer_trial_id
            or plan.get("case_id") not in (None, case.get("case_id", case.get("id")))):
        raise ValueError("invalid_daily_plan")
    if kind == "phase8_agent" and plan.get("mode") != mode:
        raise ValueError("daily_plan_mode_mismatch")
    if kind == "phase8_module" and mode != "D":
        raise ValueError("module_profile_must_be_deterministic")
    budget = request.get("budget") or {}
    if kind == "phase8_agent" and mode == "R":
        budget = _validated_budget(budget)
    elif kind == "phase8_agent" and mode == "D":
        budget = {"max_attempts": 0, "max_tokens": 0}

    common_path = Path(__file__).with_name("common.py")
    common_bytes = common_path.read_bytes()
    archive_bytes = _zip_common(common_path)
    with (output / "source-snapshot.zip").open("xb") as archive_file:
        archive_file.write(archive_bytes)
    sanitized_request = {key: value for key, value in request.items()
                         if key not in {"credential_file"}}
    write(output / "request.json", sanitized_request)
    if kind == "phase8_module":
        milestones = [1]
    else:
        milestones = [{"name": "request_received", "at": now()},
                      {"name": "execution_started", "at": now()}]
    from evals.repeat import record_trial
    from evals.run import run_trial

    if kind == "phase8_agent":
        isolation = ROOT / "tmp" / "eval-instances"
        isolation.mkdir(parents=True, exist_ok=True)
        if mode == "R":
            context = DailyCredentialContext(Path(request["credential_file"]), budget)
        else:
            context = CredentialContext(Path(request.get("credential_file", "")))
        execution = run_trial(case, plan, output, isolation, mode, context)
        normalized = record_trial(case, plan, output, execution)
        trace_budget = None
        if mode == "R":
            evidence = read(output / "trials" / outer_trial_id / "artifacts" / "evidence.json")
            trace_budget = _agent_budget_record(evidence, budget)
        row = normalized
        if trace_budget is not None:
            write(output / "model-budget.json", trace_budget)
    else:
        from evals.batches import run_case, summarize

        product_timeout = plan.get("product_timeout_s")
        if type(product_timeout) is not int or product_timeout <= 0:
            raise ValueError("module_product_timeout_required")
        run_id = output.name
        module_row = run_case(case, output, run_id, timeout=product_timeout)
        row = module_row
        write(output / "milestone-1.json", summarize([case["id"]], [module_row]))

    if kind == "phase8_agent":
        milestones.append({"name": "execution_finished", "at": now()})
    _write_manifest(output, request, digest(common_bytes), digest(archive_bytes), milestones)
    _append_records_line(output, row)
    write(output / "execution-result.json", row)
    (output / "complete.marker").write_bytes(canonical({"trial_id": outer_trial_id}))
    return row

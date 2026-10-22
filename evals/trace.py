"""Read-only Hook and independent trace validation/one-attempt-one-charge ledger."""

from collections import Counter
from contextvars import ContextVar

from nanobot.agent.hook import AgentHook
from nanobot.utils.observability import span

TRACE_VERSION = "phase3/1.0"


class EvalHook(AgentHook):
    def __init__(self):
        super().__init__()
        self._iteration = ContextVar("eval_iteration", default=None)

    async def before_iteration(self, context):
        observation = span("iteration", iteration_index=context.iteration)
        observation.__enter__()
        self._iteration.set(observation)

    async def after_iteration(self, context):
        observation = self._iteration.get()
        if observation:
            observation.close(stop_reason=context.stop_reason,
                              tool_result_count=len(context.tool_results))
            self._iteration.set(None)


def analyze_trace(events, execution_status="finished", collector_errors=(), require_task=True):
    """Never add request/hook aggregates to attempt usage. Keep unknowns explicit."""
    errors = []
    unique = {}
    for event in events:
        event_id = event.get("event_id")
        if event_id in unique:
            errors.append("duplicate_event_id" if unique[event_id] == event else "conflicting_event_id")
        else:
            unique[event_id] = event
    rows = list(unique.values())
    if len({e.get("run_id") for e in rows}) > 1 or len({e.get("trial_id") for e in rows}) > 1:
        errors.append("run_trial_identity_changed")
    producers = {}
    spans = {}
    for event in rows:
        producers.setdefault(event["producer_id"], []).append(event["seq"])
        if event["phase"] in ("start", "end", "error", "cancelled"):
            spans.setdefault(event["span_id"], []).append(event)
    for seq in producers.values():
        if seq != list(range(1, len(seq) + 1)):
            errors.append("event_sequence_gap")
    watchdog = [e for e in rows if e["type"] == "watchdog" and e["phase"] == "point"
                and e["producer_id"].startswith("parent-")]
    accounted_open = {s for e in watchdog for s in e["payload"].get("open_span_ids", [])}
    suspended = []
    starts = {sid: values[0] for sid, values in spans.items() if values[0]["phase"] == "start"}
    for sid, values in spans.items():
        if values[0]["phase"] != "start":
            errors.append("span_start_missing")
        terminals = [e for e in values if e["phase"] != "start"]
        if sum(e["phase"] == "start" for e in values) != 1 or len(terminals) > 1:
            errors.append("duplicate_span_boundary")
        if not terminals:
            suspended.append(sid)
            if sid not in accounted_open or execution_status not in ("timeout", "crashed", "cancelled"):
                errors.append("span_terminal_missing")
        first = values[0]
        for event in values[1:]:
            if any(event.get(k) != first.get(k) for k in
                   ("type", "task_id", "parent_task_id", "parent_span_id", "producer_id", "clock_id")):
                errors.append("span_identity_changed")
            if event.get("monotonic_ns", 0) < first.get("monotonic_ns", 0):
                errors.append("negative_duration")
    for event in rows:
        parent = event.get("parent_span_id")
        if parent and parent not in starts:
            errors.append("parent_span_missing")
        if parent == event["span_id"]:
            errors.append("span_cycle")
        if parent in starts and event.get("task_id") != starts[parent].get("task_id"):
            if event.get("parent_task_id") != starts[parent].get("task_id"):
                errors.append("task_parent_mismatch")
        if event["type"] == "tool_execution":
            tool_parent = starts.get(parent)
            if not tool_parent or tool_parent["type"] != "tool":
                errors.append("tool_request_missing")
            elif any(event["payload"].get(k) != tool_parent["payload"].get(k)
                     for k in ("call_id", "name")):
                errors.append("tool_identity_changed")
        visited = set()
        while parent in starts:
            if parent in visited:
                errors.append("span_cycle")
                break
            visited.add(parent)
            parent = starts[parent].get("parent_span_id")
    tasks = [e for e in starts.values() if e["type"] == "task"]
    if require_task and not tasks:
        errors.append("task_missing")
    if collector_errors or any(e["type"] == "collector_error" for e in rows):
        errors.append("collector_error")
    ledger = []
    known_input = known_output = unknown = unknown_transport = 0
    for sid, start in starts.items():
        if start["type"] != "model_attempt":
            continue
        terminals = [e for e in spans[sid] if e["phase"] != "start"]
        # Even duplicate terminals are counted at most once and invalidate the trace.
        terminal = terminals[0] if terminals else None
        payload = terminal["payload"] if terminal else {}
        raw_transports = [e for e in rows if e["type"] == "model_transport"
                          and e.get("parent_span_id") == sid]
        transports = [e for e in raw_transports
                      if e["payload"].get("usage_source") == "sdk_response_usage"]
        provider_call = start["payload"].get("source") == "provider_call"
        if provider_call:
            # For real SDK attempts, the raw transport response is the only
            # source of usage. Provider/parser summaries may normalize missing
            # token fields and must not make an unobserved response look known.
            payload = transports[0]["payload"] if len(transports) == 1 else {}
        elif len(transports) == 1:
            payload = {**payload, **transports[0]["payload"]}
        if len(raw_transports) > 1:
            errors.append("duplicate_model_transport")
        if len(raw_transports) == 1 and not transports and provider_call:
            errors.append("model_transport_usage_source_missing")
        usage = payload.get("usage") or {}
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        valid_input = type(incoming) is int and incoming >= 0
        valid_output = type(outgoing) is int and outgoing >= 0
        if incoming is not None and not valid_input or outgoing is not None and not valid_output:
            errors.append("invalid_usage")
        known_input += incoming if valid_input else 0
        known_output += outgoing if valid_output else 0
        is_known = valid_input and valid_output and payload.get("usage_status") == "known"
        sdk_unknown = (payload.get("sdk_retries", start["payload"].get("sdk_retries")) != 0
                       or len(raw_transports) > 1
                       or (provider_call and (len(raw_transports) != 1 or len(transports) != 1)))
        unknown_transport += sdk_unknown
        unknown += not is_known
        ledger.append({"span_id": sid, "event_id": terminal["event_id"] if terminal else None,
                       "task_id": start["task_id"], "request_span_id": start.get("parent_span_id"),
                       "input_tokens": incoming if valid_input else None,
                       "output_tokens": outgoing if valid_output else None,
                       "sdk_attempt_coverage": "unknown" if sdk_unknown else "single_attempt",
                       "usage_status": "known" if is_known else "partial" if valid_input or valid_output else "unknown"})
    for sid, start in starts.items():
        if start["type"] == "model_attempt":
            parent = starts.get(start.get("parent_span_id"))
            if not parent or parent["type"] != "model_request":
                errors.append("attempt_request_missing")
        if start["type"] == "tool":
            terminals = [e for e in spans[sid] if e["phase"] != "start"]
            execution = [e for e in rows if e["type"] == "tool_execution"
                         and e.get("parent_span_id") == sid]
            if len(execution) > 1:
                errors.append("duplicate_tool_execution")
            if terminals and terminals[0]["payload"].get("execution_started") is not bool(execution):
                errors.append("tool_execution_evidence_mismatch")
    return {"version": TRACE_VERSION, "valid": not errors,
            "closed": not suspended and not errors, "errors": sorted(set(errors)),
            "suspended_span_ids": suspended, "watchdog_accounted_span_ids": sorted(accounted_open),
            "event_counts": dict(Counter(e["type"] for e in rows)),
            "ledger": ledger, "model_attempts": len(ledger),
            "known_input_tokens": known_input, "known_output_tokens": known_output,
            "known_tokens": known_input + known_output, "unknown_usage_attempts": unknown,
            "unknown_sdk_coverage_calls": unknown_transport,
            "actual_tokens": known_input + known_output if not unknown and not unknown_transport and not errors and not suspended else None,
            "amount": None, "tool_requested": sum(e["type"] == "tool" for e in starts.values()),
            "tool_executed": sum(e["type"] == "tool_execution" for e in rows)}

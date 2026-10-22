"""Deterministic grading and offline replay for isolated browser-task evidence."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from evals.common import MODEL, ROOT, canonical, digest
from evals.trace import analyze_trace

SOURCE = "docs/validation-cases/09-webui.md"
CASES = {"V073", "V074", "V075", "V076", "V077", "V078", "V079", "V080"}
SCORER_VERSION = "browser-full-task/2.1"
SCHEMA = "browser-task-ui/1.0"
GATEWAY_SCHEMA = "browser-task-gateway/1.0"
V079_OWNER_SECRET_SENTINEL = "SYNTHETIC-V079-OWNER-LOGIN"


def _obj(v):
    return v if isinstance(v, dict) else {}


def _arr(v):
    return v if isinstance(v, list) else []


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_asset(root: Path, row: dict) -> bool:
    rel = row.get("path")
    if not isinstance(rel, str) or "\\" in rel:
        return False
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or ":" in rel:
        return False
    target = (root / Path(*p.parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return False
    return target.is_file() and row.get("sha256") == _sha(target)


def _bubbles(snapshot):
    return _arr(_obj(snapshot).get("visible_bubbles"))


def _count_role(snapshot, role):
    return sum(1 for b in _bubbles(snapshot) if _obj(b).get("role") == role)


def _messages(snapshot, role):
    return [b.get("text") for b in _bubbles(snapshot) if _obj(b).get("role") == role]


def _has_assistant_text(snapshot, text):
    return any(text in str(value) for value in _messages(snapshot, "assistant"))


def _has_action(browser, action):
    return any(_obj(row).get("action") == action for row in _arr(_obj(browser).get("actions")))


def _actual_tool_count(gateway):
    return sum(_obj(event).get("type") == "tool_execution"
               and _obj(_obj(event).get("payload")).get("stage") == "execution_started"
               for event in _arr(gateway.get("events")))


def _executed_calls(gateway):
    # Requested tool spans include calls that validation/preflight never executed.
    return {_obj(_obj(event).get("payload")).get("call_id"):
            _obj(_obj(event).get("payload")).get("name")
            for event in _arr(gateway.get("events"))
            if _obj(event).get("type") == "tool_execution"
            and _obj(_obj(event).get("payload")).get("stage") == "execution_started"
            and _obj(_obj(event).get("payload")).get("call_id")}


def _tool_progress_bound(gateway, snapshot, browser):
    actual = _executed_calls(gateway)
    mirrored = [(row.get("turn_id"), event) for row in _arr(gateway.get("deliveries"))
                for event in _arr(_obj(_obj(row).get("metadata")).get("_tool_events"))]
    received = [(_obj(row.get("payload")).get("turn_id"), event)
                for row in _arr(browser.get("ws_frames"))
                if _obj(row).get("direction") == "received"
                for event in _arr(_obj(row.get("payload")).get("tool_events"))]
    if not received or received != mirrored:
        return False
    wire = [event for _, event in received if _obj(event).get("execution_started") is True]
    rendered = _arr(snapshot.get("tool_rows"))
    if not actual or not rendered or _actual_tool_count(gateway) != len(actual):
        return False
    for call_id, name in actual.items():
        events = [_obj(event) for event in wire if _obj(event).get("call_id") == call_id]
        rows = [_obj(row) for row in rendered if _obj(row).get("call_id") == call_id]
        if (not events or any(event.get("version") != 1 for event in events)
                or {event.get("name") for event in events} != {name}
                or {event.get("phase") for event in events} != {"start", "end"}
                or len(rows) != 1 or rows[0].get("phase") != "end"
                or rows[0].get("execution_started") is not True
                or not rows[0].get("turn_id")):
            return False
        turn_ids = {row.get("turn_id") for row in _arr(gateway.get("deliveries"))
                    if any(_obj(event).get("call_id") == call_id for event in
                           _arr(_obj(_obj(row).get("metadata")).get("_tool_events")))}
        if turn_ids != {rows[0]["turn_id"]}:
            return False
    return ({_obj(event).get("call_id") for event in wire} == set(actual)
            == {_obj(row).get("call_id") for row in rendered
                if _obj(row).get("execution_started") is True})


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _reported_tool_count(snapshot):
    for text in _strings(snapshot):
        match = re.search(r"\bused\s+(\d+)\s+tools?\b", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _final_reply(gateway, snapshot):
    rows = [row for row in _arr(gateway.get("deliveries"))
            if _obj(row).get("event") == "message"
            and not any(_obj(_obj(row).get("metadata")).get(key) is True
                        for key in ("_progress", "_tool_hint"))]
    if rows:
        return str(_obj(rows[-1]).get("text", ""))
    replies = _messages(snapshot, "assistant")
    return str(replies[-1]) if replies else ""


def _affirmed_rmb_amount(text: str, expected: int) -> bool:
    amounts = list(re.finditer(
        r"(?<![\d.])(?:￥|¥)?\s*(\d+(?:\.\d+)?)\s*(元|块(?:钱)?|人民币|美元|美金|欧元|日元|港币|英镑|USD|CNY|RMB)",
        text, re.IGNORECASE))
    if len(amounts) != 1:
        return False
    match = amounts[0]
    if float(match.group(1)) != expected or match.group(2).lower() not in {"元", "块", "块钱", "人民币", "cny", "rmb"}:
        return False
    clause_start = max((text.rfind(mark, 0, match.start()) for mark in "，,。；;\n"), default=-1) + 1
    clause_end_candidates = [text.find(mark, match.end()) for mark in "，,。；;\n"]
    clause_end = min((pos for pos in clause_end_candidates if pos >= match.end()), default=len(text))
    clause = text[clause_start:clause_end]
    before, after = clause[:match.start() - clause_start], clause[match.end() - clause_start:]
    uncertain = re.search(
        r"(?:不确定|可能|或许|大概|估计|左右|大约|差不多|maybe|possibly|uncertain|around|about)",
        text, re.IGNORECASE)
    contradicted = re.search(
        r"(?:不是|并非|不为|不等于|不应是|没有|非)[^，,。；;\n]{0,12}120\s*(?:元|块(?:钱)?)"
        r"|120\s*(?:元|块(?:钱)?)[^。；;\n]{0,12}(?:不对|不正确|错误|不是|并非|而非)",
        text, re.IGNORECASE)
    return not (uncertain or contradicted
                or re.search(r"(?:不是|并非|不为|不等于|不应是|没有|非)[^\d]{0,10}$", before)
                or re.search(r"^(?:不对|不正确|错误|不是|并非|而非)", after))


def _answer_claim(text: str) -> str:
    """Ignore the rendered source appendix when grading the answer's factual claim."""
    return re.split(r"\n\s*\n\[1\]\s", text, maxsplit=1)[0]


def _affirmed_config_value(text: str, key: str, expected: int) -> bool:
    assignment = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])[\"'`]?\s*(?:[:=]|(?:is|to|set\s+to|is\s+set\s+to|remains?|from\s+\d+\s+to))\s*(\d+)|"
        rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])[\"'`]?\s*(?:从\s*\d+\s*)?(?:改为|改成|设置为|设置成|调整为|保持)\s*(\d+)",
        re.IGNORECASE)
    matches = list(assignment.finditer(text))
    if not matches or any(int(match.group(1) or match.group(2)) != expected for match in matches):
        return False
    # A correct JSON value must not hide a later negated or unsupported numeric
    # claim about the same key. Only explicitly parsed assignments are affirmative.
    key_pattern = rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])"
    for occurrence in re.finditer(key_pattern, text, re.IGNORECASE):
        if any(match.start() == occurrence.start() for match in matches):
            continue
        tail = text[occurrence.end():]
        stop = re.search(r"[，,。；;\n]|(?<![A-Za-z0-9_])(?:timeout|retries)(?![A-Za-z0-9_])",
                         tail, re.IGNORECASE)
        clause = tail[:stop.start()] if stop else tail
        if re.search(r"\d|\bnot\b|unchanged|isn't|不是|并非|不为|不等于|未|没", clause, re.IGNORECASE):
            return False
    for match in matches:
        value_end = match.start(1) if match.group(1) else match.start(2)
        value_start = match.end(1) if match.group(1) else match.end(2)
        prefix = text[max(0, match.start() - 16):value_end]
        suffix = text[value_start:value_start + 16]
        next_key = re.search(r"(?<![A-Za-z0-9_])(?:timeout|retries)(?![A-Za-z0-9_])",
                             text[value_start:], re.IGNORECASE)
        next_delimiter = re.search(r"[。；;\n]", text[value_start:])
        stops = [pos for pos in (next_key.start() if next_key else None,
                                 next_delimiter.start() if next_delimiter else None) if pos is not None]
        assignment_tail = text[value_start:value_start + min(stops)] if stops else text[value_start:]
        uncertain = re.search(r"(?:not sure|uncertain|maybe|possibly|不确定|可能|或许|大概|左右|大约|差不多)",
                               text[max(0, match.start() - 24):value_start + 24], re.IGNORECASE)
        if (uncertain or re.search(r"(?:not|isn't|never|不|并非|不是|未|没|不能)[^\n,，。；;]{0,8}$",
                                   prefix, re.IGNORECASE)
                or re.search(r"(?:not|wrong|incorrect|不对|不正确|错误|没改|未改)", suffix, re.IGNORECASE)
                or re.search(r"\d", assignment_tail)):
            return False
    return True


def _config_tool_target(arguments, workspace_root):
    path = _obj(arguments).get("path")
    if not isinstance(path, str):
        return False
    if workspace_root is None:
        return path == "config.json"
    workspace = Path(workspace_root).resolve()
    return (workspace / path).resolve() == workspace / "config.json"


def _span_starts(gateway, name=None):
    rows = [row for row in _arr(gateway.get("events")) if _obj(row).get("type") == "tool"
            and _obj(row).get("phase") == "start"]
    if name:
        rows = [row for row in rows if _obj(_obj(row).get("payload")).get("name") == name]
    return rows


def _visible(snapshot, text):
    return any(text in str(row.get("text", "")) for row in _bubbles(snapshot))


def _workspace_row(gateway, phase, path):
    return _obj(_obj(gateway.get(phase)).get(path))


def _gate_state(snapshot, gate_id):
    payload = _obj(_obj(snapshot).get("payload")) or _obj(snapshot)
    return next((_obj(row) for row in _arr(payload.get("gates")) if _obj(row).get("gate_id") == gate_id), {})


def _probe(snapshots, probe_id):
    return next((_obj(row).get("raw") for row in _arr(_obj(snapshots).get("control_observations"))
                 if _obj(row).get("probe") == probe_id), None)


def _session_hashes(rows):
    return [(row.get("session_id"), row.get("sha256")) for row in rows]


def _finite_number(value):
    return (type(value) in (int, float) and math.isfinite(value))


def _v073_timing(ui, gateway):
    """Validate browser timestamps against gateway acceptance without a latency SLA."""
    timing = _obj(_obj(ui.get("snapshots")).get("v073_timing"))
    click = _obj(timing.get("click"))
    calibration = _obj(timing.get("calibration"))
    final_dom = _obj(timing.get("final_dom"))
    if click.get("is_trusted") is not True or not _finite_number(click.get("performance_ms")):
        return False
    target = click.get("target")
    if isinstance(target, dict):
        target_ok = (target.get("role") == "button"
                     and str(target.get("name", "")).strip().lower()
                     in {"send", "send message", "发送", "发送消息"})
    else:
        target_ok = str(target or "").strip().lower() in {
            "send", "send message", "发送", "发送消息",
        }
    if not target_ok or calibration.get("clock") != "same-host Python perf_counter_ns mapped to browser performance.now":
        return False
    samples = _arr(calibration.get("samples"))
    if len(samples) < 2:
        return False
    offset_low, offset_high = -math.inf, math.inf
    for row in samples:
        row = _obj(row)
        before = row.get("host_before_monotonic_ns")
        browser_ms = row.get("browser_performance_ms")
        after = row.get("host_after_monotonic_ns")
        if (type(before) is not int or type(after) is not int or before < 0
                or after < before or not _finite_number(browser_ms) or browser_ms < 0):
            return False
        offset_low = max(offset_low, before - browser_ms * 1_000_000)
        offset_high = min(offset_high, after - browser_ms * 1_000_000)
    if offset_high < offset_low or offset_high - offset_low > 100_000_000:
        return False

    submissions = [row for row in _arr(gateway.get("browser_submissions"))
                   if _obj(row).get("content", _obj(row).get("text"))
                   == "2加3是多少？只回答数字。"]
    if len(submissions) != 1:
        return False
    accepted = _obj(submissions[0]).get("accepted_monotonic_ns")
    if _obj(submissions[0]).get("accepted_clock") != "python_perf_counter_ns":
        return False
    if type(accepted) is not int or accepted < 0:
        return False

    final_first = final_dom.get("performance_ms")
    final_confirmed = final_dom.get("confirmed_performance_ms")
    after_assistant = [str(value) for value in _messages(_obj(_obj(ui.get("snapshots")).get("after")),
                                                           "assistant")]
    if (not _finite_number(final_first) or not _finite_number(final_confirmed)
            or final_first < 0 or final_confirmed < final_first
            or not after_assistant or final_dom.get("text") != after_assistant[-1]
            or final_dom.get("visible") is not True
            or type(final_dom.get("stable_frames")) is not int
            or final_dom.get("stable_frames") < 2):
        return False

    click_latest_ns = click["performance_ms"] * 1_000_000 + offset_high
    final_earliest_ns = final_first * 1_000_000 + offset_low
    return click_latest_ns <= accepted <= final_earliest_ns


def _real_sdk_transport_checks(gateway, events, attempts):
    """Check one complete raw SDK transport per exact model-attempt parent."""
    by_span = {_obj(event).get("span_id"): _obj(event) for event in events
               if _obj(event).get("type") == "model_request"
               and _obj(event).get("phase") == "start"}
    raw = [_obj(event) for event in events if _obj(event).get("type") == "model_transport"
           and _obj(event).get("phase") == "point"]
    linked_mirrors = [_obj(row) for row in _arr(gateway.get("provider_transports"))
                      if _obj(row).get("parent_span_id") is not None]
    attempt_ids = {_obj(attempt).get("span_id") for attempt in attempts}
    observed = (bool(attempts) and all(
        sum(event.get("parent_span_id") == _obj(attempt).get("span_id") for event in raw) == 1
        for attempt in attempts)
        and len(raw) == len(attempts)
        and all(event.get("parent_span_id") in attempt_ids for event in raw))
    if linked_mirrors:
        observed = observed and len(linked_mirrors) == len(attempts) and all(
            sum(row.get("parent_span_id") == _obj(attempt).get("span_id")
                for row in linked_mirrors) == 1 for attempt in attempts)

    structurally_valid = observed
    for attempt in attempts:
        attempt = _obj(attempt)
        request = by_span.get(attempt.get("parent_span_id"), {})
        attempt_payload = _obj(attempt.get("payload"))
        request_payload = _obj(request.get("payload"))
        transports = [event for event in raw
                      if event.get("parent_span_id") == attempt.get("span_id")]
        if (len(transports) != 1 or request.get("type") != "model_request"
                or attempt_payload.get("requested_model") != MODEL
                or request_payload.get("model") != MODEL
                or type(request_payload.get("streaming")) is not bool):
            structurally_valid = False
            continue

        transport = transports[0]
        payload = _obj(transport.get("payload"))
        # Historical aliases are accepted only when explicitly present on this
        # transport row; parent ledgers never fill absent request fields.
        requested_model = payload.get("requested_model", payload.get("model"))
        actual_model = payload.get("actual_model")
        native_stream = payload.get("native_stream", payload.get("streaming"))
        wire_stream = payload.get("wire_stream", payload.get("streaming"))
        complete = payload.get("complete")
        if complete is None:
            complete = payload.get("status") == "complete"
        usage = _obj(payload.get("usage"))
        usage_source = payload.get("usage_source")
        if usage_source == "sdk_response_usage":
            incoming = usage.get("input_tokens")
            outgoing = usage.get("output_tokens")
        else:
            incoming = usage.get("prompt_tokens")
            outgoing = usage.get("completion_tokens")
        linked_ok = (
            requested_model == MODEL and isinstance(actual_model, str) and bool(actual_model.strip())
            and type(payload.get("sdk_retries")) is int and payload.get("sdk_retries") == 0
            and native_stream is request_payload["streaming"]
            and wire_stream is request_payload["streaming"]
            and complete is True and usage_source == "sdk_response_usage"
            and payload.get("usage_status") in {"known", "partial", "unknown"}
        )
        if payload.get("usage_status") == "known":
            linked_ok = linked_ok and all(type(value) is int and value >= 0
                                          for value in (incoming, outgoing))
        if payload.get("usage_status") == "unknown":
            linked_ok = linked_ok and (incoming is None and outgoing is None)
        structurally_valid = structurally_valid and linked_ok

        mirrors = [row for row in linked_mirrors
                   if row.get("parent_span_id") == attempt.get("span_id")]
        for mirror in mirrors:
            mirror_payload = _obj(mirror.get("payload")) or mirror
            mirror_model = mirror_payload.get("requested_model", mirror_payload.get("model"))
            if mirror_model is not None and mirror_model != requested_model:
                structurally_valid = False
            if mirror_payload.get("actual_model") is not None and mirror_payload.get("actual_model") != actual_model:
                structurally_valid = False
            if type(mirror_payload.get("streaming")) is bool and mirror_payload.get("streaming") != native_stream:
                structurally_valid = False

    return observed, structurally_valid


def score(manifest: dict, browser: dict, gateway: dict, *, workspace_root: Path | None = None) -> dict:
    """Score one browser trial using only linked UI, gateway, tool, and file evidence."""
    m, ui, gw = _obj(manifest), _obj(browser), _obj(gateway)
    cid, profile, control = m.get("case_id"), m.get("profile"), m.get("control")
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    frozen = (cid in CASES and m.get("source_ref", SOURCE).split("#", 1)[0] == SOURCE
              and m.get("source_sha256") == digest((ROOT / SOURCE).read_bytes())
              and ui.get("schema") == SCHEMA and gw.get("schema") == GATEWAY_SCHEMA
              and ui.get("case_id") == cid and ui.get("profile") == profile
              and gw.get("profile") == profile and ui.get("trial_id") == m.get("trial_id")
              and m.get("fixture") == _obj(m.get("frozen_contract")).get("fixture"))
    checks["frozen_trial_binding"] = frozen
    if not frozen:
        reasons.append("missing_or_mismatched_frozen_trial_binding")

    browser_meta = _obj(ui.get("browser"))
    expected_viewport = ({"width": 390, "height": 844} if cid == "V080"
                         else {"width": 1280, "height": 800})
    checks["fixed_browser"] = (browser_meta.get("name", "").lower() in {"chromium", "chrome"}
                                and isinstance(browser_meta.get("version"), str)
                                and _obj(browser_meta.get("viewport")) == expected_viewport
                                and bool(browser_meta.get("font_fingerprint")))
    checks["accessible_actions"] = bool(_arr(ui.get("actions"))) and all(
        _obj(a).get("role") and _obj(a).get("name") for a in _arr(ui.get("actions")))
    console_errors = _arr(ui.get("console_errors"))
    page_errors = _arr(ui.get("page_errors"))
    http_error_statuses = [row.get("status") for row in _arr(ui.get("requests"))
                           if type(_obj(row).get("status")) is int and _obj(row).get("status") >= 400]
    checks["no_console_errors"] = cid != "V073" or not console_errors
    checks["assets_present"] = (bool(_arr(ui.get("dom_artifacts"))) and bool(_arr(ui.get("screenshots")))
                                and all(_obj(a).get("sha256") for a in _arr(ui.get("dom_artifacts")) + _arr(ui.get("screenshots"))))
    checks["loopback_requests"] = all(str(_obj(r).get("url", "")).startswith(("http://127.0.0.1", "ws://127.0.0.1"))
                                       for r in _arr(ui.get("requests")))

    before, after = _obj(_obj(ui.get("snapshots")).get("before")), _obj(_obj(ui.get("snapshots")).get("after"))
    submits = _arr(gw.get("browser_submissions"))
    checks["no_failed_turn"] = not any(
        _obj(row).get("event") == "turn_end" and _obj(row).get("outcome") == "error"
        for row in _arr(gw.get("deliveries"))) and not any(
        _obj(row).get("type") == "turn_failure" for row in _arr(gw.get("events")))
    if cid == "V073":
        expected = "5"
        # The first turn is the one-at-a-time assertion; the task explicitly
        # requires that a second message can be sent after generation ends.
        next_snapshot = _obj(_obj(ui.get("snapshots")).get("after_next_send"))
        actions = _arr(ui.get("actions"))
        checks["single_submit"] = (len(submits) == 1
                                   and sum(row.get("content", row.get("text")) == "2加3是多少？只回答数字。"
                                           for row in submits) == 1
                                   and any(_obj(a).get("action") == "fill-next" for a in actions)
                                   and any(_obj(a).get("action") == "assert-enabled-after-completion"
                                           for a in actions)
                                   and _count_role(next_snapshot, "user") == 1
                                   and _count_role(next_snapshot, "assistant") == 1
                                   and next_snapshot.get("composer_enabled") is True)
        checks["one_user_one_assistant"] = _count_role(after, "user") == 1 and _count_role(after, "assistant") == 1
        checks["correct_answer"] = _messages(after, "assistant") == [expected]
        checks["generation_exited_and_ready"] = after.get("generating") is False and after.get("composer_enabled") is True
        checks["completed_turn"] = any(_obj(d).get("event") == "turn_end" for d in _arr(gw.get("deliveries")))
        checks["click_accept_final_dom_timing"] = _v073_timing(ui, gw)
        trace_events = _arr(gw.get("events"))
        scripted_starts = [e for e in trace_events if _obj(e).get("type") == "scripted_response"
                           and _obj(e).get("phase") == "start"]
        ledger = _arr(gw.get("provider_ledger"))
        real_starts = [e for e in trace_events if _obj(e).get("type") == "model_attempt"
                       and _obj(e).get("phase") == "start"]
        checks["task_reached_provider"] = bool(scripted_starts or ledger or real_starts)
    elif cid == "V074":
        file_before = _obj(_obj(gw.get("workspace_before")).get("config.json"))
        file_after = _obj(_obj(gw.get("workspace_after")).get("config.json")) or _obj(
            _obj(gw.get("workspace_state")).get("config.json"))
        fixture_before = _obj(m.get("fixture")).get("config.json")
        tools = [t for t in _arr(gw.get("tool_events")) if _obj(t).get("name") == "edit_file"]
        starts, ends = [t for t in tools if t.get("phase") == "start"], [t for t in tools if t.get("phase") == "end"]
        checks["one_submit"] = len(submits) == 1
        workspace = _obj(gw.get("workspace_state"))
        actual_file = _obj(workspace.get("config.json"))
        edit_start_count = sum(1 for row in _span_starts(gw, "edit_file"))
        edit_end_count = sum(1 for row in _arr(gw.get("events"))
                             if _obj(row).get("type") == "tool" and _obj(row).get("phase") == "end"
                             and _obj(_obj(row).get("payload")).get("name") == "edit_file")
        expected_edit_count = 1 if profile == "B-scripted" else edit_start_count
        checks["real_edit_tool"] = (edit_start_count > 0 and edit_start_count == expected_edit_count
                                    and edit_end_count == edit_start_count
                                    and bool(ends)
                                    and bool(_obj(ends[-1]).get("file_sha256"))
                                    and _config_tool_target(_obj(starts[0]).get("arguments"), workspace_root)
                                    and _obj(ends[-1]).get("file_sha256") == actual_file.get("sha256")) if ends else False
        # The gateway's final filesystem snapshot and edit result are authoritative;
        # UI supplied file observations are retained only as a cross-check.
        normalized_file = actual_file.get("text", "").replace(" ", "")
        frozen_bytes = fixture_before.encode("utf-8") if isinstance(fixture_before, str) else b""
        before_is_frozen = (file_before.get("text", "").replace(" ", "") == '{"timeout":30,"retries":3}'
                            or (frozen_bytes and file_before.get("sha256") == digest(frozen_bytes)))
        checks["file_state_correct"] = (before_is_frozen
                                         and normalized_file == '{"timeout":60,"retries":3}'
                                         and actual_file.get("sha256")
                                         and file_after.get("sha256") == actual_file.get("sha256"))
        checks["progress_bound_to_tool"] = _tool_progress_bound(gw, after, ui)
        actual_tool_count = _actual_tool_count(gw)
        displayed_tool_count = _reported_tool_count(_obj(_obj(ui.get("snapshots")).get("after")))
        checks["tool_count_matches_actual"] = (actual_tool_count > 0
                                                and displayed_tool_count == actual_tool_count)
        reply = _final_reply(gw, after).lower()
        checks["one_final_body"] = len(_messages(after, "assistant")) == 1
        visible_reply = "\n".join(_messages(after, "assistant")).lower()
        checks["final_matches_file"] = (_affirmed_config_value(reply, "timeout", 60)
                                         and _affirmed_config_value(reply, "retries", 3)
                                         and _affirmed_config_value(visible_reply, "timeout", 60)
                                         and _affirmed_config_value(visible_reply, "retries", 3))
        checks["terminal_state"] = after.get("generating") is False and any(
            _obj(d).get("event") == "turn_end" for d in _arr(gw.get("deliveries")))
    elif cid == "V075":
        snapshots = _obj(ui.get("snapshots"))
        ask = _obj(_obj(snapshots.get("ask_wait")).get("ask_user"))
        before_report = _workspace_row(gw, "workspace_before", "report.txt")
        after_report = _workspace_row(gw, "workspace_after", "report.txt")
        starts = _span_starts(gw)
        ask_calls = [row for row in starts if _obj(_obj(row).get("payload")).get("name") == "ask_user"]
        writes = [row for row in starts if _obj(_obj(row).get("payload")).get("name") == "write_file"]
        before_probe = _obj(_obj(_probe(snapshots, "before_json_choice")).get("payload"))
        after_probe = _obj(_obj(_probe(snapshots, "after_resume")).get("payload"))
        session_dump = json.dumps(after_probe.get("session_files", []), ensure_ascii=False)
        ask_call_id = _obj(_obj(ask_calls[0]).get("payload")).get("call_id") if ask_calls else None
        probe_report = _obj(_obj(before_probe.get("workspace_state")).get("report.txt"))
        checks["single_original_submit"] = (len(submits) == 2
            and sum(row.get("content") == "把设备120、运输30整理为报告，先问格式" for row in submits) == 1
            and sum(row.get("content") == "JSON" for row in submits) == 1)
        checks["ask_ui_waits_without_file"] = (ask.get("group_visible") is True
                                               and ask.get("question") == "报告用表格还是JSON？"
                                               and {"表格", "JSON"}.issubset(set(ask.get("options", [])))
                                               and not before_report
                                               and probe_report.get("exists") is False
                                               and probe_report.get("sha256") is None)
        checks["one_ask_one_resume_write"] = len(ask_calls) == 1 and len(writes) == 1
        checks["json_resume_bound"] = (snapshots.get("ask_user_selected") == "JSON"
                                       and any(_obj(a).get("name") == "JSON"
                                            and _obj(a).get("action") == "click-ask-user-option"
                                            for a in _arr(ui.get("actions")))
                                       and bool(ask_call_id) and str(ask_call_id) in session_dump
                                       and "JSON" in session_dump
                                       and _obj(writes[0]).get("monotonic_ns", 0)
                                       > _obj(ask_calls[0]).get("monotonic_ns", -1)
                                       and before_probe.get("inbound_submission_count") == 1
                                       and after_probe.get("inbound_submission_count") == 2)
        checks["exact_report_file"] = (after_report.get("sha256") == digest(b'{"total":150}')
                                       and after_report.get("bytes") == len(b'{"total":150}'))
        checks["ask_wait_exited"] = (_obj(_obj(ui.get("snapshots")).get("after")).get("generating") is False
                                     and not _obj(_obj(_obj(ui.get("snapshots")).get("after")).get("ask_user")).get("group_visible", False))
    elif cid == "V076":
        snapshots = _obj(ui.get("snapshots"))
        before_stop = _obj(_obj(_probe(snapshots, "before_stop")).get("payload"))
        at_cancel = _obj(_obj(_probe(snapshots, "cancelled_before_continue")).get("payload"))
        before_continue = _obj(_obj(_probe(snapshots, "checkpoint_before_continue")).get("payload"))
        after_continue = _obj(_obj(_probe(snapshots, "continue_completed")).get("payload"))
        mid_file = _obj(_obj(at_cancel.get("workspace_state")).get("result.txt"))
        pre_continue_file = _obj(_obj(before_continue.get("workspace_state")).get("result.txt"))
        before_result = _workspace_row(gw, "workspace_before", "result.txt")
        after_result = _workspace_row(gw, "workspace_after", "result.txt")
        waiting = _gate_state(before_stop, "v076-cancel-gate")
        cancelled = _gate_state(at_cancel, "v076-cancel-gate")
        starts = _span_starts(gw)
        writes = [row for row in starts if _obj(_obj(row).get("payload")).get("name") == "write_file"]
        checks["gate_reached_then_cancelled"] = (waiting.get("state") == "waiting"
                                                 and cancelled.get("state") == "cancelled"
                                                 and cancelled.get("cancellations") == 1)
        checks["no_write_before_resume"] = (not before_result and mid_file.get("exists") is False
                                            and pre_continue_file.get("exists") is False)
        checks["continue_after_cancel"] = (any(_obj(a).get("action") == "click-stop" for a in _arr(ui.get("actions")))
                                           and any(_obj(a).get("action") == "click-continue" for a in _arr(ui.get("actions")))
                                           and before_stop.get("inbound_submission_count") == 1
                                           and at_cancel.get("inbound_submission_count") == 2
                                           and before_continue.get("inbound_submission_count") == 2
                                           and after_continue.get("inbound_submission_count") == 3
                                           and len(submits) == 3
                                           and sum(_obj(row).get("content") == "读取fact.txt，再写result.txt为73"
                                                       for row in submits) == 1
                                           and sum(_obj(row).get("content", "").strip().lower() == "/stop"
                                                       for row in submits) == 1
                                           and sum(_obj(row).get("content") == "继续完成刚才的任务"
                                                       for row in submits) == 1
                                           and len({row.get("chat_id") for row in submits}) == 1
                                           and bool(_arr(before_stop.get("session_files")))
                                           and _obj(before_stop.get("session_files")[0]).get("session_id")
                                           == f"websocket:{submits[0].get('chat_id')}")
        pre_stop_models = before_stop.get("provider_request_count")
        cancel_models = at_cancel.get("provider_request_count")
        before_continue_models = before_continue.get("provider_request_count")
        after_continue_models = after_continue.get("provider_request_count")
        checks["stop_does_not_call_provider"] = (
            type(pre_stop_models) is int and pre_stop_models == cancel_models == before_continue_models
            and type(after_continue_models) is int and after_continue_models > before_continue_models)
        checks["one_write_after_resume"] = (len(writes) == 1 and after_result.get("sha256") == digest(b"73")
                                            and _obj(after_continue.get("workspace_state")).get("result.txt", {}).get("sha256")
                                            == digest(b"73"))
        checkpoint_before_stop = _arr(before_stop.get("checkpoint_ids"))
        cancel_sessions = _arr(at_cancel.get("session_files"))
        before_continue_sessions = _arr(before_continue.get("session_files"))
        checks["checkpoint_lifecycle"] = (bool(checkpoint_before_stop)
                                           and all(row.get("checkpoint_present") is True
                                                   for row in _arr(before_stop.get("session_files")))
                                           and all(row.get("checkpoint_id") in checkpoint_before_stop
                                                   for row in _arr(before_stop.get("session_files")))
                                           and not _arr(at_cancel.get("checkpoint_ids"))
                                           and not _arr(before_continue.get("checkpoint_ids"))
                                           and all(row.get("checkpoint_present") is False
                                                   for row in cancel_sessions + before_continue_sessions))
        checks["cancel_stopped_agent"] = (
            _obj(at_cancel.get("cancellation_state")).get("active_task_count") == 0
            and "v076-cancel-gate" in _arr(_obj(at_cancel.get("cancellation_state")).get("cancelled_gates")))
        checks["session_stable_and_read_restored"] = (
            bool(cancel_sessions) and _session_hashes(cancel_sessions) == _session_hashes(before_continue_sessions)
            and any("read_file" in json.dumps(row.get("jsonl", []), ensure_ascii=False)
                    and "73" in json.dumps(row.get("jsonl", []), ensure_ascii=False)
                    and "Task interrupted before this tool finished." in json.dumps(
                        row.get("jsonl", []), ensure_ascii=False)
                    for row in before_continue_sessions))
        checks["final_result_correct"] = _has_assistant_text(
            _obj(_obj(ui.get("snapshots")).get("after")), "result.txt为73。")
        checks["terminal_after_resume"] = _obj(_obj(ui.get("snapshots")).get("after")).get("generating") is False
    elif cid == "V077":
        snaps = _obj(ui.get("snapshots"))
        chat_ids = _obj(snaps.get("chat_ids"))
        a_id, b_id = chat_ids.get("a"), chat_ids.get("b")
        transitions = [event for event in _arr(gw.get("events"))
                       if _obj(event).get("type") == "browser_task_gate"]
        transitions.sort(key=lambda event: (
            _obj(event).get("seq") if type(_obj(event).get("seq")) is int
            else _obj(event).get("monotonic_ns", 0)))
        transitions = [(_obj(_obj(row).get("payload")).get("gate_id"),
                       _obj(_obj(row).get("payload")).get("state")) for row in transitions]
        a_waiting = [i for i, row in enumerate(transitions) if row == ("v077-A", "waiting")]
        b_waiting = [i for i, row in enumerate(transitions) if row == ("v077-B", "waiting")]
        b_released = [i for i, row in enumerate(transitions) if row == ("v077-B", "released")]
        a_released = [i for i, row in enumerate(transitions) if row == ("v077-A", "released")]
        fixture_sessions = _obj(gw.get("fixture_sessions"))
        fixture_a, fixture_b = _obj(fixture_sessions.get("a")), _obj(fixture_sessions.get("b"))
        session_before = _obj(gw.get("session_before"))
        initial_hashes = {row.get("sha256") for row in session_before.values() if isinstance(row, dict)}
        initial_rows = (fixture_a, fixture_b)
        checks["two_distinct_sessions"] = bool(a_id and b_id and a_id != b_id)
        checks["cold_seeded_sessions"] = (
            fixture_a.get("chat_id") == a_id and fixture_b.get("chat_id") == b_id
            and fixture_a.get("session_key") == f"websocket:{a_id}"
            and fixture_b.get("session_key") == f"websocket:{b_id}"
            and fixture_a.get("initial_messages") == fixture_b.get("initial_messages") == 0
            and fixture_a.get("messages") == fixture_b.get("messages") == []
            and fixture_a.get("initial_session_sha256") == fixture_a.get("session_sha256")
            and fixture_b.get("initial_session_sha256") == fixture_b.get("session_sha256")
            and fixture_a.get("initial_session_sha256") != fixture_b.get("initial_session_sha256")
            and all(row.get("initial_session_sha256") in initial_hashes for row in initial_rows)
            and len(session_before) == 2 and gw.get("fixture_model_requests") == 0)
        checks["A_B_gate_order_and_release"] = (
            len(a_waiting) == len(b_waiting) == len(b_released) == len(a_released) == 1
            and a_waiting[0] < b_waiting[0] < b_released[0] < a_released[0])
        terminal_chats = [_obj(row).get("chat_id") for row in _arr(gw.get("deliveries"))
                          if _obj(row).get("event") == "turn_end"]
        checks["B_completes_before_A"] = (terminal_chats.count(b_id) == 1 and terminal_chats.count(a_id) == 1
                                          and terminal_chats.index(b_id) < terminal_chats.index(a_id))
        b_complete = _obj(snaps.get("b_complete"))
        checks["B_visible_only_in_B"] = (_messages(b_complete, "user") == ["返回B=23"]
                                          and _messages(b_complete, "assistant") == ["B=23"]
                                          and b_complete.get("chat_id") == b_id)
        b_selected = _obj(snaps.get("b_selected_during_a"))
        b_observation = _obj(_probe(snaps, "v077-b-selected-during-a"))
        b_payload = _obj(b_observation.get("payload"))
        b_a_gate = _gate_state(b_payload, "v077-A")
        b_session_ids = {row.get("session_id") for row in _arr(b_payload.get("session_files"))}
        checks["B_selected_while_A_waits"] = (b_selected.get("chat_id") == b_id
                                              and _messages(b_selected, "user") == ["返回B=23"]
                                              and _messages(b_selected, "assistant") == ["B=23"]
                                              and _messages(b_complete, "assistant") == ["B=23"]
                                              and b_a_gate.get("state") == "waiting"
                                              and f"websocket:{b_id}" in b_session_ids
                                              and b_waiting and b_released and a_waiting
                                              and b_waiting[0] < b_released[0] < a_released[0])
        checks["A_waiting_only_in_A"] = (_obj(snaps.get("a_waiting")).get("chat_id") == a_id
                                         and _messages(snaps.get("a_waiting"), "user") == ["返回A=17"]
                                         and not _messages(snaps.get("a_waiting"), "assistant"))
        checks["B_waiting_only_in_B"] = (_obj(snaps.get("b_waiting")).get("chat_id") == b_id
                                         and _messages(snaps.get("b_waiting"), "user") == ["返回B=23"]
                                         and not _messages(snaps.get("b_waiting"), "assistant"))
        checks["A_restored_only_in_A"] = (_messages(snaps.get("a_selected"), "user") == ["返回A=17"]
                                          and _messages(snaps.get("a_selected"), "assistant") == ["A=17"]
                                          and _obj(snaps.get("a_selected")).get("chat_id") == a_id)
        checks["real_history_requests"] = sum(1 for row in _arr(ui.get("requests"))
                                              if _obj(row).get("kind") == "history") >= 2
        checks["submissions_bound_to_chat"] = (len(submits) == 2
                                               and sum(_obj(row).get("content") == "返回A=17"
                                                       and _obj(row).get("chat_id") == a_id for row in submits) == 1
                                               and sum(_obj(row).get("content") == "返回B=23"
                                                       and _obj(row).get("chat_id") == b_id for row in submits) == 1)
    elif cid == "V078":
        reload = _obj(_obj(ui.get("snapshots")).get("after_reload"))
        checks["same_session"] = bool(before.get("chat_id")) and before.get("chat_id") == reload.get("chat_id")
        checks["one_user_one_assistant_before"] = _count_role(before, "user") == 1 and _count_role(before, "assistant") == 1
        checks["one_user_one_assistant_after"] = _count_role(reload, "user") == 1 and _count_role(reload, "assistant") == 1
        checks["history_preserved"] = (_messages(before, "user") == _messages(reload, "user")
                                       and _messages(before, "assistant") == _messages(reload, "assistant") == ["HISTORY17"])
        checks["single_original_submit"] = len(submits) == 1
        observations = _obj(_obj(ui.get("snapshots")).get("history_observations"))
        pre, post = _obj(_obj(observations.get("before_reload")).get("payload")), _obj(
            _obj(observations.get("after_reload")).get("payload"))
        counts = _obj(_obj(ui.get("snapshots")).get("ui"))
        checks["no_new_model_or_tool"] = (
            type(pre.get("provider_request_count")) is int
            and pre.get("provider_request_count") == post.get("provider_request_count")
            and pre.get("tool_events") == post.get("tool_events")
            and pre.get("inbound_submission_count") == post.get("inbound_submission_count") == 1
            and counts.get("model_calls_before_reload") == counts.get("model_calls_after_reload")
            and counts.get("tool_calls_before_reload") == counts.get("tool_calls_after_reload"))
        checks["history_request_observed"] = any(_obj(r).get("kind") == "history" for r in _arr(ui.get("requests")))
    elif cid == "V079":
        fixture = _obj(m.get("fixture"))
        question = fixture.get("question") if isinstance(fixture.get("question"), str) else ""
        subject = fixture.get("subject") if isinstance(fixture.get("subject"), str) else ""
        excerpt = (fixture.get("expected_source_excerpt")
                   if isinstance(fixture.get("expected_source_excerpt"), str) else "")
        submission = _obj(submits[0]) if submits else {}
        snapshots = _obj(ui.get("snapshots"))
        after = _obj(snapshots.get("after"))
        expanded = _obj(snapshots.get("source_expanded"))
        denied = _obj(snapshots.get("source_denied"))
        source_panel = _obj(expanded.get("source_panel"))
        denied_panel = _obj(denied.get("source_panel"))
        mail_auth = _obj(gw.get("mail_auth"))
        owner = _obj(mail_auth.get("owner_binding"))
        binding_id, principal_id, owner_id = (owner.get("binding_id"), owner.get("principal_id"),
                                              owner.get("owner_id"))
        source_reads = [_obj(row) for row in _arr(mail_auth.get("source_reads"))]
        allowed_reads = [row for row in source_reads if row.get("status") == "allowed"]
        matching_reads = []
        matched_sources = []
        for row in allowed_reads:
            row_sources = [_obj(source) for source in _arr(row.get("sources"))]
            if any(source.get("subject") == subject and isinstance(excerpt, str)
                   and excerpt in str(source.get("excerpt", "")) for source in row_sources):
                matching_reads.append(row)
                matched_sources.extend(source for source in row_sources if source.get("subject") == subject
                                       and excerpt in str(source.get("excerpt", "")))
        evidence_ids = {row.get("evidence_id") for row in matched_sources if row.get("evidence_id")}
        source_read = _obj(snapshots.get("source_read"))
        read_request = _obj(source_read.get("request"))
        read_path = urlsplit(str(read_request.get("url", ""))).path
        read_evidence_id = read_path.rstrip("/").split("/")[-1]
        http_requests = [_obj(row) for row in _arr(mail_auth.get("http_requests"))]
        browser_source_requests = [_obj(row) for row in _arr(ui.get("requests"))
                                   if _obj(row).get("kind") == "mail_source"]
        identities = [_obj(row.get("identity")) for row in matching_reads]
        auth_identity = bool(binding_id and principal_id and owner_id and owner.get("channel") == "websocket"
                             and "read" in _arr(owner.get("scopes"))
                             and identities and all(
                                 row.get("principal_id") == principal_id
                                 and row.get("binding_id") == binding_id and row.get("owner_id") == owner_id
                                 for row in identities))
        checks["frozen_mail_fixture"] = (question == "竹桥预算是多少？给出邮件依据"
                                         and subject == "竹桥预算" and excerpt == "竹桥预算120元"
                                         and fixture.get("body") == excerpt)
        checks["single_mail_task"] = (len(submits) == 1 and submission.get("content") == question
                                       and bool(submission.get("chat_id")))
        checks["answer_and_citation"] = (
            _messages(after, "user") == [question] and len(_messages(after, "assistant")) == 1
            and _affirmed_rmb_amount(_answer_claim(str(_messages(after, "assistant")[0])), 120)
            and "[1]" in str(_messages(after, "assistant")[0])
            and after.get("chat_id") == submission.get("chat_id")
            and _affirmed_rmb_amount(_answer_claim(_final_reply(gw, after)), 120))
        checks["expanded_source_matches_read"] = (
            expanded.get("chat_id") == submission.get("chat_id")
            and source_panel.get("visible") is True
            and isinstance(source_panel.get("text"), str)
            and bool(subject) and subject in source_panel.get("text", "")
            and bool(excerpt) and excerpt in source_panel.get("text", "")
            and source_read.get("visible_excerpt") is True
            and read_request.get("kind") == "mail_source" and read_request.get("method") == "GET"
            and read_request.get("status") == 200
            and read_evidence_id in evidence_ids and bool(matching_reads)
            and any(row.get("status") == 200 and row.get("path") == read_path for row in http_requests)
            and any(row.get("status") == 200 and urlsplit(str(row.get("url", ""))).path == read_path
                    for row in browser_source_requests)
            and all(len(_arr(row.get("sources"))) == 1
                    and _obj(_arr(row.get("sources"))[0]).get("subject") == subject
                    and excerpt in str(_obj(_arr(row.get("sources"))[0]).get("excerpt", ""))
                    for row in allowed_reads))
        checks["owner_identity_bound_to_source"] = auth_identity
        tokens = _arr(mail_auth.get("issued_tokens"))
        socket_bindings = _arr(mail_auth.get("websocket_bindings"))
        checks["authenticated_websocket_owner"] = (
            any(_obj(row).get("authenticated") is True and _obj(row).get("principal_id") == principal_id
                and _obj(row).get("binding_id") == binding_id and _obj(row).get("owner_id") == owner_id
                for row in tokens)
            and any(_obj(row).get("authenticated") is True and _obj(row).get("principal_id") == principal_id
                    and _obj(row).get("binding_id") == binding_id and _obj(row).get("owner_id") == owner_id
                    for row in socket_bindings))
        auth_rows = [_obj(row) for row in _arr(mail_auth.get("http_authorizations"))]
        checks["http_owner_authorization"] = any(
            row.get("status") == "allowed" and row.get("path") == read_path and row.get("scope") == "read"
            and row.get("principal_id") == principal_id
            and row.get("binding_id") == binding_id and row.get("owner_id") == owner_id
            for row in auth_rows)
        probe_ids = ("v079-source-expanded", "v079-permission-revoked", "v079-source-denied")
        probes = [_obj(_probe(snapshots, probe_id)) for probe_id in probe_ids]
        probe_times = [_obj(row.get("payload")).get("monotonic_ns") for row in probes]
        checks["source_permission_timeline"] = (
            all(row.get("status") == 200 for row in probes)
            and all(_obj(row.get("payload")).get("probe_id") == probe_id
                    for row, probe_id in zip(probes, probe_ids, strict=True))
            and all(type(value) is int for value in probe_times)
            and probe_times[0] < probe_times[1] < probe_times[2])
        revoke = _obj(snapshots.get("permission_revoked"))
        revocations = [_obj(row) for row in _arr(mail_auth.get("revocations"))]
        checks["owner_revoked"] = (
            revoke.get("status") == 200 and _obj(revoke.get("payload")).get("revoked") is True
            and any(row.get("status") == "revoked" and row.get("binding_id") == binding_id
                    and row.get("principal_id") == principal_id and row.get("owner_id") == owner_id
                    for row in revocations))
        denied_request = _obj(snapshots.get("source_denied_request"))
        denied_auth = [row for row in auth_rows if row.get("status") == "denied"
                       and row.get("path") == read_path and row.get("scope") == "read"
                       and row.get("principal_id") == principal_id and row.get("binding_id") == binding_id
                       and row.get("owner_id") == owner_id]
        checks["revoked_source_read_is_403"] = (
            denied.get("chat_id") == submission.get("chat_id")
            and denied_request.get("kind") == "mail_source" and denied_request.get("status") == 403
            and urlsplit(str(denied_request.get("url", ""))).path == read_path
            and any(row.get("status") == 403 and row.get("path") == read_path for row in http_requests)
            and bool(denied_auth)
            and any(row.get("status") == 200 and urlsplit(str(row.get("url", ""))).path == read_path
                    for row in browser_source_requests)
            and any(row.get("status") == 403 and urlsplit(str(row.get("url", ""))).path == read_path
                    for row in browser_source_requests))
        denied_text = str(denied_panel.get("text", ""))
        denied_snapshot = _obj(snapshots.get("source_denied"))
        checks["revoked_dom_has_no_private_source"] = (
            denied_panel.get("visible") is True and bool(denied_panel.get("alert"))
            and excerpt not in denied_text and excerpt not in json.dumps(denied_panel, ensure_ascii=False)
            and excerpt not in json.dumps(denied_snapshot.get("visible_bubbles"), ensure_ascii=False)
            and all(excerpt not in json.dumps(row.get("body"), ensure_ascii=False)
                    for row in http_requests if row.get("status") == 403 and row.get("path") == read_path)
            and V079_OWNER_SECRET_SENTINEL not in json.dumps(ui, ensure_ascii=False))
        checks["real_expand_and_reopen_actions"] = (
            _has_action(ui, "expand-source-citation")
            and _has_action(ui, "reopen-source-after-revoke"))
    elif cid == "V080":
        fixture = _obj(m.get("fixture"))
        theme = fixture.get("theme")
        snapshots = _obj(ui.get("snapshots"))
        layouts = [_obj(row) for row in _arr(snapshots.get("layout_observations"))]
        phases = ("sidebar_open", "new_session_sidebar_closed", "answer",
                  "sidebar_open_after_answer", "chat_row_before_switch", "chat_selected")
        by_phase = {row.get("phase"): row for row in layouts}
        checks["frozen_theme"] = (theme in {"light", "dark"} and browser_meta.get("theme") == theme
                                   and fixture.get("question") == "\u8fd4\u56deMOBILE17")
        checks["theme_applied_through_all_phases"] = all(
            _obj(row.get("document")).get("theme_dark_class") is (theme == "dark")
            for row in layouts if row.get("phase") in phases)
        checks["all_mobile_layout_phases_present"] = (
            len(layouts) == len(phases) and all(sum(row.get("phase") == phase for row in layouts) == 1
                                                  for phase in phases)
            and set(by_phase) == set(phases))
        layout_ok = bool(layouts) and all(phase in by_phase for phase in phases)
        for phase in phases:
            row = by_phase.get(phase)
            document = _obj(_obj(row).get("document"))
            active = _obj(document.get("active_element"))
            active_tag = str(active.get("tag", "")).lower()
            active_role = str(active.get("role", "")).lower()
            if (document.get("inner_width") != 390 or document.get("inner_height") != 844
                    or document.get("horizontal_overflow") is not False
                    or type(document.get("document_scroll_width")) is not int
                    or type(document.get("document_client_width")) is not int
                    or document.get("document_scroll_width") > document.get("document_client_width") + 1
                    or document.get("focus_target_present") is not True
                    or not (active_tag in {"input", "textarea", "button", "select"}
                            or active_role in {"textbox", "button", "combobox"})):
                layout_ok = False
                continue
            for layout_control in _arr(_obj(row).get("controls")):
                layout_control = _obj(layout_control)
                bbox = _obj(layout_control.get("bbox"))
                x, y, width, height = (bbox.get("x"), bbox.get("y"), bbox.get("width"), bbox.get("height"))
                if (not layout_control.get("role") or not layout_control.get("name")
                        or layout_control.get("present") is not True or layout_control.get("visible") is not True
                        or layout_control.get("occluded") is not False
                        or not all(isinstance(value, (int, float)) for value in (x, y, width, height))
                        or width <= 0 or height <= 0 or x < 0 or y < 0
                        or x + width > 390 or y + height > 844
                        or not layout_control.get("center_hit_tag")):
                    layout_ok = False
        expected_controls = {
            "sidebar_open": {("button", "New chat")},
            "new_session_sidebar_closed": {("button", "Toggle sidebar"), ("textbox", "Message input"),
                                            ("button", "Send message")},
            "answer": {("button", "Toggle sidebar"), ("textbox", "Message input")},
            "sidebar_open_after_answer": {("button", "New chat")},
            "chat_row_before_switch": {("button", "New chat")},
            "chat_selected": {("button", "Toggle sidebar"), ("textbox", "Message input")},
        }
        for phase, expected in expected_controls.items():
            actual = {(_obj(row).get("role"), _obj(row).get("name"))
                      for row in _arr(_obj(by_phase.get(phase)).get("controls"))}
            binding = _obj(snapshots.get("mobile_row_binding"))
            row_matches = (phase != "chat_row_before_switch" or (
                binding.get("row_count") == 1 and bool(binding.get("accessible_name"))
                and binding.get("chat_id") == snapshots.get("mobile_chat_id")
                and ("button", binding.get("accessible_name")) in actual))
            if not expected.issubset(actual) or not row_matches:
                layout_ok = False
        checks["mobile_geometry_and_controls"] = layout_ok
        empty_chat_state = _obj(snapshots.get("mobile_empty_session_state"))
        empty_chat = _obj(snapshots.get("mobile_empty_before_switch_back"))
        chat_id = snapshots.get("mobile_chat_id")
        mobile_answer = _obj(snapshots.get("mobile_answer"))
        switched_back = _obj(snapshots.get("mobile_switched_back"))
        mobile_prompt = "返回MOBILE17"
        checks["one_mobile_task_and_answer"] = (
            len(submits) == 1 and _obj(submits[0]).get("content") == mobile_prompt
            and bool(chat_id) and _obj(submits[0]).get("chat_id") == chat_id
            and _messages(mobile_answer, "user") == [mobile_prompt]
            and _messages(mobile_answer, "assistant") == ["MOBILE17"]
            and mobile_answer.get("chat_id") == chat_id)
        checks["new_session_and_switch_back"] = (
            snapshots.get("mobile_empty_chat_id") is None
            and empty_chat_state.get("state") == "unsaved-new-chat-draft"
            and empty_chat_state.get("business_bubbles") == []
            and not _bubbles(empty_chat)
            and _messages(switched_back, "user") == [mobile_prompt]
            and _messages(switched_back, "assistant") == ["MOBILE17"]
            and switched_back.get("chat_id") == chat_id
            and _has_action(ui, "start-new-mobile-session")
            and _has_action(ui, "switch-back-to-mobile17-chat"))
        history_rows = [_obj(row) for row in _arr(ui.get("requests"))
                        if _obj(row).get("kind") == "history"
                        and unquote(urlsplit(str(_obj(row).get("url", ""))).path)
                        == f"/api/sessions/websocket:{chat_id}/messages"]
        selected_probe = _obj(_probe(snapshots, "v080-mobile-chat-selected"))
        checks["same_chat_history_reloaded"] = (
            len(history_rows) >= 2 and history_rows[-1].get("status") == 200
            and selected_probe.get("status") == 200
            and f"websocket:{chat_id}" in _arr(_obj(selected_probe.get("payload")).get("session_ids")))
        required_actions = {"open-sidebar", "start-new-mobile-session", "open-sidebar-after-answer",
                            "move-to-empty-chat-before-switch-back", "reopen-sidebar-to-switch-back",
                            "switch-back-to-mobile17-chat"}
        checks["accessible_mobile_navigation"] = required_actions.issubset(
            {str(_obj(row).get("action")) for row in _arr(ui.get("actions"))})

    ledger = _obj(gw.get("provider_ledger"))
    if profile == "B-real":
        checks["real_model_profile"] = gw.get("model") == MODEL and gw.get("observation_mode") != "scripted"
        events = _arr(gw.get("events"))
        try:
            trace = analyze_trace(events, execution_status="finished", collector_errors=_arr(
                _obj(gw.get("trace_summary")).get("collector_errors")))
        except (KeyError, TypeError, ValueError):
            trace = {"valid": False, "closed": False, "ledger": [], "actual_tokens": None}
        checks["real_trace_closed"] = trace.get("valid") is True and trace.get("closed") is True
        ledger_rows = _arr(trace.get("ledger"))
        checks["real_model_ledger"] = (bool(ledger_rows) and len(ledger_rows) <= 8
                                        and all(row.get("usage_status") == "known"
                                                and row.get("sdk_attempt_coverage") == "single_attempt"
                                                for row in ledger_rows))
        attempt_starts = [event for event in events if _obj(event).get("type") == "model_attempt"
                          and _obj(event).get("phase") == "start"]
        transport_rows = _arr(gw.get("provider_transports"))
        transport_observed, sdk_transport_valid = _real_sdk_transport_checks(
            gw, events, attempt_starts)
        no_scripted = not any(_obj(event).get("type") == "scripted_response" for event in events)
        checks["real_sdk_transport_observed"] = (transport_observed and no_scripted
                                                 and len(attempt_starts) == len(ledger_rows))
        checks["real_sdk_transport"] = (sdk_transport_valid
            and no_scripted and len(attempt_starts) == len(ledger_rows))
        if cid == "V079":
            checks["protected_mail_sdk_ledger"] = (
                len(transport_rows) == len(attempt_starts) and bool(transport_rows)
                and all(_obj(row).get("requested_model") == MODEL
                        and isinstance(_obj(row).get("actual_model"), str)
                        and bool(_obj(row).get("actual_model").strip())
                        and _obj(row).get("streaming") is False
                        and _obj(row).get("status") == "complete"
                        and _obj(row).get("sdk_retries") == 0
                        and _obj(row).get("usage_status") == "known"
                        for row in transport_rows))
    else:
        checks["scripted_profile_is_not_real"] = gw.get("observation_mode") == "scripted"
    if control:
        # Diagnostic negative controls are graded against their named adversarial effect.
        failed = {"wrong_answer": cid == "V073" and checks.get("correct_answer") is False,
                  "duplicate_delivery": (
                      cid == "V073" and (checks.get("one_user_one_assistant") is False
                                         or checks.get("correct_answer") is False)
                      or cid == "V078" and (checks.get("one_user_one_assistant_before") is False
                                           or checks.get("history_preserved") is False)),
                  "skip_edit": cid == "V074" and checks.get("file_state_correct") is False,
                  "refresh_resubmit": cid == "V078" and (checks.get("no_new_model_or_tool") is False
                                                          or checks.get("single_original_submit") is False),
                  "duplicate_bubbles": cid == "V073" and checks.get("one_user_one_assistant") is False,
                  "fake_edit": cid == "V074" and checks.get("real_edit_tool") is False,
                  "refresh_model_call": cid == "V078" and checks.get("no_new_model_or_tool") is False,
                  "wrong_source": cid == "V079" and checks.get("expanded_source_matches_read") is False,
                  "wrong_identity": cid == "V079" and checks.get("owner_identity_bound_to_source") is False,
                  "revoked_source_leak": cid == "V079" and checks.get("revoked_dom_has_no_private_source") is False,
                  "wrong_theme": cid == "V080" and checks.get("frozen_theme") is False,
                  "mobile_overflow": cid == "V080" and checks.get("mobile_geometry_and_controls") is False,
                  "mobile_occlusion": cid == "V080" and checks.get("mobile_geometry_and_controls") is False,
                  "mobile_focus_missing": cid == "V080" and checks.get("mobile_geometry_and_controls") is False,
                  "cross_trial": not frozen,
                  "tampered_evidence": not checks["assets_present"]}.get(control, False)
        checks["negative_control_detected"] = failed
    passed = all(checks.values())
    qualified = passed and cid in {"V073", "V074", "V078", "V079", "V080"} and profile == "B-real" and not control and all(
        checks.get(k, False) for k in ("real_model_profile", "real_model_ledger"))
    return {"scorer_version": SCORER_VERSION, "case_id": cid, "profile": profile, "control": control,
            "status": "pass" if passed else "fail",
            "program_pass": passed, "formal_qualification": qualified, "checks": checks,
            "reasons": reasons + [k for k, v in checks.items() if not v], "model_calls": len(_arr(gw.get("provider_requests"))),
            "ledger": ledger, "console_error_count": len(console_errors), "page_error_count": len(page_errors),
            "browser_http_error_status_counts": {str(status): http_error_statuses.count(status)
                                                  for status in sorted(set(http_error_statuses))}}


def audit(output: str | Path) -> dict:
    """Offline audit of saved browser trials; hashes, source binding and verdict replay."""
    root = Path(output).resolve()
    rows, errors = [], []
    for manifest_path in sorted(root.rglob("manifest.json")):
        trial = manifest_path.parent
        try:
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            ui = json.loads((trial / "browser-evidence.json").read_text(encoding="utf-8"))
            gw = json.loads((trial / "gateway-evidence.json").read_text(encoding="utf-8"))
            verdict = json.loads((trial / "verdict.json").read_text(encoding="utf-8"))
            asset_rows = _arr(ui.get("dom_artifacts")) + _arr(ui.get("screenshots")) + _arr(ui.get("assets"))
            if not asset_rows or not all(_valid_asset(trial, _obj(a)) for a in asset_rows):
                raise ValueError("browser_asset_hash_or_path_mismatch")
            replay = score(m, ui, gw, workspace_root=trial / "workspace")
            if canonical(verdict) != canonical(replay):
                raise ValueError("verdict_replay_mismatch")
            rows.append({"trial_id": m.get("trial_id"), "status": replay["status"],
                         "formal_qualification": replay["formal_qualification"], "audit_ok": True})
        except Exception as exc:  # audit reports a bounded type/code, never arbitrary file contents
            errors.append({"trial": trial.name, "error": type(exc).__name__, "detail": str(exc)[:160]})
    audit_ok = bool(rows) and not errors
    return {"ok": audit_ok, "trials": rows, "errors": errors,
            "qualification": audit_ok and any(row["formal_qualification"] for row in rows),
            "qualified_trials": sum(row["formal_qualification"] for row in rows) if audit_ok else 0}

"""Opt-in, context-local observations that never participate in agent decisions.

Only a trusted caller binds a sink. No global logging, credentials, raw prompts or
hidden reasoning are exported here. Sink failures are retained out of band.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable


@dataclass
class Observation:
    sink: Callable[..., None]
    task_id: str
    parent_task_id: str | None = None
    collector_errors: list[dict[str, str]] = field(default_factory=list)
    active_spans: dict[str, Any] = field(default_factory=dict)


_observation: ContextVar[Observation | None] = ContextVar("nanobot_observation", default=None)
_span: ContextVar[str | None] = ContextVar("nanobot_observation_span", default=None)


def current_span_id() -> str | None:
    return _span.get()


def current_observation() -> Observation | None:
    return _observation.get()


def has_active_span(kind: str) -> bool:
    """Return whether the current span chain already contains *kind*."""
    state = _observation.get()
    span_id = _span.get()
    seen: set[str] = set()
    while state is not None and span_id and span_id not in seen:
        seen.add(span_id)
        active = state.active_spans.get(span_id)
        if active is None:
            return False
        if active.kind == kind:
            return True
        span_id = active.parent_span_id
    return False


@contextmanager
def activate_span(span_id):
    token = _span.set(span_id)
    try:
        yield
    finally:
        _span.reset(token)


@contextmanager
def observation_context(sink, task_id: str, parent_task_id: str | None = None,
                        parent_span_id: str | None = None):
    parent_state = _observation.get()
    state = Observation(sink, task_id, parent_task_id)
    token = _observation.set(state)
    parent_token = _span.set(parent_span_id)
    try:
        yield state
    finally:
        if parent_state is not None and parent_state.sink is sink:
            parent_state.collector_errors.extend(state.collector_errors)
        _span.reset(parent_token)
        _observation.reset(token)


def _emit(state, kind, phase, span_id, parent_span_id, payload):
    try:
        state.sink(kind, phase=phase, span=span_id, parent_span_id=parent_span_id,
                   task_id=state.task_id, parent_task_id=state.parent_task_id, **payload)
    except Exception as exc:
        # Do not turn a telemetry outage into a different model/tool result.
        state.collector_errors.append({"type": kind, "phase": phase,
                                       "span_id": span_id, "error_kind": type(exc).__name__})


class Span:
    def __init__(self, kind, detached=False, **payload):
        self.state = _observation.get()
        self.kind = kind
        self.payload = payload
        self.span_id = uuid.uuid4().hex if self.state else None
        self.parent_span_id = _span.get()
        self.started_ns = 0
        self._token = None
        self._closed = False
        self.detached = detached

    def __enter__(self):
        if self.state:
            self.started_ns = time.monotonic_ns()
            _emit(self.state, self.kind, "start", self.span_id, self.parent_span_id, self.payload)
            if not self.detached:
                self._token = _span.set(self.span_id)
            self.state.active_spans[self.span_id] = self
        return self

    def update(self, **payload):
        self.payload.update(payload)

    def close(self, phase="end", **payload):
        if self._closed:
            return
        # Read-only Hooks can leave an iteration open when the product raises or
        # is cancelled before after_iteration. Unwind only this coroutine's chain.
        if self.state:
            child = self.state.active_spans.get(_span.get())
            chain = []
            while child is not None and child is not self:
                chain.append(child)
                child = self.state.active_spans.get(child.parent_span_id)
            if child is self:
                for descendant in chain:
                    if phase == "end":
                        self.state.collector_errors.append({"type": descendant.kind,
                                                           "error_kind": "UnclosedSpan"})
                    descendant.close("error" if phase == "end" else phase,
                                     reason="parent_unwind")
        self._closed = True
        self.update(**payload)
        if self.state:
            self.payload["duration_ms"] = (time.monotonic_ns() - self.started_ns) / 1e6
            _emit(self.state, self.kind, phase, self.span_id, self.parent_span_id, self.payload)
            if self._token is not None:
                _span.reset(self._token)
            self.state.active_spans.pop(self.span_id, None)

    def __exit__(self, exc_type, exc, traceback):
        phase = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error" if exc else "end"
        self.close(phase, **({"error_kind": type(exc).__name__} if exc else {}))
        return False


def span(kind, **payload):
    return Span(kind, **payload)


def point(kind, **payload):
    state = _observation.get()
    if state:
        _emit(state, kind, "point", uuid.uuid4().hex, _span.get(), payload)


def observed(kind, **payload):
    """Instrument a coroutine without changing arguments, return values or exceptions."""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            if _observation.get() is None:
                return await function(*args, **kwargs)
            with span(kind, **payload):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def model_input_summary(messages, tools=None):
    if _observation.get() is None:
        return {}
    try:
        # Hash the actual supplied payload; never persist prompt text or reasoning fields.
        data = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"input_sha256": hashlib.sha256(data).hexdigest(), "input_bytes": len(data)}
    except Exception:
        return {"input_sha256": None, "input_bytes": None,
                "input_summary_error": "unsupported_payload"}


def usage_payload(usage, source="provider_usage"):
    usage = usage if isinstance(usage, dict) else {}

    def number(*keys):
        for key in keys:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                return value
        return None

    incoming = number("input_tokens", "prompt_tokens")
    outgoing = number("output_tokens", "completion_tokens")
    cached = number("cached_input_tokens", "cached_tokens", "prompt_cache_hit_tokens")
    if cached is not None and (incoming is None or cached > incoming):
        cached = None
    known = incoming is not None and outgoing is not None
    values: dict[str, Any] | None = {
        "input_tokens": incoming, "output_tokens": outgoing, "cached_input_tokens": cached,
    } if incoming is not None or outgoing is not None else None
    return {"usage": values, "usage_status": "known" if known else "partial" if values else "unknown",
            "usage_source": source}

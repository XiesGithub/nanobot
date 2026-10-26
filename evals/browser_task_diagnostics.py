"""Bounded, read-only V073 diagnostics; never converts a failed control to success."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


def _summary(value: Any) -> dict[str, Any]:
    raw = str(value).encode("utf-8", errors="replace")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _url(value: str) -> dict[str, Any]:
    parsed = urlsplit(value)
    # No query, userinfo, data payload, or websocket credentials in diagnostics.
    return {"scheme": parsed.scheme, "host": parsed.hostname,
            "path": parsed.path if parsed.scheme in {"http", "https"} else None,
            "identity": _summary(value)}


class V073Diagnostics:
    def __init__(self, page: Any, output: Path, label: str):
        self.page = page
        self.output = output / f"{label}-{uuid.uuid4().hex}.json"
        self.events: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {}
        self.errors: list[dict[str, str]] = []
        self.listeners: list[tuple[str, Any]] = []

    def _record(self, kind: str, details: dict[str, Any]) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.events.append({"kind": kind, "host_monotonic_ns": time.perf_counter_ns(), **details})
        del self.events[:-256]

    def attach(self) -> None:
        callbacks = {
            "console": lambda item: {"type": item.type, "message": _summary(item.text)},
            "pageerror": lambda item: {"error": _summary(item)},
            "request": lambda item: {"url": _url(item.url), "method": item.method,
                                     "resource_type": item.resource_type},
            "requestfailed": lambda item: {"url": _url(item.url), "failure": item.failure},
            "requestfinished": lambda item: {"url": _url(item.url)},
            "response": lambda item: {"url": _url(item.url), "status": item.status},
            "domcontentloaded": lambda *_args: {}, "load": lambda *_args: {},
            "close": lambda *_args: {}, "crash": lambda *_args: {},
        }
        for kind, callback in callbacks.items():
            def receive(*args: Any, kind: str = kind, callback: Any = callback) -> None:
                try:
                    self._record(kind, callback(*args))
                except Exception as error:
                    self.errors.append({"stage": kind, "error": repr(error)})
            self.page.on(kind, receive)
            self.listeners.append((kind, receive))

    def save(self, original_error: BaseException | None) -> None:
        page_evidence = None
        try:
            page_evidence = self.page.evaluate("window.__nanobotV073Timing || null")
            if page_evidence is None:
                self.errors.append({"stage": "page_snapshot", "error": "timing evidence missing"})
            elif "diagnostics" not in page_evidence:
                self.errors.append({"stage": "page_snapshot", "error": "page diagnostics missing"})
            elif page_evidence.get("diagnostics", {}).get("errors"):
                self.errors.append({"stage": "page_observer", "error": repr(
                    page_evidence["diagnostics"]["errors"])})
        except Exception as error:
            self.errors.append({"stage": "page_snapshot", "error": repr(error)})
        for kind, callback in self.listeners:
            try:
                self.page.remove_listener(kind, callback)
            except Exception as error:
                self.errors.append({"stage": "remove_listener", "error": repr(error)})
        payload = {"schema": "V073-host-diagnostics/1", "complete": not self.errors,
                   "diagnostic_errors": self.errors, "counts": self.counts, "ring_limit": 256,
                   "events": self.events, "page": page_evidence,
                   "original_error": None if original_error is None else {
                       "type": type(original_error).__name__, "message": str(original_error)},
                   "network_zero_is_valid_for_data_url": True,
                   "verdict_authority": False}
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        if self.errors:
            raise RuntimeError(f"V073 diagnostic collection incomplete: {self.output}")


@contextmanager
def collect_v073(page: Any, fallback: Path, label: str) -> Iterator[V073Diagnostics]:
    collector = V073Diagnostics(page, Path(os.environ.get("NANOBOT_V073_DIAGNOSTICS_DIR", fallback)), label)
    original = None
    try:
        try:
            collector.attach()
        except Exception as error:
            collector.errors.append({"stage": "attach", "error": repr(error)})
            raise
        yield collector
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            collector.save(original)
        except Exception as diagnostic_error:
            if original is None:
                raise
            original.add_note(f"V073 diagnostics failed: {diagnostic_error!r}; output={collector.output}")

"""Real Chromium driver for the WebUI browser evaluation cases.

The driver intentionally refuses to substitute a simulated page when Playwright or
the pinned browser is unavailable. It emits observations only; the gateway evidence
and independent scorer decide whether a trial passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA = "browser-task-ui/1.0"
VIEWPORT = {"width": 1280, "height": 800}
V080_VIEWPORT = {"width": 390, "height": 844}
PLAYWRIGHT_VERSION = "1.55.0"
CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)
FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
)
PROMPTS = {
    "V073": "2加3是多少？只回答数字。",
    "V074": "把config.json的timeout改为60，retries保持3。",
    "V078": "返回HISTORY17",
    "V075": "把设备120、运输30整理为报告，先问格式",
    "V076": "读取fact.txt，再写result.txt为73",
    "V077": "返回A=17",
    "V079": "竹桥预算是多少？给出邮件依据",
    "V080": "返回MOBILE17",
}
EXPECTED = {
    "V073": "5", "V074": "60", "V078": "HISTORY17", "V075": "{\"total\":150}",
    "V076": "73", "V077": "A=17", "V079": "120元", "V080": "MOBILE17",
}

V073_TIMING_SCRIPT = r"""(() => {
  const evidence = {click: null, final_dom: null};
  window.__nanobotV073Timing = evidence;
    const diagnostics = {schema: 'V073-page-observation/1', limit: 256, counts: {}, ring: [], errors: []};
    evidence.diagnostics = diagnostics;
    function fingerprint(value) {
      let hash = 2166136261;
      for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
      return {length: value.length, fnv1a32: (hash >>> 0).toString(16).padStart(8, '0')};
    }
    function record(kind, details = {}) {
      try {
        diagnostics.counts[kind] = (diagnostics.counts[kind] || 0) + 1;
        diagnostics.ring.push({kind, performance_ms: performance.now(), ...details});
        if (diagnostics.ring.length > diagnostics.limit) diagnostics.ring.shift();
      } catch (error) { diagnostics.errors.push(String(error)); }
    }
    function targetInfo(target) {
      return {tag: target?.tagName || null, aria_label: target?.getAttribute?.('aria-label') || null,
        disabled: typeof target?.disabled === 'boolean' ? target.disabled : null};
    }
  let scheduled = false;
  let previous = null;
  let stable = 0;
  function sample(now) {
      record('raf_callback', {callback_ms: now});
    scheduled = false;
      if (!evidence.click || evidence.final_dom) { record('sample_skipped', {click: !!evidence.click, final: !!evidence.final_dom}); return; }
    const main = document.querySelector('main');
    const copy = main?.querySelector('button[aria-label="Copy reply"], button[aria-label="Reply copied"]');
    const root = copy?.parentElement?.parentElement;
    const content = root?.firstElementChild;
    const input = document.querySelector('textarea[aria-label="Message input"], input[aria-label="Message input"]');
    const stop = document.querySelector('button[aria-label="Stop response"]');
    const rect = content?.getBoundingClientRect();
    const text = content?.innerText?.trim() || '';
    const visible = !!rect && rect.width > 0 && rect.height > 0
      && getComputedStyle(content).visibility !== 'hidden';
      record('sample_branch', {main: !!main, copy: !!copy, root: !!root, content: !!content,
        input: !!input, stop: !!stop, rect: rect ? {width: rect.width, height: rect.height} : null,
        visibility: content ? getComputedStyle(content).visibility : null, visible,
        input_enabled: !!input && !input.disabled, text: fingerprint(text),
        input_value: input ? fingerprint(input.value || '') : null, stable_before: stable,
        same_previous_text: previous?.text === text});
    if (visible && text && input && !input.disabled && !stop) {
      if (previous?.text === text) stable += 1;
      else { previous = {text, performance_ms: now}; stable = 1; }
        record('stable_progress', {stable});
      if (stable >= 2) {
        evidence.final_dom = {performance_ms: previous.performance_ms,
          confirmed_performance_ms: now, text, visible: true, stable_frames: stable};
          record('final_dom_confirmed', {stable});
        return;
      }
      } else { record('stable_reset', {stable_before: stable}); previous = null; stable = 0; }
    schedule();
  }
  function schedule() {
      record('schedule', {scheduled, click: !!evidence.click, final: !!evidence.final_dom});
    if (!scheduled && evidence.click && !evidence.final_dom) {
      scheduled = true;
        record('raf_requested');
      requestAnimationFrame(sample);
    }
  }
  document.addEventListener('click', event => {
      record('click', {is_trusted: event.isTrusted, event_timestamp: event.timeStamp,
        target: targetInfo(event.target), button: targetInfo(event.target.closest?.('button'))});
    const button = event.target.closest?.('button[aria-label="Send message"]');
    if (!button || evidence.click) return;
    evidence.click = {performance_ms: performance.now(), is_trusted: event.isTrusted,
      target: button.getAttribute('aria-label')};
    schedule();
  }, true);
    for (const kind of ['beforeinput', 'input', 'change']) document.addEventListener(kind, event => {
      record(kind, {is_trusted: event.isTrusted, target: targetInfo(event.target),
        value: fingerprint(event.target?.value || ''), input_type: event.inputType || null});
    }, true);
    for (const kind of ['DOMContentLoaded', 'visibilitychange', 'freeze', 'resume'])
      document.addEventListener(kind, () => record(kind, {visibility: document.visibilityState}));
    for (const kind of ['pageshow', 'pagehide', 'load', 'beforeunload'])
      window.addEventListener(kind, () => record(kind, {visibility: document.visibilityState}));
    new MutationObserver(records => {
      record('mutation', {count: records.length, types: records.reduce((counts, item) => {
        counts[item.type] = (counts[item.type] || 0) + 1; return counts;
      }, {}), attributes: [...new Set(records.map(item => item.attributeName).filter(Boolean))]});
      schedule();
    }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
})();"""


def _v073_clock_sample(page: Any) -> dict[str, Any]:
    before = time.perf_counter_ns()
    browser_ms = page.evaluate("performance.now()")
    after = time.perf_counter_ns()
    return {"host_before_monotonic_ns": before, "browser_performance_ms": browser_ms,
            "host_after_monotonic_ns": after}


def _v073_timing(page: Any, calibration: dict[str, Any]) -> dict[str, Any]:
    raw = page.evaluate("window.__nanobotV073Timing") or {}
    return {**raw, "calibration": calibration}
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _browser_executable() -> Path:
    override = os.environ.get("NANOBOT_EVAL_CHROME")
    candidates = (Path(override),) if override else CHROME_CANDIDATES
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("No fixed local Chrome/Edge executable found; browser trial is unverified")


def _font_fingerprint(page: Any) -> dict[str, Any]:
    font_files = [{"name": item.name, "sha256": _sha256(item)} for item in FONT_CANDIDATES if item.is_file()]
    browser_font = page.evaluate(
        """() => ({family: getComputedStyle(document.body).fontFamily,
                   segoe: document.fonts.check('12px "Segoe UI"'),
                   arial: document.fonts.check('12px Arial')})"""
    )
    return {
        "family_css": browser_font["family"],
        "local_font_files": font_files,
        "browser_font_checks": browser_font,
    }


def parse_viewport(value: str) -> dict[str, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", maxsplit=1))
    except (ValueError, TypeError) as exc:
        raise ValueError("viewport must be WIDTHxHEIGHT") from exc
    if width < 1 or height < 1:
        raise ValueError("viewport dimensions must be positive")
    return {"width": width, "height": height}


def _viewport_for_case(case_id: str, theme: str | None, viewport: str | None) -> tuple[dict[str, int], str]:
    if case_id == "V080":
        if theme not in {"light", "dark"}:
            raise ValueError("V080 requires an explicit --theme light|dark")
        actual = parse_viewport(viewport or "")
        if actual != V080_VIEWPORT:
            raise ValueError("V080 requires the frozen viewport 390x844")
        return actual, theme
    if theme is not None and theme != "light":
        raise ValueError("Only V080 supports an explicit dark theme")
    actual = parse_viewport(viewport) if viewport else VIEWPORT.copy()
    if actual != VIEWPORT:
        raise ValueError("V073-V079 require the frozen viewport 1280x800")
    return actual, "light"


def _mobile_layout_observation(page: Any, phase: str, controls: list[tuple[str, str, Any]]) -> dict[str, Any]:
    observed = []
    for role, name, locator in controls:
        if not locator.count():
            observed.append({"role": role, "name": name, "present": False})
            continue
        item = locator.first.evaluate(
            """el => {
              const r = el.getBoundingClientRect();
              const x = r.left + r.width / 2, y = r.top + r.height / 2;
              const hit = document.elementFromPoint(x, y);
              const disabledPassthrough = el.matches(':disabled') && getComputedStyle(el).pointerEvents === 'none';
              let geometryHit = hit;
              if (disabledPassthrough) {
                const savedStyle = el.getAttribute('style');
                try {
                  el.style.setProperty('pointer-events', 'auto', 'important');
                  geometryHit = document.elementFromPoint(x, y);
                } finally {
                  if (savedStyle === null) el.removeAttribute('style');
                  else el.setAttribute('style', savedStyle);
                }
              }
              return {
                present: true,
                visible: !!(r.width && r.height) && getComputedStyle(el).visibility !== 'hidden',
                bbox: {x: r.x, y: r.y, width: r.width, height: r.height},
                center_hit_tag: hit?.tagName?.toLowerCase() ?? null,
                center_hit_role: hit?.getAttribute('role') ?? null,
                center_hit_name: hit?.getAttribute('aria-label') ?? hit?.innerText?.trim() ?? null,
                disabled_pointer_passthrough: disabledPassthrough,
                geometry_hit_tag: geometryHit?.tagName?.toLowerCase() ?? null,
                occluded: !geometryHit || !(geometryHit === el || el.contains(geometryHit)),
                active_animation_count: (() => {
                  let count = 0;
                  for (let current = el; current && current !== document.body; current = current.parentElement) {
                    count += current.getAnimations().filter(animation => animation.playState === 'running').length;
                  }
                  return count;
                })(),
              };
            }"""
        )
        observed.append({"role": role, "name": name, **item})
    document = page.evaluate(
        """() => {
          const active = document.activeElement;
          return {
            inner_width: window.innerWidth,
            inner_height: window.innerHeight,
            document_scroll_width: document.documentElement.scrollWidth,
            document_client_width: document.documentElement.clientWidth,
            horizontal_overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
            active_element: active ? {
              tag: active.tagName.toLowerCase(), role: active.getAttribute('role'),
              aria_label: active.getAttribute('aria-label'), placeholder: active.getAttribute('placeholder'),
              type: active.getAttribute('type'), text: active.innerText?.trim() ?? null,
            } : null,
            focus_target_present: !!active && !['BODY', 'HTML'].includes(active.tagName),
            theme_dark_class: document.documentElement.classList.contains('dark'),
          };
        }"""
    )
    return {"phase": phase, "document": document, "controls": observed}


def _layout_geometry_signature(layout: dict[str, Any]) -> tuple[Any, ...]:
    controls = tuple(
        (
            item.get("role"), item.get("name"), item.get("present"), item.get("visible"),
            tuple(round(item["bbox"][key], 1) for key in ("x", "y", "width", "height"))
            if item.get("bbox") else None,
            item.get("active_animation_count", 0),
        )
        for item in layout["controls"]
    )
    document = layout["document"]
    return (
        document.get("inner_width"), document.get("inner_height"),
        document.get("document_scroll_width"), document.get("document_client_width"),
        controls,
    )


def _wait_for_mobile_layout_stable(
    page: Any, phase: str, controls: list[tuple[str, str, Any]], timeout_seconds: float = 10,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + timeout_seconds
    previous = None
    stable_samples = 0
    latest = None
    polls = []
    while time.monotonic() < deadline:
        latest = _mobile_layout_observation(page, phase, controls)
        signature = _layout_geometry_signature(latest)
        animations_idle = all(item.get("active_animation_count", 0) == 0
                              for item in latest["controls"] if item.get("present"))
        polls.append({
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "signature": signature,
            "active_animations": [item.get("active_animation_count", 0)
                                  for item in latest["controls"] if item.get("present")],
        })
        if signature == previous and animations_idle:
            stable_samples += 1
            if stable_samples >= 2:
                latest["geometry_stability"] = {
                    "stable": True,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "consecutive_stable_samples": stable_samples + 1,
                    "polls": polls,
                }
                return latest
        else:
            previous = signature
            stable_samples = 0
        page.wait_for_timeout(50)
    if latest is None:
        latest = _mobile_layout_observation(page, phase, controls)
    latest["geometry_stability"] = {
        "stable": False,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "consecutive_stable_samples": stable_samples,
        "polls": polls,
    }
    return latest


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, "<redacted>" if re.search(r"token|secret|auth|key|signature", key, re.I) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _safe_diagnostic(value: Any, secret: str | None = None) -> str:
    text = str(value)
    text = re.sub(
        r"(?i)([?&](?:token|access_token|secret|password|authorization|api_key)=)[^&\s'\"<>]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<redacted>", text)
    text = re.sub(r"\bnbwt_[A-Za-z0-9_-]+", "<redacted-session-token>", text)
    if secret:
        text = text.replace(secret, "<redacted>")
    return text


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _save_failure(
    out: Path, page: Any, case_id: str, profile: str, control: str | None,
    executable: Path, browser_version: str, actions: list[dict[str, Any]],
    requests: list[dict[str, Any]], ws_frames: list[dict[str, Any]],
    console_errors: list[str], page_errors: list[str], snapshots: dict[str, Any],
    dom_artifacts: list[dict[str, str]], screenshots: list[dict[str, str]], error: Exception,
    viewport: dict[str, int] | None = None, theme: str = "light",
    diagnostic_secret: str | None = None,
) -> None:
    try:
        font_fingerprint = _font_fingerprint(page)
        font_status = "observed"
    except Exception as font_error:
        font_fingerprint = None
        font_status = f"unavailable:{type(font_error).__name__}"
    try:
        html = page.locator("body").evaluate("""el => {
          const clone = el.cloneNode(true);
          for (const input of clone.querySelectorAll('input[type="password"]')) {
            input.removeAttribute('value');
            input.value = '[redacted]';
          }
          return clone.outerHTML;
        }""")
        dom = out / "dom-failure.html"
        dom.write_text(html, encoding="utf-8")
        dom_artifacts.append({"path": dom.name, "sha256": _sha256(dom)})
    except Exception:
        pass
    try:
        image = out / "screen-failure.png"
        page.screenshot(path=str(image), full_page=True)
        screenshots.append({"path": image.name, "sha256": _sha256(image)})
    except Exception:
        pass
    evidence = {
        "schema": SCHEMA, "case_id": case_id, "profile": profile, "trial_id": out.name,
        "browser": {"name": "Chromium", "version": browser_version,
                    "executable": str(executable), "executable_sha256": _sha256(executable),
                    "viewport": viewport or VIEWPORT, "theme": theme,
                    "playwright_version": PLAYWRIGHT_VERSION,
                    "font_fingerprint": font_fingerprint,
                    "font_fingerprint_status": font_status},
        "actions": actions, "snapshots": snapshots, "dom_artifacts": dom_artifacts,
        "screenshots": screenshots, "console_errors": console_errors,
        "page_errors": page_errors, "requests": requests, "ws_frames": ws_frames,
        "control": control,
        "failure": {"type": type(error).__name__,
                    "message": _safe_diagnostic(error, diagnostic_secret)},
    }
    _write_json(out / "browser-evidence.json", evidence)


def _classify_message_candidates(
    candidates: list[dict[str, Any]], user_texts: set[str]
) -> list[dict[str, Any]]:
    """Classify real message containers while preserving their source paragraphs.

    A completed assistant message is anchored by its accessible Copy reply action;
    a user message is identified only by an exact known submitted string. A row with
    neither signal remains unknown instead of being guessed from its paragraph.
    """
    bubbles = []
    for candidate in candidates:
        paragraphs = candidate.get("paragraphs", [])
        paragraph_texts = [str(row.get("text", "")).strip() for row in paragraphs]
        paragraph_texts = [value for value in paragraph_texts if value]
        if candidate.get("copy_reply_root"):
            role = "assistant"
            text = str(candidate.get("markdown_text", "")).strip()
            source = "copy_reply_root"
        else:
            text = "\n".join(paragraph_texts)
            if text in user_texts:
                role = "user"
                source = "exact_submitted_text"
            else:
                role = "unknown"
                source = str(candidate.get("source", "unclassified_structure"))
        if not text and not paragraphs:
            continue
        bubble = {
            "role": role,
            "text": text,
            "message_container_path": candidate.get("container_path", []),
            "paragraph_records": paragraphs,
            "classification_source": source,
            "structure_confidence": candidate.get("structure_confidence", "structural"),
        }
        if paragraphs:
            bubble["paragraph_index"] = paragraphs[0].get("index")
        bubbles.append(bubble)
    return bubbles


def _user_texts_for_case(case_id: str, mail_fixture: dict[str, Any] | None = None) -> set[str]:
    known = {PROMPTS[case_id]}
    if case_id == "V075":
        known.add("JSON")
    elif case_id == "V076":
        known.add("继续完成刚才的任务")
    elif case_id == "V077":
        known.add("返回B=23")
    elif case_id == "V079" and mail_fixture and mail_fixture.get("question"):
        known.add(str(mail_fixture["question"]))
    return known


def _visible_bubbles(page: Any, user_texts: set[str] | None = None) -> list[dict[str, Any]]:
    # Use the actual conversation row as the unit. Paragraphs inside one Markdown
    # reply are retained as evidence but never promoted to separate messages.
    candidates = page.evaluate(
        """() => {
          const main = document.querySelector('main');
          if (!main) return [];
          const excluded = (element) => element.closest(
            '[role="group"][aria-label="Question"], [role="dialog"]'
          );
          const pathOf = (element) => {
            const path = [];
            for (let current = element; current && current !== main;
                 current = current.parentElement) {
              const parent = current.parentElement;
              path.unshift({tag: current.tagName.toLowerCase(), index: parent
                ? [...parent.children].indexOf(current) : 0});
            }
            return path;
          };
          const candidates = new Map();
          const paragraphRows = (root) => [
            ...(root.tagName === 'P' ? [root] : []),
            ...root.querySelectorAll('p'),
          ]
            .filter((paragraph) => !excluded(paragraph))
            .map((paragraph) => ({text: (paragraph.innerText || '').trim(), path: pathOf(paragraph)}))
            .filter((row) => row.text);
          const structuralRoot = (paragraph) => {
            let node = paragraph;
            while (node?.parentElement && node.parentElement !== main) {
              const parent = node.parentElement;
              // Only sibling block containers indicate conversation rows. A prose
              // block with several <p> children is one message, not several rows.
              const rows = [...parent.children].filter((child) =>
                child.tagName === 'DIV' && child.querySelector('p') && !excluded(child)
              );
              if (rows.length >= 2) return node;
              node = parent;
            }
            return null;
          };
          const ensure = (root, source, confidence) => {
            if (!candidates.has(root)) {
              candidates.set(root, {
                root,
                source,
                container_path: pathOf(root),
                paragraphs: paragraphRows(root),
                copy_reply_root: false,
                markdown_text: '',
                structure_confidence: confidence,
              });
            }
            return candidates.get(root);
          };

          const allParagraphs = [...main.querySelectorAll('p')];
          allParagraphs.forEach((paragraph, index) => {
            if (excluded(paragraph)) return;
            const root = structuralRoot(paragraph);
            if (root) {
              ensure(root, 'sibling-message-rows', 'structural-message-row');
            } else {
              // Keep an unanchored paragraph visible as an unknown record; Python
              // may classify it as a user only when its text exactly matches a
              // known submission. It is never inferred to be an assistant turn.
              ensure(paragraph, 'paragraph-only-fallback', 'paragraph-only');
            }
          });

          const replyButtons = main.querySelectorAll(
            'button[aria-label="Copy reply"], button[aria-label="Reply copied"]'
          );
          for (const button of replyButtons) {
            if (excluded(button)) continue;
            const root = button.parentElement?.parentElement;
            if (!root || !main.contains(root)) continue;
            const candidate = ensure(root, 'copy_reply_action', 'assistant-action-root');
            candidate.copy_reply_root = true;
            candidate.markdown_text = (root.firstElementChild?.innerText || '').trim();
            const markdown = root.firstElementChild;
            candidate.paragraphs = markdown ? paragraphRows(markdown) : [];
          }

          const ordered = [...candidates.values()].sort((a, b) => {
            if (a.root === b.root) return 0;
            return a.root.compareDocumentPosition(b.root) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
          });
          return ordered.map((candidate) => ({
            source: candidate.source,
            copy_reply_root: candidate.copy_reply_root,
            markdown_text: candidate.markdown_text,
            container_path: candidate.container_path,
            paragraphs: candidate.paragraphs.map((row) => ({
              ...row,
              index: allParagraphs.findIndex((paragraph) => pathOf(paragraph).length === row.path.length
                && JSON.stringify(pathOf(paragraph)) === JSON.stringify(row.path)),
            })),
            structure_confidence: candidate.structure_confidence,
          }));
        }""",
    )
    return _classify_message_candidates(candidates, user_texts or set())


def _generating(page: Any) -> bool:
    return page.get_by_role("button", name="Stop response", exact=True).count() > 0


def _source_panel(page: Any) -> dict[str, Any] | None:
    dialog = page.get_by_role("dialog")
    if not dialog.count() or not dialog.first.is_visible():
        return None
    alert = dialog.first.get_by_role("alert")
    return {
        "visible": True,
        "text": dialog.first.inner_text(),
        "alert": alert.first.inner_text().strip() if alert.count() else None,
    }


def _snapshot(
    page: Any, chat_id: str | None, ws_frames: list[dict[str, Any]] | None = None,
    user_texts: set[str] | None = None,
) -> dict[str, Any]:
    composer = page.get_by_role("textbox", name="Message input", exact=True).first
    visible_bubbles = _visible_bubbles(page, user_texts)
    paragraph_records = [
        {
            "bubble_index": bubble_index,
            "role": bubble["role"],
            "bubble_text": bubble["text"],
            "container_path": bubble["message_container_path"],
            **paragraph,
        }
        for bubble_index, bubble in enumerate(visible_bubbles)
        for paragraph in bubble.get("paragraph_records", [])
    ]
    trace_summaries = []
    for button in page.get_by_role("button", name=re.compile(r"^Used \d+ tools?$", re.I)).all():
        label = button.inner_text().strip()
        match = re.search(r"(\d+)", label)
        expanded = button.get_attribute("aria-expanded") == "true"
        rows: list[str] = []
        if expanded:
            rows = button.locator("xpath=..").get_by_role("list").get_by_role("listitem").all_text_contents()
        frame_ids = []
        for frame in ws_frames or []:
            payload = frame.get("payload")
            if isinstance(payload, dict):
                call_id = payload.get("tool_call_id") or payload.get("call_id")
                if call_id and call_id not in frame_ids:
                    frame_ids.append(call_id)
        trace_summaries.append({
            "label": label,
            "count": int(match.group(1)) if match else None,
            "expanded": expanded,
            "lines": [line.strip() for line in rows],
            "gateway_call_ids_observed_on_wire": frame_ids,
        })
    question = page.get_by_role("group", name="Question", exact=True)
    tool_rows = [{
        "call_id": row.get_attribute("data-tool-call-id"),
        "phase": row.get_attribute("data-tool-phase"),
        "execution_started": row.get_attribute("data-execution-started") == "true",
        "turn_id": row.get_attribute("data-turn-id"),
        "text": row.inner_text(),
    } for row in page.locator("[data-tool-call-id]").all()]
    ask_user = None
    if question.count():
        ask_user = {
            "group_visible": question.is_visible(),
            "question": question.locator("p").first.inner_text().strip(),
            "options": [name.strip() for name in question.get_by_role("button").all_inner_texts()],
        }
    return {
        "url": page.url,
        "chat_id": chat_id,
        "visible_bubbles": visible_bubbles,
        "paragraph_records": paragraph_records,
        "bubble_classification": {
            "assistant": "MessageBubble root anchored by accessible Copy reply/Reply copied button; text is its first Markdown child innerText",
            "user": "exact match against known submitted input within a structurally grouped message row",
            "unknown": "message row without reliable assistant or user evidence remains unknown",
            "excluded": "question groups and dialogs are outside visible_bubbles; full DOM is preserved as an artifact",
        },
        "composer_enabled": composer.is_enabled() if composer.count() else False,
        "generating": _generating(page),
        "trace_summaries": trace_summaries,
        "tool_rows": tool_rows,
        "ask_user": ask_user,
        "source_panel": _source_panel(page),
        "theme_dark_class": page.evaluate("document.documentElement.classList.contains('dark')"),
    }


def _history_count(
    control_url: str | None, chat_id: str | None, probe_id: str | None = None
) -> dict[str, Any] | None:
    if not control_url:
        return None
    # The gateway's control endpoint is loopback-only and returns counts, not bodies.
    import urllib.request

    query = []
    if chat_id:
        query.append(f"chat_id={chat_id}")
    if probe_id:
        query.append(f"probe_id={probe_id}")
    delimiter = "&" if "?" in control_url else "?"
    request = urllib.request.Request(
        f"{control_url}{delimiter}{'&'.join(query)}" if query else control_url,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return {"status": response.status, "payload": payload}


def _read_json_url(url: str) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as response:
        return {"status": response.status, "payload": json.loads(response.read().decode("utf-8"))}


def _read_json_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _post_json_url(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    import urllib.request

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return {"status": response.status, "payload": json.loads(response.read().decode("utf-8"))}


def _poll_gate(gate_state_url: str, gate_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = _read_json_url(gate_state_url)
        for gate in last["payload"].get("gates", []):
            if gate.get("gate_id") == gate_id and gate.get("state") == "waiting":
                return last
        time.sleep(0.05)
    raise TimeoutError(f"Gateway gate {gate_id} did not enter waiting state: {last}")


def _session_file_for_chat(observation: dict[str, Any], chat_id: str) -> dict[str, Any] | None:
    payload = observation.get("payload", {})
    return next((row for row in payload.get("session_files", [])
                 if isinstance(row, dict)
                 and (row.get("session_id") == chat_id
                      or str(row.get("session_id", "")).endswith(f":{chat_id}"))), None)


def _cancelled_session_is_restored(observation: dict[str, Any], chat_id: str) -> bool:
    row = _session_file_for_chat(observation, chat_id)
    if not row or row.get("checkpoint_present") is not False or row.get("checkpoint_id") is not None:
        return False
    text = json.dumps(row.get("jsonl", []), ensure_ascii=False)
    return ("read_file" in text and "值73" in text
            and "Error: Task interrupted before this tool finished." in text)


def _cancelled_session_is_persisted(observation: dict[str, Any], chat_id: str) -> bool:
    row = _session_file_for_chat(observation, chat_id)
    if not row or row.get("checkpoint_present") is not False or not row.get("sha256"):
        return False
    records = [entry for entry in row.get("jsonl", [])
               if isinstance(entry, dict) and entry.get("_type") != "metadata"]
    return len(records) >= 2


def _cancel_complete(
    observation: dict[str, Any], gate_state: dict[str, Any], gate_id: str, chat_id: str
) -> bool:
    payload = observation.get("payload", {})
    gate = next((row for row in gate_state.get("payload", {}).get("gates", [])
                 if row.get("gate_id") == gate_id), {})
    state = payload.get("cancellation_state", {})
    return (
        gate.get("state") == "cancelled"
        and gate.get("cancellations", 0) >= 1
        and state.get("active_task_count") == 0
        and _cancelled_session_is_persisted(observation, chat_id)
    )


def _wait_cancel_complete(
    control_url: str, gate_state_url: str, chat_id: str, gate_id: str,
    timeout_seconds: float = 30,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout_seconds
    polls: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        probe_id = f"v076-stop-poll-{len(polls) + 1:03d}"
        observation = _history_count(control_url, chat_id, probe_id)
        gate_state = _read_json_url(gate_state_url)
        session_file = _session_file_for_chat(observation, chat_id)
        polls.append({
            "probe_id": probe_id,
            "active_task_count": observation["payload"].get("cancellation_state", {}).get("active_task_count"),
            "checkpoint_ids": observation["payload"].get("checkpoint_ids", []),
            "session_id": session_file.get("session_id") if session_file else None,
            "checkpoint_present": session_file.get("checkpoint_present") if session_file else None,
            "session_sha256": session_file.get("sha256") if session_file else None,
            "persisted_partial_context": _cancelled_session_is_persisted(observation, chat_id),
            "restored_partial_context": _cancelled_session_is_restored(observation, chat_id),
            "gate_state": next((row.get("state") for row in gate_state["payload"].get("gates", [])
                                if row.get("gate_id") == gate_id), None),
        })
        if _cancel_complete(observation, gate_state, gate_id, chat_id):
            return observation, gate_state, polls
        time.sleep(0.05)
    raise TimeoutError(f"Cancellation did not reach a persisted idle checkpoint state: {polls[-1:]}")


def run_browser(
    case_id: str,
    profile: str,
    url: str,
    output: str | Path,
    control: str | None = None,
    control_url: str | None = None,
    gate_state_url: str | None = None,
    gate_release_url: str | None = None,
    v077_a_chat_id: str | None = None,
    v077_b_chat_id: str | None = None,
    ready_json: str | Path | None = None,
    theme: str | None = None,
    viewport: str | None = None,
) -> dict[str, Any]:
    if case_id not in PROMPTS:
        raise ValueError(f"Unsupported browser case: {case_id}")
    actual_viewport, actual_theme = _viewport_for_case(case_id, theme, viewport)
    ready = _read_json_file(ready_json) if ready_json else {}
    mail_bootstrap = ready.get("mail_bootstrap") or {}
    mail_fixture = ready.get("mail_fixture") or {}
    if case_id == "V079" and not (
        ready_json and mail_bootstrap.get("login_secret") and mail_bootstrap.get("url")
        and mail_fixture.get("question") and mail_fixture.get("subject")
        and mail_fixture.get("expected_source_excerpt")
        and mail_bootstrap.get("permission_revoke_url")
        and control_url
    ):
        raise ValueError("V079 requires its isolated gateway ready.json bootstrap and mail fixture")
    cached_deps = Path(__file__).resolve().parents[1] / ".tmp" / "browser-python"
    if cached_deps.is_dir() and str(cached_deps) not in os.sys.path:
        os.sys.path.insert(0, str(cached_deps))
    try:
        from importlib.metadata import version as package_version

        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright 1.55.0 is required; no simulated browser fallback is allowed") from exc
    if package_version("playwright") != PLAYWRIGHT_VERSION:
        raise RuntimeError(f"Pinned Playwright {PLAYWRIGHT_VERSION} required")

    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    requests: list[dict[str, Any]] = []
    ws_frames: list[dict[str, Any]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    actions: list[dict[str, Any]] = []
    snapshots: dict[str, Any] = {}
    dom_artifacts: list[dict[str, str]] = []
    screenshots: list[dict[str, str]] = []
    origin = time.monotonic()
    executable = _browser_executable()

    def elapsed() -> int:
        return round((time.monotonic() - origin) * 1000)

    def record_action(role: str, name: str, action: str) -> None:
        actions.append({"role": role, "name": name, "action": action, "monotonic_ms": elapsed()})

    case_user_texts = _user_texts_for_case(case_id, mail_fixture)

    def save_snapshot(page: Any, name: str, chat_id: str | None) -> None:
        html = page.locator("body").evaluate("el => el.outerHTML")
        artifact = out / f"dom-{name}.html"
        artifact.write_text(html, encoding="utf-8")
        dom_artifacts.append({"path": artifact.name, "sha256": _sha256(artifact)})
        image = out / f"screen-{name}.png"
        page.screenshot(path=str(image), full_page=True)
        screenshots.append({"path": image.name, "sha256": _sha256(image)})
        snapshots[name] = _snapshot(page, chat_id, ws_frames, case_user_texts)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(executable), args=["--no-sandbox"])
        version = browser.version
        context = browser.new_context(viewport=actual_viewport, locale="en-US", timezone_id="UTC")
        if case_id == "V073":
            context.add_init_script(V073_TIMING_SCRIPT)
        if case_id == "V080":
            context.add_init_script(
                "localStorage.setItem('nanobot-webui.theme', " + json.dumps(actual_theme) + ");"
            )
        page = context.new_page()
        diagnostic_secret = mail_bootstrap.get("login_secret") if case_id == "V079" else None
        page.on("console", lambda message: console_errors.append(
            _safe_diagnostic(message.text, diagnostic_secret)
        ) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(
            _safe_diagnostic(error, diagnostic_secret)
        ))
        # Keep the actual owner credential only in memory for a post-refresh
        # history denial probe. Never serialize it into browser evidence.
        source_auth: dict[str, str] = {}

        def observe_request(request):
            _record_request(requests, request)
            if case_id == "V079" and "/api/mail-knowledge/sources/" in request.url:
                source_auth["authorization"] = request.headers.get("authorization", "")

        page.on("request", observe_request)
        page.on("response", lambda response: _record_response(requests, response))
        page.on("requestfinished", lambda request: _record_request_finished(requests, request))
        page.on("websocket", lambda socket: _record_socket(socket, ws_frames))

        def guarded(operation: Any) -> Any:
            try:
                return operation()
            except Exception as exc:
                if case_id == "V073":
                    try:
                        calibration = snapshots.get("v073_timing", {}).get("calibration", {})
                        snapshots["v073_timing"] = _v073_timing(page, calibration)
                    except Exception:
                        pass
                _save_failure(out, page, case_id, profile, control, executable, browser.version,
                              actions, requests, ws_frames, console_errors, page_errors,
                              snapshots, dom_artifacts, screenshots, exc, actual_viewport, actual_theme,
                              diagnostic_secret)
                browser.close()
                raise

        guarded(lambda: page.goto(url, wait_until="domcontentloaded", timeout=30000))
        if case_id == "V078":
            guarded(lambda: save_snapshot(page, "start", None))
        if case_id == "V079":
            auth_password = page.get_by_role("textbox", name="Password", exact=True)
            guarded(lambda: auth_password.wait_for(timeout=30000))
            guarded(lambda: auth_password.fill(mail_bootstrap["login_secret"]))
            record_action("textbox", "Password", "fill-redacted-owner-secret")
            guarded(lambda: page.get_by_role("button", name="Connect", exact=True).click())
            record_action("button", "Connect", "click-owner-bootstrap")
        guarded(lambda: page.get_by_role("textbox", name="Message input", exact=True).first.wait_for(timeout=30000))
        if case_id == "V080":
            actual_dark = guarded(lambda: page.evaluate("document.documentElement.classList.contains('dark')"))
            guarded(lambda: _require(actual_dark == (actual_theme == "dark"),
                                     "The requested frozen V080 theme was not applied by the WebUI"))
        if case_id == "V077":
            guarded(lambda: page.get_by_role("button", name="V077-A", exact=True).wait_for(timeout=30000))
            guarded(lambda: page.get_by_role("button", name="V077-B", exact=True).wait_for(timeout=30000))
        if case_id != "V078":
            guarded(lambda: save_snapshot(page, "before", None))

        if case_id == "V077":
            guarded(lambda: _require(bool(v077_a_chat_id and v077_b_chat_id
                                         and v077_a_chat_id != v077_b_chat_id),
                                     "V077 requires distinct pre-seeded empty A and B chat IDs"))
            guarded(lambda: page.get_by_role("button", name="V077-A", exact=True).wait_for(timeout=30000))
            guarded(lambda: page.get_by_role("button", name="V077-B", exact=True).wait_for(timeout=30000))
            guarded(lambda: page.get_by_role("button", name="V077-A", exact=True).click())
            record_action("button", "V077-A", "click-select-a")
            guarded(lambda: _wait_for_chat_attached(page, ws_frames, v077_a_chat_id, 0))
        if case_id == "V080":
            nav = page.get_by_role("navigation", name="Sidebar navigation", exact=True)
            sidebar_toggle = page.get_by_role("button", name="Toggle sidebar", exact=True)
            guarded(lambda: sidebar_toggle.click())
            record_action("button", "Toggle sidebar", "open-sidebar")
            guarded(lambda: nav.wait_for(state="visible", timeout=10000))
            observations = snapshots.setdefault("layout_observations", [])
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "sidebar_open", [
                ("button", "New chat", nav.get_by_role("button", name="New chat", exact=True)),
            ]))
            observations.append(layout)
            guarded(lambda: _assert_mobile_layout(layout))
            guarded(lambda: save_snapshot(page, "mobile_sidebar_open", None))
            new_chat = nav.get_by_role("button", name="New chat", exact=True)
            guarded(new_chat.click)
            record_action("button", "New chat", "start-new-mobile-session")
            guarded(lambda: nav.wait_for(state="hidden", timeout=10000))
            textbox = page.get_by_role("textbox", name="Message input", exact=True).first
            guarded(lambda: textbox.wait_for(state="visible", timeout=10000))
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "new_session_sidebar_closed", [
                ("button", "Toggle sidebar", sidebar_toggle),
                ("textbox", "Message input", textbox),
                ("button", "Send message", page.get_by_role("button", name="Send message", exact=True)),
            ]))
            observations.append(layout)
            guarded(lambda: _assert_mobile_layout(layout))
            guarded(lambda: save_snapshot(page, "mobile_new_session", None))
            snapshots["mobile_new_session_state"] = {
                "chat_id": None,
                "state": "unsaved-new-chat-draft",
                "business_bubbles": snapshots["mobile_new_session"]["visible_bubbles"],
            }
            guarded(lambda: _require(not snapshots["mobile_new_session_state"]["business_bubbles"],
                                     "New mobile chat draft unexpectedly contains business messages"))
        prompt = str(mail_fixture["question"]) if case_id == "V079" else PROMPTS[case_id]
        textbox = page.get_by_role("textbox", name="Message input", exact=True).first
        guarded(lambda: textbox.fill(prompt))
        record_action("textbox", "Message input", "fill-a" if case_id == "V077" else "fill")
        terminal_count_before_send = _turn_end_count(ws_frames)
        button = page.get_by_role("button", name="Send message", exact=True)
        if case_id == "V073":
            calibration = {"clock": "same-host Python perf_counter_ns mapped to browser performance.now",
                           "samples": [guarded(lambda: _v073_clock_sample(page)) for _ in range(3)]}
            snapshots["v073_timing"] = {"click": None, "final_dom": None, "calibration": calibration}
        guarded(button.click)
        record_action("button", "Send message", "click-a" if case_id == "V077" else "click")

        chat_id = _current_chat_id(requests, ws_frames, prompt)
        if case_id == "V077":
            guarded(lambda: _require(bool(gate_state_url and gate_release_url),
                                     "V077 requires gateway gate state and release URLs"))
            chat_a = guarded(lambda: _wait_for_submitted_chat_id(page, ws_frames, prompt))
            guarded(lambda: _require(chat_a == v077_a_chat_id,
                                     "A task submission did not use the pre-seeded A session"))
            gate_a_waiting = guarded(lambda: _poll_gate(gate_state_url, "v077-A"))
            snapshots["gate_a_waiting"] = gate_a_waiting
            snapshots["chat_ids"] = {"a": chat_a, "b": v077_b_chat_id}
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "a_waiting", "probe": "a_gate_waiting",
                    "raw": guarded(lambda: _history_count(control_url, chat_a, "v077-a-waiting")),
                })
            guarded(lambda: save_snapshot(page, "a_waiting", chat_a))

            attached_before_b = _attached_count(ws_frames, v077_b_chat_id)
            guarded(lambda: page.get_by_role("button", name="V077-B", exact=True).click())
            record_action("button", "V077-B", "click-select-b")
            guarded(lambda: _wait_for_chat_attached(page, ws_frames, v077_b_chat_id, attached_before_b))
            textbox = page.get_by_role("textbox", name="Message input", exact=True).first
            b_prompt = "返回B=23"
            guarded(lambda: textbox.fill(b_prompt))
            record_action("textbox", "Message input", "fill-b")
            guarded(lambda: page.get_by_role("button", name="Send message", exact=True).click())
            record_action("button", "Send message", "click-b")
            gate_b_waiting = guarded(lambda: _poll_gate(gate_state_url, "v077-B"))
            chat_b = guarded(lambda: _wait_for_submitted_chat_id(page, ws_frames, b_prompt))
            guarded(lambda: _require(chat_b == v077_b_chat_id,
                                     "B task submission did not use the pre-seeded B session"))
            snapshots["gate_b_waiting"] = gate_b_waiting
            snapshots["chat_ids"] = {"a": chat_a, "b": chat_b}
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "b_waiting", "probe": "b_gate_waiting",
                    "raw": guarded(lambda: _history_count(control_url, chat_b, "v077-b-waiting")),
                })
            guarded(lambda: save_snapshot(page, "b_waiting", chat_b))
            terminal_count_before = _turn_end_count(ws_frames, chat_b)
            release_b = guarded(lambda: _post_json_url(gate_release_url, {"gate_id": "v077-B"}))
            snapshots["gate_b_release"] = release_b
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000,
                                               after_count=terminal_count_before, chat_id=chat_b))
            guarded(lambda: _wait_for_assistant_text(page, "B=23", case_user_texts))
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "b_complete", "probe": "b_history_selected",
                    "raw": guarded(lambda: _history_count(control_url, chat_b, "v077-b-selected")),
                })
            guarded(lambda: save_snapshot(page, "b_complete", chat_b))
            a_state_after_b = guarded(lambda: _read_json_url(gate_state_url))
            snapshots["gate_a_after_b"] = a_state_after_b
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "b_complete_a_still_waiting", "probe": "b_complete_a_waiting",
                    "raw": guarded(lambda: _history_count(control_url, chat_a, "v077-a-still-waiting")),
                })
            guarded(lambda: _require(
                any(row.get("gate_id") == "v077-A" and row.get("state") == "waiting"
                    for row in a_state_after_b.get("payload", {}).get("gates", [])),
                "A gate left waiting before B completed",
            ))
            guarded(lambda: save_snapshot(page, "b_selected_during_a", chat_b))
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "b_selected_during_a", "probe": "v077-b-selected-during-a",
                    "raw": guarded(lambda: _history_count(control_url, chat_b, "v077-b-selected-during-a")),
                })

        if case_id == "V075":
            question = page.get_by_role("group", name="Question", exact=True)
            guarded(lambda: question.wait_for(timeout=60000))
            guarded(lambda: save_snapshot(page, "ask_wait", chat_id))
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "ask_wait", "probe": "before_json_choice",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v075-ask-wait")),
                })
            terminal_count_before = _turn_end_count(ws_frames)
            guarded(lambda: page.get_by_role("button", name="JSON", exact=True).click())
            record_action("button", "JSON", "click-ask-user-option")
            snapshots["ask_user_selected"] = "JSON"
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000, after_count=terminal_count_before))
            if control_url:
                snapshots["control_observations"].append({
                    "phase": "after", "probe": "after_resume",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v075-after-json")),
                })
            guarded(lambda: save_snapshot(page, "after", chat_id))
        elif case_id == "V076":
            guarded(lambda: _require(bool(gate_state_url and control_url),
                                     "V076 requires gateway gate_state_url and control_url"))
            gate_before = guarded(lambda: _poll_gate(gate_state_url, "v076-cancel-gate"))
            chat_id = guarded(lambda: _wait_for_submitted_chat_id(page, ws_frames, PROMPTS[case_id]))
            snapshots["gate_waiting"] = gate_before
            before_stop = guarded(lambda: _history_count(control_url, chat_id, "v076-before-stop"))
            snapshots.setdefault("control_observations", []).append({
                "phase": "gate_waiting", "probe": "before_stop", "raw": before_stop,
            })
            checkpoint_file = _session_file_for_chat(before_stop, chat_id)
            guarded(lambda: _require(
                bool(checkpoint_file and checkpoint_file.get("checkpoint_present")),
                "Gate waiting probe did not capture the matching runtime checkpoint",
            ))
            snapshots["checkpoint_at_gate_waiting"] = {
                "session_id": checkpoint_file.get("session_id"),
                "checkpoint_id": checkpoint_file.get("checkpoint_id"),
                "sha256": checkpoint_file.get("sha256"),
            }
            guarded(lambda: save_snapshot(page, "gate_waiting", chat_id))
            guarded(lambda: page.get_by_role("button", name="Stop response", exact=True).click())
            record_action("button", "Stop response", "click-stop")
            guarded(lambda: page.get_by_role("button", name="Stop response", exact=True).wait_for(state="detached", timeout=10000))
            textbox = page.get_by_role("textbox", name="Message input", exact=True).first
            guarded(lambda: _assert_input_enabled(textbox))
            cancel_observation, cancel_gate_state, cancel_polls = guarded(
                lambda: _wait_cancel_complete(control_url, gate_state_url, chat_id, "v076-cancel-gate")
            )
            snapshots["gate_cancelled"] = cancel_gate_state
            snapshots["stop_poll_observations"] = cancel_polls
            snapshots.setdefault("control_observations", []).append({
                "phase": "stop_completed", "probe": "cancelled_before_continue",
                "raw": cancel_observation,
            })
            cancel_session = _session_file_for_chat(cancel_observation, chat_id)
            guarded(lambda: _require(
                bool(cancel_session and _cancelled_session_is_persisted(cancel_observation, chat_id)),
                "Cancelled session partial history was not persisted before Continue",
            ))
            guarded(lambda: save_snapshot(page, "cancelled", chat_id))
            before_continue = guarded(lambda: _history_count(control_url, chat_id, "v076-before-continue"))
            snapshots["control_observations"].append({
                "phase": "before_continue", "probe": "checkpoint_before_continue",
                "raw": before_continue,
            })
            before_continue_session = _session_file_for_chat(before_continue, chat_id)
            guarded(lambda: _require(
                bool(before_continue_session
                     and before_continue_session.get("sha256") == cancel_session.get("sha256")
                     and before_continue_session.get("checkpoint_present") is False
                     and not before_continue["payload"].get("workspace_state", {}).get("result.txt", {}).get("exists")),
                "Cancelled partial context changed before Continue or result.txt already exists",
            ))
            guarded(lambda: textbox.fill("继续完成刚才的任务"))
            record_action("textbox", "Message input", "fill-continue")
            terminal_count_before = _turn_end_count(ws_frames)
            guarded(lambda: page.get_by_role("button", name="Send message", exact=True).click())
            record_action("button", "Send message", "click-continue")
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000, after_count=terminal_count_before))
            if control_url:
                snapshots["control_observations"].append({
                    "phase": "after", "probe": "continue_completed",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v076-after-continue")),
                })
            guarded(lambda: save_snapshot(page, "after", chat_id))
        elif case_id == "V077":
            chat_b = snapshots["chat_ids"]["b"]
            chat_a = snapshots["chat_ids"]["a"]
            guarded(lambda: _require(bool(chat_a and chat_b and chat_a != chat_b),
                                     "V077 requires distinct observed A and B session IDs"))
            terminal_count_before = _turn_end_count(ws_frames, chat_a)
            release_a = guarded(lambda: _post_json_url(gate_release_url, {"gate_id": "v077-A"}))
            snapshots["gate_a_release"] = release_a
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000,
                                               after_count=terminal_count_before, chat_id=chat_a))
            guarded(lambda: save_snapshot(page, "a_complete_offscreen", chat_b))
            attached_before_a = _attached_count(ws_frames, chat_a)
            history_before_a = _history_request_count(requests, chat_a)
            guarded(lambda: page.get_by_role("button", name="V077-A", exact=True).click())
            record_action("button", "V077-A", "click-switch-to-a")
            guarded(lambda: _wait_for_chat_attached(page, ws_frames, chat_a, attached_before_a))
            guarded(lambda: _wait_for_history_request(requests, chat_a, history_before_a, page))
            history_response = guarded(
                lambda: _wait_for_history_response(requests, chat_a, history_before_a, page)
            )
            answer_visible = guarded(lambda: _has_assistant_text(page, "A=17", case_user_texts))
            guarded(lambda: save_snapshot(page, "a_selected", chat_a))
            snapshots["a_selected"]["history_http_response"] = history_response
            snapshots["a_selected"]["required_answer_check"] = {
                "expected": "A=17",
                "observed": answer_visible,
                "status": "observed" if answer_visible else "missing_after_completed_http_200",
            }
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "a_selected", "probe": "a_after_release",
                    "raw": guarded(lambda: _history_count(control_url, chat_a, "v077-a-selected")),
                })
                snapshots["a_selected"]["control_observation"] = snapshots["control_observations"][-1]["raw"]
        elif case_id == "V079":
            chat_id = guarded(lambda: _wait_for_submitted_chat_id(page, ws_frames, prompt))
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000,
                                               after_count=terminal_count_before_send, chat_id=chat_id))
            guarded(lambda: _wait_for_assistant_text(page, "120元", case_user_texts))
            guarded(lambda: save_snapshot(page, "after", chat_id))
            fixture_subject = str(mail_fixture["subject"])
            fixture_excerpt = str(mail_fixture["expected_source_excerpt"])
            citation_name = f"[1] {fixture_subject}"
            citation = page.get_by_role("button", name=citation_name, exact=True)
            guarded(lambda: citation.wait_for(state="visible", timeout=30000))
            guarded(citation.click)
            record_action("button", citation_name, "expand-source-citation")
            dialog = page.get_by_role("dialog")
            guarded(lambda: dialog.wait_for(state="visible", timeout=30000))
            guarded(lambda: _wait_for_locator_text(dialog, fixture_excerpt, page))
            guarded(lambda: save_snapshot(page, "source_expanded", chat_id))
            source_read = next((item for item in reversed(requests)
                                if item.get("kind") == "mail_source" and item.get("status") == 200), None)
            guarded(lambda: _require(source_read is not None,
                                     "The citation did not produce a successful source HTTP request"))
            snapshots["source_read"] = {
                "request": source_read,
                "visible_excerpt": fixture_excerpt in (snapshots["source_expanded"].get("source_panel", {}).get("text", "")),
            }
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "source_expanded", "probe": "v079-source-expanded",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v079-source-expanded")),
                })
            close_button = dialog.get_by_role("button", name="Close", exact=True)
            guarded(close_button.click)
            record_action("button", "Close", "close-source-dialog-before-revoke")
            revoke_url = str(mail_bootstrap["permission_revoke_url"])
            revoke = guarded(lambda: _post_json_url(revoke_url, {}))
            snapshots["permission_revoked"] = {
                "status": revoke["status"],
                "payload": {key: value for key, value in revoke["payload"].items()
                            if key in {"revoked", "policy_revision"}},
            }
            guarded(lambda: _require(revoke["payload"].get("revoked") is True,
                                     "The gateway did not revoke owner source access"))
            if control_url:
                snapshots["control_observations"].append({
                    "phase": "permission_revoked", "probe": "v079-permission-revoked",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v079-permission-revoked")),
                })
            before_request_count = len([item for item in requests if item.get("kind") == "mail_source"])
            guarded(citation.click)
            record_action("button", citation_name, "reopen-source-after-revoke")
            guarded(lambda: dialog.get_by_role("alert").wait_for(timeout=30000))
            guarded(lambda: _wait_for_request_status(requests, "mail_source", 403, before_request_count,
                                                      page, timeout_seconds=30))
            guarded(lambda: save_snapshot(page, "source_denied", chat_id))
            denial_panel = snapshots["source_denied"].get("source_panel") or {}
            guarded(lambda: _require(fixture_excerpt not in denial_panel.get("text", ""),
                                     "Revoked source excerpt remained visible in the citation dialog"))
            denied_request = next((item for item in reversed(requests)
                                   if item.get("kind") == "mail_source" and item.get("status") == 403), None)
            guarded(lambda: _require(denied_request is not None,
                                     "No real 403 source response was observed after revocation"))
            snapshots["source_denied_request"] = denied_request
            if control_url:
                snapshots["control_observations"].append({
                    "phase": "source_denied", "probe": "v079-source-denied",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v079-source-denied")),
                })
            guarded(lambda: page.reload(wait_until="domcontentloaded", timeout=30000))
            record_action("page", "Reload", "reload-after-owner-revoke")
            guarded(lambda: page.get_by_role("textbox", name="Password", exact=True).wait_for(timeout=30000))
            guarded(lambda: save_snapshot(page, "revoked_after_reload", chat_id))
            # Exercise the persisted private history with the *old* credential,
            # even though bootstrap has correctly returned to the login form.
            history_probe = guarded(lambda: page.evaluate(
                """async ({chatId, authorization}) => {
                  const response = await fetch(`/api/sessions/${encodeURIComponent('websocket:' + chatId)}/messages`, {
                    headers: {Authorization: authorization}, cache: 'no-store'
                  });
                  return {status: response.status, body: await response.text()};
                }""", {"chatId": chat_id, "authorization": source_auth.get("authorization", "")}
            ))
            snapshots["revoked_history_probe"] = history_probe
            guarded(lambda: save_snapshot(page, "revoked_after_history_probe", chat_id))
        elif case_id == "V080":
            chat_id = guarded(lambda: _wait_for_submitted_chat_id(page, ws_frames, prompt))
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000,
                                               after_count=terminal_count_before_send, chat_id=chat_id))
            guarded(lambda: _wait_for_assistant_text(page, "MOBILE17", case_user_texts))
            guarded(lambda: save_snapshot(page, "mobile_answer", chat_id))
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "answer", [
                ("button", "Toggle sidebar", page.get_by_role("button", name="Toggle sidebar", exact=True)),
                ("textbox", "Message input", page.get_by_role("textbox", name="Message input", exact=True)),
            ]))
            snapshots.setdefault("layout_observations", []).append(layout)
            guarded(lambda: _assert_mobile_layout(layout))

            nav = page.get_by_role("navigation", name="Sidebar navigation", exact=True)
            sidebar_toggle = page.get_by_role("button", name="Toggle sidebar", exact=True)
            guarded(sidebar_toggle.click)
            record_action("button", "Toggle sidebar", "open-sidebar-after-answer")
            guarded(lambda: nav.wait_for(state="visible", timeout=10000))
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "sidebar_open_after_answer", [
                ("button", "New chat", nav.get_by_role("button", name="New chat", exact=True)),
            ]))
            snapshots["layout_observations"].append(layout)
            guarded(lambda: _assert_mobile_layout(layout))
            guarded(lambda: save_snapshot(page, "mobile_sidebar_after_answer", chat_id))
            mobile_rows = nav.get_by_role("listitem")
            guarded(lambda: _require(mobile_rows.count() == 1, "Expected the single newly created mobile chat"))
            mobile_row_name = mobile_rows.inner_text().strip()
            snapshots["mobile_row_binding"] = {"chat_id": chat_id, "accessible_name": mobile_row_name,
                                               "row_count": mobile_rows.count()}

            guarded(lambda: nav.get_by_role("button", name="New chat", exact=True).click())
            record_action("button", "New chat", "move-to-empty-chat-before-switch-back")
            guarded(lambda: nav.wait_for(state="hidden", timeout=10000))
            guarded(lambda: page.get_by_role("textbox", name="Message input", exact=True).wait_for(timeout=10000))
            guarded(lambda: save_snapshot(page, "mobile_empty_before_switch_back", None))
            snapshots["mobile_empty_chat_id"] = None
            snapshots["mobile_empty_session_state"] = {
                "chat_id": None,
                "state": "unsaved-new-chat-draft",
                "business_bubbles": snapshots["mobile_empty_before_switch_back"]["visible_bubbles"],
            }
            guarded(lambda: _require(not snapshots["mobile_empty_session_state"]["business_bubbles"],
                                     "New mobile chat draft retained business messages before switch-back"))
            guarded(sidebar_toggle.click)
            record_action("button", "Toggle sidebar", "reopen-sidebar-to-switch-back")
            guarded(lambda: nav.wait_for(state="visible", timeout=10000))
            row = nav.get_by_role("button", name=mobile_row_name, exact=True)
            guarded(lambda: row.wait_for(state="visible", timeout=30000))
            observed_row_name = row.first.inner_text().strip()
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "chat_row_before_switch", [
                ("button", observed_row_name, row),
                ("button", "New chat", nav.get_by_role("button", name="New chat", exact=True)),
            ]))
            snapshots["layout_observations"].append(layout)
            guarded(lambda: _assert_mobile_layout(layout))
            attached_before = _attached_count(ws_frames, chat_id)
            history_before = _history_request_count(requests, chat_id)
            guarded(row.first.click)
            record_action("button", observed_row_name, "switch-back-to-mobile17-chat")
            guarded(lambda: nav.wait_for(state="hidden", timeout=10000))
            guarded(lambda: _wait_for_chat_attached(page, ws_frames, chat_id, attached_before))
            guarded(lambda: _wait_for_history_request(requests, chat_id, history_before, page))
            guarded(lambda: _wait_for_assistant_text(page, "MOBILE17", case_user_texts))
            layout = guarded(lambda: _wait_for_mobile_layout_stable(page, "chat_selected", [
                ("button", "Toggle sidebar", sidebar_toggle),
                ("textbox", "Message input", page.get_by_role("textbox", name="Message input", exact=True)),
            ]))
            snapshots["layout_observations"].append(layout)
            guarded(lambda: _assert_mobile_layout(layout))
            snapshots["mobile_chat_id"] = chat_id
            if control_url:
                snapshots.setdefault("control_observations", []).append({
                    "phase": "chat_selected", "probe": "v080-mobile-chat-selected",
                    "raw": guarded(lambda: _history_count(control_url, chat_id, "v080-mobile-chat-selected")),
                })
            guarded(lambda: save_snapshot(page, "mobile_switched_back", chat_id))
        else:
            guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000))
            if case_id == "V073":
                guarded(lambda: page.wait_for_function("!!window.__nanobotV073Timing?.final_dom", timeout=5000))
                calibration["samples"].extend(guarded(lambda: _v073_clock_sample(page)) for _ in range(3))
                snapshots["v073_timing"] = guarded(lambda: _v073_timing(page, calibration))
            guarded(lambda: save_snapshot(page, "after", chat_id))
        if case_id == "V078":
            guarded(lambda: save_snapshot(page, "before", chat_id))
        if case_id == "V074" and control_url:
            snapshots["control_observations"] = [{
                "phase": "after_tool_actions", "probe": "v074-dom-final",
                "raw": guarded(lambda: _history_count(control_url, chat_id, "v074-dom-final")),
            }]

        if case_id == "V073":
            textbox = page.get_by_role("textbox", name="Message input", exact=True).first
            guarded(lambda: textbox.fill("继续"))
            record_action("textbox", "Type your message", "fill-next")
            next_send = page.get_by_role("button", name="Send message", exact=True)
            guarded(lambda: _assert_send_enabled(next_send))
            record_action("button", "Send message", "assert-enabled-after-completion")
            guarded(lambda: textbox.fill(""))
            guarded(lambda: save_snapshot(page, "after_next_send", chat_id))

        if case_id == "V078":
            before_observation = guarded(lambda: _history_count(control_url, chat_id)) if chat_id else None
            guarded(lambda: page.reload(wait_until="domcontentloaded", timeout=30000))
            guarded(lambda: _wait_for_assistant_text(page, "HISTORY17", case_user_texts))
            after_observation = guarded(lambda: _history_count(control_url, chat_id)) if chat_id else None
            snapshots["history_observations"] = {"before_reload": before_observation, "after_reload": after_observation}
            if before_observation and after_observation:
                before_payload = before_observation["payload"]
                after_payload = after_observation["payload"]
                snapshots["ui"] = {
                    "model_calls_before_reload": before_payload.get("provider_request_count"),
                    "model_calls_after_reload": after_payload.get("provider_request_count"),
                    "tool_calls_before_reload": len(before_payload.get("tool_events", [])),
                    "tool_calls_after_reload": len(after_payload.get("tool_events", [])),
                    "history_requests_before_reload": len(before_payload.get("history_http_requests", [])),
                    "history_requests_after_reload": len(after_payload.get("history_http_requests", [])),
                }
            if control == "refresh_resubmit":
                guarded(lambda: save_snapshot(page, "after_reload_pre_resubmit", chat_id))
                terminal_count_before = _turn_end_count(ws_frames)
                textbox = page.get_by_role("textbox", name="Message input", exact=True).first
                guarded(lambda: textbox.fill(PROMPTS[case_id]))
                record_action("textbox", "Type your message", "negative-resubmit-fill")
                guarded(lambda: page.get_by_role("button", name="Send message", exact=True).click())
                record_action("button", "Send message", "negative-resubmit-click")
                guarded(lambda: _wait_for_turn_end(page, ws_frames, timeout_ms=120000, after_count=terminal_count_before))
                after_resubmit = guarded(lambda: _history_count(control_url, chat_id)) if chat_id else None
                snapshots["history_observations"]["after_resubmit"] = after_resubmit
                if after_resubmit and "ui" in snapshots:
                    payload = after_resubmit["payload"]
                    snapshots["ui"]["model_calls_after_resubmit"] = payload.get("provider_request_count")
                    snapshots["ui"]["tool_calls_after_resubmit"] = len(payload.get("tool_events", []))
                    snapshots["ui"]["history_requests_after_resubmit"] = len(payload.get("history_http_requests", []))
                guarded(lambda: save_snapshot(page, "after_reload", chat_id))
            else:
                guarded(lambda: save_snapshot(page, "after_reload", chat_id))

        font = guarded(lambda: _font_fingerprint(page))
        browser.close()

    evidence = {
        "schema": SCHEMA,
        "case_id": case_id,
        "profile": profile,
        "trial_id": out.name,
        "browser": {"name": "Chromium", "version": version, "executable": str(executable),
                    "executable_sha256": _sha256(executable), "viewport": actual_viewport,
                    "theme": actual_theme,
                    "font_fingerprint": font, "playwright_version": PLAYWRIGHT_VERSION},
        "actions": actions,
        "snapshots": snapshots,
        "dom_artifacts": dom_artifacts,
        "screenshots": screenshots,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "requests": requests,
        "ws_frames": ws_frames,
        "control": control,
    }
    _write_json(out / "browser-evidence.json", evidence)
    return evidence


def _record_response(requests: list[dict[str, Any]], response: Any) -> None:
    request = response.request
    safe = _safe_url(request.url)
    for item in reversed(requests):
        if item["url"] == safe and item["status"] is None:
            item["status"] = response.status
            if response.status == 403 and any(path in request.url for path in (
                "/api/mail-knowledge/sources/", "/api/sessions/", "/webui/bootstrap",
            )):
                try:
                    # Denied private responses should contain only an authorization reason.
                    item["response_body"] = response.text()[:512]
                except Exception:
                    item["response_body"] = None
            return


def _record_request_finished(requests: list[dict[str, Any]], request: Any) -> None:
    safe = _safe_url(request.url)
    for item in reversed(requests):
        if item["url"] == safe and item.get("finished") is not True:
            item["finished"] = True
            return


def _turn_end_count(frames: list[dict[str, Any]], chat_id: str | None = None) -> int:
    return sum(
        frame.get("direction") == "received"
        and isinstance(frame.get("payload"), dict)
        and frame["payload"].get("event") in {"turn_end", "error"}
        and (chat_id is None or frame["payload"].get("chat_id") == chat_id)
        for frame in frames
    )


def _history_request_count(requests: list[dict[str, Any]], chat_id: str) -> int:
    from urllib.parse import quote

    encoded = quote(chat_id, safe="")
    return sum(item.get("kind") == "history" and encoded in item.get("url", "")
               for item in requests)


def _wait_for_history_request(
    requests: list[dict[str, Any]], chat_id: str, after_count: int, page: Any,
    timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _history_request_count(requests, chat_id) > after_count:
            return
        page.wait_for_timeout(50)
    raise TimeoutError(f"No real history HTTP request observed after selecting {chat_id}")


def _wait_for_history_response(
    requests: list[dict[str, Any]], chat_id: str, after_count: int, page: Any,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    from urllib.parse import quote

    encoded = quote(chat_id, safe="")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        matching = [item for item in requests
                    if item.get("kind") == "history" and encoded in item.get("url", "")]
        completed = [item for item in matching[after_count:]
                     if item.get("status") == 200 and item.get("finished") is True]
        if completed:
            return completed[-1]
        page.wait_for_timeout(50)
    raise TimeoutError(f"No completed HTTP 200 history response observed after selecting {chat_id}")


def _wait_for_request_status(
    requests: list[dict[str, Any]], kind: str, status: int, after_count: int, page: Any,
    timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        matching = [item for item in requests if item.get("kind") == kind]
        if any(item.get("status") == status for item in matching[after_count:]):
            return
        page.wait_for_timeout(50)
    raise TimeoutError(f"No {kind} HTTP {status} response observed")


def _wait_for_assistant_text(
    page: Any, text: str, user_texts: set[str], timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _has_assistant_text(page, text, user_texts):
            return
        page.wait_for_timeout(50)
    raise TimeoutError(f"No assistant message container contained expected visible text {text!r}")


def _has_assistant_text(page: Any, text: str, user_texts: set[str]) -> bool:
    return any(bubble["role"] == "assistant" and text in bubble["text"]
               for bubble in _visible_bubbles(page, user_texts))


def _wait_for_locator_text(locator: Any, text: str, page: Any, timeout_seconds: float = 30) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if locator.count() and locator.first.is_visible() and text in locator.first.inner_text():
                return
        except Exception:
            pass
        page.wait_for_timeout(50)
    raise TimeoutError("Expected text did not appear inside the visible accessible container")


def _assert_mobile_layout(layout: dict[str, Any]) -> None:
    stability = layout.get("geometry_stability")
    if stability is not None and not stability.get("stable"):
        raise AssertionError(
            f"Mobile layout did not settle before measurement in {layout['phase']}: "
            f"{stability.get('elapsed_ms')}ms, {len(stability.get('polls', []))} geometry polls"
        )
    document = layout["document"]
    if document["horizontal_overflow"]:
        raise AssertionError(
            f"Mobile document overflows horizontally in {layout['phase']}: "
            f"scrollWidth={document.get('document_scroll_width')} "
            f"clientWidth={document.get('document_client_width')}"
        )
    if not document["focus_target_present"]:
        raise AssertionError(f"No meaningful focus target remained after mobile interaction in {layout['phase']}")
    failed = [item for item in layout["controls"]
              if not item.get("present") or not item.get("visible") or item.get("occluded")]
    if failed:
        raise AssertionError(f"Mobile controls were missing, hidden, or occluded in {layout['phase']}: {failed}")


def _history_chat_ids(requests: list[dict[str, Any]]) -> list[str]:
    ids = []
    for item in requests:
        match = re.search(r"/api/sessions/([^/?]+)/messages", item["url"])
        if match:
            from urllib.parse import unquote
            chat_id = unquote(match.group(1))
            if chat_id not in ids:
                ids.append(chat_id)
    return ids


def _assert_send_enabled(button: Any) -> None:
    if not button.is_enabled():
        raise AssertionError("Composer did not re-enable after completion")


def _assert_input_enabled(textbox: Any) -> None:
    if not textbox.is_enabled():
        raise AssertionError("Composer input did not re-enable after stop")


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_for_turn_end(
    page: Any, frames: list[dict[str, Any]], timeout_ms: int,
    after_count: int = 0, chat_id: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _turn_end_count(frames, chat_id) > after_count:
            page.wait_for_timeout(50)
            return
        page.wait_for_timeout(50)
    raise TimeoutError("WebSocket did not deliver turn_end/error before timeout")


def _wait_for_chat_attached(page: Any, frames: list[dict[str, Any]], chat_id: str, after_count: int = 0) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        attached = sum(
            frame.get("direction") == "received"
            and isinstance(frame.get("payload"), dict)
            and frame["payload"].get("event") == "attached"
            and frame["payload"].get("chat_id") == chat_id
            for frame in frames
        )
        if attached > after_count:
            page.wait_for_timeout(50)
            return
        page.wait_for_timeout(50)
    raise TimeoutError(f"WebSocket did not confirm UI attachment to chat {chat_id}")


def _attached_count(frames: list[dict[str, Any]], chat_id: str) -> int:
    return sum(
        frame.get("direction") == "received"
        and isinstance(frame.get("payload"), dict)
        and frame["payload"].get("event") == "attached"
        and frame["payload"].get("chat_id") == chat_id
        for frame in frames
    )


def _record_request(requests: list[dict[str, Any]], request: Any) -> None:
    url = _safe_url(request.url)
    is_history = bool(re.search(r"/api/sessions/[^/]+/messages(?:\?|$)", url))
    is_mail_source = "/api/mail-knowledge/sources/" in url
    requests.append({
        "url": url,
        "method": request.method,
        "status": None,
        "kind": "history" if is_history else "mail_source" if is_mail_source else request.resource_type,
        "resource_type": request.resource_type,
    })


def _record_socket(socket: Any, frames: list[dict[str, Any]]) -> None:
    frames.append({"url": _safe_url(socket.url), "direction": "opened"})
    socket.on("framesent", lambda payload: frames.append({"url": _safe_url(socket.url), "direction": "sent", "payload": _safe_ws_payload(payload)}))
    socket.on("framereceived", lambda payload: frames.append({"url": _safe_url(socket.url), "direction": "received", "payload": _safe_ws_payload(payload)}))


def _safe_ws_payload(payload: Any) -> Any:
    if isinstance(payload, bytes):
        return {"binary_length": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return str(payload)[:2048]
    if isinstance(value, dict):
        return {key: value[key] for key in (
            "event", "type", "chat_id", "message_id", "stream_id", "turn_id", "resuming",
            "tool_events", "kind", "text", "content", "outcome",
            "citations", "mailChatId", "tool_call_id", "call_id"
        ) if key in value}
    return value


def _chat_id_from_requests(requests: list[dict[str, Any]]) -> str | None:
    for item in requests:
        match = re.search(r"/sessions/([^/?]+)/messages", item["url"])
        if match:
            from urllib.parse import unquote
            return unquote(match.group(1))
    for item in requests:
        if item["kind"] == "websocket":
            continue
    return None


def _chat_ids(requests: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[str]:
    ids = _history_chat_ids(requests)
    for frame in frames:
        payload = frame.get("payload")
        if isinstance(payload, dict):
            chat_id = payload.get("chat_id")
            if isinstance(chat_id, str) and chat_id not in ids:
                ids.append(chat_id)
    return ids


def _current_chat_id(
    requests: list[dict[str, Any]], frames: list[dict[str, Any]], message_text: str | None = None
) -> str | None:
    if message_text:
        for frame in reversed(frames):
            payload = frame.get("payload")
            if isinstance(payload, dict) and message_text in (payload.get("text"), payload.get("content")):
                if isinstance(payload.get("chat_id"), str):
                    return payload["chat_id"]
    ids = _chat_ids(requests, frames)
    return ids[-1] if ids else None


def _wait_for_submitted_chat_id(page: Any, frames: list[dict[str, Any]], message_text: str) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for frame in reversed(frames):
            payload = frame.get("payload")
            if (frame.get("direction") == "sent" and isinstance(payload, dict)
                    and message_text in (payload.get("text"), payload.get("content"))
                    and isinstance(payload.get("chat_id"), str)):
                return payload["chat_id"]
        page.wait_for_timeout(50)
    raise TimeoutError(f"No actual WebSocket submission frame found for {message_text!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, choices=sorted(PROMPTS))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--control")
    parser.add_argument("--control-url")
    parser.add_argument("--gate-state-url")
    parser.add_argument("--gate-release-url")
    parser.add_argument("--v077-a-chat-id")
    parser.add_argument("--v077-b-chat-id")
    parser.add_argument("--ready-json")
    parser.add_argument("--theme", choices=["light", "dark"])
    parser.add_argument("--viewport", help="Frozen viewport as WIDTHxHEIGHT")
    args = parser.parse_args()
    run_browser(
        args.case, args.profile, args.url, args.output, args.control,
        args.control_url, args.gate_state_url, args.gate_release_url,
        args.v077_a_chat_id, args.v077_b_chat_id, args.ready_json, args.theme, args.viewport,
    )


if __name__ == "__main__":
    main()

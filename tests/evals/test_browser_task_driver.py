from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from evals import browser_task_driver as driver


def test_browser_profile_is_fixed_and_real() -> None:
    cached_deps = Path(driver.__file__).resolve().parents[1] / ".tmp" / "browser-python"
    if cached_deps.is_dir() and str(cached_deps) not in sys.path:
        sys.path.insert(0, str(cached_deps))
    assert driver.SCHEMA == "browser-task-ui/1.0"
    assert driver.VIEWPORT == {"width": 1280, "height": 800}
    assert driver.PLAYWRIGHT_VERSION == "1.55.0"
    assert importlib.util.find_spec("playwright") is not None
    assert driver._browser_executable().is_file()


def test_mobile_profile_requires_explicit_frozen_theme_and_viewport() -> None:
    with pytest.raises(ValueError, match="explicit --theme"):
        driver._viewport_for_case("V080", None, "390x844")
    with pytest.raises(ValueError, match="390x844"):
        driver._viewport_for_case("V080", "dark", "1280x800")
    assert driver._viewport_for_case("V080", "light", "390x844") == (
        {"width": 390, "height": 844}, "light",
    )
    with pytest.raises(ValueError, match="1280x800"):
        driver._viewport_for_case("V079", None, "390x844")


def test_viewport_parser_rejects_invalid_and_nonpositive_dimensions() -> None:
    assert driver.parse_viewport("390X844") == {"width": 390, "height": 844}
    with pytest.raises(ValueError, match="WIDTHxHEIGHT"):
        driver.parse_viewport("390 by 844")
    with pytest.raises(ValueError, match="positive"):
        driver.parse_viewport("0x844")


def test_mobile_layout_gate_checks_overflow_focus_and_occlusion() -> None:
    passing = {
        "phase": "mobile-test",
        "document": {"horizontal_overflow": False, "focus_target_present": True},
        "controls": [{"present": True, "visible": True, "occluded": False}],
    }
    driver._assert_mobile_layout(passing)
    for changed, message in (
        ({"document": {"horizontal_overflow": True, "focus_target_present": True}}, "overflows"),
        ({"document": {"horizontal_overflow": False, "focus_target_present": False}}, "focus target"),
        ({"controls": [{"present": True, "visible": True, "occluded": True}]}, "occluded"),
    ):
        value = {**passing, **changed}
        with pytest.raises(AssertionError, match=message):
            driver._assert_mobile_layout(value)


def test_mobile_stability_signature_tracks_control_geometry() -> None:
    base = {
        "document": {
            "inner_width": 390, "inner_height": 844,
            "document_scroll_width": 390, "document_client_width": 390,
        },
        "controls": [{
            "role": "button", "name": "New chat", "present": True, "visible": True,
            "bbox": {"x": 0.04, "y": 88, "width": 254, "height": 32},
        }],
    }
    moved = {
        **base,
        "controls": [{
            **base["controls"][0],
            "bbox": {"x": -263, "y": 88, "width": 254, "height": 32},
        }],
    }
    assert driver._layout_geometry_signature(base) == driver._layout_geometry_signature({
        **base, "controls": [{**base["controls"][0], "bbox": {**base["controls"][0]["bbox"], "x": 0.03}}],
    })
    assert driver._layout_geometry_signature(base) != driver._layout_geometry_signature(moved)


def test_safe_url_redacts_credentials_and_fragments() -> None:
    value = driver._safe_url("http://127.0.0.1/api?token=secret&chat_id=c1#unsafe")
    assert value == "http://127.0.0.1/api?token=%3Credacted%3E&chat_id=c1"
    assert "secret" not in value


def test_diagnostics_redact_query_tokens_bearer_tokens_and_known_secret() -> None:
    diagnostic = driver._safe_diagnostic(
        "WebSocket ws://localhost/?token=nbwt_abcd failed Authorization: Bearer abc.def",
        "synthetic-password",
    )
    assert "nbwt_abcd" not in diagnostic
    assert "abc.def" not in diagnostic
    assert "token=<redacted>" in diagnostic
    assert driver._safe_diagnostic("connect failed: synthetic-password", "synthetic-password").endswith(
        "<redacted>"
    )


def test_message_rows_group_paragraphs_and_preserve_markdown_lists() -> None:
    submitted = "same text"
    candidates = [
        {
            "source": "sibling-message-rows",
            "copy_reply_root": False,
            "container_path": [{"tag": "div", "index": 0}],
            "paragraphs": [{"text": submitted, "index": 0, "path": [{"tag": "p", "index": 0}]}],
            "structure_confidence": "structural-message-row",
        },
        {
            "source": "copy_reply_action",
            "copy_reply_root": True,
            "markdown_text": "Fees are 120 yuan\n- item one\n- item two",
            "container_path": [{"tag": "div", "index": 1}],
            "paragraphs": [
                {"text": "Fees are 120 yuan", "index": 1, "path": [{"tag": "p", "index": 0}]},
            ],
            "structure_confidence": "assistant-action-root",
        },
        {
            "source": "copy_reply_action",
            "copy_reply_root": True,
            "markdown_text": submitted,
            "container_path": [{"tag": "div", "index": 2}],
            "paragraphs": [{"text": submitted, "index": 2, "path": [{"tag": "p", "index": 0}]}],
            "structure_confidence": "assistant-action-root",
        },
    ]

    bubbles = driver._classify_message_candidates(candidates, {submitted})

    assert [(row["role"], row["text"]) for row in bubbles] == [
        ("user", submitted),
        ("assistant", "Fees are 120 yuan\n- item one\n- item two"),
        ("assistant", submitted),
    ]
    assert len(bubbles[1]["paragraph_records"]) == 1
    assert bubbles[1]["structure_confidence"] == "assistant-action-root"


def test_message_rows_without_reliable_role_evidence_remain_unknown() -> None:
    bubbles = driver._classify_message_candidates([
        {
            "source": "paragraph-only-fallback",
            "copy_reply_root": False,
            "container_path": [{"tag": "p", "index": 0}],
            "paragraphs": [{"text": "unclassified text", "index": 0}],
            "structure_confidence": "paragraph-only",
        },
    ], {"known user message"})
    assert len(bubbles) == 1
    assert bubbles[0]["role"] == "unknown"
    assert bubbles[0]["text"] == "unclassified text"


def test_assistant_visibility_check_does_not_accept_matching_user_text() -> None:
    class Page:
        @staticmethod
        def evaluate(_script):
            return [{
                "source": "sibling-message-rows",
                "copy_reply_root": False,
                "container_path": [],
                "paragraphs": [{"text": "same text", "index": 0}],
                "structure_confidence": "structural-message-row",
            }]

    assert not driver._has_assistant_text(Page(), "same text", {"same text"})


def test_known_user_inputs_are_scoped_to_the_actual_case() -> None:
    assert driver._user_texts_for_case("V073") == {driver.PROMPTS["V073"]}
    assert driver._user_texts_for_case("V077") == {
        driver.PROMPTS["V077"], "返回B=23",
    }
    assert driver._user_texts_for_case("V079", {"question": "synthetic mail question"}) == {
        driver.PROMPTS["V079"], "synthetic mail question",
    }


def test_chat_id_is_derived_from_real_history_request() -> None:
    requests = [{"url": "http://localhost/api/sessions/websocket%3Ac1/messages", "kind": "fetch"}]
    assert driver._chat_id_from_requests(requests) == "websocket:c1"


def test_socket_payload_keeps_only_observable_fields() -> None:
    payload = driver._safe_ws_payload('{"event":"delta","chat_id":"c1","text":"hello","secret":"x"}')
    assert payload == {"event": "delta", "chat_id": "c1", "text": "hello"}


def test_socket_payload_keeps_real_citation_references() -> None:
    payload = driver._safe_ws_payload(
        '{"event":"assistant_message","chat_id":"c1","citations":[{"evidence_id":"ev-1","subject":"synthetic"}],"secret":"x"}'
    )
    assert payload["citations"] == [{"evidence_id": "ev-1", "subject": "synthetic"}]
    assert "secret" not in payload


def test_history_request_has_distinct_kind_and_retains_resource_type() -> None:
    class Request:
        url = "http://localhost/api/sessions/websocket%3Ac1/messages"
        method = "GET"
        resource_type = "fetch"

    requests: list[dict] = []
    driver._record_request(requests, Request())
    assert requests[0]["kind"] == "history"
    assert requests[0]["resource_type"] == "fetch"


def test_history_response_capture_requires_completed_real_request() -> None:
    class Request:
        url = "http://localhost/api/sessions/websocket%3Ac1/messages"
        method = "GET"
        resource_type = "fetch"

    class Page:
        @staticmethod
        def wait_for_timeout(_timeout):
            pass

    requests: list[dict] = []
    driver._record_request(requests, Request())
    requests[0]["status"] = 200
    assert "finished" not in requests[0]
    driver._record_request_finished(requests, Request())
    assert requests[0]["finished"] is True
    assert driver._wait_for_history_response(requests, "websocket:c1", 0, Page()) is requests[0]


def test_mail_source_request_is_explicit_and_keeps_denied_response_body() -> None:
    class Request:
        url = "http://127.0.0.1/api/mail-knowledge/sources/ev-1?chat_id=c1"
        method = "GET"
        resource_type = "fetch"

    class Response:
        request = Request()
        status = 403

        @staticmethod
        def text() -> str:
            return '{"error":"authorization_revoked"}'

    requests: list[dict] = []
    driver._record_request(requests, Request())
    driver._record_response(requests, Response())
    assert requests[0]["kind"] == "mail_source"
    assert requests[0]["status"] == 403
    assert requests[0]["response_body"] == '{"error":"authorization_revoked"}'


def test_terminal_wait_uses_new_turn_boundary() -> None:
    old_frames = [{"direction": "received", "payload": {"event": "turn_end"}}]
    assert driver._turn_end_count(old_frames) == 1
    assert driver._turn_end_count(old_frames + [
        {"direction": "received", "payload": {"event": "turn_end"}}
    ]) == 2


def test_chat_context_comes_from_the_matching_websocket_submission() -> None:
    frames = [
        {"direction": "sent", "payload": {"event": "message", "chat_id": "chat-A", "text": "ask A"}},
        {"direction": "sent", "payload": {"event": "message", "chat_id": "chat-B", "text": "ask B"}},
    ]
    assert driver._current_chat_id([], frames, "ask A") == "chat-A"


def test_cancel_completion_requires_cancelled_gate_idle_loop_and_persisted_restore() -> None:
    observation = {"payload": {
        "cancellation_state": {"active_task_count": 0},
        "checkpoint_ids": [],
        "session_files": [{
            "session_id": "websocket:chat-a",
            "sha256": "session-hash",
            "checkpoint_present": False,
            "checkpoint_id": None,
            "jsonl": [
                {"role": "tool", "name": "read_file", "content": "值73"},
                {"role": "tool", "name": "evaluation_gate",
                 "content": "Error: Task interrupted before this tool finished."},
            ],
        }],
    }}
    gate = {"payload": {"gates": [{
        "gate_id": "v076-cancel-gate", "state": "cancelled", "cancellations": 1,
    }]}}
    assert driver._cancel_complete(observation, gate, "v076-cancel-gate", "chat-a")
    observation["payload"]["session_files"][0]["jsonl"] = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "partial context"},
    ]
    assert not driver._cancelled_session_is_restored(observation, "chat-a")
    assert driver._cancel_complete(observation, gate, "v076-cancel-gate", "chat-a")
    observation["payload"]["session_files"][0]["checkpoint_present"] = True
    assert not driver._cancel_complete(observation, gate, "v076-cancel-gate", "chat-a")


def test_submitted_chat_id_requires_exact_outbound_message_binding(monkeypatch) -> None:
    frames = [
        {"direction": "sent", "payload": {"type": "message", "chat_id": "chat-b", "content": "other"}},
        {"direction": "received", "payload": {"event": "attached", "chat_id": "chat-a"}},
    ]

    class Page:
        @staticmethod
        def wait_for_timeout(_timeout):
            pass

    ticks = iter((0, 20))
    monkeypatch.setattr(driver.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="No actual WebSocket submission"):
        driver._wait_for_submitted_chat_id(Page(), frames, "task A")


def test_unsupported_case_fails_before_launch() -> None:
    with pytest.raises(ValueError, match="Unsupported browser case"):
        driver.run_browser("V000", "B-scripted", "http://127.0.0.1", Path("unused"))


def test_v079_requires_isolated_ready_file_before_launch() -> None:
    with pytest.raises(ValueError, match="ready.json"):
        driver.run_browser("V079", "B-scripted", "http://127.0.0.1", Path("unused"))

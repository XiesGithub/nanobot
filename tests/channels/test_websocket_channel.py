"""Unit and lightweight integration tests for the WebSocket channel."""

import asyncio
import base64
import functools
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from nanobot.bus.events import OutboundMessage
from nanobot.channels.websocket import (
    WebSocketChannel,
    WebSocketConfig,
    _is_valid_chat_id,
    _issue_route_secret_matches,
    _normalize_config_path,
    _normalize_http_path,
    _parse_envelope,
    _parse_inbound_payload,
    _parse_query,
    _parse_request_path,
)
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config
from nanobot.session.manager import SessionManager

# -- Shared helpers (aligned with test_websocket_integration.py) ---------------

_PORT = 29876


@pytest.mark.asyncio
async def test_stream_and_tool_progress_keep_execution_identity(bus: MagicMock) -> None:
    channel = _ch(bus)
    connection = MagicMock()
    connection.send = AsyncMock()
    channel._attach(connection, "progress-chat")
    await channel.send_delta("progress-chat", "准备读取", {
        "_turn_id": "turn-1", "_stream_id": "stream-1",
    })
    await channel.send_delta("progress-chat", "", {
        "_turn_id": "turn-1", "_stream_id": "stream-1",
        "_stream_end": True, "_resuming": True,
    })
    event = {"version": 1, "phase": "start", "call_id": "read-1",
             "name": "read_file", "execution_started": True}
    await channel.send(OutboundMessage(channel="websocket", chat_id="progress-chat", content="",
        metadata={"_progress": True, "_turn_id": "turn-1", "_tool_events": [event]}))
    await channel.send_turn_end("progress-chat", turn_id="turn-1")
    frames = [json.loads(call.args[0]) for call in connection.send.await_args_list]
    assert frames == [
        {"event": "delta", "chat_id": "progress-chat", "text": "准备读取",
         "turn_id": "turn-1", "stream_id": "stream-1"},
        {"event": "stream_end", "chat_id": "progress-chat", "resuming": True,
         "turn_id": "turn-1", "stream_id": "stream-1"},
        {"event": "message", "chat_id": "progress-chat", "text": "", "kind": "progress",
         "turn_id": "turn-1", "tool_events": [event]},
        {"event": "turn_end", "chat_id": "progress-chat", "turn_id": "turn-1"},
    ]


def _ch(bus: Any, **kw: Any) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": _PORT,
        "path": "/ws",
        "websocketRequiresToken": False,
    }
    cfg.update(kw)
    return WebSocketChannel(cfg, bus)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


async def _http_get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """Run GET in a thread to avoid blocking the asyncio loop shared with websockets."""
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


def test_normalize_http_path_strips_trailing_slash_except_root() -> None:
    assert _normalize_http_path("/chat/") == "/chat"
    assert _normalize_http_path("/chat?x=1") == "/chat"
    assert _normalize_http_path("/") == "/"


def test_parse_request_path_matches_normalize_and_query() -> None:
    path, query = _parse_request_path("/ws/?token=secret&client_id=u1")
    assert path == _normalize_http_path("/ws/?token=secret&client_id=u1")
    assert query == _parse_query("/ws/?token=secret&client_id=u1")


def test_normalize_config_path_matches_request() -> None:
    assert _normalize_config_path("/ws/") == "/ws"
    assert _normalize_config_path("/") == "/"


def test_parse_query_extracts_token_and_client_id() -> None:
    query = _parse_query("/?token=secret&client_id=u1")
    assert query.get("token") == ["secret"]
    assert query.get("client_id") == ["u1"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ('{"content": "hi"}', "hi"),
        ('{"text": "there"}', "there"),
        ('{"message": "x"}', "x"),
        ("  ", None),
        ("{}", None),
    ],
)
def test_parse_inbound_payload(raw: str, expected: str | None) -> None:
    assert _parse_inbound_payload(raw) == expected


def test_parse_inbound_invalid_json_falls_back_to_raw_string() -> None:
    assert _parse_inbound_payload("{not json") == "{not json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"content": ""}', None),           # empty string content
        ('{"content": 123}', None),          # non-string content
        ('{"content": "  "}', None),         # whitespace-only content
        ('["hello"]', '["hello"]'),           # JSON array: not a dict, treated as plain text
        ('{"unknown_key": "val"}', None),    # unrecognized key
        ('{"content": null}', None),         # null content
    ],
)
def test_parse_inbound_payload_edge_cases(raw: str, expected: str | None) -> None:
    assert _parse_inbound_payload(raw) == expected


def test_web_socket_config_path_must_start_with_slash() -> None:
    with pytest.raises(ValueError, match='path must start with "/"'):
        WebSocketConfig(path="bad")


def test_ssl_context_requires_both_cert_and_key_files() -> None:
    bus = MagicMock()
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "sslCertfile": "/tmp/c.pem", "sslKeyfile": ""},
        bus,
    )
    with pytest.raises(ValueError, match="ssl_certfile and ssl_keyfile"):
        channel._build_ssl_context()


def test_default_config_includes_safe_bind_and_streaming() -> None:
    defaults = WebSocketChannel.default_config()
    assert defaults["enabled"] is False
    assert defaults["host"] == "127.0.0.1"
    assert defaults["streaming"] is True
    assert defaults["allowFrom"] == ["*"]
    assert defaults.get("tokenIssuePath", "") == ""


def test_token_issue_path_must_differ_from_websocket_path() -> None:
    with pytest.raises(ValueError, match="token_issue_path must differ"):
        WebSocketConfig(path="/ws", token_issue_path="/ws")


def test_issue_route_secret_matches_bearer_and_header() -> None:
    from websockets.datastructures import Headers

    secret = "my-secret"
    bearer_headers = Headers([("Authorization", "Bearer my-secret")])
    assert _issue_route_secret_matches(bearer_headers, secret) is True
    x_headers = Headers([("X-Nanobot-Auth", "my-secret")])
    assert _issue_route_secret_matches(x_headers, secret) is True
    wrong = Headers([("Authorization", "Bearer other")])
    assert _issue_route_secret_matches(wrong, secret) is False


def test_issue_route_secret_matches_empty_secret() -> None:
    from websockets.datastructures import Headers

    # Empty secret always returns True regardless of headers
    assert _issue_route_secret_matches(Headers([]), "") is True
    assert _issue_route_secret_matches(Headers([("Authorization", "Bearer anything")]), "") is True


@pytest.mark.asyncio
async def test_webui_message_envelope_marks_inbound_metadata(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()
    conn.remote_address = ("127.0.0.1", 50123)
    channel._conn_operator.add(conn)  # Test the envelope routing after operator authorization.

    await channel._dispatch_envelope(
        conn,
        "webui-client",
        {"type": "message", "chat_id": "chat-1", "content": "hello", "webui": True},
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert msg.channel == "websocket"
    assert msg.chat_id == "chat-1"
    assert msg.metadata["webui"] is True
    assert msg.metadata["_wants_stream"] is True


@pytest.mark.asyncio
async def test_plain_websocket_message_does_not_mark_webui(bus: MagicMock) -> None:
    channel = _ch(bus)
    conn = MagicMock()
    channel._conn_operator.add(conn)  # Test metadata behavior after operator authorization.

    await channel._dispatch_envelope(
        conn,
        "custom-client",
        {"type": "message", "chat_id": "chat-1", "content": "hello"},
    )

    msg = bus.publish_inbound.await_args.args[0]
    assert "webui" not in msg.metadata


@pytest.mark.asyncio
async def test_send_delivers_json_message_with_media_and_reply() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="hello",
        reply_to="m1",
        media=["/tmp/a.png"],
        buttons=[["Yes", "No"]],
    )
    await channel.send(msg)

    mock_ws.send.assert_awaited_once()
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["event"] == "message"
    assert payload["chat_id"] == "chat-1"
    assert payload["text"] == "hello\n\n1. Yes\n2. No"
    assert payload["button_prompt"] == "hello"
    assert payload["reply_to"] == "m1"
    assert payload["media"] == ["/tmp/a.png"]
    assert payload["buttons"] == [["Yes", "No"]]


@pytest.mark.asyncio
async def test_send_stages_external_media_as_signed_url(monkeypatch, tmp_path) -> None:
    bus = MagicMock()
    media_root = tmp_path / "media"
    ws_media = media_root / "websocket"
    ws_media.mkdir(parents=True)
    external = tmp_path / "clip.mp4"
    external.write_bytes(b"video")

    def fake_media_dir(channel: str | None = None):
        return ws_media if channel == "websocket" else media_root

    monkeypatch.setattr("nanobot.channels.websocket.get_media_dir", fake_media_dir)
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(
        OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="video",
            media=[str(external)],
        )
    )

    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["media"] == [str(external)]
    assert payload["media_urls"][0]["name"] == "clip.mp4"
    assert payload["media_urls"][0]["url"].startswith("/api/media/")
    assert any(p.name.endswith("-clip.mp4") for p in ws_media.iterdir())


@pytest.mark.asyncio
async def test_send_missing_connection_is_noop_without_error() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    msg = OutboundMessage(channel="websocket", chat_id="missing", content="x")
    await channel.send(msg)


@pytest.mark.asyncio
async def test_send_removes_connection_on_connection_closed() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    mock_ws.send.side_effect = ConnectionClosed(Close(1006, ""), Close(1006, ""), True)
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(channel="websocket", chat_id="chat-1", content="hello")
    await channel.send(msg)

    assert "chat-1" not in channel._subs
    assert mock_ws not in channel._conn_chats


@pytest.mark.asyncio
async def test_send_delta_removes_connection_on_connection_closed() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    mock_ws = AsyncMock()
    mock_ws.send.side_effect = ConnectionClosed(Close(1006, ""), Close(1006, ""), True)
    channel._attach(mock_ws, "chat-1")

    await channel.send_delta("chat-1", "chunk", {"_stream_delta": True, "_stream_id": "s1"})

    assert "chat-1" not in channel._subs
    assert mock_ws not in channel._conn_chats


@pytest.mark.asyncio
async def test_send_delta_emits_delta_and_stream_end() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send_delta("chat-1", "part", {"_stream_delta": True, "_stream_id": "sid"})
    await channel.send_delta("chat-1", "", {"_stream_end": True, "_stream_id": "sid"})

    assert mock_ws.send.await_count == 2
    first = json.loads(mock_ws.send.call_args_list[0][0][0])
    second = json.loads(mock_ws.send.call_args_list[1][0][0])
    assert first["event"] == "delta"
    assert first["chat_id"] == "chat-1"
    assert first["text"] == "part"
    assert first["stream_id"] == "sid"
    assert second["event"] == "stream_end"
    assert second["chat_id"] == "chat-1"
    assert second["stream_id"] == "sid"


@pytest.mark.asyncio
async def test_send_turn_end_emits_turn_end_event() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_turn_end": True},
    ))

    mock_ws.send.assert_awaited_once()
    body = json.loads(mock_ws.send.await_args.args[0])
    assert body == {"event": "turn_end", "chat_id": "chat-1"}


@pytest.mark.asyncio
async def test_failed_turn_end_is_explicitly_failed() -> None:
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, MagicMock())
    connection = AsyncMock()
    channel._attach(connection, "chat-1")
    await channel.send(OutboundMessage(channel="websocket", chat_id="chat-1", content="",
        metadata={"_turn_end": True, "_turn_error": True}))
    assert json.loads(connection.send.await_args.args[0]) == {
        "event": "turn_end", "chat_id": "chat-1", "outcome": "error"}


@pytest.mark.asyncio
async def test_send_session_updated_emits_session_updated_event() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    channel._attach(mock_ws, "chat-1")

    await channel.send(OutboundMessage(
        channel="websocket",
        chat_id="chat-1",
        content="",
        metadata={"_session_updated": True},
    ))

    mock_ws.send.assert_awaited_once()
    body = json.loads(mock_ws.send.await_args.args[0])
    assert body == {"event": "session_updated", "chat_id": "chat-1"}


@pytest.mark.asyncio
async def test_send_non_connection_closed_exception_is_raised() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    mock_ws = AsyncMock()
    mock_ws.send.side_effect = RuntimeError("unexpected")
    channel._attach(mock_ws, "chat-1")

    msg = OutboundMessage(channel="websocket", chat_id="chat-1", content="hello")
    with pytest.raises(RuntimeError, match="unexpected"):
        await channel.send(msg)


@pytest.mark.asyncio
async def test_send_delta_missing_connection_is_noop() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"], "streaming": True}, bus)
    # No exception, no error — just a no-op
    await channel.send_delta("nonexistent", "chunk", {"_stream_delta": True, "_stream_id": "s1"})


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    bus = MagicMock()
    channel = WebSocketChannel({"enabled": True, "allowFrom": ["*"]}, bus)
    # stop() before start() should not raise
    await channel.stop()
    await channel.stop()


@pytest.mark.asyncio
async def test_end_to_end_client_receives_ready_and_agent_sees_inbound(bus: MagicMock) -> None:
    port = 29876
    channel = _ch(bus, port=port)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=tester") as client:
            ready_raw = await client.recv()
            ready = json.loads(ready_raw)
            assert ready["event"] == "ready"
            assert ready["client_id"] == "tester"
            chat_id = ready["chat_id"]

            await client.send(json.dumps({"content": "ping from client"}))
            await asyncio.sleep(0.08)

            bus.publish_inbound.assert_awaited()
            inbound = bus.publish_inbound.call_args[0][0]
            assert inbound.channel == "websocket"
            assert inbound.sender_id == "tester"
            assert inbound.chat_id == chat_id
            assert inbound.content == "ping from client"

            await client.send("plain text frame")
            await asyncio.sleep(0.08)
            assert bus.publish_inbound.await_count >= 2
            second = [c[0][0] for c in bus.publish_inbound.call_args_list][-1]
            assert second.content == "plain text frame"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_token_rejects_handshake_when_mismatch(bus: MagicMock) -> None:
    port = 29877
    channel = _ch(bus, port=port, path="/", token="secret")

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as excinfo:
            async with websockets.connect(f"ws://127.0.0.1:{port}/?token=wrong"):
                pass
        assert excinfo.value.response.status_code == 401
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_wrong_path_returns_404(bus: MagicMock) -> None:
    port = 29878
    channel = _ch(bus, port=port)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as excinfo:
            async with websockets.connect(f"ws://127.0.0.1:{port}/other"):
                pass
        assert excinfo.value.response.status_code == 404
    finally:
        await channel.stop()
        await server_task


def test_registry_discovers_websocket_channel() -> None:
    from nanobot.channels.registry import load_channel_class

    cls = load_channel_class("websocket")
    assert cls.name == "websocket"


@pytest.mark.asyncio
async def test_http_route_issues_token_then_websocket_requires_it(bus: MagicMock) -> None:
    port = 29879
    channel = _ch(
        bus, port=port,
        tokenIssuePath="/auth/token",
        tokenIssueSecret="route-secret",
        websocketRequiresToken=True,
    )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        deny = await _http_get(f"http://127.0.0.1:{port}/auth/token")
        assert deny.status_code == 401

        issue = await _http_get(
            f"http://127.0.0.1:{port}/auth/token",
            headers={"Authorization": "Bearer route-secret"},
        )
        assert issue.status_code == 200
        token = issue.json()["token"]
        assert token.startswith("nbwt_")

        with pytest.raises(websockets.exceptions.InvalidStatus) as missing_token:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=x"):
                pass
        assert missing_token.value.response.status_code == 401

        uri = f"ws://127.0.0.1:{port}/ws?token={token}&client_id=caller"
        async with websockets.connect(uri) as client:
            ready = json.loads(await client.recv())
            assert ready["event"] == "ready"
            assert ready["client_id"] == "caller"

        with pytest.raises(websockets.exceptions.InvalidStatus) as reuse:
            async with websockets.connect(uri):
                pass
        assert reuse.value.response.status_code == 401
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_settings_api_returns_safe_subset_and_updates_whitelist(
    bus: MagicMock,
    monkeypatch,
    tmp_path,
) -> None:
    port = 29891
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "secret-key"
    config.tools.web.search.provider = "brave"
    config.tools.web.search.api_key = "brave-secret"
    save_config(config, config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    channel = _ch(bus, port=port)
    channel._api_tokens["tok"] = time.monotonic() + 300

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        settings = await _http_get(
            f"http://127.0.0.1:{port}/api/settings",
            headers={"Authorization": "Bearer tok"},
        )
        assert settings.status_code == 200
        body = settings.json()
        assert body["agent"]["model"] == "openai/gpt-4o"
        assert body["agent"]["provider"] == "openai"
        providers = {provider["name"]: provider for provider in body["providers"]}
        assert providers["openai"]["configured"] is True
        assert providers["openai"]["api_key_hint"] == "secr••••-key"
        assert providers["openrouter"]["configured"] is False
        assert body["agent"]["has_api_key"] is True
        assert body["web_search"]["provider"] == "brave"
        assert body["web_search"]["api_key_hint"] == "brav••••cret"
        search_providers = {provider["name"]: provider for provider in body["web_search"]["providers"]}
        assert search_providers["duckduckgo"]["credential"] == "none"
        assert search_providers["searxng"]["credential"] == "base_url"
        assert "secret-key" not in settings.text
        assert "brave-secret" not in settings.text

        provider_updated = await _http_get(
            "http://127.0.0.1:"
            f"{port}/api/settings/provider/update?provider=openrouter"
            "&api_key=sk-or-test&api_base=https%3A%2F%2Fopenrouter.ai%2Fapi%2Fv1",
            headers={"Authorization": "Bearer tok"},
        )
        assert provider_updated.status_code == 200
        provider_body = provider_updated.json()
        assert provider_body["requires_restart"] is False
        provider_rows = {provider["name"]: provider for provider in provider_body["providers"]}
        assert provider_rows["openrouter"]["configured"] is True
        assert "sk-or-test" not in provider_updated.text

        updated = await _http_get(
            "http://127.0.0.1:"
            f"{port}/api/settings/update?model=openrouter/test"
            "&provider=openrouter",
            headers={"Authorization": "Bearer tok"},
        )
        assert updated.status_code == 200
        assert updated.json()["requires_restart"] is False

        search_updated = await _http_get(
            "http://127.0.0.1:"
            f"{port}/api/settings/web-search/update?provider=searxng"
            "&base_url=https%3A%2F%2Fsearch.example.com",
            headers={"Authorization": "Bearer tok"},
        )
        assert search_updated.status_code == 200
        search_body = search_updated.json()
        assert search_body["requires_restart"] is False
        assert search_body["web_search"]["provider"] == "searxng"
        assert search_body["web_search"]["api_key_hint"] is None
        assert search_body["web_search"]["base_url"] == "https://search.example.com"

        saved = load_config(config_path)
        assert saved.agents.defaults.model == "openrouter/test"
        assert saved.agents.defaults.provider == "openrouter"
        assert saved.providers.openrouter.api_key == "sk-or-test"
        assert saved.providers.openrouter.api_base == "https://openrouter.ai/api/v1"
        assert saved.tools.web.search.provider == "searxng"
        assert saved.tools.web.search.api_key == ""
        assert saved.tools.web.search.base_url == "https://search.example.com"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_commands_api_returns_slash_command_metadata(bus: MagicMock) -> None:
    port = 29892
    channel = _ch(bus, port=port)
    channel._api_tokens["tok"] = time.monotonic() + 300

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        denied = await _http_get(f"http://127.0.0.1:{port}/api/commands")
        assert denied.status_code == 401

        response = await _http_get(
            f"http://127.0.0.1:{port}/api/commands",
            headers={"Authorization": "Bearer tok"},
        )
        assert response.status_code == 200
        body = response.json()
        commands = {row["command"]: row for row in body["commands"]}
        assert commands["/stop"]["title"] == "Stop current task"
        assert commands["/history"]["arg_hint"] == "[n]"
        assert all("description" in row for row in body["commands"])
    finally:
        await channel.stop()
        await server_task


def test_settings_payload_normalizes_camel_case_provider(
    bus: MagicMock,
    monkeypatch,
    tmp_path,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.provider = "minimaxAnthropic"
    save_config(config, config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)

    body = _ch(bus)._settings_payload()

    assert body["agent"]["provider"] == "minimax_anthropic"


@pytest.mark.asyncio
async def test_end_to_end_server_pushes_streaming_deltas_to_client(bus: MagicMock) -> None:
    port = 29880
    channel = _ch(bus, port=port, streaming=True)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=stream-tester") as client:
            ready_raw = await client.recv()
            ready = json.loads(ready_raw)
            chat_id = ready["chat_id"]

            # Server pushes deltas directly
            await channel.send_delta(
                chat_id, "Hello ", {"_stream_delta": True, "_stream_id": "s1"}
            )
            await channel.send_delta(
                chat_id, "world", {"_stream_delta": True, "_stream_id": "s1"}
            )
            await channel.send_delta(
                chat_id, "", {"_stream_end": True, "_stream_id": "s1"}
            )

            delta1 = json.loads(await client.recv())
            assert delta1["event"] == "delta"
            assert delta1["text"] == "Hello "
            assert delta1["stream_id"] == "s1"

            delta2 = json.loads(await client.recv())
            assert delta2["event"] == "delta"
            assert delta2["text"] == "world"
            assert delta2["stream_id"] == "s1"

            end = json.loads(await client.recv())
            assert end["event"] == "stream_end"
            assert end["stream_id"] == "s1"

            await channel.send(OutboundMessage(
                channel="websocket",
                chat_id=chat_id,
                content="",
                metadata={"_turn_end": True},
            ))

            turn_end = json.loads(await client.recv())
            assert turn_end == {"event": "turn_end", "chat_id": chat_id}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_token_issue_rejects_when_at_capacity(bus: MagicMock) -> None:
    port = 29881
    channel = _ch(bus, port=port, tokenIssuePath="/auth/token", tokenIssueSecret="s")

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        # Fill issued tokens to capacity
        channel._issued_tokens = {
            f"nbwt_fill_{i}": time.monotonic() + 300 for i in range(channel._MAX_ISSUED_TOKENS)
        }

        resp = await _http_get(
            f"http://127.0.0.1:{port}/auth/token",
            headers={"Authorization": "Bearer s"},
        )
        assert resp.status_code == 429
        data = resp.json()
        assert "error" in data
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_allow_from_rejects_unauthorized_client_id(bus: MagicMock) -> None:
    port = 29882
    channel = _ch(bus, port=port, allowFrom=["alice", "bob"])

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=eve"):
                pass
        assert exc_info.value.response.status_code == 403
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_client_id_truncation(bus: MagicMock) -> None:
    port = 29883
    channel = _ch(bus, port=port)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        long_id = "x" * 200
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id={long_id}") as client:
            ready = json.loads(await client.recv())
            assert ready["client_id"] == "x" * 128
            assert len(ready["client_id"]) == 128
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_non_utf8_binary_frame_ignored(bus: MagicMock) -> None:
    port = 29884
    channel = _ch(bus, port=port)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=bin-test") as client:
            await client.recv()  # consume ready
            # Send non-UTF-8 bytes
            await client.send(b"\xff\xfe\xfd")
            await asyncio.sleep(0.05)
            # publish_inbound should NOT have been called
            bus.publish_inbound.assert_not_awaited()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_static_token_accepts_issued_token_as_fallback(bus: MagicMock) -> None:
    port = 29885
    channel = _ch(
        bus, port=port,
        token="static-secret",
        tokenIssuePath="/auth/token",
        tokenIssueSecret="route-secret",
    )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        # Get an issued token
        resp = await _http_get(
            f"http://127.0.0.1:{port}/auth/token",
            headers={"Authorization": "Bearer route-secret"},
        )
        assert resp.status_code == 200
        issued_token = resp.json()["token"]

        # Connect using issued token (not the static one)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={issued_token}&client_id=caller") as client:
            ready = json.loads(await client.recv())
            assert ready["event"] == "ready"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_allow_from_empty_list_denies_all(bus: MagicMock) -> None:
    port = 29886
    channel = _ch(bus, port=port, allowFrom=[])

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=anyone"):
                pass
        assert exc_info.value.response.status_code == 403
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_websocket_requires_token_without_issue_path(bus: MagicMock) -> None:
    """When websocket_requires_token is True but no token or issue path configured, all connections are rejected."""
    port = 29887
    channel = _ch(bus, port=port, websocketRequiresToken=True)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        # No token at all → 401
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=u"):
                pass
        assert exc_info.value.response.status_code == 401

        # Wrong token → 401
        with pytest.raises(websockets.exceptions.InvalidStatus) as exc_info:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=u&token=wrong"):
                pass
        assert exc_info.value.response.status_code == 401
    finally:
        await channel.stop()
        await server_task


# -- Multi-chat multiplexing -------------------------------------------------
#
# The multiplex protocol lets one WS connection route N logical chats over
# typed envelopes (`new_chat` / `attach` / `message`). Legacy frames must keep
# working on the connection's default chat_id.


@pytest.mark.asyncio
async def test_multiplex_legacy_still_works(bus: MagicMock) -> None:
    port = 29930
    channel = _ch(bus, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=legacy") as client:
            ready = json.loads(await client.recv())
            default_chat = ready["chat_id"]

            # Plain text frame routes to default chat_id
            await client.send("hello from legacy")
            await asyncio.sleep(0.1)
            inbound = bus.publish_inbound.call_args[0][0]
            assert inbound.chat_id == default_chat
            assert inbound.content == "hello from legacy"

            # {"content": ...} frame routes to default chat_id
            await client.send(json.dumps({"content": "structured legacy"}))
            await asyncio.sleep(0.1)
            assert bus.publish_inbound.call_args[0][0].chat_id == default_chat
            assert bus.publish_inbound.call_args[0][0].content == "structured legacy"

            # Outbound still reaches the legacy client, with chat_id annotated
            await channel.send(
                OutboundMessage(channel="websocket", chat_id=default_chat, content="reply")
            )
            reply = json.loads(await client.recv())
            assert reply["event"] == "message"
            assert reply["chat_id"] == default_chat
            assert reply["text"] == "reply"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_multiplex_new_chat_roundtrip(bus: MagicMock) -> None:
    port = 29931
    channel = _ch(bus, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=mp") as client:
            ready = json.loads(await client.recv())
            default_chat = ready["chat_id"]

            await client.send(json.dumps({"type": "new_chat"}))
            attached = json.loads(await client.recv())
            assert attached["event"] == "attached"
            new_chat = attached["chat_id"]
            assert new_chat and new_chat != default_chat

            # Send on the new chat via typed envelope
            await client.send(
                json.dumps({"type": "message", "chat_id": new_chat, "content": "hi on new"})
            )
            await asyncio.sleep(0.1)
            inbound = bus.publish_inbound.call_args[0][0]
            assert inbound.chat_id == new_chat
            assert inbound.content == "hi on new"

            # Server pushes a message back; chat_id must match
            await channel.send(
                OutboundMessage(channel="websocket", chat_id=new_chat, content="ok")
            )
            reply = json.loads(await client.recv())
            assert reply["event"] == "message"
            assert reply["chat_id"] == new_chat
            assert reply["text"] == "ok"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_multiplex_two_chats_isolated(bus: MagicMock) -> None:
    port = 29932
    channel = _ch(bus, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=two") as client:
            await client.recv()  # ready

            await client.send(json.dumps({"type": "new_chat"}))
            chat_a = json.loads(await client.recv())["chat_id"]
            await client.send(json.dumps({"type": "new_chat"}))
            chat_b = json.loads(await client.recv())["chat_id"]
            assert chat_a != chat_b

            # Push A → client sees A only (FIFO over the single WS).
            await channel.send(
                OutboundMessage(channel="websocket", chat_id=chat_a, content="for-A")
            )
            msg_a = json.loads(await client.recv())
            assert msg_a["chat_id"] == chat_a
            assert msg_a["text"] == "for-A"

            # Push B → client sees B only.
            await channel.send(
                OutboundMessage(channel="websocket", chat_id=chat_b, content="for-B")
            )
            msg_b = json.loads(await client.recv())
            assert msg_b["chat_id"] == chat_b
            assert msg_b["text"] == "for-B"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_multiplex_invalid_frames_return_error(bus: MagicMock) -> None:
    port = 29933
    channel = _ch(bus, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=bad") as client:
            await client.recv()  # ready

            # attach with bad chat_id
            await client.send(json.dumps({"type": "attach", "chat_id": "has space"}))
            err1 = json.loads(await client.recv())
            assert err1["event"] == "error"

            # message with missing content
            await client.send(json.dumps({"type": "message", "chat_id": "abc", "content": ""}))
            err2 = json.loads(await client.recv())
            assert err2["event"] == "error"

            # unknown type
            await client.send(json.dumps({"type": "nope"}))
            err3 = json.loads(await client.recv())
            assert err3["event"] == "error"

            # Connection survives: legacy frame still works.
            await client.send("still-alive")
            await asyncio.sleep(0.1)
            bus.publish_inbound.assert_awaited()
            assert bus.publish_inbound.call_args[0][0].content == "still-alive"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_multiplex_cleanup_on_disconnect(bus: MagicMock) -> None:
    port = 29934
    channel = _ch(bus, port=port)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=dc") as client:
            ready = json.loads(await client.recv())
            default_chat = ready["chat_id"]
            await client.send(json.dumps({"type": "new_chat"}))
            extra_chat = json.loads(await client.recv())["chat_id"]
            assert default_chat in channel._subs
            assert extra_chat in channel._subs
        # Client gone. Server-side tracking must be empty.
        await asyncio.sleep(0.2)
        assert default_chat not in channel._subs
        assert extra_chat not in channel._subs
        assert not channel._conn_chats
        assert not channel._conn_default
    finally:
        await channel.stop()
        await server_task


def test_parse_envelope_detects_typed_frames() -> None:
    assert _parse_envelope('{"type":"new_chat"}') == {"type": "new_chat"}
    env = _parse_envelope('{"type":"message","chat_id":"abc","content":"hi"}')
    assert env == {"type": "message", "chat_id": "abc", "content": "hi"}


def test_parse_envelope_rejects_legacy_and_garbage() -> None:
    # No `type` field → legacy, caller falls back to _parse_inbound_payload.
    assert _parse_envelope('{"content":"hi"}') is None
    assert _parse_envelope("plain text") is None
    assert _parse_envelope("{broken") is None
    assert _parse_envelope("[1,2,3]") is None
    # Non-string `type` is not a valid envelope.
    assert _parse_envelope('{"type":123}') is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", True),
        ("a1b2_c:d-e", True),
        ("x" * 64, True),
        ("unified:default", True),
        ("", False),
        ("x" * 65, False),
        ("has space", False),
        ("a/b", False),
        ("a.b", False),
        (None, False),
        (123, False),
    ],
)
def test_is_valid_chat_id(value: Any, expected: bool) -> None:
    assert _is_valid_chat_id(value) is expected


@pytest.mark.asyncio
async def test_v065_text_upload_chat_authorization_and_resume(bus: MagicMock, tmp_path: Path, monkeypatch) -> None:
    import nanobot.channels.websocket as websocket_module

    media_root = tmp_path / "media"
    monkeypatch.setattr(
        websocket_module, "websocket_media_dir", lambda chat_id: media_root / chat_id
    )
    channel = _ch(bus, port=29941)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    payload = base64.b64encode("会议日期2026-10-12".encode()).decode()
    media = [{"data_url": f"data:text/plain;base64,{payload}", "name": "notice.txt"}]
    try:
        async with websockets.connect("ws://127.0.0.1:29941/ws?client_id=shared") as a:
            ready_a = json.loads(await a.recv())
            await a.send(json.dumps({
                "type": "message", "chat_id": ready_a["chat_id"], "content": "",
                "media": media,
            }))
            await asyncio.sleep(0.1)
            uploaded = Path(bus.publish_inbound.await_args_list[-1].args[0].media[0])
            assert uploaded.read_bytes() == "会议日期2026-10-12".encode()
            assert uploaded.name == "notice.txt"
            assert uploaded.is_relative_to(media_root / ready_a["chat_id"])
            file_set = {p for p in media_root.rglob("*") if p.is_file()}

            async with websockets.connect("ws://127.0.0.1:29941/ws?client_id=shared") as b:
                ready_b = json.loads(await b.recv())
                assert ready_b["chat_id"] != ready_a["chat_id"]
                for envelope in (
                    {"type": "attach", "chat_id": ready_a["chat_id"]},
                    {"type": "message", "chat_id": ready_a["chat_id"], "content": "steal", "media": media},
                ):
                    await b.send(json.dumps(envelope))
                    rejected = json.loads(await b.recv())
                    assert rejected == {"event": "error", "detail": "chat_unauthorized"}
                assert {p for p in media_root.rglob("*") if p.is_file()} == file_set
                assert bus.publish_inbound.await_count == 1

            async with websockets.connect("ws://127.0.0.1:29941/ws?client_id=spoofed-A") as resumed:
                await resumed.recv()
                await resumed.send(json.dumps({
                    "type": "attach", "chat_id": ready_a["chat_id"],
                    "chat_token": ready_a["chat_token"],
                }))
                assert json.loads(await resumed.recv())["event"] == "attached"
                await resumed.send(json.dumps({
                    "type": "message", "chat_id": ready_a["chat_id"], "content": "read notice",
                }))
                await asyncio.sleep(0.1)
                assert bus.publish_inbound.await_count == 2
                assert bus.publish_inbound.await_args_list[-1].args[0].chat_id == ready_a["chat_id"]
                await channel.send(OutboundMessage(
                    channel="websocket", chat_id=ready_a["chat_id"], content="A private reply",
                ))
                resumed_frame = json.loads(await resumed.recv())
                original_frame = json.loads(await a.recv())
                assert resumed_frame["chat_id"] == original_frame["chat_id"] == ready_a["chat_id"]
                assert resumed_frame["text"] == original_frame["text"] == "A private reply"
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_v065_image_upload_remains_supported(bus: MagicMock, tmp_path: Path, monkeypatch) -> None:
    import nanobot.channels.websocket as websocket_module

    monkeypatch.setattr(websocket_module, "websocket_media_dir", lambda chat_id: tmp_path / chat_id)
    channel = _ch(bus, port=29942)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
    )
    png = base64.b64encode(png_bytes).decode()
    try:
        async with websockets.connect("ws://127.0.0.1:29942/ws?client_id=image") as client:
            ready = json.loads(await client.recv())
            await client.send(json.dumps({
                "type": "message", "chat_id": ready["chat_id"], "content": "image",
                "media": [{"data_url": f"data:image/png;base64,{png}", "name": "image.png"}],
            }))
            await asyncio.sleep(0.1)
            inbound = bus.publish_inbound.await_args_list[-1].args[0]
            assert Path(inbound.media[0]).read_bytes() == png_bytes
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_valid_protected_bootstrap_token_is_operator_but_arbitrary_token_is_not(
    bus: MagicMock,
) -> None:
    channel = _ch(bus, port=29943, tokenIssueSecret="bootstrap-secret")
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        async with websockets.connect("ws://127.0.0.1:29943/ws?client_id=owner") as owner:
            owner_chat = json.loads(await owner.recv())["chat_id"]
            async with httpx.AsyncClient() as http_client:
                response = await http_client.get(
                    "http://127.0.0.1:29943/webui/bootstrap",
                    headers={"Authorization": "Bearer bootstrap-secret"},
                )
            assert response.status_code == 200
            api_token = response.json()["token"]
            async with websockets.connect(
                f"ws://127.0.0.1:29943/ws?client_id=operator&token={api_token}"
            ) as operator:
                await operator.recv()
                await operator.send(json.dumps({"type": "attach", "chat_id": owner_chat}))
                assert json.loads(await operator.recv())["event"] == "attached"

            async with websockets.connect(
                "ws://127.0.0.1:29943/ws?client_id=owner&token=made-up"
            ) as spoof:
                await spoof.recv()
                await spoof.send(json.dumps({"type": "attach", "chat_id": owner_chat}))
                assert json.loads(await spoof.recv()) == {
                    "event": "error", "detail": "chat_unauthorized",
                }
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_public_local_bootstrap_token_keeps_single_user_operator_contract(bus: MagicMock) -> None:
    """The localhost-only public bootstrap matches the existing single-user REST API contract."""
    channel = _ch(bus, port=29944, tokenIssueSecret="")
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        async with websockets.connect("ws://127.0.0.1:29944/ws?client_id=owner") as owner:
            owner_chat = json.loads(await owner.recv())["chat_id"]
            async with httpx.AsyncClient() as http_client:
                response = await http_client.get("http://127.0.0.1:29944/webui/bootstrap")
            assert response.status_code == 200
            api_token = response.json()["token"]
            async with websockets.connect(
                f"ws://127.0.0.1:29944/ws?client_id=single-user-operator&token={api_token}"
            ) as operator:
                await operator.recv()
                await operator.send(json.dumps({"type": "attach", "chat_id": owner_chat}))
                attached = json.loads(await operator.recv())
                assert attached == {"event": "attached", "chat_id": owner_chat}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_websocket_session_history_lifecycle_and_access_boundary(
    bus: MagicMock, tmp_path: Path,
) -> None:
    port = 29945
    manager = SessionManager(tmp_path, allow_legacy=False)
    base = _ch(bus, port=port)
    channel = WebSocketChannel(base.config, bus, session_manager=manager)
    channel._api_tokens["history-test"] = time.monotonic() + 300
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    headers = {"Authorization": "Bearer history-test"}

    def messages_url(key: str) -> str:
        return f"http://127.0.0.1:{port}/api/sessions/{key.replace(':', '%3A')}/messages"

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=history-owner") as client:
            ready = json.loads(await client.recv())
            ready_key = f"websocket:{ready['chat_id']}"
            assert not manager._get_session_path(ready_key).exists()
            listed = await _http_get(f"http://127.0.0.1:{port}/api/sessions", headers)
            assert listed.status_code == 200
            assert ready_key not in {row["key"] for row in listed.json()["sessions"]}

            await client.send(json.dumps({
                "type": "message", "chat_id": ready["chat_id"], "content": "first default turn",
            }))
            await asyncio.sleep(0.05)
            assert bus.publish_inbound.await_count == 1
            assert manager._get_session_path(ready_key).is_file()
            first_history = await _http_get(messages_url(ready_key), headers)
            assert first_history.status_code == 200
            assert first_history.json()["messages"] == []

            # Explicit new chats become readable before the attached ack lets
            # the WebUI select the ID and start its eager history request.
            await client.send(json.dumps({"type": "new_chat"}))
            attached = json.loads(await client.recv())
            assert attached["event"] == "attached"
            new_key = f"websocket:{attached['chat_id']}"
            assert manager._get_session_path(new_key).is_file()
            new_history = await _http_get(messages_url(new_key), headers)
            assert new_history.status_code == 200
            assert new_history.json()["messages"] == []

            session = manager.get_or_create(new_key)
            session.add_message("user", "persisted history")
            manager.save(session)
            saved_history = await _http_get(messages_url(new_key), headers)
            assert saved_history.status_code == 200
            assert saved_history.json()["messages"][0]["content"] == "persisted history"

            unauthorized = await _http_get(messages_url(new_key))
            assert unauthorized.status_code == 401
            unknown_key = "websocket:unknown-server-id"
            unknown = await _http_get(messages_url(unknown_key), headers)
            assert unknown.status_code == 404

            corrupt_key = "websocket:corrupt-session"
            manager._get_session_path(corrupt_key).write_text("not-json\n", encoding="utf-8")
            corrupt = await _http_get(messages_url(corrupt_key), headers)
            assert corrupt.status_code == 404

            await client.send(json.dumps({"type": "attach", "chat_id": "not-server-issued"}))
            attach_error = json.loads(await client.recv())
            assert attach_error == {"event": "error", "detail": "chat_unauthorized"}
            assert not manager._get_session_path("websocket:not-server-issued").exists()

            deleted = await _http_get(
                f"http://127.0.0.1:{port}/api/sessions/{new_key.replace(':', '%3A')}/delete",
                headers,
            )
            assert deleted.status_code == 200
            assert deleted.json()["deleted"] is True
            assert (await _http_get(messages_url(new_key), headers)).status_code == 404

        # A reconnect's default ready ID remains provisional and does not add
        # an empty session to either disk or the WebUI session list.
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?client_id=reconnect") as client:
            reconnect = json.loads(await client.recv())
            reconnect_key = f"websocket:{reconnect['chat_id']}"
            assert not manager._get_session_path(reconnect_key).exists()
            listed = await _http_get(f"http://127.0.0.1:{port}/api/sessions", headers)
            assert reconnect_key not in {row["key"] for row in listed.json()["sessions"]}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_failed_server_chat_session_save_blocks_ack_and_message_ingress(
    bus: MagicMock,
) -> None:
    from nanobot.session.manager import Session

    base = _ch(bus)
    manager = MagicMock()
    manager.get_or_create.return_value = Session(key="websocket:pending")
    manager.save.side_effect = PermissionError("session storage unavailable")
    channel = WebSocketChannel(base.config, bus, session_manager=manager)

    new_chat_connection = MagicMock()
    new_chat_connection.send = AsyncMock()
    with pytest.raises(PermissionError, match="session storage unavailable"):
        await channel._dispatch_envelope(new_chat_connection, "owner", {"type": "new_chat"})
    new_chat_connection.send.assert_not_awaited()
    assert not channel._subs

    default_connection = MagicMock()
    default_connection.send = AsyncMock()
    channel._unpersisted_default_chats.add("default-pending")
    channel._conn_owned_chats[default_connection] = {"default-pending"}
    with pytest.raises(PermissionError, match="session storage unavailable"):
        await channel._dispatch_envelope(default_connection, "owner", {
            "type": "message", "chat_id": "default-pending", "content": "do not publish",
        })
    assert bus.publish_inbound.await_count == 0
    assert not channel._subs
    assert "default-pending" in channel._unpersisted_default_chats

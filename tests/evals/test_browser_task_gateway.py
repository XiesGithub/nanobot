import asyncio
import json
from types import SimpleNamespace
from urllib.parse import quote, unquote

import pytest
from aiohttp import ClientSession
from websockets.asyncio.client import connect

from evals.browser_task_gateway import (
    BrowserTaskGateway,
    EvaluationGateTool,
    ScriptedBrowserProvider,
    _emit_operation_event,
    _instrument_standard_browser_provider,
    _provider_ledger_summary,
)


@pytest.mark.asyncio
async def test_scripted_provider_still_requests_real_edit_tool():
    provider = ScriptedBrowserProvider("V074")
    first = await provider.chat([], model="deepseek-v4-flash")
    assert first.tool_calls[0].name == "read_file"
    edit = await provider.chat([{"role": "tool", "tool_call_id": "browser-v074-read-1",
                                "content": '{"timeout":30,"retries":3}'}],
                               model="deepseek-v4-flash")
    assert edit.tool_calls[0].name == "edit_file"
    assert edit.tool_calls[0].arguments == {
        "path": "config.json", "old_text": '"timeout":30', "new_text": '"timeout":60'
    }
    final = await provider.chat([{"role": "tool", "content": "Successfully edited"}],
                                model="deepseek-v4-flash")
    assert final.content and "60" in final.content and "3" in final.content


@pytest.mark.asyncio
async def test_v075_script_uses_real_ask_and_write_tools():
    provider = ScriptedBrowserProvider("V075")
    ask = await provider.chat([], model="deepseek-v4-flash")
    assert ask.tool_calls[0].name == "ask_user"
    assert ask.tool_calls[0].arguments["options"] == ["表格", "JSON"]
    write = await provider.chat([{"role": "tool", "tool_call_id": "browser-v075-ask-1",
                                 "content": "JSON"}], model="deepseek-v4-flash")
    assert write.tool_calls[0].name == "write_file"
    assert write.tool_calls[0].arguments == {"path": "report.txt", "content": '{"total":150}'}


@pytest.mark.asyncio
async def test_v079_script_uses_live_mail_evidence_for_answer_and_audit():
    provider = ScriptedBrowserProvider("V079")
    plan = await provider.chat([{"role": "user", "content": json.dumps({
        "schema": {"title": "QueryPlan"}, "text": "竹桥预算是多少？"
    }, ensure_ascii=False)}], model="deepseek-v4-flash")
    assert json.loads(plan.content) == {
        "intent": "mail_fact",
        "queries": [{"query": "竹桥预算", "scope_action": "replace", "mode": "keyword", "limit": 8}],
    }
    draft = await provider.chat([{"role": "user", "content": json.dumps({
        "schema": {"title": "AnswerDraft"},
        "evidence": [{"evidence_id": "ev_fixture", "excerpt": "竹桥预算120元"}],
    }, ensure_ascii=False)}], model="deepseek-v4-flash")
    assert json.loads(draft.content) == {
        "status": "answered",
        "claims": [{"text": "竹桥预算为120元。", "evidence_ids": ["ev_fixture"]}],
    }
    audit = await provider.chat([{"role": "user", "content": json.dumps([{"claim": {"text": "120元"}}])}],
                                model="deepseek-v4-flash")
    assert json.loads(audit.content) == {"supported": [True]}


@pytest.mark.asyncio
async def test_v079_webui_owner_source_read_then_real_revoke_denies_reread(tmp_path):
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    provider = ScriptedBrowserProvider("V079")
    gateway = await BrowserTaskGateway.create(
        profile="B-scripted", runtime=runtime, workspace=workspace, provider=provider,
        control={"case_id": "V079"},
    )
    try:
        async with ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{gateway.port}/webui/bootstrap",
                                headers={"X-Nanobot-Auth": "SYNTHETIC-V079-OWNER-LOGIN"}) as response:
                assert response.status == 200
                bootstrap = await response.json()
                assert bootstrap["mail_owner"] is True
                token = bootstrap["token"]
            headers = {"Authorization": f"Bearer {token}"}
            ws_url = f"{gateway.url}&token={quote(token, safe='')}"
            async with connect(ws_url) as ws:
                ready = json.loads(await ws.recv())
                chat_id = ready["chat_id"]
                await ws.send(json.dumps({"type": "message", "chat_id": chat_id,
                                          "content": "竹桥预算是多少？给出邮件依据", "webui": True},
                                         ensure_ascii=False))
                frames = []
                while True:
                    frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                    frames.append(frame)
                    if frame.get("event") == "turn_end":
                        break
                message = next((frame for frame in frames if frame.get("event") == "message"), None)
                assert message is not None, {"frames": frames, "deliveries": gateway.observation["deliveries"],
                                             "provider": provider.calls,
                                             "mail_auth": gateway.observation["mail_auth"]}
                assert "120元" in message["text"]
                assert len(message["citations"]) == 1
                evidence_id = message["citations"][0]["evidence_id"]
                source_url = (f"http://127.0.0.1:{gateway.port}/api/mail-knowledge/sources/"
                              f"{quote(evidence_id, safe='')}?chat_id={quote(chat_id, safe='')}")
                async with http.get(source_url, headers=headers) as response:
                    assert response.status == 200
                    source = await response.json()
                    assert source["sources"][0]["excerpt"] == "竹桥预算120元"
                history_url = (f"http://127.0.0.1:{gateway.port}/api/sessions/"
                               f"{quote('websocket:' + chat_id, safe='')}/messages")
                async with http.get(history_url, headers=headers) as response:
                    assert response.status == 200
                    assert "竹桥预算120元" in await response.text()
            async with http.post(gateway.observation["permission_revoke_url"]) as response:
                assert response.status == 200
                assert (await response.json())["revoked"] is True
            async with http.get(source_url, headers=headers) as response:
                assert response.status == 403
                denied = await response.text()
                assert "竹桥预算120元" not in denied
            async with http.get(history_url, headers=headers) as response:
                assert response.status == 403
                assert "竹桥预算120元" not in await response.text()
            async with http.get(f"http://127.0.0.1:{gateway.port}/webui/bootstrap",
                                headers={"X-Nanobot-Auth": "SYNTHETIC-V079-OWNER-LOGIN"}) as response:
                assert response.status == 403
                assert "竹桥预算120元" not in await response.text()
        mail_auth = gateway.observation["mail_auth"]
        assert mail_auth["issued_tokens"][0]["principal_id"] == gateway.mail_binding.principal_id
        assert mail_auth["websocket_bindings"][0]["binding_id"] == gateway.mail_binding.binding_id
        assert [row["status"] for row in mail_auth["http_requests"]
                if "/sources/" in row["path"]] == [200, 403]
        assert mail_auth["http_authorizations"][-1]["status"] == "denied"
        assert mail_auth["revocations"][0]["status"] == "revoked"
        assert any(row["status"] == "allowed" and row["sources"][0]["excerpt"] == "竹桥预算120元"
                   for row in mail_auth["source_reads"])
        assert gateway.observation["allowed_tools"] == []
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_v080_scripted_provider_returns_mobile_fixture_without_tools():
    provider = ScriptedBrowserProvider("V080")
    result = await provider.chat([], model="deepseek-v4-flash")
    assert result.content == "MOBILE17"
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_standard_provider_ledger_tracks_actual_nonstream_sdk_usage():
    dispatched = []

    async def create(**kwargs):
        dispatched.append(kwargs)
        return SimpleNamespace(model="deepseek-v4-flash", usage=SimpleNamespace(
            prompt_tokens=21, completion_tokens=7,
        ))

    provider = SimpleNamespace(_client=SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create),
    )))
    _instrument_standard_browser_provider(provider, emit=None)
    result = await provider._client.chat.completions.create(
        model="deepseek-v4-flash", stream=False,
        messages=[{"role": "user", "content": "synthetic question"}],
    )

    assert result.model == "deepseek-v4-flash"
    assert dispatched[0]["stream"] is False
    assert provider._browser_request_records[0]["terminal"] is True
    assert provider._browser_request_records[0]["usage"] == {
        "prompt_tokens": 21, "completion_tokens": 7,
    }
    assert _provider_ledger_summary(provider) == {
        "requests": 1, "known_tokens": 28, "request_limit": 8,
        "token_limit": 32000, "provider_closed": False,
        "completed": 1, "errors": 0,
        "pending": 0, "usage_complete": True, "token_budget_overrun": False,
    }


@pytest.mark.asyncio
async def test_standard_provider_ledger_closes_failed_sdk_attempt():
    async def fail(**_kwargs):
        raise OSError("synthetic transport failure")

    provider = SimpleNamespace(_client=SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=fail),
    )))
    _instrument_standard_browser_provider(provider, emit=None)

    with pytest.raises(OSError, match="synthetic transport failure"):
        await provider._client.chat.completions.create(
            model="deepseek-v4-flash", stream=False, messages=[],
        )

    assert provider._browser_request_records[0]["status"] == "error"
    assert provider._browser_request_records[0]["terminal"] is True
    summary = _provider_ledger_summary(provider)
    assert summary["errors"] == 1
    assert summary["pending"] == 0
    assert summary["usage_complete"] is False


@pytest.mark.asyncio
async def test_v079_b_real_selects_standard_nonstream_provider(monkeypatch, tmp_path):
    import evals.channel_task_worker as channel_worker
    import evals.worker as worker

    async def unused_create(**_kwargs):
        raise AssertionError("test does not dispatch a model request")

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=unused_create))

        async def close(self):
            return None

    provider = ScriptedBrowserProvider("V079")
    provider._client = FakeClient()
    calls = []

    def real_provider_factory(credentials, _emit):
        calls.append(credentials)
        return provider

    monkeypatch.setattr(worker, "real_provider", real_provider_factory)
    monkeypatch.setattr(channel_worker, "native_provider", lambda *_args: pytest.fail(
        "V079 protected-mail requests must use the standard non-streaming provider"
    ))
    gateway = await BrowserTaskGateway.create(
        profile="B-real", runtime=tmp_path / "runtime", workspace=tmp_path / "workspace",
        credentials=tmp_path / "api.txt", control={"case_id": "V079"},
    )
    try:
        assert calls == [tmp_path / "api.txt"]
        assert provider.observation_mode == "real_nonstream_protected_mail"
        assert provider._browser_ledger_kind == "sdk_attempts"
        assert gateway.observation["allowed_tools"] == []
    finally:
        evidence = await gateway.close()
    assert evidence["provider_ledger_summary"]["provider_closed"] is True


@pytest.mark.asyncio
async def test_v080_real_gateway_delivers_mobile_fixture_answer(tmp_path):
    gateway = await BrowserTaskGateway.create(
        profile="B-scripted", runtime=tmp_path / "runtime", workspace=tmp_path / "workspace",
        provider=ScriptedBrowserProvider("V080"), control={"case_id": "V080"},
    )
    try:
        async with connect(gateway.url) as ws:
            ready = json.loads(await ws.recv())
            chat_id = ready["chat_id"]
            await ws.send(json.dumps({"type": "message", "chat_id": chat_id,
                                      "content": "请回复测试码", "webui": True}, ensure_ascii=False))
            frames = []
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                frames.append(frame)
                if frame.get("event") == "turn_end":
                    break
        visible_text = "".join(
            frame.get("text", "")
            for frame in frames
            if frame.get("event") == "delta"
            or (frame.get("event") == "message" and frame.get("kind") not in {"progress", "tool_hint"})
        )
        assert visible_text == "MOBILE17", frames
        gateway.snapshot()
        assert gateway.observation["allowed_tools"] == []
        assert gateway.observation["tool_events"] == []
        assert len(gateway.observation["browser_submissions"]) == 1
        assert len(gateway.observation["provider_ledger"]) == 1
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_evaluation_gate_waits_for_release_and_propagates_cancel():
    class FakeGateway:
        def __init__(self):
            self.gates = {}
            self.observation = {"gates": []}
            self.emit = None

    gateway = FakeGateway()
    tool = EvaluationGateTool(gateway)
    task = asyncio.create_task(tool.execute("case-A"))
    for _ in range(20):
        if gateway.gates.get("case-A", {}).get("state") == "waiting":
            break
        await asyncio.sleep(0)
    assert gateway.gates["case-A"]["state"] == "waiting"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.gates["case-A"]["state"] == "cancelled"
    assert gateway.gates["case-A"]["cancellations"] == 1


def test_tool_operation_events_do_not_create_unclosed_trace_spans():
    from evals.trace import analyze_trace
    from evals.worker import Emitter
    from nanobot.utils.observability import observation_context, span

    class Connection:
        def __init__(self):
            self.rows = []

        def send(self, row):
            self.rows.append(row["event"])

    connection = Connection()
    emitter = Emitter(connection, "run", "trial")
    with observation_context(emitter.event, "trial"):
        with span("task"):
            with span("tool", name="edit_file", call_id="edit-1", execution_started=False):
                _emit_operation_event(emitter, "browser_task_tool", "start", name="edit_file")
                _emit_operation_event(emitter, "browser_task_tool", "end", name="edit_file")
    result = analyze_trace(connection.rows)
    assert result["errors"] == []
    operation_events = [row for row in connection.rows if row["type"] == "browser_task_tool"]
    assert [row["phase"] for row in operation_events] == ["point", "point"]
    assert [row["payload"]["operation_phase"] for row in operation_events] == ["start", "end"]


@pytest.mark.asyncio
async def test_loopback_gateway_runs_agent_and_edits_workspace(tmp_path):
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    runtime.mkdir()
    workspace.mkdir()
    config_file = workspace / "config.json"
    config_file.write_text('{"timeout":30,"retries":3}', encoding="utf-8")
    provider = ScriptedBrowserProvider("V074")
    gateway = await BrowserTaskGateway.create(profile="B-scripted", runtime=runtime,
                                              workspace=workspace, provider=provider,
                                              control={"case_id": "V074"})
    frames = []
    try:
        async with connect(gateway.url) as ws:
            ready = json.loads(await ws.recv())
            assert ready["event"] == "ready"
            chat_id = ready["chat_id"]
            await ws.send(json.dumps({"type": "message", "chat_id": chat_id,
                                      "content": "把config.json的timeout改为60，retries保持3。",
                                      "webui": True}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                frames.append(frame)
                if frame.get("event") == "turn_end":
                    break
        assert json.loads(config_file.read_text(encoding="utf-8")) == {"timeout": 60, "retries": 3}
        assert any(row.get("name") == "edit_file" and row.get("phase") == "end"
                   for row in gateway.observation["tool_events"])
        assert any(row.get("event") == "turn_end" for row in frames)
        async with ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{gateway.port}/webui/bootstrap") as response:
                token = (await response.json())["token"]
            headers = {"Authorization": f"Bearer {token}"}
            async with http.get(f"http://127.0.0.1:{gateway.port}/api/sessions", headers=headers) as response:
                assert response.status == 200
                sessions = await response.json()
            assert sessions["sessions"]
            session_key = quote(sessions["sessions"][0]["key"], safe="")
            async with http.get(f"http://127.0.0.1:{gateway.port}/api/sessions/{session_key}/messages",
                                headers=headers) as response:
                assert response.status == 200
                history = await response.json()
                assert history["messages"]
        observed_paths = {unquote(row["path"]) for row in gateway.observation["history_http_requests"]}
        assert observed_paths >= {
            "/api/sessions", f"/api/sessions/{unquote(session_key)}/messages"
        }
        async with ClientSession() as http:
            async with http.get(f"{gateway.observation['control_url']}?probe_id=integration-check") as response:
                probe = await response.json()
                assert response.status == 200
        assert probe["probe_id"] == "integration-check"
        assert probe["workspace_state"]["config.json"]["text"] == '{"timeout":60,"retries":3}'
        assert probe["session_ids"]
        assert probe["snapshot_sha256"]
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_v077_seeds_two_persisted_empty_sessions_without_model_calls(tmp_path):
    runtime, workspace = tmp_path / "runtime", tmp_path / "workspace"
    provider = ScriptedBrowserProvider("V077")
    gateway = await BrowserTaskGateway.create(
        profile="B-scripted", runtime=runtime, workspace=workspace, provider=provider,
        control={"case_id": "V077"},
    )
    try:
        fixtures = gateway.observation["fixture_sessions"]
        assert set(fixtures) == {"a", "b"}
        assert fixtures["a"]["chat_id"] != fixtures["b"]["chat_id"]
        assert all(row["initial_messages"] == 0 for row in fixtures.values())
        assert all(row["messages"] == [] and row["session_sha256"] == row["initial_session_sha256"]
                   for row in fixtures.values())
        assert provider.calls == []
        async with ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{gateway.port}/webui/bootstrap") as response:
                token = (await response.json())["token"]
            async with http.get(f"http://127.0.0.1:{gateway.port}/api/sessions",
                                headers={"Authorization": f"Bearer {token}"}) as response:
                assert response.status == 200
                payload = await response.json()
        assert {row["title"] for row in payload["sessions"]} == {"V077-A", "V077-B"}
    finally:
        await gateway.close()

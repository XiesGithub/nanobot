import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
from nanobot.knowledge.mail.service import MailService
from nanobot.security.context import SecurityContext
from tests.knowledge.mail.test_email_ingress import setup


def completed_message(channel):
    item = channel._fetch_new_messages()[0]
    security = item["_security"]
    dispatch = channel._mail_ingress.dispatch
    assert dispatch.transition(security.task_id, "pending", "dispatching")
    assert dispatch.transition(security.task_id, "dispatching", "running")
    assert dispatch.transition(security.task_id, "running", "completed")
    channel.config.smtp_host = "smtp.example.test"
    return OutboundMessage("email", item["sender"], "Fictional answer", security=security)


def delivery(service):
    with service.store.connect() as db:
        return dict(db.execute("SELECT * FROM interaction_deliveries").fetchone())


@pytest.mark.parametrize('archive', [False, True])
@pytest.mark.parametrize('trigger', [False, True])
@pytest.mark.parametrize('auto_reply', [False, True])
@pytest.mark.asyncio
async def test_archive_trigger_auto_reply_eight_mode_combinations(tmp_path, monkeypatch, archive, trigger, auto_reply):
    service, channel, fake = setup(tmp_path, monkeypatch, archive=archive, trigger=trigger)
    channel.config.auto_reply_enabled = auto_reply
    channel.config.smtp_host = 'smtp.example.test'
    calls = []
    monkeypatch.setattr(channel, '_smtp_send', calls.append)
    items = channel._fetch_new_messages()
    assert len(items) == int(trigger)
    with service.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM mail_items').fetchone()[0] == int(archive)
    if items:
        item = items[0]
        security = item['_security']
        dispatch = channel._mail_ingress.dispatch
        for previous, new in [('pending', 'dispatching'), ('dispatching', 'running'), ('running', 'completed')]:
            assert dispatch.transition(security.task_id, previous, new)
        # EmailChannel.start records this trusted projection before dispatch.
        channel._last_subject_by_chat[item['sender']] = item['subject']
        await channel.send(OutboundMessage('email', item['sender'], 'Fictional', security=security))
    assert len(calls) == int(trigger and auto_reply)
    assert fake.content_fetches == int(archive or trigger)


@pytest.mark.asyncio
async def test_completion_is_not_smtp_success_and_duplicate_delivery_is_suppressed(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    assert delivery(service)["status"] == "queued"
    calls = []
    monkeypatch.setattr(channel, "_smtp_send", calls.append)
    await channel.send(message)
    await channel.send(message)
    assert len(calls) == 1
    assert delivery(service)["status"] == "submitted"  # Server acceptance, not recipient delivery.


@pytest.mark.asyncio
async def test_uncertain_smtp_is_not_retried_or_reported_as_submitted(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    calls = []

    def lost_reply(mail):
        calls.append(mail)
        raise OSError("Fictional lost SMTP reply")

    monkeypatch.setattr(channel, "_smtp_send", lost_reply)
    await channel.send(message)
    await channel.send(message)
    assert len(calls) == 1
    assert delivery(service)["status"] == "uncertain"
    assert delivery(service)["error_code"] == "smtp_result_uncertain"


@pytest.mark.asyncio
async def test_disable_then_reenable_does_not_release_old_reply(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    service.set_mode(archive_enabled=True, trigger_agent=False, expected_revision=2)
    service.set_mode(archive_enabled=True, trigger_agent=True, expected_revision=3)
    monkeypatch.setattr(channel, "_smtp_send", lambda _: pytest.fail("Old reply must be suppressed"))
    await channel.send(message)
    assert delivery(service)["status"] == "suppressed"


@pytest.mark.asyncio
async def test_old_attempt_message_cannot_consume_new_attempt_delivery(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    with service.store.connect(write=True) as db:
        db.execute("UPDATE interaction_dispatches SET attempt_id='attempt_new'")
        db.execute("INSERT INTO interaction_deliveries VALUES('attempt_new',?,'queued',NULL,0)",
                   (message.security.task_id,))
    monkeypatch.setattr(channel, "_smtp_send", lambda _: pytest.fail("Stale attempt must not send"))
    await channel.send(message)
    with service.store.connect() as db:
        assert db.execute("SELECT status FROM interaction_deliveries WHERE attempt_id='attempt_new'").fetchone()[0] == "queued"


@pytest.mark.asyncio
async def test_missing_smtp_records_not_sent_and_crash_recovery_preserves_uncertainty(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    channel.config.smtp_host = ""
    await channel.send(message)
    assert delivery(service)["status"] == "not_sent"
    with service.store.connect(write=True) as db:
        db.execute("UPDATE interaction_deliveries SET status='sending'")
    service.store.recover()
    assert delivery(service)["status"] == "uncertain"


@pytest.mark.asyncio
async def test_mode_change_from_independent_service_cancels_running_generation(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    item = channel._fetch_new_messages()[0]
    security = item["_security"]
    channel._mail_ingress.dispatch.transition(security.task_id, "pending", "dispatching")
    # No live Worker or provider in this cancellation test.
    bridge = MailTurnExtension(SimpleNamespace(store=service.store, access=service.access,
                                              config=service.config))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(spec):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop = SimpleNamespace(model="deepseek-v4-flash", tools=ToolRegistry(),
        runner=SimpleNamespace(run=run), max_iterations=2, max_tool_result_chars=10000)
    message = InboundMessage("email", item["sender"], item["sender"], "Fictional input", security=security)
    task = asyncio.create_task(bridge.intercept(loop, message))
    await asyncio.wait_for(started.wait(), 2)
    independent = MailService(service.store.root, service.config, worker_enabled=False)
    result = await asyncio.to_thread(independent.set_mode,
        archive_enabled=True, trigger_agent=False, expected_revision=2)
    assert result["cancellation_requested"] == 1
    assert await asyncio.wait_for(task, 2) == (True, None)
    assert cancelled.is_set()
    with service.store.connect() as db:
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "cancelled"
        assert db.execute("SELECT COUNT(*) FROM interaction_deliveries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_degraded_untracked_interaction_is_still_cancelled_on_mode_change(tmp_path, monkeypatch):
    service, _, _ = setup(tmp_path, monkeypatch, trigger=True)
    bridge = MailTurnExtension(SimpleNamespace(store=service.store, access=service.access,
                                              config=service.config, mode=service.mode))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def run(spec):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    loop = SimpleNamespace(model="deepseek-v4-flash", tools=ToolRegistry(),
        runner=SimpleNamespace(run=run), max_iterations=2, max_tool_result_chars=10000)
    security = SecurityContext(origin_kind="email_ingress", source_key="fictional", mode_revision=2)
    message = InboundMessage("email", "sender@example.test", "sender@example.test", "Fictional", security=security)
    task = asyncio.create_task(bridge.intercept(loop, message))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.to_thread(service.set_mode, archive_enabled=True, trigger_agent=False, expected_revision=2)
    assert await asyncio.wait_for(task, 2) == (True, None)
    assert cancelled.is_set()
    service.set_mode(archive_enabled=True, trigger_agent=True, expected_revision=3)
    assert await bridge.intercept(loop, message) == (True, None)


@pytest.mark.asyncio
async def test_degraded_reply_cannot_survive_mode_toggle_or_retry_uncertain_smtp(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    channel.config.smtp_host = "smtp.example.test"
    security = SecurityContext(origin_kind="email_ingress", source_key="fictional", mode_revision=2)
    message = OutboundMessage("email", "sender@example.test", "Fictional", security=security)
    calls = []

    def smtp(_):
        calls.append(1)
        raise OSError("fictional lost SMTP result")

    monkeypatch.setattr(channel, "_smtp_send", smtp)
    with pytest.raises(OSError):
        await channel.send(message)
    await channel.send(message)
    assert calls == [1]
    service.set_mode(archive_enabled=True, trigger_agent=False, expected_revision=2)
    service.set_mode(archive_enabled=True, trigger_agent=True, expected_revision=3)
    channel._degraded_delivery_attempts.clear()
    await channel.send(message)
    assert calls == [1]


@pytest.mark.asyncio
async def test_full_degraded_delivery_memory_never_evicts_uncertain_attempts(tmp_path, monkeypatch):
    _, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    channel.config.smtp_host = 'smtp.example.test'
    channel._degraded_delivery_attempts = {(str(i), 2) for i in range(100000)}
    calls = []
    monkeypatch.setattr(channel, '_smtp_send', lambda message: calls.append(message))
    security = SecurityContext(origin_kind='email_ingress', source_key='new-source', mode_revision=2)
    await channel.send(OutboundMessage('email', 'sender@example.test', 'Fictional', security=security))
    assert calls == []
    assert len(channel._degraded_delivery_attempts) == 100000
    assert ('0', 2) in channel._degraded_delivery_attempts
    assert channel._mail_ingress.last_error == 'delivery_tracking_capacity'


@pytest.mark.asyncio
async def test_mode_applied_waits_for_inflight_smtp_and_blocks_later_submission(tmp_path, monkeypatch):
    service, channel, _ = setup(tmp_path, monkeypatch, trigger=True)
    message = completed_message(channel)
    started, release = Event(), Event()

    def smtp(_):
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(channel, "_smtp_send", smtp)
    sending = asyncio.create_task(channel.send(message))
    assert await asyncio.to_thread(started.wait, 2)
    other = MailService(service.store.root, service.config, worker_enabled=False)
    switching = asyncio.create_task(asyncio.to_thread(other.set_mode,
        archive_enabled=True, trigger_agent=False, expected_revision=2))
    try:
        await asyncio.sleep(.1)
        assert not switching.done()
    finally:
        release.set()
    await sending
    assert (await switching)["status"] == "applied"
    assert delivery(service)["status"] == "submitted"
    monkeypatch.setattr(channel, "_smtp_send", lambda _: pytest.fail("Must not start after applied"))
    await channel.send(message)

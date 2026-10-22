import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config, MailOwnerBinding
from nanobot.knowledge.mail.contracts import MailError
from tests.knowledge.mail.test_access import make_access
from tests.knowledge.mail.test_answering import service_for


class Platform(BaseChannel):
    name = "telegram"

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, message):
        self.sent.append(message)


def setup(tmp_path):
    store, auth, _, _ = make_access(tmp_path)
    auth.grant(MailOwnerBinding(binding_id="platform-owner", principal_id="123", channel="telegram"))
    service = service_for(store, auth)
    bus = MessageBus()
    channel = Platform(SimpleNamespace(allow_from=["*"]), bus)
    channel._mail_service, channel.sent = service, []
    manager = ChannelManager(Config(), bus, mail_service=service)
    return auth, bus, channel, manager


def test_protected_adapter_logs_do_not_expose_sdk_errors_or_traceback(tmp_path):
    from loguru import logger

    _, _, channel, _ = setup(tmp_path)
    records = []
    sink = logger.add(lambda message: records.append(message.record), diagnose=True)
    try:
        channel.logger.bind(request="FICTIONAL_PRIVATE_REQUEST").info(
            "Downloaded {}", "FICTIONAL_PRIVATE_FILENAME")
        try:
            raise RuntimeError("FICTIONAL_PRIVATE_SDK_RESPONSE")
        except RuntimeError:
            channel.logger.exception("Failed {}", "FICTIONAL_PRIVATE_BODY")
    finally:
        logger.remove(sink)
    assert len(records) == 2
    for record in records:
        assert record["message"] == "Private channel event; details omitted"
        assert record["exception"] is None
        assert record["extra"] == {"channel": "telegram"}


def test_never_enabled_channel_preserves_diagnostic_logs():
    from loguru import logger

    channel = Platform(SimpleNamespace(allow_from=["*"]), MessageBus())
    messages = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        channel.logger.info("Normal event {}", 7)
    finally:
        logger.remove(sink)
    assert messages == ["Normal event 7"]


@pytest.mark.asyncio
async def test_private_principal_is_separate_from_alias_and_free_metadata(tmp_path):
    _, bus, channel, _ = setup(tmp_path)
    await channel._handle_message("123|mutable_name", "123", "question",
        verified_private_principal="123")
    message = await bus.consume_inbound()
    assert message.security.authenticated and message.security.principal_id == "123"
    assert message.security.reply_chat_id == "123"
    await channel._handle_message("123|mutable_name", "group", "question",
        metadata={"verified_private_principal": "123", "private_chat": True,
                  "owner_id": "local-owner", "security": message.security})
    assert (await bus.consume_inbound()).security is None
    await channel._handle_message("attacker", "attacker", "question", verified_private_principal="456")
    assert not (await bus.consume_inbound()).security.authenticated


async def reply(bus, channel):
    await channel._handle_message("123", "123", "question", verified_private_principal="123")
    message = await bus.consume_inbound()
    return OutboundMessage("telegram", "123", "private answer", security=message.security,
                           metadata={"private_mail": True, "mail_session_key": "private-session"})


@pytest.mark.asyncio
async def test_queue_revocation_redirect_and_stream_do_not_send(tmp_path):
    auth, bus, channel, manager = setup(tmp_path)
    message = await reply(bus, channel)
    for invalid in (replace(message, chat_id="group"),
                    replace(message, metadata={**message.metadata, "_stream_delta": True}),
                    replace(message, security=None)):
        with pytest.raises(MailError, match="private_delivery_target_mismatch"):
            await manager._send_guarded(channel, invalid)
    await manager._send_guarded(channel, message)
    assert len(channel.sent) == 1
    auth.revoke(message.security.binding_id)
    await manager._send_with_retry(channel, message)
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_revocation_waits_for_transport_commit_then_blocks_next_send(tmp_path):
    auth, bus, channel, manager = setup(tmp_path)
    message = await reply(bus, channel)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(_):
        entered.set()
        await release.wait()

    channel.send = send
    sending = asyncio.create_task(manager._send_guarded(channel, message))
    await entered.wait()
    revoking = asyncio.create_task(asyncio.to_thread(auth.revoke, message.security.binding_id))
    await asyncio.sleep(.08)
    assert not revoking.done()
    release.set()
    await sending
    await revoking
    with pytest.raises(MailError, match="authorization_revoked"):
        await manager._send_guarded(channel, message)


@pytest.mark.asyncio
async def test_retry_rechecks_revocation_before_next_network_call(tmp_path, monkeypatch):
    auth, bus, channel, manager = setup(tmp_path)
    message = await reply(bus, channel)
    attempts = []

    async def send(_):
        attempts.append(1)
        raise OSError("fictional network failure")

    import nanobot.channels.manager as module
    monkeypatch.setattr(module, "_SEND_RETRY_DELAYS", (.1,))
    channel.send = send
    task = asyncio.create_task(manager._send_with_retry(channel, message))
    while not attempts:
        await asyncio.sleep(.01)
    await asyncio.to_thread(auth.revoke, message.security.binding_id)
    await task
    assert attempts == [1]


@pytest.mark.asyncio
async def test_telegram_adapter_uses_numeric_id_only_for_explicit_private_chat(tmp_path):
    from nanobot.channels.telegram import TelegramChannel, TelegramConfig
    auth, bus, _, manager = setup(tmp_path)
    channel = TelegramChannel(TelegramConfig(allow_from=["*"]), bus)
    channel._mail_service = manager.mail_service
    channel._remember_thread_context = lambda _: None
    channel._build_message_metadata = lambda *_: {}
    channel._derive_topic_session_key = lambda _: None
    user = SimpleNamespace(id=123, username="mutable_username")
    message = SimpleNamespace(chat_id=123, text="/mail-confirm fictional", chat=SimpleNamespace(type="private"))
    await channel._forward_command(SimpleNamespace(message=message, effective_user=user), None)
    inbound = await bus.consume_inbound()
    assert inbound.security.authenticated and inbound.security.principal_id == "123"
    for kind in ("group", "supergroup", "channel", None):
        message.chat.type = kind
        await channel._forward_command(SimpleNamespace(message=message, effective_user=user), None)
        assert (await bus.consume_inbound()).security is None
    auth.revoke("platform-owner")
    message.chat.type = "private"
    await channel._forward_command(SimpleNamespace(message=message, effective_user=user), None)
    assert not (await bus.consume_inbound()).security.authenticated

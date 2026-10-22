import asyncio
from email.message import EmailMessage
from uuid import uuid4

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.email import EmailChannel, EmailConfig
from nanobot.config.schema import MailKnowledgeConfig
from nanobot.knowledge.mail.service import MailService
from tests.knowledge.mail.test_store import envelope


class FakeIMAP:
    def __init__(self, raw, *, size=None, uids=None):
        self.raw = raw
        self.size = size or len(raw)
        self.uids = uids or [1]
        self.content_fetches = 0
        self.acks = []

    def login(self, *args):
        return "OK", []

    def select(self, *args):
        return "OK", []

    def response(self, name):
        return "UIDVALIDITY", [b"7"]

    def logout(self):
        pass

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [" ".join(map(str, self.uids)).encode()]
        if command == "STORE":
            self.acks.append(int(args[0]))
            return "OK", []
        if command == "FETCH":
            query = args[1]
            if "BODY.PEEK[]" in query:
                self.content_fetches += 1
                raw = self.raw
            else:
                raw = self.raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
            response = f'1 (UID {args[0]} RFC822.SIZE {self.size} INTERNALDATE "18-Sep-2026 10:32:00 +0800")'.encode()
            return "OK", [(response, raw)]
        raise AssertionError(command)


def setup(tmp_path, monkeypatch, *, trigger=False, archive=True):
    config = MailKnowledgeConfig(enabled=True, archive_enabled=archive, account_id=uuid4(), semantic_enabled=False)
    service = MailService(tmp_path / "mail", config)
    service.store.initialize()
    service.set_mode(archive_enabled=archive, trigger_agent=trigger, expected_revision=1)
    channel = EmailChannel(EmailConfig(
        consent_granted=True, trigger_agent=trigger, imap_host="imap.example.test", imap_username="owner@example.test",
        imap_password="fictional-only", verify_dkim=False, verify_spf=False, allow_from=["*"],
    ), MessageBus(), mail_service=service)
    mail = EmailMessage()
    mail["From"] = "HR <hr@example.test>"
    mail["Subject"] = "Fictional test"
    mail["Date"] = "Fri, 18 Sep 2026 10:00:00 +0800"
    mail.set_content("正文" * 7000 + "TAIL-CLAUSE")
    mail.add_attachment(b"private attachment", maintype="text", subtype="plain", filename="same.txt")
    fake = FakeIMAP(mail.as_bytes(policy=__import__('email').policy.SMTP))
    monkeypatch.setattr("imaplib.IMAP4_SSL", lambda *args, **kwargs: fake)
    return service, channel, fake


def test_archive_only_never_dispatches_and_preserves_full_body(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    assert channel._validate_config()  # No SMTP required.
    assert channel._fetch_new_messages() == []
    assert fake.acks == [1]
    assert fake.content_fetches == 1
    with service.store.connect() as db:
        body = db.execute("SELECT object_ref FROM documents WHERE kind='body'").fetchone()[0]
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "not_requested"
        assert db.execute("SELECT received_at FROM mail_items").fetchone()[0] == "2026-09-18T02:32:00+00:00"
    assert "TAIL-CLAUSE" in service.store.safe_path(body).read_text(encoding="utf-8")
    channel._fetch_new_messages()
    assert fake.content_fetches == 1
    service.set_mode(archive_enabled=True, trigger_agent=True, expected_revision=2)
    assert channel._fetch_new_messages() == []  # Switching mode never replays old mail.


def test_ack_failure_retries_ack_without_refetch_or_duplicate_archive(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    original = fake.uid
    failed = []

    def uid(command, *args):
        if command == "STORE" and not failed:
            failed.append(True)
            return "NO", [b"fictional temporary failure"]
        return original(command, *args)

    fake.uid = uid
    channel._fetch_new_messages()
    assert fake.content_fetches == 1 and fake.acks == []
    channel._mail_ingress._waiting[1] = (0, 0)
    channel._fetch_new_messages()
    assert fake.content_fetches == 1 and fake.acks == [1]
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM admission_waits").fetchone()[0] == 0
    assert channel._mail_ingress._waiting == {}
    assert channel._mail_ingress._attempts == {}


def test_uidvalidity_change_does_not_carry_old_capacity_head(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    reservation = service.store.reserve("other", fake.size)
    channel._fetch_new_messages()
    assert channel._mail_ingress._capacity_head == 1
    service.store.release_reservation(reservation)
    fake.response = lambda name: ("UIDVALIDITY", [b"8"])
    # UID 1 still exists but now belongs to another mailbox epoch. It must not
    # prevent an earlier candidate of the new epoch from using available space.
    fake.uids = [2, 1]
    attempts = []
    original = channel._mail_ingress._receive

    def receive(client, source, mode):
        attempts.append(channel._mail_ingress._capacity_head)
        return original(client, source, mode)

    monkeypatch.setattr(channel._mail_ingress, "_receive", receive)
    channel._fetch_new_messages()
    assert attempts[0] is None
    assert fake.content_fetches >= 1


def test_parallel_projection_is_bounded_and_pending_is_durable(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch, trigger=True)
    items = channel._fetch_new_messages()
    assert len(items) == 1
    assert "TAIL-CLAUSE" not in items[0]["content"]
    assert items[0]["_security"].origin_kind == "email_ingress"
    with service.store.connect() as db:
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "pending"
    task = items[0]["_security"].task_id
    assert channel._mail_ingress.dispatch.transition(task, "pending", "dispatching")
    assert not channel._mail_ingress.dispatch.transition(task, "pending", "dispatching")
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 1
    service.store.recover()
    assert channel._fetch_new_messages() == []


def test_capacity_pause_keeps_unread_then_automatically_recovers(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    token = service.store.reserve("other", fake.size)
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 0
    assert fake.acks == []
    service.store.release_reservation(token)
    channel._mail_ingress._waiting[1] = (0, 0)
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 1
    assert fake.acks == [1]


def test_waiting_source_no_longer_unseen_is_reported_as_gap_without_backfill(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    reservation = service.store.reserve("other", fake.size)
    channel._fetch_new_messages()
    fake.uids = []  # Another client marked it Seen or removed it; we cannot tell which.
    service.store.release_reservation(reservation)
    channel._fetch_new_messages()
    assert fake.content_fetches == 0 and fake.acks == []
    assert not channel._mail_ingress._waiting
    with service.store.connect() as db:
        assert db.execute("SELECT error_code FROM admission_waits").fetchone()[0] == "source_no_longer_unseen"
        assert db.execute("SELECT COUNT(*) FROM ingest_jobs").fetchone()[0] == 0


def test_parallel_capacity_does_not_repeat_interaction_or_ack(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch, trigger=True)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    service.store.reserve("other", fake.size)
    items = channel._fetch_new_messages()
    assert len(items) == 1
    task = items[0]["_security"].task_id
    channel._mail_ingress.dispatch.transition(task, "pending", "dispatching")
    channel._mail_ingress._waiting[1] = (0, 0)
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 1
    assert fake.acks == []


def test_ingress_backoff_survives_restart_and_grows_without_ack(tmp_path, monkeypatch):
    from nanobot.knowledge.mail.email_ingress import MailIngress

    service, channel, fake = setup(tmp_path, monkeypatch)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    service.store.reserve("other", fake.size)
    monkeypatch.setattr("nanobot.knowledge.mail.email_ingress.random.uniform", lambda a, b: 1)
    channel._fetch_new_messages()
    with service.store.connect() as db:
        first = dict(db.execute("SELECT * FROM admission_waits").fetchone())
    assert first["attempt"] == 1
    assert 4.5 <= first["next_attempt_at"] - first["first_at"] <= 5.5
    channel._mail_ingress = MailIngress(channel, service)
    channel._fetch_new_messages()  # Reload durable schedule without retrying early.
    assert channel._mail_ingress._attempts[1] == 1
    channel._mail_ingress._waiting[1] = (first["first_at"], 0)
    channel._fetch_new_messages()
    with service.store.connect() as db:
        second = dict(db.execute("SELECT * FROM admission_waits").fetchone())
    assert second["attempt"] == 2 and second["next_attempt_at"] > first["next_attempt_at"] + 4
    assert fake.content_fetches == 0 and fake.acks == []


def test_capacity_release_resumes_waiting_source_before_backoff_expires(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    service.config.max_message_bytes = fake.size
    service.config.max_pending_bytes = fake.size
    reservation = service.store.reserve("other", fake.size)
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 0
    service.store.release_reservation(reservation)
    channel._mail_ingress.capacity_available()
    channel._fetch_new_messages()
    assert fake.content_fetches == 1 and fake.acks == [1]
    assert channel._mail_ingress._attempts == {}


def test_attached_message_is_recorded_as_unsupported_not_silently_dropped(tmp_path, monkeypatch):
    from email import policy

    service, channel, fake = setup(tmp_path, monkeypatch)
    inner = EmailMessage()
    inner.set_content("Nested mail text must not be mixed into current mail")
    inner.add_attachment(b"nested", maintype="text", subtype="plain", filename="nested.txt")
    outer = EmailMessage()
    outer["From"] = "hr@example.test"
    outer.set_content("Current message")
    outer.add_attachment(inner, filename="forwarded.eml")
    fake.raw = outer.as_bytes(policy=policy.SMTP)
    fake.size = len(fake.raw)
    channel._fetch_new_messages()
    with service.store.connect() as db:
        rows = list(db.execute("SELECT filename,mime,error_code,object_ref FROM documents WHERE kind='attachment'"))
    assert len(rows) == 1
    assert tuple(rows[0]) == ("forwarded.eml", "message/rfc822", "unsupported_type", None)


def test_both_branches_off_do_not_poll_and_force_send_cannot_bypass(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch, archive=False)
    assert channel._fetch_new_messages() == []
    assert fake.content_fetches == 0
    channel.config.smtp_host = "smtp.example.test"
    sends = []
    monkeypatch.setattr(channel, "_smtp_send", sends.append)
    asyncio.run(channel.send(OutboundMessage("email", "hr@example.test", "should not send", metadata={"force_send": True})))
    assert not sends


def test_more_than_two_batches_mark_unseen_do_not_starve(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    fake.uids = list(range(1, 56))
    channel.config.mark_seen = False
    for _ in range(4):
        channel._fetch_new_messages()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 55
    assert fake.content_fetches == 55


def test_rotating_scan_progresses_with_backoff_archived_unseen_and_continuous_arrivals(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch)
    fake.uids = list(range(1, 261))  # More than the 200-UID scan and two fetch batches.
    channel.config.mark_seen = False
    channel._fetch_new_messages()  # First 20 remain UNSEEN but are already durable.
    ingress = channel._mail_ingress
    ingress._waiting.update({uid: (0, 10**15) for uid in range(21, 121)})
    for turn in range(25):
        fake.uids.extend(range(261 + turn * 5, 266 + turn * 5))
        if turn == 10:
            ingress._waiting.update({uid: (0, 0) for uid in range(21, 121)})
        channel._fetch_new_messages()
    with service.store.connect() as db:
        admitted = {row[0] for row in db.execute("SELECT uid FROM mail_items")}
    assert set(range(1, 261)) <= admitted
    assert any(uid > 300 for uid in admitted)
    assert fake.content_fetches == len(admitted) and fake.acks == []


def test_archive_failure_still_creates_independent_interaction(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch, trigger=True)
    original = service.store.accept
    monkeypatch.setattr(service.store, "accept", lambda *args: (_ for _ in ()).throw(OSError()))
    items = channel._fetch_new_messages()
    assert len(items) == 1
    assert fake.acks == []
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 0
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "pending"
    monkeypatch.setattr(service.store, "accept", original)
    # This test deliberately does not connect to a real SMTP/IMAP server.
    assert envelope().sender_address.endswith(".test")


def test_degraded_dispatch_limit_keeps_old_fences_and_durable_recovery(tmp_path, monkeypatch):
    service, channel, fake = setup(tmp_path, monkeypatch, trigger=True, archive=False)
    channel._fetch_new_messages()  # Durable intents do not consume fallback memory.
    ingress = channel._mail_ingress
    assert not ingress._dispatched
    # Ignore earlier durable pending work; test only newly seen source IDs.
    monkeypatch.setattr(ingress.dispatch, 'pending', lambda: [])
    original = ingress.dispatch.intent
    monkeypatch.setattr('nanobot.knowledge.mail.email_ingress.MAX_DEGRADED_DISPATCHES', 1)
    monkeypatch.setattr(ingress.dispatch, 'intent', lambda *args: (_ for _ in ()).throw(OSError()))
    fake.uids = [2]
    assert len(channel._fetch_new_messages()) == 1
    fence = set(ingress._dispatched)
    fake.uids = [2, 3]
    assert channel._fetch_new_messages() == []
    assert ingress._dispatched == fence
    assert ingress.last_error == 'dispatch_tracking_capacity'
    monkeypatch.setattr(ingress.dispatch, 'intent', original)
    assert len(channel._fetch_new_messages()) == 1  # New UID 3 is now durable.
    assert ingress._dispatched == fence

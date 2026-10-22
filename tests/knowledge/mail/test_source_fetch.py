from email.message import EmailMessage

import pytest

from nanobot.config.schema import MailOwnerBinding
from nanobot.knowledge.mail.contracts import (
    AccessContext,
    MailError,
    MailQuery,
    ManageAction,
    SourceRead,
)
from nanobot.knowledge.mail.ingestion import MailWorker
from nanobot.knowledge.mail.maintenance import MailMaintenance
from nanobot.knowledge.mail.management import MailManagement
from nanobot.knowledge.mail.source_fetch import SourceFetcher
from tests.knowledge.mail.test_email_ingress import setup


async def archived(tmp_path, monkeypatch, *, skip_attachment=False, extra_attachment=False):
    service, channel, fake = setup(tmp_path, monkeypatch)
    channel.config.enabled = True
    binding = MailOwnerBinding(binding_id="owner", principal_id="alice", channel="cli", scopes={"read", "manage"})
    service.access.initialize_bindings([binding])
    access = AccessContext.from_security(service.access.authenticate("cli", "alice", private_chat=True), "cli:test")
    if skip_attachment:
        service.config.max_attachments = 0
    if extra_attachment:
        from email import policy
        from email.parser import BytesParser
        mail = BytesParser(policy=policy.default).parsebytes(fake.raw)
        mail.add_attachment(b'second retained terms', maintype='text', subtype='plain', filename='second.txt')
        fake.raw = mail.as_bytes(policy=policy.SMTP)
        fake.size = len(fake.raw)
    channel._fetch_new_messages()
    await MailWorker(service.store).process(service.store.claim())
    fake.acks.clear()
    # Simulated SELECT accepts the explicit read-only flag used by maintenance.
    fake.select = lambda *args, **kwargs: ("OK", [])
    return service, channel, fake, access


@pytest.mark.asyncio
async def test_batch_refetch_same_mail_reclaims_all_staged_inputs_without_duplicate_body(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch,
        skip_attachment=True, extra_attachment=True)
    with service.store.connect() as db:
        targets = [r[0] for r in db.execute("SELECT document_id FROM documents WHERE kind='attachment'")]
        mail_id = db.execute('SELECT mail_id FROM mail_items').fetchone()[0]
        before_dispatch = [tuple(r) for r in db.execute('SELECT * FROM interaction_dispatches')]
    assert len(targets) == 2
    service.config.max_attachments = 20
    confirmed(service, access, 'prepare_refetch', targets)
    maintenance = MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress))
    assert maintenance.process_one()
    assert len(list(service.store.safe_path('maintenance').glob('input_*'))) == 2
    await MailWorker(service.store).process(service.store.claim())
    maintenance.process_one()
    assert list(service.store.safe_path('maintenance').glob('input_*')) == []
    with service.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 3
        assert {r[0] for r in db.execute('SELECT extract_status FROM documents')} == {'ready'}
        assert {r[0] for r in db.execute('SELECT active_generation FROM documents')} == {1}
        assert db.execute('SELECT status FROM management_actions').fetchone()[0] == 'completed'
        assert [tuple(r) for r in db.execute('SELECT * FROM interaction_dispatches')] == before_dispatch
    assert fake.acks == []
    service.store.remove(mail_id, 1)
    assert list(service.store.safe_path('maintenance').glob('input_*')) == []
    assert not service.store.safe_path('objects/' + mail_id).exists()


def confirmed(service, access, action, ids):
    manager = MailManagement(service.store, service.access)
    receipt = manager.manage(access, ManageAction(action=action, target_ids=ids, request_id=action))
    manager.confirm(access, receipt.confirmation_token)
    return receipt


@pytest.mark.asyncio
async def test_restore_uses_same_mail_id_new_content_and_never_revives_old_reference(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    maintenance = MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress))
    assert maintenance.process_one()
    await MailWorker(service.store).process(service.store.claim())
    maintenance.process_one()
    page = await service.search(access, MailQuery())
    assert page.matches[0].mail_id == old.mail_id
    assert page.matches[0].revision > old.revision
    with pytest.raises(MailError, match="source_removed"):
        await service.read_source(access, SourceRead(evidence_id=old.evidence_id))
    assert fake.acks == []
    with service.store.connect() as db:
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "cancelled"
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 0
        assert db.execute("SELECT status FROM management_actions").fetchone()[0] == "completed"


@pytest.mark.asyncio
async def test_refetch_only_confirmed_skipped_part_and_no_dispatch(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch, skip_attachment=True)
    with service.store.connect() as db:
        target = db.execute("SELECT document_id FROM documents WHERE kind='attachment'").fetchone()[0]
        before = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    service.config.max_attachments = 20
    confirmed(service, access, "prepare_refetch", [target])
    maintenance = MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress))
    maintenance.process_one()
    await MailWorker(service.store).process(service.store.claim())
    maintenance.process_one()
    assert fake.acks == []
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == before
        assert db.execute("SELECT extract_status FROM documents WHERE document_id=?", (target,)).fetchone()[0] == "ready"
        assert db.execute("SELECT state FROM interaction_dispatches").fetchone()[0] == "not_requested"


@pytest.mark.asyncio
async def test_uidvalidity_change_keeps_tombstone(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    fake.response = lambda name: ("UIDVALIDITY", [b"8"])
    MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress)).process_one()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 1
        assert "source_unrecoverable" in db.execute("SELECT receipt_json FROM management_actions").fetchone()[0]
    assert fake.acks == []


@pytest.mark.asyncio
async def test_capacity_retry_is_durable_delayed_and_keeps_tombstone(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    calls = []

    def full(*args):
        calls.append(True)
        raise MailError("capacity_backpressure")

    maintenance = MailMaintenance(service.store, source_fetcher=full)
    assert maintenance.process_one()
    assert not maintenance.process_one()  # No hot retry loop or second download.
    assert len(calls) == 1
    with service.store.connect() as db:
        row = db.execute("SELECT status,receipt_json FROM management_actions").fetchone()
        assert row[0] == "accepted_waiting_service" and "capacity_backpressure" in row[1]
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 1
    with service.store.connect(write=True) as db:
        db.execute("UPDATE management_actions SET payload_json=json_set(payload_json,'$.retry.next_at',0)")
    resumed = MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress))
    resumed.recover()
    assert resumed.process_one()
    assert service.store.claim() is not None
    assert fake.acks == []


@pytest.mark.asyncio
async def test_shutdown_during_fetch_does_not_publish(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    fetcher = SourceFetcher(channel._mail_ingress)
    original = fetcher._download

    def stop_after_download(source):
        result = original(source)
        service.worker._stopping = True
        return result

    monkeypatch.setattr(fetcher, "_download", stop_after_download)
    MailMaintenance(service.store, source_fetcher=fetcher).process_one()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        row = db.execute("SELECT status,receipt_json FROM management_actions").fetchone()
        assert row[0] == "accepted_waiting_service" and "service_stopping" in row[1]


@pytest.mark.asyncio
async def test_same_filename_changed_bytes_cannot_be_refetched_as_original(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch, skip_attachment=True)
    with service.store.connect() as db:
        target = db.execute("SELECT document_id FROM documents WHERE kind='attachment'").fetchone()[0]
    service.config.max_attachments = 20
    confirmed(service, access, "prepare_refetch", [target])
    mail = EmailMessage()
    mail["From"] = "hr@example.test"
    mail.set_content("Changed message")
    mail.add_attachment(b"different content!", maintype="text", subtype="plain", filename="same.txt")
    fake.raw = mail.as_bytes(policy=__import__('email').policy.SMTP)
    MailMaintenance(service.store, source_fetcher=SourceFetcher(channel._mail_ingress)).process_one()
    with service.store.connect() as db:
        assert db.execute("SELECT object_ref FROM documents WHERE document_id=?", (target,)).fetchone()[0] is None
        assert "source_identity_changed" in db.execute("SELECT receipt_json FROM management_actions").fetchone()[0]


@pytest.mark.asyncio
async def test_revocation_during_download_prevents_publication(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    fetcher = SourceFetcher(channel._mail_ingress)
    original = fetcher._download

    def download(source):
        value = original(source)
        service.access.revoke(access.binding_id)
        return value

    monkeypatch.setattr(fetcher, "_download", download)
    MailMaintenance(service.store, source_fetcher=fetcher).process_one()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_tombstones").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
        assert "authorization_revoked" in db.execute("SELECT receipt_json FROM management_actions").fetchone()[0]


@pytest.mark.asyncio
async def test_crash_after_restore_publication_does_not_redownload_or_restore_twice(tmp_path, monkeypatch):
    service, channel, fake, access = await archived(tmp_path, monkeypatch)
    old = (await service.search(access, MailQuery())).matches[0]
    service.store.remove(old.mail_id, old.revision)
    confirmed(service, access, "request_restore", [old.mail_id])
    fetcher = SourceFetcher(channel._mail_ingress)

    def interrupted(*args):
        fetcher(*args)
        raise KeyboardInterrupt("simulated interruption after commit")

    maintenance = MailMaintenance(service.store, source_fetcher=interrupted)
    with pytest.raises(KeyboardInterrupt):
        maintenance.process_one()
    fetched = fake.content_fetches
    maintenance.source_fetcher = fetcher
    maintenance.recover()
    maintenance.process_one()
    assert fake.content_fetches == fetched
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_items").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM maintenance_effects").fetchone()[0] == 1
        assert db.execute("SELECT status FROM management_actions").fetchone()[0] == "waiting_source_jobs"

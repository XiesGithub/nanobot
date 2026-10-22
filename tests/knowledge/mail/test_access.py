from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import AccessContext, MailError
from nanobot.knowledge.mail.store import MailStore


def make_access(tmp_path):
    store = MailStore(tmp_path / "mail", MailKnowledgeConfig())
    store.initialize()
    auth = MailAccess(store)
    binding = MailOwnerBinding(binding_id="alice", principal_id="alice", channel="cli", scopes={"read", "manage"})
    auth.initialize_bindings([binding])
    access = AccessContext.from_security(auth.authenticate("cli", "alice", private_chat=True), "cli:chat")
    return store, auth, binding, access


def test_revoked_snapshot_and_stale_config_fail_closed(tmp_path):
    store, auth, binding, access = make_access(tmp_path)
    auth.require(access)
    reader = MailAccess(MailStore(store.root, store.config, readonly=True))
    reader.require(access)
    auth.revoke("alice")
    auth.initialize_bindings([binding])
    with pytest.raises(MailError, match="authorization_revoked"):
        reader.require(access)
    assert not auth.authenticate("cli", "alice", private_chat=True).authenticated
    auth.grant(binding)
    with pytest.raises(MailError, match="authorization_revoked"):
        reader.require(access)


def test_delivery_commit_and_revocation_are_serialized(tmp_path):
    _, auth, _, access = make_access(tmp_path)
    committed, revoke_started, release = Event(), Event(), Event()

    def send_chunk():
        with auth.delivery(access):
            committed.set()
            assert release.wait(5)

    def revoke():
        revoke_started.set()
        auth.revoke("alice")

    with ThreadPoolExecutor(2) as pool:
        sending = pool.submit(send_chunk)
        assert committed.wait(5)
        revoking = pool.submit(revoke)
        assert revoke_started.wait(5)
        assert not revoking.done()
        release.set()
        sending.result()
        revoking.result()
    with pytest.raises(MailError, match="authorization_revoked"):
        with auth.delivery(access):
            pytest.fail("Post-revocation chunk was delivered")


def test_untrusted_origins_and_groups_never_authorized(tmp_path):
    _, auth, _, access = make_access(tmp_path)
    assert not auth.authenticate("cli", "alice", private_chat=False).authenticated
    assert not auth.authenticate("email", "alice", private_chat=True).authenticated
    with pytest.raises(MailError, match="unauthorized"):
        auth.require(access.model_copy(update={"origin": "email"}))


def test_authority_failure_is_not_cached_authorization(tmp_path, monkeypatch):
    store, auth, _, access = make_access(tmp_path)
    auth.require(access)
    monkeypatch.setattr(store, "connect", lambda: (_ for _ in ()).throw(OSError()))
    with pytest.raises(MailError, match="authorization_unavailable"):
        auth.require(access)

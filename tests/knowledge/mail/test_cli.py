import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nanobot.cli.mail import app
from nanobot.config.schema import Config
from nanobot.knowledge.mail.factory import create_mail_service
from tests.knowledge.mail.test_retrieval import ingest
from tests.knowledge.mail.test_store import envelope

runner = CliRunner()


def initialized(tmp_path):
    path = tmp_path / "instance" / "config.json"
    result = runner.invoke(app, ["--config", str(path), "init", "--local-owner"])
    assert result.exit_code == 0, result.output
    config = Config.model_validate_json(path.read_text(encoding="utf-8"))
    return path, create_mail_service(config, config_path=path, role="management"), json.loads(result.output)


def test_initialization_requires_explicit_local_owner_and_does_not_connect(tmp_path):
    path = tmp_path / "config.json"
    result = runner.invoke(app, ["--config", str(path), "init"])
    assert result.exit_code != 0
    assert not path.exists()
    path, service, receipt = initialized(tmp_path)
    assert service.config.account_id is not None
    assert service.worker is None
    assert receipt["archive_enabled"] is False
    result = runner.invoke(app, ["--config", str(path), "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["worker_online"] is False


def test_cli_query_source_and_queue_do_not_need_gateway(tmp_path):
    path, service, _ = initialized(tmp_path)
    accepted = ingest(service.store, envelope())
    result = runner.invoke(app, ["--config", str(path), "search", "", "--limit", "1"])
    assert result.exit_code == 0, result.output
    page = json.loads(result.output)
    assert page["matches"][0]["mail_id"] == accepted.mail_id
    result = runner.invoke(app, ["--config", str(path), "source", page["matches"][0]["evidence_id"]])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["--config", str(path), "manage", "reindex", "all", "--request-id", "index-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "accepted_waiting_service"
    with service.store.connect() as db:
        assert db.execute("SELECT active_index_revision FROM schema_meta").fetchone()[0] == "text-v1"


def test_cli_self_revocation_commits_and_blocks_next_query(tmp_path):
    path, service, binding = initialized(tmp_path)
    result = runner.invoke(app, ["--config", str(path), "revoke", binding["binding_id"]])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "completed"
    result = runner.invoke(app, ["--config", str(path), "status"])
    assert result.exit_code == 1
    assert "unauthorized" in result.output
    with service.store.connect() as db:
        assert db.execute("SELECT revoked FROM bindings").fetchone()[0] == 1


def test_cli_grant_revoke_and_mode_revision(tmp_path):
    path, service, _ = initialized(tmp_path)
    prefix = ["--config", str(path)]
    result = runner.invoke(app, [*prefix, "bind", "websocket", "webui-owner", "--binding-id", "web-owner"])
    assert result.exit_code == 0, result.output
    assert service.access.authenticate("websocket", "webui-owner", private_chat=True).authenticated
    result = runner.invoke(app, [*prefix, "bindings"])
    assert len(json.loads(result.output)["bindings"]) == 2
    result = runner.invoke(app, [*prefix, "mode", "--no-archive", "--no-trigger", "--expected-revision", "1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, [*prefix, "mode", "--no-archive", "--trigger", "--expected-revision", "1"])
    assert result.exit_code == 1 and "revision_conflict" in result.output
    result = runner.invoke(app, [*prefix, "revoke", "web-owner"])
    assert result.exit_code == 0, result.output
    assert not service.access.authenticate("websocket", "webui-owner", private_chat=True).authenticated
    result = runner.invoke(app, [*prefix, "bind", "email", "test@example.invalid", "--binding-id", "bad"])
    assert result.exit_code == 1 and "invalid_binding" in result.output


def test_cli_backup_restore_requires_explicit_confirmation_and_preserves_deletion(tmp_path):
    path, service, _ = initialized(tmp_path)
    receipt = ingest(service.store, envelope())
    prefix = ["--config", str(path)]
    result = runner.invoke(app, [*prefix, "backup"])
    assert result.exit_code == 0, result.output
    backup_id = json.loads(result.output)["backup_id"]
    service.store.remove(receipt.mail_id, 1)
    result = runner.invoke(app, [*prefix, "restore-backup", backup_id])
    assert result.exit_code != 0
    result = runner.invoke(app, [*prefix, "restore-backup", backup_id, "--confirm-local-restore"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "completed"
    result = runner.invoke(app, [*prefix, "status"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["counts"].get("removed") == 1


@pytest.mark.asyncio
async def test_agent_cli_local_owner_final_print_rechecks_revocation(tmp_path):
    from nanobot.bus.events import OutboundMessage
    from nanobot.cli.mail import local_principal, owner_turn
    from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
    from nanobot.knowledge.mail.contracts import MailError
    from nanobot.knowledge.mail.service import MailService

    service = MailService(tmp_path / "mail", MailKnowledgeConfig(semantic_enabled=False,
        owner_bindings=[MailOwnerBinding(binding_id="local", principal_id=local_principal(), channel="cli")]),
        worker_enabled=False)
    captured = []

    async def process(content, **kwargs):
        assert kwargs["channel"] == "cli" and kwargs["chat_id"] == "same-chat"
        assert "on_stream" not in kwargs and "on_progress" not in kwargs
        security = kwargs["security"]
        assert security.authenticated
        service.access.revoke("local")
        return OutboundMessage("cli", "same-chat", "Private answer", security=security)

    loop = SimpleNamespace(mail_service=service, process_direct=process)
    try:
        with pytest.raises(MailError, match="authorization_revoked"):
            await owner_turn(loop, "Fictional question", "same-chat", captured.append)
        assert captured == []
        with pytest.raises(MailError, match="unauthorized"):
            await owner_turn(loop, "again", "same-chat", captured.append)
    finally:
        await service.close()

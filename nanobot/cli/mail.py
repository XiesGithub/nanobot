"""Explicit local owner management; ordinary query clients never become writers."""

from __future__ import annotations

import asyncio
import getpass
import json
from pathlib import Path
from uuid import uuid4

import typer

from nanobot.config.schema import Config, MailOwnerBinding
from nanobot.knowledge.mail.contracts import (
    AccessContext,
    MailError,
    MailQuery,
    ManageAction,
    SourceRead,
)
from nanobot.knowledge.mail.factory import create_mail_service

app = typer.Typer(help="Manage private email knowledge as the explicitly bound local owner.")


def local_principal():
    return "local:" + getpass.getuser()


async def owner_turn(loop, content, session_id, render):
    """Authenticate the OS account each turn, then guard the actual console write."""
    from nanobot.knowledge.mail.delivery import delivery
    service = loop.mail_service
    if service is None:
        raise MailError("mail_not_initialized")
    await service.start()
    security = service.access.authenticate("cli", local_principal(), private_chat=True)
    service.access.require(AccessContext.from_security(security, "cli:" + session_id))
    response = await loop.process_direct(content, session_key=session_id,
        channel="cli", chat_id=session_id, security=security)
    with delivery(service, response):
        render(response)


@app.callback()
def options(ctx: typer.Context, config: Path | None = typer.Option(None, "--config", "-c")):
    from nanobot.config.paths import get_config_path
    ctx.obj = (config or get_config_path()).expanduser().resolve()


def _config(path):
    from nanobot.config.loader import resolve_config_env_vars
    # Never silently fall back to defaults after a malformed private config.
    try:
        return resolve_config_env_vars(Config.model_validate_json(path.read_text(encoding="utf-8")))
    except Exception:
        raise MailError("config_invalid") from None


def _run(ctx, operation, *, writer=False, public_receipt=False):
    async def execute():
        service = create_mail_service(_config(ctx.obj), config_path=ctx.obj,
                                      role="management" if writer else "reader")
        if service is None:
            raise MailError("mail_not_initialized")
        try:
            await service.start()
            security = service.access.authenticate("cli", local_principal(), private_chat=True)
            access = AccessContext.from_security(security, "cli:mail-management")
            service.access.require(access, "manage" if writer else "read")
            value = await operation(service, access)
            if public_receipt:
                typer.echo(json.dumps(value))
                return
            # Serialize and print inside the same cross-process delivery gate.
            with service.access.delivery(access), service.store.lock:
                for evidence in [*getattr(value, "matches", []), *getattr(value, "sources", [])]:
                    service.retrieval.read_source(access, SourceRead(evidence_id=evidence.evidence_id))
                typer.echo(value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value, ensure_ascii=False))
        finally:
            await service.close()
    try:
        asyncio.run(execute())
    except MailError as error:
        typer.echo(json.dumps({"status": "failed", "error": error.code}))
        raise typer.Exit(1) from None


@app.command("init")
def initialize(ctx: typer.Context, local_owner: bool = typer.Option(False, "--local-owner")):
    """Create a private instance and local binding; does not connect to a mailbox."""
    if not local_owner:
        raise typer.BadParameter("Explicit --local-owner is required")
    from nanobot.knowledge.mail.store import atomic_write
    path = ctx.obj
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        mail = data.setdefault("mailKnowledge", data.pop("mail_knowledge", {}))
        if (path.parent / "knowledge" / "mail" / "knowledge.sqlite3").exists():
            raise MailError("mail_already_initialized")
        mail.update(enabled=True, archiveEnabled=False)
        if not mail.get("accountId"):
            mail["accountId"] = str(uuid4())
        bindings = mail.setdefault("ownerBindings", [])
        owner = bindings[0].get("ownerId", "local-owner") if bindings else "local-owner"
        binding = MailOwnerBinding(binding_id="local-" + uuid4().hex, principal_id=local_principal(),
                                   owner_id=owner, channel="cli", scopes={"read", "manage"})
        bindings.append(binding.model_dump(mode="json", by_alias=True))
        defaults = data.setdefault("agents", {}).setdefault("defaults", {})
        defaults["model"] = "deepseek-v4-flash"
        if defaults.get("dream", {}).get("modelOverride"):
            defaults["dream"]["modelOverride"] = "deepseek-v4-flash"
        config = Config.model_validate(data)
        service = create_mail_service(config, config_path=path, role="management")
        service.store.initialize()
        service.access.initialize_bindings(config.mail_knowledge.owner_bindings)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode())
        typer.echo(json.dumps({"status": "completed", "binding_id": binding.binding_id,
                               "principal_id": binding.principal_id, "archive_enabled": False,
                               "model": "deepseek-v4-flash"}, ensure_ascii=False))
    except MailError as error:
        typer.echo(json.dumps({"status": "failed", "error": error.code}))
        raise typer.Exit(1) from None
    except Exception:
        typer.echo('{"status":"failed","error":"initialization_failed"}')
        raise typer.Exit(1) from None


@app.command()
def status(ctx: typer.Context):
    _run(ctx, lambda service, access: service.status(access))


@app.command("backup")
def backup_archive(ctx: typer.Context):
    """Create a verified private backup; stop Gateway first. Never copies credentials."""
    from nanobot.knowledge.mail.store import identifier

    async def operation(service, access):
        name = identifier("backup_")
        with service.access.delivery(access, "manage"):
            manifest = service.store.backup(service.store.root / "backups" / name)
        return {"status": "completed", "backup_id": name,
                "file_count": len(manifest["files"]), "query_state_included": False,
                "private_history_included": False}
    _run(ctx, operation, writer=True)


@app.command("restore-backup")
def restore_archive(ctx: typer.Context, backup_id: str = typer.Argument(""),
                    confirm_local_restore: bool = typer.Option(False, "--confirm-local-restore"),
                    resume: bool = typer.Option(False, "--resume")):
    """Offline restore: current deletions/bindings win; old query/token state expires."""
    if not confirm_local_restore:
        raise typer.BadParameter("Explicit --confirm-local-restore is required; stop Gateway and create a current backup first")
    from nanobot.knowledge.mail.restore import restore_backup

    if resume:
        # Ordinary startup deliberately refuses a pending restore. Only the same
        # local OS principal that began it may complete the durable recovery plan.
        try:
            service = create_mail_service(_config(ctx.obj), config_path=ctx.obj, role="management")
            if service is None:
                raise MailError("mail_not_initialized")
            result = restore_backup(service.store, backup_id, principal_id=local_principal(), resume=True)
            typer.echo(json.dumps(result))
        except MailError as error:
            typer.echo(json.dumps({"status": "failed", "error": error.code}))
            raise typer.Exit(1) from None
        return

    async def operation(service, access):
        with service.access.delivery(access, "manage"):
            return restore_backup(service.store, backup_id, principal_id=local_principal())
    _run(ctx, operation, writer=True, public_receipt=True)


@app.command()
def search(ctx: typer.Context, query: str, exact: list[str] = typer.Option([], "--exact"),
           limit: int = typer.Option(8, min=1, max=20)):
    _run(ctx, lambda service, access: service.search(access, MailQuery(query=query, exact_terms=exact, limit=limit)))


@app.command()
def source(ctx: typer.Context, evidence: str):
    _run(ctx, lambda service, access: service.read_source(access, SourceRead(evidence_id=evidence)))


@app.command()
def manage(ctx: typer.Context, action: str, targets: list[str],
           request_id: str = typer.Option(..., "--request-id")):
    """Queue local retry/reparse/reindex or prepare a confirmed source action."""
    try:
        request = ManageAction(action=action, target_ids=targets, request_id=request_id)
    except Exception:
        raise typer.BadParameter("Invalid action, targets or request ID") from None
    _run(ctx, lambda service, access: service.manage(access, request), writer=True)


@app.command()
def confirm(ctx: typer.Context, token: str):
    """Consume an exact, single-use confirmation previously prepared locally."""
    async def operation(service, access):
        from nanobot.knowledge.mail.management import MailManagement
        return MailManagement(service.store, service.access).confirm(access, token)
    _run(ctx, operation, writer=True)


@app.command()
def receipt(ctx: typer.Context, action_id: str):
    async def operation(service, access):
        with service.store.connect() as db:
            row = db.execute("SELECT receipt_json FROM management_actions WHERE action_id=? AND binding_id=?", (action_id, access.binding_id)).fetchone()
        if not row:
            raise MailError("source_not_found")
        return json.loads(row[0])
    _run(ctx, operation)


@app.command()
def revoke(ctx: typer.Context, binding_id: str):
    async def operation(service, access):
        return {"status": "completed", "policy_revision": service.access.revoke(binding_id, access=access)}
    _run(ctx, operation, writer=True, public_receipt=True)


@app.command()
def bindings(ctx: typer.Context):
    """Inspect the durable authorization registry, including revoked bindings."""
    async def operation(service, access):
        with service.store.connect() as db:
            return {"bindings": [dict(row) for row in db.execute(
                "SELECT binding_id,principal_id,channel,scopes_json,policy_revision,revoked FROM bindings WHERE owner_id=? ORDER BY binding_id",
                (access.owner_id,))]}
    _run(ctx, operation)


@app.command()
def bind(ctx: typer.Context, channel: str, principal: str,
         binding_id: str = typer.Option(..., "--binding-id"),
         read_only: bool = typer.Option(False, "--read-only")):
    """Explicitly grant a verified private principal access; never inferred from chat text."""
    async def operation(service, access):
        try:
            binding = MailOwnerBinding(binding_id=binding_id, principal_id=principal,
                owner_id=access.owner_id, channel=channel,
                scopes={"read"} if read_only else {"read", "manage"})
        except ValueError:
            raise MailError("invalid_binding") from None
        service.access.grant(binding, access=access)
        return {"status": "completed", "binding_id": binding_id}
    _run(ctx, operation, writer=True, public_receipt=True)


@app.command()
def mode(ctx: typer.Context, archive: bool = typer.Option(..., "--archive/--no-archive"),
         trigger: bool = typer.Option(..., "--trigger/--no-trigger"),
         expected_revision: int = typer.Option(..., "--expected-revision", min=1)):
    """Apply a revision-checked receiving mode without connecting to the mailbox."""
    async def operation(service, access):
        return service.set_mode(archive_enabled=archive, trigger_agent=trigger,
                                expected_revision=expected_revision, access=access)
    _run(ctx, operation, writer=True)

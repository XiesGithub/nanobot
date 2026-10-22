"""One instance path and explicit runtime roles for all mail entry points."""

from __future__ import annotations

from pathlib import Path

from nanobot.knowledge.mail.contracts import GENERATION_MODEL, MailError


def create_mail_service(config, *, config_path: Path | None = None, role="reader"):
    if role not in {"reader", "gateway", "management"}:
        raise ValueError("Unknown mail runtime role")
    if config_path is None:
        from nanobot.config.paths import get_config_path
        config_path = get_config_path()
    root = config_path.expanduser().resolve().parent / "knowledge" / "mail"
    # Existing private state keeps isolation active even when enabled is later
    # turned off. An unreadable database must fail closed during service startup.
    if not config.mail_knowledge.enabled and not (root / "knowledge.sqlite3").exists():
        return None
    if config.agents.defaults.unified_session:
        raise MailError("unified_session_incompatible")
    if config.agents.defaults.model != GENERATION_MODEL:
        raise MailError("model_not_allowed")
    from nanobot.knowledge.mail.service import MailService
    email = getattr(config.channels, "email", {})
    trigger = email.get("triggerAgent", email.get("trigger_agent", True)) if isinstance(email, dict) else email.trigger_agent
    if role == "gateway" and config.mail_knowledge.archive_enabled and config.mail_knowledge.account_id is None:
        raise MailError("account_initialization_required")
    service = MailService(root, config.mail_knowledge, readonly=role == "reader",
                          worker_enabled=role == "gateway", initial_trigger_agent=trigger)
    from nanobot.channels.email import EmailConfig
    service.email_config = EmailConfig.model_validate(email) if isinstance(email, dict) else email
    return service

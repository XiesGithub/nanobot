"""Owner-only management receipts and explicit single-use confirmations."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import AccessContext, MailError, ManageAction, ManageReceipt
from nanobot.knowledge.mail.store import MailStore, identifier, json_text


def same_targets(left, right):
    # Cleanup finishing changes availability, not the confirmed source identity.
    return [{k: v for k, v in t.items() if k != "cleanup_pending"} for t in left] == [
        {k: v for k, v in t.items() if k != "cleanup_pending"} for t in right
    ]


class MailManagement:
    def __init__(self, store: MailStore, access: MailAccess, *, worker_online=lambda: False):
        self.store = store
        self.access = access
        self.worker_online = worker_online

    def _targets(self, db, access, action, ids):
        result = []
        for target in dict.fromkeys(ids):
            if action == "reindex" and ids == ["all"]:
                result.append({"id": "all", "index_revision": db.execute(
                    "SELECT active_index_revision FROM schema_meta").fetchone()[0]})
            elif action == "request_restore":
                row = db.execute("SELECT * FROM source_tombstones WHERE old_mail_id=? AND owner_id=?",
                                 (target, access.owner_id)).fetchone()
                if not row:
                    raise MailError("source_not_found")
                result.append({"id": target, "revision": row["restore_revision"],
                               "account_id": row["account_id"], "mailbox": row["mailbox_server_name"],
                               "removed_at": row["removed_at"], "cleanup_pending": bool(row["cleanup_pending"])})
            elif action in {"retry", "reparse", "prepare_refetch", "reindex"}:
                row = db.execute("""SELECT d.*,m.owner_id,m.revision,m.deleted_at,m.subject
                    FROM documents d JOIN mail_items m USING(mail_id) WHERE document_id=? AND owner_id=?""",
                    (target, access.owner_id)).fetchone()
                if not row:
                    raise MailError("source_not_found")
                if row["deleted_at"] is not None:
                    raise MailError("source_removed")
                if action in {"retry", "reparse"} and (not row["object_ref"] or not self.store.safe_path(row["object_ref"]).is_file()):
                    raise MailError("local_input_missing")
                if action == "reindex" and not row["active_generation"]:
                    raise MailError("retained_text_missing")
                result.append({"id": target, "mail_id": row["mail_id"], "revision": row["revision"],
                               "generation": row["active_generation"], "filename": row["filename"],
                               "error_code": row["error_code"], "size": row["size"]})
            elif action in {"dismiss_interaction", "prepare_interaction_retry"}:
                row = db.execute("""SELECT i.* FROM interaction_dispatches i JOIN mail_items m USING(source_key)
                    WHERE task_id=? AND owner_id=? AND deleted_at IS NULL""", (target, access.owner_id)).fetchone()
                if not row or row["state"] != "uncertain":
                    raise MailError("interaction_not_uncertain")
                result.append({"id": target, "revision": row["revision"], "state": row["state"],
                               "warning": "May already have executed or replied; effects cannot be undone."})
            else:
                row = db.execute("SELECT * FROM mail_items WHERE mail_id=? AND owner_id=? AND deleted_at IS NULL",
                                 (target, access.owner_id)).fetchone()
                if not row:
                    raise MailError("source_not_found")
                result.append({"id": target, "revision": row["revision"], "subject": row["subject"]})
        return result

    def manage(self, access: AccessContext, action: ManageAction) -> ManageReceipt:
        self.access.require(access, "manage")
        with self.access.delivery(access, "manage"), self.store.lock, self.store.connect(write=True) as db:
            old = db.execute("SELECT receipt_json FROM management_actions WHERE binding_id=? AND request_id=?",
                             (access.binding_id, action.request_id)).fetchone()
            if old:
                return ManageReceipt.model_validate_json(old[0])
            targets = self._targets(db, access, action.action, action.target_ids)
            action_id = identifier("action_")
            confirmation = action.action in {"prepare_remove", "request_restore", "prepare_refetch", "prepare_interaction_retry"}
            token = secrets.token_urlsafe(32) if confirmation else None
            receipt = ManageReceipt(
                status="needs_confirmation" if confirmation else ("accepted" if self.worker_online() else "accepted_waiting_service"),
                action_id=action_id, target_ids=[t["id"] for t in targets], confirmation_token=token,
                preview=targets,
            )
            payload = {"action": action.model_dump(), "access": access.model_dump(mode="json"),
                       "targets": targets, "policy": self.store.config.model_dump(mode="json")}
            if token:
                db.execute("INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,?,?,NULL)", (
                    hashlib.sha256(token.encode()).hexdigest(), access.owner_id, access.binding_id,
                    access.policy_revision, access.session_key, action.action,
                    json_text(receipt.target_ids), json_text(targets), time.time() + 300,
                ))
            db.execute("INSERT INTO management_actions VALUES(?,?,?,?,?,?,?,?)", (
                action_id, access.binding_id, action.request_id, action.action, json_text(payload),
                receipt.model_dump_json(), receipt.status, time.time(),
            ))
        return receipt

    def confirm(self, access: AccessContext, token: str, *, expected_action=None, request_id=None) -> ManageReceipt:
        """Only authenticated UI/CLI/command handlers call this; never a model tool."""
        self.access.require(access, "manage")
        digest = hashlib.sha256(token.encode()).hexdigest()
        payload_hash = hashlib.sha256((digest + "|" + str(expected_action)).encode()).hexdigest()
        with self.access.delivery(access, "manage"), self.store.lock:
            with self.store.connect(write=True) as db:
                if request_id:
                    old = db.execute("SELECT * FROM web_requests WHERE binding_id=? AND session_key=? AND request_id=?", (access.binding_id, access.session_key, request_id)).fetchone()
                    if old:
                        if old["action"] != "confirm" or old["payload_hash"] != payload_hash:
                            raise MailError("request_id_conflict")
                        return ManageReceipt.model_validate_json(old["response_json"])
                row = db.execute("SELECT * FROM confirmations WHERE token_hash=?", (digest,)).fetchone()
                if not row or row["used_at"] is not None or row["expires_at"] <= time.time():
                    raise MailError("confirmation_expired")
                if expected_action is not None and row["action"] != expected_action:
                    raise MailError("confirmation_action_mismatch")
                if (row["owner_id"], row["binding_id"], row["session_key"], row["policy_revision"]) != (
                    access.owner_id, access.binding_id, access.session_key, access.policy_revision,
                ):
                    raise MailError("unauthorized")
                ids = json.loads(row["target_ids"])
                current = self._targets(db, access, row["action"], ids)
                if not same_targets(current, json.loads(row["target_revisions"])):
                    raise MailError("revision_conflict")
                actions = list(db.execute("SELECT * FROM management_actions WHERE binding_id=? AND status='needs_confirmation'", (access.binding_id,)))
                action = next((a for a in actions if json.loads(a["receipt_json"]).get("confirmation_token") == token), None)
                if action is None:
                    raise MailError("confirmation_expired")
                payload = json.loads(action["payload_json"])
                if payload["policy"] != self.store.config.model_dump(mode="json"):
                    raise MailError("revision_conflict")
                if row["action"] == "prepare_interaction_retry" and not db.execute("SELECT trigger_agent FROM schema_meta").fetchone()[0]:
                    raise MailError("interaction_disabled")
                receipt = ManageReceipt(status="accepted" if self.worker_online() else "accepted_waiting_service",
                                        action_id=action["action_id"], target_ids=ids)
                db.execute("UPDATE confirmations SET used_at=? WHERE token_hash=?", (time.time(), digest))
                db.execute("UPDATE management_actions SET status=?,receipt_json=? WHERE action_id=?",
                           (receipt.status, receipt.model_dump_json(), action["action_id"]))
                if request_id:
                    db.execute("INSERT INTO web_requests VALUES(?,?,?,'confirm',?,?)", (access.binding_id, access.session_key, request_id, payload_hash, receipt.model_dump_json()))
            # Removal must become invisible at confirmation time, even without Worker.
            if row["action"] == "prepare_remove":
                cleanup_pending = False
                for target in current:
                    try:
                        self.store.remove(target["id"], target["revision"])
                    except (OSError, sqlite3.Error):
                        # Logical removal already committed; report pending cleanup,
                        # never claim physical deletion completed after a disk error.
                        cleanup_pending = True
                receipt.status = "accepted" if cleanup_pending else "completed"
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE management_actions SET status=?,receipt_json=? WHERE action_id=?",
                               ("waiting_cleanup" if cleanup_pending else "completed", receipt.model_dump_json(), receipt.action_id))
                    if request_id:
                        db.execute("UPDATE web_requests SET response_json=? WHERE binding_id=? AND session_key=? AND request_id=?", (receipt.model_dump_json(), access.binding_id, access.session_key, request_id))
            return receipt

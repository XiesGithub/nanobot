"""Worker-side consumption of durable, authorized management actions."""

from __future__ import annotations

import json
import random
import time

from nanobot.knowledge.mail.access import MailAccess
from nanobot.knowledge.mail.contracts import AccessContext, MailError, ManageReceipt
from nanobot.knowledge.mail.management import MailManagement, same_targets
from nanobot.knowledge.mail.store import identifier


class MailMaintenance:
    def __init__(self, store, *, encoder=None, source_fetcher=None):
        self.store = store
        self.access = MailAccess(store)
        self.encoder = encoder
        self.source_fetcher = source_fetcher

    def recover(self):
        # Actions here only publish local state. Source retrieval must itself be
        # fenced and replay-safe; interaction retry only requeues its one task ID.
        with self.store.connect(write=True) as db:
            db.execute("UPDATE management_actions SET status='accepted' WHERE status='running'")

    def process_one(self) -> bool:
        with self.store.connect() as db:
            waiting = list(db.execute("SELECT * FROM management_actions WHERE status IN ('waiting_jobs','waiting_source_jobs')"))
            row = db.execute("""SELECT * FROM management_actions
                WHERE status IN ('accepted','accepted_waiting_service')
                AND COALESCE(json_extract(payload_json,'$.retry.next_at'),0)<=?
                ORDER BY created_at LIMIT 1""", (time.time(),)).fetchone()
            cleanup = list(db.execute("SELECT * FROM management_actions WHERE status='waiting_cleanup'"))
        for item in waiting:
            self._finish_waiting(item)
        for item in cleanup:
            self._finish_cleanup(item)
        if row is None:
            return False
        payload = json.loads(row["payload_json"])
        payload["action_id"] = row["action_id"]
        access = AccessContext.model_validate(payload["access"])
        receipt = ManageReceipt.model_validate_json(row["receipt_json"])
        try:
            with self.access.delivery(access, "manage"), self.store.lock, self.store.connect(write=True) as db:
                changed = db.execute("UPDATE management_actions SET status='running' WHERE action_id=? AND status IN ('accepted','accepted_waiting_service')", (row["action_id"],)).rowcount
                if not changed:
                    return True
                action = row["action"]
                done = {r[0] for r in db.execute("SELECT target_id FROM maintenance_effects WHERE action_id=?", (row["action_id"],))}
                current = MailManagement(self.store, self.access)._targets(db, access, action, [t for t in receipt.target_ids if t not in done])
                if not same_targets(current, [t for t in payload["targets"] if t["id"] not in done]):
                    raise MailError("revision_conflict")
                if action == "request_restore" and any(t["cleanup_pending"] for t in current):
                    db.execute("UPDATE management_actions SET status='accepted_waiting_service' WHERE action_id=?", (row["action_id"],))
                    return False
                if action in {"prepare_refetch", "request_restore"} and payload["policy"] != self.store.config.model_dump(mode="json"):
                    raise MailError("revision_conflict")
                if action in {"retry", "reparse"}:
                    for target in current:
                        db.execute("UPDATE documents SET extract_status='pending',error_code=NULL WHERE document_id=?", (target["id"],))
                        db.execute("UPDATE ingest_jobs SET stage='accepted',attempt=0,next_run_at=0,lease_until=0,generation=generation+1,error_code=NULL WHERE mail_id=?", (target["mail_id"],))
                        db.execute("UPDATE mail_items SET status='queued' WHERE mail_id=?", (target["mail_id"],))
                    db.execute("UPDATE management_actions SET status='waiting_jobs' WHERE action_id=?", (row["action_id"],))
                elif action in {"dismiss_interaction", "prepare_interaction_retry"}:
                    if action == "prepare_interaction_retry" and not db.execute("SELECT trigger_agent FROM schema_meta").fetchone()[0]:
                        raise MailError("interaction_disabled")
                    for target in current:
                        db.execute("UPDATE interaction_dispatches SET state=?,attempt_id=?,revision=revision+1,updated_at=? WHERE task_id=? AND revision=?", (
                            "pending" if action == "prepare_interaction_retry" else "cancelled",
                            identifier("attempt_"), time.time(), target["id"], target["revision"],
                        ))
                elif action == "prepare_remove":
                    # A crash between token consumption and logical removal must
                    # still complete the authorized removal after restart.
                    pass
                elif action not in {"reindex", "prepare_refetch", "request_restore"}:
                    raise MailError("unsupported_action")
                if action in {"dismiss_interaction", "prepare_interaction_retry"}:
                    receipt.status = "completed"
                    db.execute("UPDATE management_actions SET status='completed',receipt_json=? WHERE action_id=?",
                               (receipt.model_dump_json(), receipt.action_id))
            if action in {"retry", "reparse", "dismiss_interaction", "prepare_interaction_retry"}:
                return True
            if action == "reindex":
                from nanobot.knowledge.mail.versioning import rebuild
                rebuild(self.store, self.encoder,
                        document_ids=None if receipt.target_ids == ["all"] else receipt.target_ids,
                        publication_guard=lambda: self.access.delivery(access, "manage"))
            elif action == "prepare_remove":
                with self.access.delivery(access, "manage"):
                    for target in current:
                        self.store.remove(target["id"], target["revision"])
            elif action in {"prepare_refetch", "request_restore"}:
                if self.source_fetcher is None:
                    raise MailError("source_connection_unavailable")
                self.source_fetcher(action, access, payload)
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE management_actions SET status='waiting_source_jobs' WHERE action_id=?", (receipt.action_id,))
                return True
            receipt.status = "completed"
        except MailError as error:
            if error.code in {"capacity_backpressure", "disk_full", "service_stopping", "source_connection_unavailable"}:
                previous = payload.get("retry", {}).get("attempt", 0)
                if error.code != "source_connection_unavailable" or previous < 3:
                    attempt = min(previous + 1, 8)
                    payload["retry"] = {"attempt": attempt,
                        "next_at": time.time() + min(60, 2 ** attempt) * random.uniform(.8, 1.2)}
                    receipt.status, receipt.error_code = "accepted_waiting_service", error.code
                    with self.store.connect(write=True) as db:
                        db.execute("UPDATE management_actions SET status=?,receipt_json=?,payload_json=? WHERE action_id=?",
                            (receipt.status, receipt.model_dump_json(), json.dumps(payload), receipt.action_id))
                    return True
            receipt.status, receipt.error_code = "failed", error.code
        except Exception:
            receipt.status, receipt.error_code = "failed", "maintenance_failed"
        with self.store.connect(write=True) as db:
            db.execute("UPDATE management_actions SET status=?,receipt_json=? WHERE action_id=?",
                       (receipt.status, receipt.model_dump_json(), receipt.action_id))
        return True

    def _finish_waiting(self, row):
        payload = json.loads(row["payload_json"])
        receipt = ManageReceipt.model_validate_json(row["receipt_json"])
        with self.store.connect(write=True) as db:
            if row["status"] == "waiting_source_jobs":
                outcomes = list(db.execute("SELECT j.stage,d.extract_status,d.error_code FROM documents d JOIN ingest_jobs j USING(mail_id) WHERE d.mail_id IN (SELECT mail_id FROM maintenance_effects WHERE action_id=?)", (row["action_id"],)))
            else:
                outcomes = [db.execute("SELECT j.stage,d.extract_status,d.error_code FROM documents d JOIN ingest_jobs j USING(mail_id) WHERE document_id=?", (t["id"],)).fetchone() for t in payload["targets"]]
            if any(r and r["stage"] not in {"finished", "failed", "removed"} for r in outcomes):
                return
            receipt.status = "completed" if all(r and r["extract_status"] == "ready" for r in outcomes) else "failed"
            if receipt.status == "failed":
                receipt.error_code = next((r["error_code"] for r in outcomes if r and r["error_code"]), "processing_failed")
            db.execute("UPDATE management_actions SET status=?,receipt_json=? WHERE action_id=?",
                       (receipt.status, receipt.model_dump_json(), receipt.action_id))

    def cleanup_one(self) -> bool:
        with self.store.connect() as db:
            row = db.execute("SELECT old_mail_id FROM source_tombstones WHERE cleanup_pending=1 LIMIT 1").fetchone()
        if row is None:
            return False
        self.store.cleanup_removed(row[0])
        return True

    def _finish_cleanup(self, row):
        receipt = ManageReceipt.model_validate_json(row["receipt_json"])
        with self.store.connect(write=True) as db:
            if any(db.execute("SELECT 1 FROM source_tombstones WHERE old_mail_id=? AND cleanup_pending=1", (mid,)).fetchone() for mid in receipt.target_ids):
                return
            receipt.status = "completed"
            db.execute("UPDATE management_actions SET status='completed',receipt_json=? WHERE action_id=?",
                       (receipt.model_dump_json(), receipt.action_id))

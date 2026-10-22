"""Interaction intention tracking, independent of archive success and SMTP delivery."""

import json
import time

from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.store import MailStore, identifier, json_text


class InteractionDispatch:
    def __init__(self, store: MailStore):
        self.store = store

    def intent(self, source_key: str, item: dict, mode_revision: int):
        with self.store.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO interaction_dispatches VALUES(?,?,?,'pending',?,?,?,1)", (
                source_key, identifier("task_"), mode_revision, json_text(item), identifier("attempt_"), time.time(),
            ))
        return self.get(source_key)

    def get(self, source_key: str):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM interaction_dispatches WHERE source_key=?", (source_key,)).fetchone()
            if row:
                return dict(row)
        return None

    def pending(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM interaction_dispatches WHERE state='pending' ORDER BY updated_at LIMIT 20").fetchall()
        return [{**dict(row), "input": json.loads(row["input_json"])} for row in rows if row["input_json"]]

    def transition(self, task_id: str, before: str, after: str, *, attempt_id=None) -> bool:
        allowed = {("pending", "dispatching"), ("dispatching", "running"),
                   ("running", "completed"), ("uncertain", "cancelled")}
        if (before, after) not in allowed:
            raise MailError("invalid_dispatch_transition")
        with self.store.connect(write=True) as db:
            if after in {"dispatching", "running"}:
                mode = db.execute("SELECT trigger_agent FROM schema_meta").fetchone()[0]
                if not mode:
                    return False
            result = db.execute("UPDATE interaction_dispatches SET state=?,updated_at=?,revision=revision+1 WHERE task_id=? AND state=? AND (? IS NULL OR attempt_id=?)",
                                (after, time.time(), task_id, before, attempt_id, attempt_id))
            if result.rowcount == 1 and after == "completed":
                db.execute("INSERT OR IGNORE INTO interaction_deliveries SELECT attempt_id,task_id,'queued',NULL,? FROM interaction_dispatches WHERE task_id=?", (time.time(), task_id))
            return result.rowcount == 1

    def active(self, task_id: str, attempt_id: str) -> bool:
        with self.store.connect() as db:
            return bool(db.execute("""SELECT 1 FROM interaction_dispatches, schema_meta
                WHERE task_id=? AND attempt_id=? AND state IN ('dispatching','running') AND trigger_agent=1""",
                (task_id, attempt_id)).fetchone())

    def delivery_result(self, task_id: str, attempt_id: str, status: str, code: str | None = None):
        with self.store.connect(write=True) as db:
            db.execute("""UPDATE interaction_deliveries SET status=?,error_code=?,updated_at=?
                WHERE attempt_id=? AND task_id=?
                AND status IN ('queued','sending')""", (status, code, time.time(), attempt_id, task_id))

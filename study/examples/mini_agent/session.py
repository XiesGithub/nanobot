"""Single-writer JSON persistence. Caller owns the storage directory."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        if not isinstance(key, str) or not key:
            raise ValueError("session key must be nonempty text")
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("session path escapes storage directory")
        return path

    def load(self, key):
        path = self._path(key)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("version") != 1
                or data.get("key") != key or not isinstance(data.get("messages"), list)):
            raise ValueError("invalid session envelope")
        for message in data["messages"]:
            if (not isinstance(message, dict)
                    or message.get("role") not in {"user", "assistant", "tool"}
                    or not isinstance(message.get("content"), str)):
                raise ValueError("invalid stored message")
        return data["messages"]

    def save(self, key, messages):
        path = self._path(key)
        payload = json.dumps({"version": 1, "key": key, "messages": messages},
                             ensure_ascii=False, indent=2)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

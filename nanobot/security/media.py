"""Per-chat access scope for WebSocket media paths."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterator

from nanobot.config.paths import get_media_dir

_media_scope: ContextVar[tuple[str, str] | None] = ContextVar("media_scope", default=None)


def websocket_media_dir(chat_id: str) -> Path:
    """Return the private media directory for a WebSocket chat."""
    digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    return get_media_dir("websocket") / digest


def current_media_scope() -> tuple[str, str] | None:
    """Return the channel and chat currently being processed, if any."""
    return _media_scope.get()


def assert_media_path_allowed(path: str | Path) -> Path:
    """Resolve and enforce the active chat scope for WebSocket media paths."""
    resolved = Path(path).expanduser().resolve()
    scope = current_media_scope()
    if scope is None:
        return resolved

    media_root = get_media_dir().resolve()
    websocket_root = get_media_dir("websocket").resolve()
    # Paths within the WebSocket tree and its media parent can expose sibling
    # chat names through direct reads or directory traversal.
    touches_websocket_tree = _is_under(resolved, websocket_root) or (
        _is_under(resolved, media_root) and _is_under(websocket_root, resolved)
    )
    if touches_websocket_tree:
        channel, chat_id = scope
        own_dir = websocket_media_dir(chat_id).resolve()
        if channel != "websocket" or not _is_under(resolved, own_dir):
            raise PermissionError("Path is outside the current chat's allowed media directory")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


@contextmanager
def bind_media_scope(channel: str, chat_id: str) -> Iterator[None]:
    """Bind a channel/chat scope for path checks in this async context."""
    token: Token[tuple[str, str] | None] = _media_scope.set((channel, chat_id))
    try:
        yield
    finally:
        _media_scope.reset(token)

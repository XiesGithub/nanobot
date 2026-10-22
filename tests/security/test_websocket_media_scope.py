"""WebSocket uploads stay readable only in their originating chat scope."""

import asyncio
import hashlib

import pytest

from nanobot.agent.tools.filesystem import ListDirTool, ReadFileTool, _resolve_path
from nanobot.security.media import bind_media_scope, websocket_media_dir


def _media_dir(root, channel=None):
    return root / channel if channel else root


def _patch_media_roots(monkeypatch, root):
    monkeypatch.setattr(
        "nanobot.agent.tools.filesystem.get_media_dir",
        lambda channel=None: _media_dir(root, channel),
    )
    monkeypatch.setattr(
        "nanobot.security.media.get_media_dir",
        lambda channel=None: _media_dir(root, channel),
    )


@pytest.mark.asyncio
async def test_chat_can_read_own_media_but_not_another_chat(tmp_path, monkeypatch):
    _patch_media_roots(monkeypatch, tmp_path / "media")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    a_file = websocket_media_dir("A") / "notice.txt"
    a_file.parent.mkdir(parents=True)
    a_file.write_text("notice for A", encoding="utf-8")
    tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)

    with bind_media_scope("websocket", "A"):
        result = await tool.execute(path=str(a_file))
        assert "notice for A" in result
        assert "Error" not in result

    with bind_media_scope("websocket", "B"):
        result = await tool.execute(path=str(a_file))
        assert "Error" in result
        with pytest.raises(PermissionError):
            _resolve_path(str(a_file), workspace, workspace)


@pytest.mark.asyncio
async def test_scope_blocks_websocket_parent_glob_and_traversal(tmp_path, monkeypatch):
    _patch_media_roots(monkeypatch, tmp_path / "media")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chat_dir = websocket_media_dir("A")
    chat_dir.mkdir(parents=True)
    (chat_dir / "notice.txt").write_text("notice", encoding="utf-8")
    b_dir = websocket_media_dir("B")
    b_dir.mkdir(parents=True)
    (b_dir / "private-name.txt").write_text("B only", encoding="utf-8")
    listing = ListDirTool(workspace=workspace, allowed_dir=workspace)

    with bind_media_scope("websocket", "A"):
        assert "notice.txt" in await listing.execute(path=str(chat_dir))
        for forbidden in (
            tmp_path / "media" / "websocket",
            tmp_path / "media" / "websocket" / "*",
            chat_dir / ".." / hashlib.sha256(b"B").hexdigest(),
        ):
            with pytest.raises(PermissionError):
                _resolve_path(str(forbidden), workspace, workspace)

        parent_listing = ListDirTool(workspace=tmp_path, allowed_dir=tmp_path)
        recursive_result = await parent_listing.execute(path=str(tmp_path), recursive=True)
        assert "notice.txt" in recursive_result
        assert "private-name.txt" not in recursive_result


@pytest.mark.asyncio
async def test_scopes_are_isolated_between_concurrent_tasks(tmp_path, monkeypatch):
    _patch_media_roots(monkeypatch, tmp_path / "media")
    a_file = websocket_media_dir("A") / "notice.txt"
    a_file.parent.mkdir(parents=True)
    a_file.write_text("A only", encoding="utf-8")

    async def check(chat_id):
        with bind_media_scope("websocket", chat_id):
            await asyncio.sleep(0)
            try:
                _resolve_path(str(a_file))
                return True
            except PermissionError:
                return False

    assert await asyncio.gather(check("A"), check("B")) == [True, False]


def test_other_channels_cannot_read_websocket_media_but_unscoped_ops_remain(tmp_path, monkeypatch):
    _patch_media_roots(monkeypatch, tmp_path / "media")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    a_file = websocket_media_dir("A") / "notice.txt"
    a_file.parent.mkdir(parents=True)
    a_file.write_text("A only", encoding="utf-8")

    with bind_media_scope("telegram", "chat"):
        with pytest.raises(PermissionError):
            _resolve_path(str(a_file), workspace, workspace)

    assert _resolve_path(str(a_file), workspace) == a_file.resolve()


def test_websocket_media_dir_uses_stable_chat_hash(tmp_path, monkeypatch):
    _patch_media_roots(monkeypatch, tmp_path / "media")
    assert websocket_media_dir("A") == tmp_path / "media" / "websocket" / hashlib.sha256(b"A").hexdigest()

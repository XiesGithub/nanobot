"""Redacted diagnostics and failure preservation for atomic session saves."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.session.manager import SessionManager
from nanobot.utils.observability import observation_context

_SESSION_KEY = "private.session.key.with.dot.secret"
_SESSION_TEXT = "private-session-body-95a20"
_EXCEPTION_TEXT = "private-os-error-274bc"


class InjectedPermissionError(PermissionError):
    @property
    def winerror(self):
        return 1234


def _failure() -> InjectedPermissionError:
    return InjectedPermissionError(errno.EACCES, _EXCEPTION_TEXT)


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    return SessionManager(workspace=tmp_path)


def _saved_session(manager: SessionManager):
    session = manager.get_or_create(_SESSION_KEY)
    session.add_message("user", "original persisted text")
    manager.save(session)
    session.add_message("user", _SESSION_TEXT)
    target = manager._get_session_path(_SESSION_KEY)
    return session, target, target.read_bytes()


def _owned_temp_paths(manager: SessionManager) -> list[Path]:
    return list(manager.sessions_dir.glob(".session-save-*.tmp"))


def _save_with_observation(manager: SessionManager, session):
    events = []
    with observation_context(
        lambda kind, **payload: events.append({"type": kind, **payload}),
        task_id="session-save-test",
    ):
        manager.save(session)
    return events


def _assert_redacted_failure(event, *, operation: str, extension: str) -> None:
    assert event["type"] == "session_save_failure"
    assert event["operation"] == operation
    assert event["path"] == f"<session>{extension}"
    assert event["location"] == (
        f"nanobot.session.manager.SessionManager.save.{operation}"
    )
    assert event["error_kind"] == "InjectedPermissionError"
    assert event["errno"] == errno.EACCES
    assert event["winerror"] == 1234
    assert len(event["save_id"]) == 32
    assert event["save_phase"] == operation
    assert event["committed"] is False
    serialized = json.dumps(event)
    for secret in (_SESSION_KEY, _SESSION_TEXT, _EXCEPTION_TEXT, str(Path.cwd())):
        assert secret not in serialized


def test_open_failure_is_diagnosed_without_changing_persisted_file(manager, monkeypatch):
    session, target, original_bytes = _saved_session(manager)
    error = _failure()
    foreign = manager.sessions_dir / ".session-save-foreign.tmp"
    foreign.write_bytes(b"foreign temporary file")

    def fail_temp_open(**_kwargs):
        raise error

    events = []
    original_mkstemp = tempfile.mkstemp
    monkeypatch.setattr("nanobot.session.manager.tempfile.mkstemp", fail_temp_open)
    with observation_context(
        lambda kind, **payload: events.append({"type": kind, **payload}),
        task_id="session-save-open-failure",
    ):
        with pytest.raises(InjectedPermissionError) as caught:
            manager.save(session)

    assert caught.value is error
    _assert_redacted_failure(events[0], operation="temp_open", extension=".jsonl.tmp")
    assert target.read_bytes() == original_bytes
    assert foreign.read_bytes() == b"foreign temporary file"
    assert events[0]["temp_created"] is False

    monkeypatch.setattr("nanobot.session.manager.tempfile.mkstemp", original_mkstemp)
    manager.save(session)
    assert SessionManager(workspace=manager.workspace).get_or_create(_SESSION_KEY).messages[-1][
        "content"
    ] == _SESSION_TEXT


def test_mkstemp_retries_existing_candidate_without_deleting_foreign_file(manager, monkeypatch):
    session = manager.get_or_create("collision:session")
    session.add_message("user", "persist after collision")
    original_os_open = os.open
    collided_paths: list[Path] = []

    def collide_first_candidate(file, flags, *args, **kwargs):
        candidate = Path(file)
        if (
            not collided_paths
            and candidate.parent == manager.sessions_dir
            and candidate.name.startswith(".session-save-")
        ):
            candidate.write_bytes(b"foreign owner")
            collided_paths.append(candidate)
        return original_os_open(file, flags, *args, **kwargs)

    monkeypatch.setattr("nanobot.session.manager.os.open", collide_first_candidate)
    manager.save(session)

    assert len(collided_paths) == 1
    assert collided_paths[0].read_bytes() == b"foreign owner"
    assert _owned_temp_paths(manager) == [collided_paths[0]]
    reloaded = SessionManager(workspace=manager.workspace).get_or_create("collision:session")
    assert [message["content"] for message in reloaded.messages] == ["persist after collision"]


def test_parallel_managers_use_distinct_temps_and_leave_readable_session(manager, monkeypatch):
    first_manager = manager
    second_manager = SessionManager(workspace=manager.workspace, allow_legacy=False)
    first = first_manager.get_or_create("parallel:session")
    first.add_message("user", "writer A")
    first_manager.save(first)
    second = second_manager.get_or_create("parallel:session")
    second.messages[-1]["content"] = "writer B"

    original_mkstemp = tempfile.mkstemp
    created: list[Path] = []
    lock = threading.Lock()
    both_created = threading.Barrier(2)

    def pause_after_unique_create(**kwargs):
        fd, name = original_mkstemp(**kwargs)
        with lock:
            created.append(Path(name))
        both_created.wait(timeout=5)
        return fd, name

    monkeypatch.setattr("nanobot.session.manager.tempfile.mkstemp", pause_after_unique_create)
    results: dict[str, BaseException | None] = {"A": None, "B": None}
    events: list[dict[str, object]] = []

    def save(name: str, session_manager: SessionManager, item) -> None:
        try:
            with observation_context(
                lambda kind, **payload: events.append({"type": kind, **payload}),
                task_id=f"parallel-save-{name.lower()}",
            ):
                session_manager.save(item)
        except BaseException as exc:
            results[name] = exc

    threads = [
        threading.Thread(target=save, args=("A", first_manager, first)),
        threading.Thread(target=save, args=("B", second_manager, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    failures = {name: error for name, error in results.items() if error is not None}
    assert len(failures) <= 1
    assert all(isinstance(error, PermissionError) for error in failures.values())
    assert len(created) == 2
    assert len(set(created)) == 2
    assert _owned_temp_paths(manager) == []
    reloaded = SessionManager(workspace=manager.workspace).get_or_create("parallel:session")
    assert [message["content"] for message in reloaded.messages] in (["writer A"], ["writer B"])

    if failures:
        failure = next(event for event in events if event["type"] == "session_save_failure")
        assert failure["operation"] == "os_replace"
        assert failure["committed"] is False
        assert failure["temp_created"] is True
        assert failure["path"] == "<session>.jsonl"
        monkeypatch.setattr("nanobot.session.manager.tempfile.mkstemp", original_mkstemp)
        name = next(iter(failures))
        failed_session = first if name == "A" else second
        failed_manager = first_manager if name == "A" else second_manager
        failed_manager.save(failed_session)
        recovered = SessionManager(workspace=manager.workspace).get_or_create("parallel:session")
        assert recovered.messages[-1]["content"] == failed_session.messages[-1]["content"]
        assert _owned_temp_paths(manager) == []


def test_held_temp_does_not_block_second_manager_then_both_commit(manager, monkeypatch):
    first_manager = manager
    second_manager = SessionManager(workspace=manager.workspace, allow_legacy=False)
    first = first_manager.get_or_create("ordered:session")
    first.add_message("user", "writer A")
    first_manager.save(first)
    second = second_manager.get_or_create("ordered:session")
    second.messages[-1]["content"] = "writer B"

    original_mkstemp = tempfile.mkstemp
    first_temp_open = threading.Event()
    release_first = threading.Event()
    created: dict[str, Path] = {}
    events: list[dict[str, object]] = []

    def ordered_mkstemp(**kwargs):
        fd, name = original_mkstemp(**kwargs)
        writer = threading.current_thread().name
        created[writer] = Path(name)
        if writer == "save-A":
            first_temp_open.set()
            if not release_first.wait(timeout=5):
                os.close(fd)
                raise TimeoutError("first save was not released")
        return fd, name

    monkeypatch.setattr("nanobot.session.manager.tempfile.mkstemp", ordered_mkstemp)
    results: dict[str, BaseException | None] = {"A": None, "B": None}

    def save(name: str, session_manager: SessionManager, item) -> None:
        try:
            with observation_context(
                lambda kind, **payload: events.append({"type": kind, **payload}),
                task_id=f"ordered-save-{name.lower()}",
            ):
                session_manager.save(item)
        except BaseException as exc:
            results[name] = exc

    thread_a = threading.Thread(target=save, name="save-A", args=("A", first_manager, first))
    thread_b = threading.Thread(target=save, name="save-B", args=("B", second_manager, second))
    thread_a.start()
    assert first_temp_open.wait(timeout=5)
    thread_b.start()
    thread_b.join(timeout=5)

    assert not thread_b.is_alive()
    assert results["B"] is None
    interim = SessionManager(workspace=manager.workspace).get_or_create("ordered:session")
    assert interim.messages[-1]["content"] == "writer B"
    assert thread_a.is_alive()

    release_first.set()
    thread_a.join(timeout=5)

    assert not thread_a.is_alive()
    assert results == {"A": None, "B": None}
    assert created["save-A"] != created["save-B"]
    assert _owned_temp_paths(manager) == []
    final = SessionManager(workspace=manager.workspace).get_or_create("ordered:session")
    assert final.messages[-1]["content"] == "writer A"
    assert not any(event["type"] == "session_save_failure" for event in events)


def test_write_failure_is_diagnosed_and_temp_file_is_cleaned(manager, monkeypatch):
    session, target, original_bytes = _saved_session(manager)
    error = _failure()
    original_fdopen = os.fdopen

    class FailingWriter:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.file.__exit__(exc_type, exc_value, traceback)

        def write(self, _value):
            raise error

    def fail_temp_write(fd, *args, **kwargs):
        return FailingWriter(original_fdopen(fd, *args, **kwargs))

    events = []
    monkeypatch.setattr("nanobot.session.manager.os.fdopen", fail_temp_write)
    with observation_context(
        lambda kind, **payload: events.append({"type": kind, **payload}),
        task_id="session-save-write-failure",
    ):
        with pytest.raises(InjectedPermissionError) as caught:
            manager.save(session)

    assert caught.value is error
    _assert_redacted_failure(events[0], operation="temp_write", extension=".jsonl.tmp")
    assert target.read_bytes() == original_bytes
    assert _owned_temp_paths(manager) == []
    assert events[0]["temp_created"] is True


def test_close_failure_does_not_mask_write_exception(manager, monkeypatch):
    session, target, original_bytes = _saved_session(manager)
    write_error = _failure()
    close_error = InjectedPermissionError(errno.EACCES, "private-close-error")
    original_fdopen = os.fdopen

    class FailingWriter:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.file.close()
            raise close_error

        def write(self, _value):
            raise write_error

    def fail_temp_write_and_close(fd, *args, **kwargs):
        return FailingWriter(original_fdopen(fd, *args, **kwargs))

    events = []
    monkeypatch.setattr("nanobot.session.manager.os.fdopen", fail_temp_write_and_close)
    with observation_context(
        lambda kind, **payload: events.append({"type": kind, **payload}),
        task_id="session-save-write-close-failure",
    ):
        with pytest.raises(InjectedPermissionError) as caught:
            manager.save(session)

    assert caught.value is write_error
    assert [event["operation"] for event in events] == ["temp_write", "temp_close"]
    _assert_redacted_failure(events[0], operation="temp_write", extension=".jsonl.tmp")
    _assert_redacted_failure(events[1], operation="temp_close", extension=".jsonl.tmp")
    assert target.read_bytes() == original_bytes
    assert _owned_temp_paths(manager) == []
    assert "close also failed" in " ".join(getattr(write_error, "__notes__", []))


def test_replace_failure_is_diagnosed_and_temp_file_is_cleaned(manager):
    session, target, original_bytes = _saved_session(manager)
    error = _failure()
    events = []
    with patch("nanobot.session.manager.os.replace", side_effect=error):
        with observation_context(
            lambda kind, **payload: events.append({"type": kind, **payload}),
            task_id="session-save-replace-failure",
        ):
            with pytest.raises(InjectedPermissionError) as caught:
                manager.save(session)

    assert caught.value is error
    _assert_redacted_failure(events[0], operation="os_replace", extension=".jsonl")
    assert target.read_bytes() == original_bytes
    assert _owned_temp_paths(manager) == []
    assert events[0]["temp_created"] is True


def test_cleanup_failure_does_not_mask_primary_exception(manager, monkeypatch):
    session, target, original_bytes = _saved_session(manager)
    primary_error = _failure()
    cleanup_error = InjectedPermissionError(errno.EACCES, "private-cleanup-error-" + _EXCEPTION_TEXT)
    original_unlink = Path.unlink
    events = []

    def fail_temp_unlink(path, *args, **kwargs):
        if path.parent == manager.sessions_dir and path.name.startswith(".session-save-"):
            raise cleanup_error
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    with patch("nanobot.session.manager.os.replace", side_effect=primary_error):
        with observation_context(
            lambda kind, **payload: events.append({"type": kind, **payload}),
            task_id="session-save-cleanup-failure",
        ):
            with pytest.raises(InjectedPermissionError) as caught:
                manager.save(session)

    assert caught.value is primary_error
    assert [event["operation"] for event in events] == ["os_replace", "temp_cleanup"]
    _assert_redacted_failure(events[0], operation="os_replace", extension=".jsonl")
    _assert_redacted_failure(events[1], operation="temp_cleanup", extension=".jsonl.tmp")
    assert target.read_bytes() == original_bytes
    temp_paths = _owned_temp_paths(manager)
    assert len(temp_paths) == 1
    assert _EXCEPTION_TEXT not in " ".join(getattr(primary_error, "__notes__", []))

    # Restore the real unlink before the temporary test workspace is removed.
    monkeypatch.setattr(Path, "unlink", original_unlink)
    temp_paths[0].unlink()


def test_directory_close_failure_does_not_mask_fsync_exception(manager):
    session, _target, _original_bytes = _saved_session(manager)
    fsync_error = OSError(errno.ENOSPC, "private-directory-fsync-error")
    fsync_error.winerror = 1235
    close_error = InjectedPermissionError(errno.EACCES, "private-directory-close-error")
    fsync_calls = 0
    events = []
    original_os_open = os.open

    def open_directory(path, flags, *args, **kwargs):
        if Path(path) == manager.sessions_dir:
            return 83
        return original_os_open(path, flags, *args, **kwargs)

    def fail_directory_fsync(_fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise fsync_error

    with (
        patch("nanobot.session.manager.os.open", side_effect=open_directory),
        patch("nanobot.session.manager.os.fsync", side_effect=fail_directory_fsync),
        patch("nanobot.session.manager.os.close", side_effect=close_error),
        observation_context(
            lambda kind, **payload: events.append({"type": kind, **payload}),
            task_id="session-save-directory-close-failure",
        ),
    ):
        with pytest.raises(OSError) as caught:
            manager.save(session, fsync=True)

    assert caught.value is fsync_error
    assert [event["operation"] for event in events] == [
        "directory_fsync", "directory_close",
    ]
    for event, operation in zip(events, ("directory_fsync", "directory_close")):
        assert event["type"] == "session_save_failure"
        assert event["operation"] == operation
        assert event["path"] == "."
        assert event["committed"] is True
        assert event["temp_created"] is True
        assert event["location"] == (
            f"nanobot.session.manager.SessionManager.save.{operation}"
        )
        assert event["errno"] in {errno.ENOSPC, errno.EACCES}
        assert event["winerror"] in {1235, 1234}
        serialized = json.dumps(event)
        for secret in (_SESSION_KEY, _SESSION_TEXT, "private-directory-"):
            assert secret not in serialized


def test_directory_open_permission_error_propagates_outside_windows(manager, monkeypatch):
    session, target, original_bytes = _saved_session(manager)
    error = _failure()
    original_os_open = os.open
    events = []

    def fail_directory_open(path, flags, *args, **kwargs):
        if Path(path) == manager.sessions_dir:
            raise error
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("nanobot.session.manager.os.open", fail_directory_open)
    monkeypatch.setattr("nanobot.session.manager.sys.platform", "linux")
    with observation_context(
        lambda kind, **payload: events.append({"type": kind, **payload}),
        task_id="session-save-directory-open-failure",
    ):
        with pytest.raises(InjectedPermissionError) as caught:
            manager.save(session, fsync=True)

    assert caught.value is error
    assert events[0]["operation"] == "directory_open"
    assert events[0]["committed"] is True
    assert events[0]["temp_created"] is True
    assert target.read_bytes() != original_bytes
    assert _owned_temp_paths(manager) == []


def test_directory_fsync_permission_error_propagates_and_closes_handle(manager):
    session, target, original_bytes = _saved_session(manager)
    error = _failure()
    fsync_calls = 0
    close_calls: list[int] = []
    events = []
    original_os_open = os.open

    def open_directory(path, flags, *args, **kwargs):
        if Path(path) == manager.sessions_dir:
            return 83
        return original_os_open(path, flags, *args, **kwargs)

    def fail_directory_fsync(_fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise error

    with (
        patch("nanobot.session.manager.os.open", side_effect=open_directory),
        patch("nanobot.session.manager.os.fsync", side_effect=fail_directory_fsync),
        patch("nanobot.session.manager.os.close", side_effect=lambda fd: close_calls.append(fd)),
        observation_context(
            lambda kind, **payload: events.append({"type": kind, **payload}),
            task_id="session-save-directory-fsync-permission-error",
        ),
    ):
        with pytest.raises(InjectedPermissionError) as caught:
            manager.save(session, fsync=True)

    assert caught.value is error
    assert close_calls == [83]
    assert [event["operation"] for event in events] == ["directory_fsync"]
    assert events[0]["committed"] is True
    assert target.read_bytes() != original_bytes
    assert _owned_temp_paths(manager) == []


def test_normal_save_remains_readable_by_a_fresh_manager(manager, tmp_path):
    session = manager.get_or_create("normal:session")
    session.add_message("user", "hello")
    session.add_message("assistant", "saved")

    manager.save(session)

    reloaded = SessionManager(workspace=tmp_path).get_or_create("normal:session")
    assert [message["content"] for message in reloaded.messages] == ["hello", "saved"]

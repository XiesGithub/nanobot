"""Owned instance cleanup preserves scope and records process teardown evidence."""

import ctypes
import json
import multiprocessing as mp
import os
import stat
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from evals import run


def _return_normally():
    return None


def _sleep_forever():
    time.sleep(60)


def _spawn_child_and_sleep(child_pid_path):
    child = mp.get_context("spawn").Process(target=_sleep_forever)
    child.start()
    Path(child_pid_path).write_text(str(child.pid), encoding="ascii")
    _sleep_forever()


def _spawn_child_subprocess_then_exit(child_pid_path):
    child = subprocess.Popen(
        [os.environ.get("PYTHON", os.sys.executable), "-c", "import time; time.sleep(60)"],
        close_fds=True,
    )
    Path(child_pid_path).write_text(str(child.pid), encoding="ascii")
    time.sleep(0.3)


def _windows_attributes(path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    value = get_attributes(str(path))
    if value == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def _windows_acl_sddl(path):
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    convert.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    owner, group, dacl, sacl, descriptor = (ctypes.c_void_p() for _ in range(5))
    security_info = 0x1 | 0x2 | 0x4  # owner, group, DACL
    status = get_security(
        str(path), 1, security_info,
        ctypes.byref(owner), ctypes.byref(group), ctypes.byref(dacl),
        ctypes.byref(sacl), ctypes.byref(descriptor),
    )
    if status:
        raise OSError(status, "GetNamedSecurityInfoW failed", str(path))
    sddl = ctypes.c_wchar_p()
    length = ctypes.c_uint32()
    try:
        if not convert(descriptor, 1, security_info, ctypes.byref(sddl), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sddl.value
    finally:
        if sddl:
            local_free(sddl)
        if descriptor:
            local_free(descriptor)


def _create_windows_file_handle_without_delete_share(path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle


def _create_directory_junction(link, target):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"directory junction creation unavailable: exit {result.returncode}")
    assert link.exists()


def test_cleanup_removes_owned_target_and_preserves_outside(tmp_path):
    root = tmp_path / "instances"
    target = root / "owned"
    outside = tmp_path / "outside"
    root.mkdir()
    target.mkdir()
    outside.mkdir()
    owned_file = target / "owned.txt"
    outside_file = outside / "keep.txt"
    owned_file.write_text("owned", encoding="utf-8")
    outside_file.write_text("outside", encoding="utf-8")

    assert run.cleanup_instance(target, root) is True
    assert not target.exists()
    assert outside_file.read_text(encoding="utf-8") == "outside"


def test_cleanup_rejects_root_external_and_reparse_paths(tmp_path):
    root = tmp_path / "instances"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="unsafe_cleanup_target"):
        run.cleanup_instance(root, root)
    with pytest.raises(ValueError, match="unsafe_cleanup_target"):
        run.cleanup_instance(outside, root)
    assert sentinel.exists()

    link_target = root / "target-link"
    _create_directory_junction(link_target, outside)
    try:
        with pytest.raises(ValueError, match="reparse_point"):
            run.cleanup_instance(link_target, root)
        assert sentinel.exists()
    finally:
        link_target.rmdir()

    link_tree = root / "tree-with-link"
    link_tree.mkdir()
    nested_link = link_tree / "outside"
    _create_directory_junction(nested_link, outside)
    if os.name == "nt":
        os.chmod(link_tree, stat.S_IREAD)
        before = _windows_attributes(link_tree)
        assert before & 0x1
    try:
        with pytest.raises(ValueError, match="reparse_point"):
            run.cleanup_instance(link_tree, root)
        if os.name == "nt":
            assert _windows_attributes(link_tree) == before
        assert sentinel.exists()
    finally:
        nested_link.rmdir()
    assert run.cleanup_instance(link_tree, root)

    root_alias = tmp_path / "instances-alias"
    _create_directory_junction(root_alias, root)
    valid_child = root / "valid-child"
    valid_child.mkdir()
    try:
        with pytest.raises(ValueError, match="reparse_point"):
            run.cleanup_instance(root_alias / "valid-child", root_alias)
        assert valid_child.is_dir()
    finally:
        root_alias.rmdir()
    assert run.cleanup_instance(valid_child, root)


def test_cleanup_directory_readonly_removal_preserves_acl_and_other_attributes(
        tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows FILE_ATTRIBUTE_READONLY control")

    root = tmp_path / "instances"
    target = root / "owned"
    readonly_dir = target / "artifacts"
    root.mkdir()
    readonly_dir.mkdir(parents=True)
    file_path = readonly_dir / "evidence.json"
    file_path.write_text("evidence", encoding="utf-8")
    os.chmod(readonly_dir, stat.S_IREAD)
    attrs_before = _windows_attributes(readonly_dir)
    assert attrs_before & 0x1 and attrs_before & 0x10
    acl_before = _windows_acl_sddl(readonly_dir)
    file_attrs_before = _windows_attributes(file_path)
    file_acl_before = _windows_acl_sddl(file_path)

    original_rmtree = run.shutil.rmtree
    calls = 0

    def observe_then_remove(path):
        nonlocal calls
        calls += 1
        assert Path(path) == target
        assert _windows_attributes(readonly_dir) == attrs_before & ~0x1
        assert _windows_acl_sddl(readonly_dir) == acl_before
        assert _windows_attributes(file_path) == file_attrs_before
        assert _windows_acl_sddl(file_path) == file_acl_before
        return original_rmtree(path)

    monkeypatch.setattr(run.shutil, "rmtree", observe_then_remove)
    assert run.cleanup_instance(target, root) is True
    assert calls == 1


def test_readonly_file_failure_propagates_without_attribute_change(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows read-only file deletion control")

    root = tmp_path / "instances"
    target = root / "owned"
    target.mkdir(parents=True)
    file_path = target / "readonly.txt"
    file_path.write_text("owned", encoding="utf-8")
    os.chmod(file_path, stat.S_IREAD)
    attrs_before = _windows_attributes(file_path)
    assert attrs_before & 0x1
    acl_before = _windows_acl_sddl(file_path)
    original_rmtree = run.shutil.rmtree
    calls = 0

    def count_then_remove(path):
        nonlocal calls
        calls += 1
        return original_rmtree(path)

    monkeypatch.setattr(run.shutil, "rmtree", count_then_remove)
    with pytest.raises(OSError) as caught:
        run.cleanup_instance(target, root)
    assert caught.value.winerror == 5
    details = run._failure_details(caught.value, "remove_instance", target.name,
                                  "test", scope=target)
    assert details["filename"] == "readonly.txt"
    assert details["winerror"] == 5
    assert calls == 1
    assert target.is_dir()
    assert _windows_attributes(file_path) == attrs_before
    assert _windows_acl_sddl(file_path) == acl_before

    os.chmod(file_path, stat.S_IREAD | stat.S_IWRITE)
    assert run.cleanup_instance(target, root)
    assert calls == 2


def test_sharing_handle_failure_is_reported_then_explicit_release_allows_cleanup(
        tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows sharing violation control")

    root = tmp_path / "instances"
    target = root / "owned"
    target.mkdir(parents=True)
    file_path = target / "held.txt"
    file_path.write_text("owned", encoding="utf-8")
    kernel32, handle = _create_windows_file_handle_without_delete_share(file_path)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    original_rmtree = run.shutil.rmtree
    calls = 0

    def count_then_remove(path):
        nonlocal calls
        calls += 1
        return original_rmtree(path)

    monkeypatch.setattr(run.shutil, "rmtree", count_then_remove)
    try:
        with pytest.raises(OSError) as caught:
            run.cleanup_instance(target, root)
        assert caught.value.winerror == 32
        assert caught.value.filename is not None
        details = run._failure_details(caught.value, "remove_instance", target.name,
                                      "test", scope=target)
        assert details["filename"] == "held.txt"
        assert details["winerror"] == 32
        assert calls == 1
        assert target.is_dir()
    finally:
        if not close_handle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    # A single fresh call after the known blocking handle is explicitly released.
    assert run.cleanup_instance(target, root)
    assert calls == 2


def test_failure_details_keeps_scoped_paths_and_redacts_external_paths(tmp_path):
    scope = tmp_path / "instance"
    nested = scope / "artifacts"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside-secret.txt"
    exc = OSError(5, "private diagnostic text", str(nested), 5, str(outside))

    details = run._failure_details(
        exc, "remove_instance", "instance", "test", scope=scope)

    assert details["errno"] == 13
    assert details["winerror"] == 5
    assert details["filename"] == "artifacts"
    assert details["filename2"] == "[redacted]"
    assert "private diagnostic text" not in repr(details)
    assert str(outside) not in repr(details)


def test_stop_process_records_natural_exit_and_preserves_bool_api():
    process = mp.get_context("spawn").Process(target=_return_normally)
    process.start()
    details = {}

    result = run.stop_process(process, diagnostics=details)

    assert result is True
    assert details["pid"] == process.pid
    assert details["stop_method"] == "natural_exit"
    assert details["exitcode"] == 0
    assert details["create_time"] is None or isinstance(details["create_time"], float)
    assert details["remaining"] == []
    assert isinstance(run.stop_process(process), bool)


def test_stop_process_terminates_only_owned_worker_and_child(tmp_path):
    context = mp.get_context("spawn")
    child_pid_path = tmp_path / "owned-child.pid"
    process = context.Process(target=_spawn_child_and_sleep, args=(str(child_pid_path),))
    process.start()
    deadline = time.monotonic() + 10
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_path.exists(), "worker did not report its owned child"
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    child_created = psutil.Process(child_pid).create_time()
    details = {}

    try:
        result = run.stop_process(process, diagnostics=details)
    finally:
        child_pid_path.unlink(missing_ok=True)

    assert result is True
    assert details["pid"] == process.pid
    assert details["create_time"] is not None
    assert details["stop_method"] in {"terminate", "kill"}
    assert details["exitcode"] is not None
    assert any(row["pid"] == child_pid and row["create_time"] == child_created
               for row in details["owned_descendants"])
    assert details["remaining"] == []
    assert details["remaining_unknown"] is False
    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(child_pid).create_time()


def test_stop_process_cleans_captured_child_after_worker_exits_naturally(tmp_path):
    context = mp.get_context("spawn")
    child_pid_path = tmp_path / "natural-parent-child.pid"
    process = context.Process(
        target=_spawn_child_subprocess_then_exit, args=(str(child_pid_path),))
    process.start()
    deadline = time.monotonic() + 10
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid_path.exists(), "worker did not report its owned child"
    child_pid = int(child_pid_path.read_text(encoding="ascii"))
    child_created = psutil.Process(child_pid).create_time()
    details = {}

    try:
        result = run.stop_process(process, diagnostics=details, grace_timeout=1)
    finally:
        child_pid_path.unlink(missing_ok=True)
        if process.is_alive():
            process.terminate()
            process.join(2)
        try:
            psutil.Process(child_pid).terminate()
            psutil.wait_procs([psutil.Process(child_pid)], timeout=2)
        except psutil.NoSuchProcess:
            pass

    assert result is True
    assert process.exitcode == 0
    assert details["stop_method"] == "natural_exit"
    child_row = next(row for row in details["owned_descendants"]
                     if row["pid"] == child_pid and row["create_time"] == child_created)
    assert child_row["stop_method"] in {"terminate", "kill"}
    assert child_row["remaining"] is False
    assert details["remaining"] == []
    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(child_pid).create_time()


def test_run_trial_preserves_instance_when_worker_stop_is_not_confirmed(tmp_path, monkeypatch):
    class ProcessProxy:
        def __init__(self, process, fail_terminate=False):
            self._process = process
            self._fail_terminate = fail_terminate
            self.real_terminate = process.terminate

        def __getattr__(self, name):
            return getattr(self._process, name)

        def terminate(self):
            if self._fail_terminate:
                raise PermissionError(5, "controlled terminate denial")
            return self._process.terminate()

    class ContextProxy:
        def __init__(self):
            self._context = mp.get_context("spawn")
            self.processes = []
            self.Pipe = self._context.Pipe
            self.Process = self._create_process

        def _create_process(self, *args, **kwargs):
            process = self._context.Process(*args, **kwargs)
            proxy = ProcessProxy(process, fail_terminate=not self.processes)
            self.processes.append(proxy)
            return proxy

    case = next(case for case in run.load_cases() if case["case_id"] == "V001")
    plan = {"trial_id": "cleanup-stop-denied", "purpose": "diagnostic", "fault": "timeout",
            "profile": "D-cleanup-control", "product_timeout_s": 0.15,
            "setup_timeout_s": 20}
    run_dir = tmp_path / "run"
    isolation_root = tmp_path / "instances"
    instance = isolation_root / "controlled-run"
    run_dir.mkdir()
    isolation_root.mkdir()
    context = ContextProxy()
    cleanup_calls = []
    original_cleanup = run.cleanup_instance

    def observe_cleanup(instance, root):
        cleanup_calls.append(Path(instance))
        return original_cleanup(instance, root)

    monkeypatch.setattr(run, "cleanup_instance", observe_cleanup)
    try:
        result = run._run_trial(case, plan, run_dir, isolation_root, "D", context, instance)
        worker = context.processes[0]
        assert worker.is_alive()
        assert result["cleanup"]["ok"] is False
        assert result["cleanup"]["instance_removed"] is False
        assert cleanup_calls == []
        assert instance.is_dir()

        trial_dir = run_dir / "trials" / plan["trial_id"]
        evidence = json.loads((trial_dir / "artifacts/evidence.json").read_text(encoding="utf-8"))
        trace = json.loads((trial_dir / "artifacts/trace-analysis.json").read_text(encoding="utf-8"))
        assert evidence["cleanup"]["process_stopped"] is False
        assert evidence["cleanup"]["instance_removed"] is False
        assert "cleanup_blocked:process_tree_not_stopped" in evidence["collector_errors"]
        assert evidence["worker_process"]["stop_errors"][0]["operation"] == "terminate_worker"
        assert trace["valid"] is False
        assert "collector_error" in trace["errors"]
    finally:
        if context.processes:
            worker = context.processes[0]
            if worker.is_alive():
                worker.real_terminate()
                worker.join(2)
        for instance in isolation_root.iterdir():
            if instance.is_dir():
                original_cleanup(instance, isolation_root)

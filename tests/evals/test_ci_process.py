"""Controlled subprocess lifecycle checks; no external services or credentials."""

import subprocess
import time
from pathlib import Path

import psutil
import pytest

from evals import paired


def _subject(tmp_path, source):
    package = tmp_path / "subject" / "evals"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "paired_worker.py").write_text(source, encoding="utf-8")
    return package.parent


def test_d_subprocess_does_not_read_credential_file(tmp_path, monkeypatch):
    subject = _subject(tmp_path, "print('diagnostic output', flush=True)\n")
    credential = tmp_path / "api.txt"
    credential.write_text("synthetic-only", encoding="utf-8")
    original_read = Path.read_text

    def reject_credential_read(path, *args, **kwargs):
        if path == credential:
            pytest.fail("D read a credential file")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(paired, "ROOT", tmp_path)
    monkeypatch.setattr(Path, "read_text", reject_credential_read)
    result = paired.run_process(subject, [], tmp_path / "worker.log", 10)
    assert result == {"returncode": 0, "watchdog_timeout": False}


def test_explicit_synthetic_credential_is_redacted(tmp_path):
    subject = _subject(tmp_path, "print('sk-synthetic-ci-sentinel', flush=True)\n")
    credential = tmp_path / "synthetic.txt"
    credential.write_text("sk-synthetic-ci-sentinel\n", encoding="utf-8")
    output = tmp_path / "worker.log"
    paired.run_process(subject, [], output, 10, credential_file=credential)
    assert b"sk-synthetic-ci-sentinel" not in output.read_bytes()
    assert b"[credential_redacted]" in output.read_bytes()


@pytest.mark.parametrize("interrupt", [False, True], ids=["timeout", "interrupt"])
def test_timeout_and_interrupt_reap_descendants_and_preserve_logs(tmp_path, monkeypatch, interrupt):
    marker = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); "
        "print('controlled-child-ready', flush=True); time.sleep(60)"
    )
    subject = _subject(tmp_path, (
        "import subprocess,sys\n"
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}])\n"
        "print('controlled-parent-ready', flush=True)\n"
        "child.wait()\n"
    ))
    original_popen = subprocess.Popen
    processes = []
    child_pids = []

    def launch(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        original_communicate = process.communicate
        first = True

        def controlled_communicate(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert marker.exists(), "child gate was not reached"
                child_pids.append(int(marker.read_text()))
                if interrupt:
                    raise KeyboardInterrupt()
                kwargs["timeout"] = 0.05
            return original_communicate(*args, **kwargs)

        process.communicate = controlled_communicate
        return process

    monkeypatch.setattr(paired.subprocess, "Popen", launch)
    output = tmp_path / "worker.log"
    try:
        if interrupt:
            with pytest.raises(KeyboardInterrupt):
                paired.run_process(subject, [], output, 10)
        else:
            result = paired.run_process(subject, [], output, 10)
            assert result["watchdog_timeout"] is True and result["returncode"] != 0
        assert processes[0].poll() is not None
        for pid in child_pids:
            assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        assert b"controlled-parent-ready" in output.read_bytes()
    finally:
        for pid in [*child_pids, *(process.pid for process in processes)]:
            if psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass

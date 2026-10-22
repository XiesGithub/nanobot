import pytest

from evals import isolated_os_preflight as preflight


def test_installed_cli_cannot_establish_isolation(monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None if name == "wsl" else name)
    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch workload"))
    result = preflight.inspect()
    assert result["path_availability"]["docker"] is True
    assert result["ready"] is False
    assert all(value is None for value in result["required_gates"].values())
    assert result["agent_runs"] == result["model_requests"] == 0
    assert result["dangerous_host_execution"] is False


def test_wrong_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    with pytest.raises(ValueError, match="Conda nanobot"):
        preflight.inspect()

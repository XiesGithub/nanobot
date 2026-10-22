"""Read-only availability check; never substitutes host execution for OS isolation."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from evals.common import ROOT, digest, now, write


def inspect():
    if os.environ.get("CONDA_DEFAULT_ENV") != "nanobot":
        raise ValueError("Use Conda nanobot")
    names = ("docker", "podman", "VBoxManage", "qemu-system-x86_64", "wsl")
    installed = {
        "docker_desktop": r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        "virtualbox": r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
        "podman": r"C:\Program Files\RedHat\Podman\podman.exe",
    }
    wsl = {"queried": False, "exitcode": None, "exception_type": None}
    if shutil.which("wsl"):
        try:
            result = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, timeout=15)
            wsl.update(queried=True, exitcode=result.returncode,
                       output_present=bool(result.stdout), output_sha256=digest(result.stdout),
                       error_sha256=digest(result.stderr))
        except (OSError, subprocess.TimeoutExpired) as error:
            wsl["exception_type"] = type(error).__name__
    sources = {name: digest((ROOT / name).read_bytes()) for name in
               (".agent/security.md", "docs/validation-cases/02-tools.md",
                "docs/validation-cases/11-security.md", "evals/isolated_os_preflight.py")}
    return {
        "schema": "isolated-os-preflight/1.0", "checked_at": now(), "conda_env": "nanobot",
        "sources": sources, "path_availability": {name: bool(shutil.which(name)) for name in names},
        "known_installations": {name: Path(path).is_file() for name, path in installed.items()},
        "wsl_listing": wsl,
        "required_gates": {name: None for name in
                           ("disposable_instance", "controlled_egress", "synthetic_only_mounts",
                            "process_reaping", "credential_isolation", "in_guest_conda_nanobot")},
        "ready": False, "case_ids": ["V022", "V101", "V104", "V105", "V109"],
        "task_status": "environment_unverified", "agent_runs": 0, "model_requests": 0,
        "dangerous_host_execution": False, "private_network_requests": 0,
        "scope": "Availability probes only; no claim that every possible VM facility is absent.",
        "reason": "No disposable instance has been established and required isolation gates have no evidence.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("output_outside_workspace")
    write(output, inspect())

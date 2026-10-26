"""One-shot native Linux engineering evidence, never an evaluation/model runner."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/linux-targeted"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, data):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def command(label, args, env=None):
    with (OUT / f"{label}.log").open("xb") as stream:
        result = subprocess.run(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                                env=env, timeout=900)
    write(f"{label}-receipt.json", {"argv": args, "exit": result.returncode})
    return result.returncode == 0


def source_snapshot():
    expected = json.loads((ROOT / "evals/linux-targeted-source.json").read_text())
    actual = {name: sha(ROOT / name) for name in expected}
    assert actual == expected, "candidate source bytes changed"
    return actual


def pdf_probe():
    import pymupdf
    from pypdf import PdfReader

    from evals.batch_basic import _build_v019_pdf, cases, execute

    workspace = OUT / "pdf"
    workspace.mkdir()
    for name, content in cases()["V019"]["fixtures"].items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    provenance = _build_v019_pdf(workspace / "first.pdf")
    _build_v019_pdf(workspace / "second.pdf")
    assert (workspace / "first.pdf").read_bytes() == (workspace / "second.pdf").read_bytes()
    expected = ["合同编号 HT-017；预付款 100 元", "尾款 230 元；到期日 2026-10-15"]
    reader = PdfReader(workspace / "first.pdf")
    assert [page.extract_text().strip() for page in reader.pages] == expected
    with pymupdf.open(workspace / "first.pdf") as document:
        assert [page.get_text().strip() for page in document] == expected
        for index, page in enumerate(document):
            page.get_pixmap().save(workspace / f"page-{index + 1}.png")
    observed = asyncio.run(execute("V019", workspace))
    assert observed["fixture_unchanged"] and observed["images"] == []
    write("pdf.json", {"font": provenance, "observed": observed,
          "same_runtime_deterministic": True, "pdf_sha256": sha(workspace / "first.pdf"),
          "visual_review": "pending independent artifact review"})


def encoder_probe():
    from evals.mail_encoder_fixture import MAX_FILE_BYTES, prepare
    from evals.mail_task_cases import encoder_pin

    destination = ROOT / encoder_pin()["path"]
    assert not destination.exists(), "cold-download destination must not exist"
    records = []
    write("encoder-initial.json", {"destination": str(destination), "exists": False,
                                   "cache_reuse": False})

    def fetch(url):
        row = {"url": url}
        records.append(row)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={
                    "User-Agent": "nanobot-targeted-linux/1"}), timeout=60) as response:
                final = urlsplit(response.url)
                row.update(status=response.status, final_url=urlunsplit(
                    (final.scheme, final.netloc, final.path, "", "")))
                data = response.read(MAX_FILE_BYTES + 1)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            assert len(data) <= MAX_FILE_BYTES
            return data
        except Exception as error:
            row["error_type"] = type(error).__name__
            raise

    result = None
    try:
        result = prepare(destination, download_missing=True, fetch=fetch)
        assert len(records) == 10 and result["files_verified"] == 11
        prepare(destination)
    finally:
        write("encoder-download.json", {"requests": records, "result": result,
              "files": {str(p.relative_to(destination)): sha(p)
                        for p in destination.rglob("*") if p.is_file()}})


def browser_identity():
    import playwright
    from playwright.sync_api import sync_playwright

    assert importlib.metadata.version("playwright") == "1.55.0"
    metadata = Path(playwright.__file__).parent / "driver/package/browsers.json"
    pinned = json.loads(metadata.read_text())
    with sync_playwright() as engine:
        executable = Path(engine.chromium.executable_path)
        browser = engine.chromium.launch(headless=True, executable_path=str(executable))
        try:
            version = browser.version
        finally:
            browser.close()
    chromium = next(item for item in pinned["browsers"] if item["name"] == "chromium")
    assert version == chromium["browserVersion"], "downloaded Chromium differs from package pin"
    write("browser.json", {"playwright": "1.55.0", "metadata_sha256": sha(metadata),
          "browsers": pinned, "executable": str(executable),
          "executable_sha256": sha(executable), "version": version})
    os.environ["NANOBOT_EVAL_CHROME"] = str(executable)


def main():
    OUT.mkdir(parents=True, exist_ok=False)
    assert platform.system() == "Linux" and sys.platform == "linux", "native Linux required"
    assert os.environ.get("CONDA_DEFAULT_ENV") == "nanobot", "Conda nanobot required"
    assert "microsoft" not in platform.release().lower(), "this proposal requires hosted native Linux"
    assert os.environ.get("GITHUB_REF") == "refs/heads/codex/linux-targeted-20261026-r1"
    assert os.environ.get("GITHUB_EVENT_NAME") == "push"
    assert os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "reruns are outside this proposal"
    before = source_snapshot()
    write("environment.json", {"platform": platform.platform(), "uname": list(platform.uname()),
          "python": sys.version, "executable": sys.executable, "root": str(ROOT),
          "os_release": Path("/etc/os-release").read_text(),
          "github": {k: os.environ.get(k) for k in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME",
                       "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")},
          "packages": sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions()),
          "model_requests": 0, "qualification_transfer": False})
    outcomes = {}

    def stage(label, action):
        try:
            value = action()
            outcomes[label] = value is not False
        except Exception as error:
            import traceback
            (OUT / f"{label}-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            outcomes[label] = False
            write(f"{label}-error.json", {"type": type(error).__name__, "message": str(error)})

    stage("git", lambda: command("git", ["git", "rev-parse", "HEAD", "HEAD^{tree}"]))
    node = subprocess.check_output(["node", "--version"], text=True).strip()
    write("node.json", {"actual": node, "expected": "v22.20.0",
                        "lock_sha256": sha(ROOT / "webui/package-lock.json")})
    assert node == "v22.20.0"
    stage("npm", lambda: command("npm", ["npm", "ci", "--prefix", "webui", "--no-audit", "--no-fund"]))
    stage("chromium-install", lambda: command("chromium-install", [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]))
    if outcomes["chromium-install"]:
        stage("browser-identity", browser_identity)
    else:
        outcomes["browser-identity"] = False
    stage("pdf", pdf_probe)
    stage("encoder", encoder_probe)
    groups = {
        "v019": ["tests/evals/test_v019_fixture.py", "tests/evals/test_batch_basic.py::test_supported_module_contracts[V019]"],
        "history": ["tests/evals/test_privacy_task_audit.py", "tests/evals/test_semantic_revision.py",
                    "tests/evals/test_save_failure_harness.py::test_old_invalid_is_preserved",
                    "tests/evals/test_save_ws_recovery.py", "tests/evals/test_trace.py::test_all_phase2_evidence_keeps_its_original_verdict",
                    "tests/evals/test_history_fixtures.py"],
        "encoder-controls": ["tests/evals/test_mail_encoder_fixture.py"],
        "v073": ["tests/evals/test_browser_task_timing.py"],
        "v073-diagnostics": ["tests/evals/test_browser_task_diagnostics.py"],
        "ui-dependencies": [
            "tests/evals/test_channel_task_stream.py::test_channel_task_stream_adapter_returns_traceable_evidence",
        ],
    }
    os.environ["NANOBOT_V073_DIAGNOSTICS_DIR"] = str(OUT / "v073")
    for label, targets in groups.items():
        if label == "v073" and not outcomes["browser-identity"]:
            outcomes[label] = False
            write("v073-not-run.json", {"reason": "fixed Chromium identity not qualified",
                                       "fallback_browser_allowed": False})
            continue
        stage(label, lambda label=label, targets=targets: command(label, [sys.executable,
              "-m", "pytest", "-q", *targets, f"--junitxml={OUT / (label + '.xml')}"]))
    stage("source-unchanged", lambda: source_snapshot() == before)
    write("result.json", {"stages": outcomes, "engineering_passed": all(outcomes.values()),
                           "V073_root_cause": "requires branch evidence review", "R_runs": 0})
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

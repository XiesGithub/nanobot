"""The refreshed candidate records workflow identity and independent D evidence."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_refresh_push_keeps_broad_scope_and_does_not_enable_r():
    source = (ROOT / ".github/workflows/evaluation.yml").read_text(encoding="utf-8")
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["branches"] == ["main", "nightly", "codex/ci-evidence-20261026-r1"]
    engineering = workflow["jobs"]["engineering"]
    run = next(step["run"] for step in engineering["steps"]
               if step.get("name") == "Lint and run the engineering suite")
    assert "python -m pytest tests/ --ignore=tests/evals/test_holdout.py" in run
    assert "python -m evals.v065_attachment_d --mode D --output reports/engineering/v065" in run
    assert "$v065Exit = $LASTEXITCODE" in run
    assert "if ($v065Exit -ne 0 -or" in run
    assert engineering["env"]["NANOBOT_V073_DIAGNOSTICS_DIR"].endswith("/reports/engineering/v073")
    assert "workflow_sha = $env:GITHUB_WORKFLOW_SHA" in source
    assert "workflow_ref = $env:GITHUB_WORKFLOW_REF" in source
    real = workflow["jobs"]["real-model-evaluation"]["if"]
    assert "workflow_dispatch" in real and "inputs.mode == 'R'" in real
    assert "github.ref == 'refs/heads/main'" in real and "github.ref == 'refs/heads/nightly'" in real

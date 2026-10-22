"""Replay preserved real-browser failures; never turn negative tasks into passes."""
import pytest

from evals.browser_task_evidence import score
from evals.common import ROOT, read


@pytest.mark.parametrize("name", ["B-scripted-V073-duplicate_delivery",
    "B-scripted-V078-duplicate_delivery", "B-scripted-V078-refresh_resubmit"])
def test_real_browser_negative_effect_is_detected_even_when_not_extra_bubbles(name):
    path = ROOT / "docs/agent-evaluation-browser-fulltask-20261014/B-final-20261015" / name
    if not (path / "gateway-evidence.json").is_file():
        pytest.skip("preserved local browser evidence not installed")
    verdict = score(read(path / "manifest.json"), read(path / "browser-evidence.json"),
                    read(path / "gateway-evidence.json"))
    assert verdict["status"] == "fail"
    assert verdict["formal_qualification"] is False
    assert verdict["checks"]["negative_control_detected"] is True

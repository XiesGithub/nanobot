"""Daily gates and tier plans, without requests or exposing release gold."""

from copy import deepcopy

import pytest

from evals.daily import audit, budgets, build_plan, gate


def test_public_tiers_do_not_load_holdout(monkeypatch):
    import evals.holdout as holdout

    monkeypatch.setattr(holdout, "create_release_set", lambda *_: pytest.fail("gold access"))
    for tier, count in (("development", 5), ("smoke", 24), ("nightly", 180)):
        bundle = build_plan(tier, "D")
        assert len(bundle["plan"]) == count
        assert bundle["gold"] is None
        assert all(p["case_id"].startswith("V") for p in bundle["plan"])
    assert "V022" in build_plan("nightly", "D")["active"]


def test_release_plan_is_400_and_uses_fresh_set_factory(monkeypatch):
    import evals.holdout as holdout

    template = deepcopy(build_plan("development", "D")["active"]["V001"]["contract"])
    def fake(nonce, public):
        assert len(public) == 120 and nonce == "round"
        return {"cases": [{**template, "case_id": f"R{i:03}"} for i in range(1, 21)],
                "data_sha256": "fresh"}
    monkeypatch.setattr(holdout, "create_release_set", fake)
    bundle = build_plan("release", "D", nonce="round")
    assert len(bundle["plan"]) == 400
    assert len(bundle["active"]) == 80
    assert len([p for p in bundle["plan"] if p["case_id"].startswith("R")]) == 100


@pytest.mark.parametrize("args", [(None, None, None, False), (8, 32000, 180, False),
                                 (0, 32000, 180, True), (8, None, 180, True),
                                 (8, 32000, -1, True), (True, 32000, 180, True)])
def test_real_budget_is_required_and_explicit(args):
    with pytest.raises(ValueError):
        budgets("R", *args)


def test_modes_have_distinct_budgets_and_unknown_prices():
    assert budgets("D")["max_attempts"] == 0
    assert budgets("R", 8, 32000, 180, True)["prices"] is None
    with pytest.raises(ValueError):
        budgets("D", attempts=1)


def specimen(mode="D"):
    bundle = build_plan("development", mode, selected=["V001"])
    manifest = {**bundle, "mode": mode}
    row = {**bundle["plan"][0], "status": "pass", "evidence_valid": True,
           "execution_complete": True, "scoring_complete": True,
           "observation_complete": True, "safety": {"actual": 0},
           "product_cost": {"closed": True, "unknown_usage_attempts": 0},
           "grading_cost": {"closed": True}}
    return manifest, row


def test_diagnostic_pass_is_never_quality_or_optimization_pass():
    manifest, row = specimen()
    result = gate(manifest, [row])
    assert result["state"] == "passed_diagnostic"
    assert result["quality_passed"] is False
    assert result["optimization_effective"] is False


@pytest.mark.parametrize("change,state", [({"status": "product_fail"}, "regression"),
                                         ({"status": "invalid"}, "invalid"),
                                         ({"evidence_valid": False}, "invalid"),
                                         ({"observation_complete": False}, "insufficient_evidence"),
                                         ({"status": "undetermined"}, "insufficient_evidence"),
                                         ({"mode": "R"}, "invalid")])
def test_failed_invalid_or_insufficient_evidence_never_pass(change, state):
    manifest, row = specimen()
    result = gate(manifest, [{**row, **change}])
    assert result["state"] == state
    assert result["quality_passed"] is False


def test_missing_duplicate_and_timeout_remain_failures():
    manifest, row = specimen()
    assert gate(manifest, [])["state"] == "insufficient_evidence"
    assert gate(manifest, [row, row])["state"] == "invalid"
    assert gate(manifest, [row], "wall_budget_exhausted")["state"] == "insufficient_evidence"


def test_unknown_real_cost_and_usage_do_not_pass():
    manifest, row = specimen("R")
    row["product_cost"] = {"closed": False, "unknown_usage_attempts": 1}
    result = gate(manifest, [row])
    assert result["state"] == "insufficient_evidence"
    assert "unknown_model_usage" in result["reasons"]
    assert "unknown_product_or_grading_fee" in result["reasons"]


def test_closed_trace_does_not_mean_known_model_bill():
    manifest, row = specimen("R")
    row["product_cost"].update(amount=None)
    row["grading_cost"].update(amount=0)
    result = gate(manifest, [row])
    assert result["state"] == "insufficient_evidence"
    assert "unknown_product_or_grading_fee" in result["reasons"]


@pytest.mark.parametrize("tier,cases", [("smoke", ["V001"]), ("development", ["V001", "V001"]),
                                      ("development", ["V999"])])
def test_targeted_run_cannot_masquerade_as_full_tier(tier, cases):
    with pytest.raises(ValueError):
        build_plan(tier, "D", cases)


def test_missing_report_evidence_audit_fails_without_reruns(tmp_path):
    result = audit(tmp_path)
    assert not result["ok"] and not result["quality_passed"]
    assert result["agent_reruns"] == result["model_requests"] == 0

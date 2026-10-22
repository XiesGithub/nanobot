"""Even rehashed forged scores cannot replace the program evidence."""

import zipfile

from evals.batch_audit import audit
from evals.batches import score
from evals.common import ROOT, canonical, digest, write
from evals.trace import analyze_trace
from evals.worker import Emitter
from nanobot.utils.observability import observation_context, span


def setup_run(output):
    case = {"scope": "module_contract", "supported": True, "limitations": [],
            "fixture_hashes": {}, "assertions": [{"key": "answer", "op": "equal",
                                                   "expected": 330}]}
    source = b"# frozen synthetic audit source\n"
    write(output / "manifest.json", {"cases": {"V001": case}, "plan": ["V001"],
                                     "milestones": [], "sources": {"sample.py": digest(source)}})
    with zipfile.ZipFile(output / "source-snapshot.zip", "x") as archive:
        archive.writestr("sample.py", source)
    directory = output / "trials" / "synthetic"
    directory.mkdir(parents=True)
    events = []

    class Connection:
        def send(self, message):
            events.append(message["event"])

    emitter = Emitter(Connection(), "run", "trial")
    with observation_context(emitter.event, "trial"):
        with span("task"):
            pass
    (directory / "events.jsonl").write_bytes(b"\n".join(canonical(e) for e in events) + b"\n")
    evidence = {"case_id": "V001", "trial_id": "trial", "status": "finished",
                "observed": {"answer": 329}, "collector_errors": [], "before": {}, "after": {},
                "trace": analyze_trace(events)}
    verdict = score(case, evidence)
    write(directory / "contract.json", case)
    write(directory / "evidence.json", evidence)
    write(directory / "verdict.json", verdict)
    write(directory / "cleanup.json", {"owned_instance": str(
        ROOT / "tmp" / "phase6-instances" / "trial"), "removed": True})
    row = {"case_id": "V001", "trial_id": "trial", "scope": "module_contract",
           "status": "finished", "verdict": "product_fail", "scoring_complete": True,
           "observation_complete": True, "model_attempts": 0, "cost": 0,
           "artifacts": "trials/synthetic"}
    (output / "records.jsonl").write_bytes(canonical(row) + b"\n")
    rehash(directory)
    return directory, verdict


def rehash(directory):
    index = {p.relative_to(directory).as_posix(): digest(p.read_bytes())
             for p in directory.rglob("*") if p.is_file() and p.name != "final-artifact-index.json"}
    path = directory / "final-artifact-index.json"
    if path.exists():
        path.write_bytes(canonical(index))
    else:
        write(path, index)


def test_replay_and_rehashed_forgery(tmp_path):
    directory, verdict = setup_run(tmp_path)
    assert audit(tmp_path)["ok"]
    verdict["verdict"] = "pass"
    for judgment in verdict["requirements"]:
        judgment["passed"] = True
    (directory / "verdict.json").write_bytes(canonical(verdict))
    rehash(directory)
    result = audit(tmp_path)
    assert not result["ok"]
    assert "score_replay:V001" in result["errors"]


def test_missing_and_extra_artifact_rejected(tmp_path):
    directory, _ = setup_run(tmp_path)
    (directory / "extra.txt").write_text("unindexed", encoding="utf-8")
    assert "artifact_inventory:V001" in audit(tmp_path)["errors"]


def test_rehashed_cleanup_for_another_trial_is_rejected(tmp_path):
    directory, _ = setup_run(tmp_path)
    (directory / "cleanup.json").write_bytes(canonical({"removed": True, "owned_instance": str(
        ROOT / "tmp" / "phase6-instances" / "other-trial")}))
    rehash(directory)
    assert "cleanup_identity:V001" in audit(tmp_path)["errors"]

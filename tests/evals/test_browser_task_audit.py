import json
import zipfile

import pytest

from evals import browser_task_audit
from evals.browser_task_audit import DomTree, Paragraphs
from evals.common import digest


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("tamper", [None, "input", "missing_input", "extra_input", "grader"])
def test_revision_envelope_binds_all_originals_and_current_graders(tmp_path, tamper):
    names = ("manifest.json", "browser-evidence.json", "gateway-evidence.json",
             "source-snapshot.zip", "verdict.json", "frozen-contract.json")
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    inputs = {name: digest((tmp_path / name).read_bytes()) for name in names}
    graders = {name: digest((browser_task_audit.ROOT / name).read_bytes()) for name in
               ("evals/browser_task_evidence.py", "evals/browser_task_audit.py")}
    envelope = {"inputs": inputs, "grading_sources": graders, "verdict": {"status": "pass"}}
    if tamper == "input":
        envelope["inputs"]["verdict.json"] = "0" * 64
    elif tamper == "missing_input":
        del envelope["inputs"]["frozen-contract.json"]
    elif tamper == "extra_input":
        envelope["inputs"]["../other.json"] = "0" * 64
    elif tamper == "grader":
        envelope["grading_sources"]["evals/browser_task_evidence.py"] = "0" * 64
    revision = tmp_path / "revision.json"
    _write(revision, envelope)
    errors = []
    assert browser_task_audit._revision_verdict(tmp_path, revision, errors) == {"status": "pass"}
    assert bool(errors) is (tamper is not None)


@pytest.mark.parametrize("tamper", [None, "hash", "audit_not_ok", "D_sources", "source_changed"])
def test_new_frozen_d_prerequisites_do_not_require_legacy_scripted_results(tmp_path, tamper):
    output = tmp_path / "B-real-V074"
    output.mkdir()
    prerequisite = tmp_path / "D-independent-audit.json"
    _write(prerequisite, {"ok": tamper != "audit_not_ok"})
    target = {"sources": {"product.py": "frozen-product-hash"}}
    _write(tmp_path / "frozen-D-plan.json", {"sources": {} if tamper == "D_sources" else target["sources"]})
    _write(tmp_path / "D-B-results.json", {"source_unchanged": tamper != "source_changed"})
    frozen = {"D_audit_sha256": "0" * 64 if tamper == "hash" else digest(prerequisite.read_bytes())}
    errors = []
    browser_task_audit._real_prerequisites(output, frozen, target, errors)
    assert bool(errors) is (tamper is not None)
    assert not (tmp_path / "scripted-results.json").exists()


@pytest.mark.parametrize("tamper", [
    None, "missing_scripted_hash", "missing_audit_hash", "scripted_hash",
    "scripted_not_ok", "audit_hash", "audit_not_ok", "plan_sources",
    "audit_sources", "conflicting_legacy_alias", "missing_schema",
])
def test_v073_d_hash_fields_are_bound_and_required(tmp_path, tamper):
    output = tmp_path / "B-real-V073-01"
    output.mkdir()
    sources = {"product.py": "frozen-product-hash"}
    scripted = output.parent / "scripted-results.json"
    audit = output.parent / "D-independent-audit.json"
    plan = output.parent / "frozen-D-plan.json"
    _write(scripted, {"ok": tamper != "scripted_not_ok"})
    _write(audit, {"ok": tamper != "audit_not_ok",
                   "source_map": {} if tamper == "audit_sources" else sources})
    _write(plan, {"sources": {} if tamper == "plan_sources" else sources})
    frozen = {
        "D_scripted_results_sha256": digest(scripted.read_bytes()),
        "D_independent_audit_sha256": digest(audit.read_bytes()),
        "runs": [{"directory": output.name, "case_id": "V073", "sources": sources}],
    }
    if tamper == "missing_scripted_hash":
        del frozen["D_scripted_results_sha256"]
    elif tamper == "missing_audit_hash":
        del frozen["D_independent_audit_sha256"]
    elif tamper == "scripted_hash":
        frozen["D_scripted_results_sha256"] = "0" * 64
    elif tamper == "audit_hash":
        frozen["D_independent_audit_sha256"] = "0" * 64
    elif tamper == "conflicting_legacy_alias":
        frozen["scripted_results_sha256"] = "0" * 64
    elif tamper == "missing_schema":
        del frozen["D_scripted_results_sha256"]
        del frozen["D_independent_audit_sha256"]

    errors = []
    browser_task_audit._real_prerequisites(output, frozen, frozen["runs"][0], errors)
    assert bool(errors) is (tamper is not None)


def test_real_frozen_target_uses_unique_directory_for_repeated_case(tmp_path):
    runs = [
        {"directory": "B-real-V073-01", "case_id": "V073", "theme": None},
        {"directory": "B-real-V073-02", "case_id": "V073", "theme": None},
        {"directory": "B-real-V073-03", "case_id": "V073", "theme": None},
    ]
    frozen = {"runs": runs}
    errors = []
    assert browser_task_audit._real_target(frozen, tmp_path / "B-real-V073-02", errors) is runs[1]
    assert errors == []
    assert browser_task_audit._real_target(frozen, tmp_path / "B-real-V073-04", errors) is None
    assert errors == ["real_frozen_target"]


def test_duplicate_real_frozen_directory_is_rejected(tmp_path):
    row = {"directory": "B-real-V073-01", "case_id": "V073", "theme": None}
    errors = []
    assert browser_task_audit._real_target({"runs": [row, dict(row)]},
                                           tmp_path / "B-real-V073-01", errors) is None
    assert errors == ["real_frozen_target"]


def test_legacy_scripted_prerequisite_hash_check_remains_strict(tmp_path):
    prerequisite = tmp_path / "scripted-results.json"
    _write(prerequisite, {"ok": True})
    errors = []
    frozen = {"scripted_results_sha256": digest(prerequisite.read_bytes())}
    browser_task_audit._real_prerequisites(tmp_path / "B-real", frozen, {}, errors)
    assert errors == []
    _write(prerequisite, {"ok": False})
    browser_task_audit._real_prerequisites(tmp_path / "B-real", frozen, {}, errors)
    assert errors == ["scripted_prerequisites"]


def test_legacy_prerequisite_modes_cannot_be_mixed(tmp_path):
    errors = []
    browser_task_audit._real_prerequisites(
        tmp_path / "B-real", {"D_audit_sha256": "a" * 64,
                              "scripted_results_sha256": "b" * 64}, {}, errors)
    assert errors == ["D_prerequisite_schema_conflict"]


@pytest.mark.parametrize("tamper", [None, "audit_hash", "D_plan_hash"])
def test_historical_scripted_and_independent_audit_schema_remains_bound(tmp_path, monkeypatch, tamper):
    base = tmp_path / "verified"
    run_root = base / "real"
    output = run_root / "B-real-V073"
    output.mkdir(parents=True)
    sources = {"product.py": "frozen-product-hash"}
    scripted = run_root / "scripted-results.json"
    audit = base / "D-independent-audit.json"
    plan = base / "frozen-D-plan.json"
    _write(scripted, {"ok": True})
    _write(audit, {"ok": True, "source_map": sources})
    _write(plan, {"sources": sources})
    monkeypatch.setattr(browser_task_audit, "ROOT", tmp_path)
    frozen = {
        "scripted_results_sha256": digest(scripted.read_bytes()),
        "D_independent_audit_sha256": "0" * 64 if tamper == "audit_hash" else digest(audit.read_bytes()),
        "D_source_plan_ref": {"path": "verified/frozen-D-plan.json",
                              "sha256": "0" * 64 if tamper == "D_plan_hash" else digest(plan.read_bytes())},
    }
    target = {"sources": sources}
    errors = []
    browser_task_audit._real_prerequisites(output, frozen, target, errors)
    assert bool(errors) is (tamper is not None)


@pytest.mark.parametrize("target_theme,fixture_theme,expected_error", [
    (None, None, False), ("dark", "dark", False), ("light", "dark", True),
    (None, "dark", True),
])
def test_real_frozen_theme_is_bound_to_manifest_fixture(
    target_theme, fixture_theme, expected_error
):
    sources = {"product.py": "frozen-product-hash"}
    target = {"sources": sources, "case_id": "V080", "theme": target_theme}
    manifest = {"sources": sources, "case_id": "V080", "fixture": {"theme": fixture_theme}}
    errors = []
    browser_task_audit._check_real_binding(target, manifest, errors)
    assert ("real_frozen_theme" in errors) is expected_error


def test_revision_audit_preserves_original_zip_but_allows_explicit_grader_upgrade(tmp_path, monkeypatch):
    repo, output = tmp_path / "repo", tmp_path / "archive"
    (repo / "evals").mkdir(parents=True)
    output.mkdir()
    for name in ("browser_task_evidence.py", "browser_task_audit.py"):
        (repo / "evals" / name).write_bytes(b"upgraded grader")
    (repo / "product.py").write_bytes(b"different current checkout")
    monkeypatch.setattr(browser_task_audit, "ROOT", repo)
    verdict = {"status": "pass", "formal_qualification": False}
    pin = {"browser": {"version": "fixed", "executable_sha256": "browser-hash"},
           "font_fingerprint": {"local_font_files": []}}
    _write(output / "manifest.json", {"sources": {"product.py": digest(b"original product")},
           "browser_environment": pin, "budget": {"requests": 0, "known_tokens": 0},
           "profile": "B-scripted"})
    _write(output / "browser-evidence.json", {"browser": {**pin["browser"],
           "font_fingerprint": pin["font_fingerprint"]}, "dom_artifacts": [], "screenshots": [], "snapshots": {}})
    _write(output / "gateway-evidence.json", {"events": [], "trace_summary": {"collector_errors": []}})
    _write(output / "verdict.json", verdict)
    _write(output / "cleanup.json", {"gateway_stopped": True, "vite_stopped": True})
    with zipfile.ZipFile(output / "source-snapshot.zip", "w") as archive:
        archive.writestr("product.py", b"original product")
    monkeypatch.setattr(browser_task_audit, "score", lambda m, ui, gw, *, workspace_root:
                        verdict if workspace_root == output / "workspace" else {})
    monkeypatch.setattr(browser_task_audit, "analyze_trace", lambda *args, **kwargs:
                        {"valid": True, "closed": True, "model_attempts": 0, "known_tokens": 0})
    assert browser_task_audit.audit(output)["errors"] == ["source_hash:product.py"]
    revision = output / "revision.json"
    envelope = {"verdict": verdict, "inputs": {name: digest((output / name).read_bytes()) for name in
                ("manifest.json", "browser-evidence.json", "gateway-evidence.json", "verdict.json", "source-snapshot.zip")},
                "grading_sources": {f"evals/{name}": digest((repo / "evals" / name).read_bytes()) for name in
                                    ("browser_task_evidence.py", "browser_task_audit.py")}}
    _write(revision, envelope)
    assert browser_task_audit.audit(output, revision=revision)["ok"] is True
    with zipfile.ZipFile(output / "source-snapshot.zip", "w") as archive:
        archive.writestr("product.py", b"tampered original")
    rejected = browser_task_audit.audit(output, revision=revision)
    assert rejected["ok"] is False
    assert {"revision_input_hashes", "source_hash:product.py"} <= set(rejected["errors"])


def test_dom_replay_excludes_sidebar_question_and_private_dialog():
    parser = Paragraphs()
    parser.feed('<aside><p>unrelated</p></aside><main><div><p>返回MOBILE17</p>'
                '<p>MOBILE17</p></div><div role="group" aria-label="Question">'
                '<p>report format?</p></div><div role="dialog"><p>private source</p></div>'
                '<p>after dialog</p><button>Used 2 tools</button></main>')
    assert parser.rows == [
        {"text": "返回MOBILE17", "paragraph_index": 0},
        {"text": "MOBILE17", "paragraph_index": 1},
        {"text": "after dialog", "paragraph_index": 4},
    ]
    assert parser.buttons == ["Used 2 tools"]


def test_dom_replay_preserves_duplicate_paragraphs_for_rejection():
    parser = Paragraphs()
    parser.feed('<main><p>5</p><p>5</p><p>A&amp;B<br>second</p></main>')
    assert [row["text"] for row in parser.rows] == ["5", "5", "A&B\nsecond"]


def test_dom_message_boundaries_include_markdown_lists_and_do_not_count_paragraphs():
    tree = DomTree()
    tree.feed('<main><div><div><p>返回MOBILE17</p></div><div><div>'
              '<h2>Summary</h2><p>first paragraph</p><ul><li>MOBILE17</li></ul>'
              '<p>second paragraph</p></div><div><button aria-label="Copy reply"></button>'
              '</div></div></div><div role="dialog"><p>private source</p></div></main>')
    bubbles = tree.bubbles({"返回MOBILE17"})
    assert len(bubbles) == 2
    assert bubbles[0]["role"] == "user"
    assert bubbles[1]["role"] == "assistant"
    assert " ".join(bubbles[1]["text"].split()) == "Summary first paragraph MOBILE17 second paragraph"


def test_dom_copy_action_proves_same_text_is_assistant():
    tree = DomTree()
    tree.feed('<main><div><div><p>返回MOBILE17</p></div><div><div><p>返回MOBILE17</p>'
              '</div><div><button aria-label="Copy reply"></button></div></div></div></main>')
    assert [row["role"] for row in tree.bubbles({"返回MOBILE17"})] == ["user", "assistant"]


def test_dom_code_header_preserves_copy_control_as_separate_visible_text():
    tree = DomTree()
    tree.feed('<main><div><div><p>edit config</p></div><div><div><p>Updated config</p><div><span>json</span>'
              '<button>Copy</button></div><pre>{"timeout":60}</pre></div>'
              '<div><button aria-label="Copy reply"></button></div></div></div></main>')
    bubbles = tree.bubbles({"edit config"})
    assert [row["role"] for row in bubbles] == ["user", "assistant"]
    assert " ".join(bubbles[1]["text"].split()) == 'Updated config json Copy {"timeout":60}'

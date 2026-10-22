from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from evals import identity


@pytest.fixture(autouse=True)
def conda_environment(monkeypatch):
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setenv("CONDA_PREFIX", sys.prefix)


def _write(root: Path, relative: str, content: str | bytes = "fixture") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def _candidate(root: Path) -> Path:
    root.mkdir(parents=True)
    files = {
        "AGENTS.md": "rules",
        "pyproject.toml": "[project]\nname='candidate'\n",
        "uv.lock": "version = 1\n",
        ".agent/security.md": "security rules",
        "nanobot/agent/loop.py": "loop source",
        "nanobot/agent/context.py": "context prompt builder",
        "nanobot/agent/tools/filesystem.py": "tool source",
        "nanobot/templates/skills/basic.md": "template skill",
        "nanobot/templates/prompts/base.j2": "rendered prompt template",
        "nanobot/skills/basic.yaml": "skill config",
        "nanobot/utils/prompt_templates.py": "PROMPT_TEMPLATE = 'source'\n",
        "evals/__init__.py": "",
        "evals/scorer.py": "SCORER = 'v1'\n",
        "evals/batches.py": "BATCH = 1\n",
        "evals/grading_transport.py": "PROMPT_VERSION = 'v1'\n",
        "evals/semantic_run.py": "JUDGE = 1\n",
        "evals/semantic_samples.py": "GOLD = 1\n",
        "evals/holdout.py": "GOLD = {}\n",
        "evals/holdout-registry.json": '{"holdout":"v1"}',
        "evals/cases/V001.json": '{"case":"one"}',
        "evals/fixtures/V001/config.json": '{"safe":true}',
        "tests/evals/test_helper.py": "def test_helper(): pass\n",
        "tests/knowledge/mail/eval_contract_cases.py": "CASES = []\n",
        "tests/knowledge/mail/helper.py": "def helper(): pass\n",
        "tests/tools/test_tool_validation.py": "def test_tool(): pass\n",
        "docs/validation-cases/01-basic.md": "The original full validation cases",
        "holdout-registry.json": '{"dataset":"holdout-v1"}',
        "bridge/src/index.ts": "export const bridge = true;",
        "bridge/package.json": '{"name":"bridge"}',
        "webui/src/App.tsx": "export default function App() {}",
        "webui/public/brand/logo.svg": "<svg></svg>",
        "webui/package.json": '{"name":"webui"}',
        "webui/bun.lock": "lock contents",
        ".git/HEAD": "private git metadata",
        "evals/runs/previous.json": '{"result":"private run"}',
        "webui/node_modules/private/index.js": "dependency cache",
        "webui/dist/index.js": "build output",
        "config.json": '{"api_key":"private-config-secret"}',
        "api.txt": "REAL-CREDENTIAL-CANARY-012345",
    }
    for name, content in files.items():
        _write(root, name, content)
    return root


def _freeze(root: Path, output: Path, conditions=None):
    return identity.freeze(source_root=root, output=output, conditions=conditions)


def _conditions(**values):
    return {"model_id": identity.MODEL_ID, **values}


def _rehash_group(group: dict) -> None:
    group["identity_hash"] = identity._group_hash(group["files"], group.get("data"))


def test_freeze_captures_selected_sources_and_excludes_credentials_and_artifacts(tmp_path):
    source = _candidate(tmp_path / "source")
    output = tmp_path / "snapshot"

    manifest = _freeze(source, output)
    checked = identity.verify(output)

    assert checked["ok"] is True
    assert manifest["snapshot_id"].startswith("sha256:")
    assert manifest["model_id"] == "deepseek-v4-flash"
    assert manifest["environment"]["conda_environment"] == "nanobot"
    assert manifest["conditions"]["mode"] == {"status": "unknown"}
    assert "evals/scorer.py" in manifest["groups"]["scoring"]["files"]
    assert "evals/grading_transport.py" in manifest["groups"]["prompt"]["files"]
    assert "nanobot/templates/prompts/base.j2" in manifest["groups"]["prompt"]["files"]
    assert "nanobot/agent/context.py" in manifest["groups"]["prompt"]["files"]
    assert "evals/fixtures/V001/config.json" in manifest["groups"]["cases"]["files"]
    assert "evals/holdout-registry.json" in manifest["groups"]["cases"]["files"]
    assert "evals/holdout.py" in manifest["groups"]["scoring"]["files"]
    assert "docs/validation-cases/01-basic.md" in manifest["files"]
    assert "holdout-registry.json" in manifest["groups"]["cases"]["files"]
    assert not any(name in manifest["files"] for name in (
        "api.txt", "config.json", ".git/HEAD", "evals/runs/previous.json",
        "webui/node_modules/private/index.js", "webui/dist/index.js",
    ))
    with zipfile.ZipFile(output / identity.ARCHIVE_NAME) as archive:
        assert b"REAL-CREDENTIAL-CANARY-012345" not in archive.read("pyproject.toml")
        assert "api.txt" not in archive.namelist()


def test_selected_secret_in_source_aborts_before_output_is_created(tmp_path):
    source = _candidate(tmp_path / "source")
    secret = "REAL-CREDENTIAL-CANARY-012345"
    _write(source, "nanobot/agent/loop.py", f"# accidental {secret}\n")
    output = tmp_path / "must-not-exist"

    with pytest.raises(RuntimeError, match="credential_found_in_selected_source"):
        _freeze(source, output)

    assert not output.exists()


def test_identity_components_track_prompt_fixture_scoring_config_and_environment(
        tmp_path, monkeypatch):
    source = _candidate(tmp_path / "source")
    conditions = _conditions(mode="R")
    baseline = _freeze(source, tmp_path / "base", conditions)

    changes = [
        ("evals/grading_transport.py", "PROMPT_VERSION = 'v2'\n", "prompt"),
        ("nanobot/templates/prompts/base.j2", "changed prompt template", "prompt"),
        ("evals/fixtures/V001/config.json", '{"safe":false}', "cases"),
        ("evals/scorer.py", "SCORER = 'v2'\n", "scoring"),
        ("pyproject.toml", "[project]\nname='changed'\n", "project_config"),
    ]
    for index, (relative, content, group) in enumerate(changes):
        _write(source, relative, content)
        changed = _freeze(source, tmp_path / f"changed-{index}", conditions)
        assert changed["groups"][group]["identity_hash"] != baseline["groups"][group]["identity_hash"]
        assert changed["snapshot_id"] != baseline["snapshot_id"]
        _write(source, relative, {
            "evals/grading_transport.py": "PROMPT_VERSION = 'v1'\n",
            "nanobot/templates/prompts/base.j2": "rendered prompt template",
            "evals/fixtures/V001/config.json": '{"safe":true}',
            "evals/scorer.py": "SCORER = 'v1'\n",
            "pyproject.toml": "[project]\nname='candidate'\n",
        }[relative])

    monkeypatch.setattr(identity.platform, "release", lambda: "changed-release")
    environment_changed = _freeze(source, tmp_path / "changed-environment", conditions)
    assert environment_changed["groups"]["environment"]["identity_hash"] != baseline[
        "groups"]["environment"]["identity_hash"]
    assert environment_changed["snapshot_id"] != baseline["snapshot_id"]


def test_conditions_and_runtime_config_are_frozen_without_credentials(tmp_path):
    source = _candidate(tmp_path / "source")
    baseline_conditions = _conditions(mode="R", budget={"calls": 4})
    changed_conditions = _conditions(mode="R", budget={"calls": 5})
    baseline = _freeze(source, tmp_path / "base", baseline_conditions)
    changed = _freeze(source, tmp_path / "changed", changed_conditions)

    assert changed["groups"]["conditions"]["identity_hash"] != baseline["groups"][
        "conditions"]["identity_hash"]
    assert changed["snapshot_id"] != baseline["snapshot_id"]
    assert baseline["conditions"] == baseline_conditions
    serialized = json.dumps(baseline, ensure_ascii=False)
    assert "private-safe-isolated" in serialized
    assert "private-config-secret" not in serialized
    assert "REAL-CREDENTIAL-CANARY-012345" not in serialized


def test_snapshot_identity_is_content_based_and_output_is_exclusive(tmp_path):
    source = _candidate(tmp_path / "source")
    first = _freeze(source, tmp_path / "first")
    _write(source, ".git/HEAD", "a different commit")
    second = _freeze(source, tmp_path / "second")

    assert first["snapshot_id"] == second["snapshot_id"]
    with pytest.raises(FileExistsError, match="snapshot_output_already_exists"):
        _freeze(source, tmp_path / "first")


def test_freeze_requires_the_real_nanobot_conda_environment(tmp_path, monkeypatch):
    source = _candidate(tmp_path / "source")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "another-env")

    with pytest.raises(RuntimeError, match="conda_environment_required:nanobot"):
        _freeze(source, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()

    monkeypatch.setenv("CONDA_DEFAULT_ENV", "nanobot")
    monkeypatch.setenv("CONDA_PREFIX", str(tmp_path / "not-the-running-python"))
    with pytest.raises(RuntimeError, match="conda_environment_mismatch:nanobot"):
        _freeze(source, tmp_path / "snapshot")


def test_verify_detects_missing_tampered_and_conflicting_identity_data(tmp_path):
    source = _candidate(tmp_path / "source")
    output = tmp_path / "snapshot"
    _freeze(source, output)
    (output / identity.ARCHIVE_NAME).unlink()
    assert "archive_unreadable_or_unsafe" in identity.verify(output)["errors"]

    output = tmp_path / "snapshot2"
    _freeze(source, output)
    archive_path = output / identity.ARCHIVE_NAME
    with zipfile.ZipFile(archive_path) as original:
        entries = {name: original.read(name) for name in original.namelist()
                   if name != "evals/fixtures/V001/config.json"}
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    assert "archive_inventory_mismatch" in identity.verify(output)["errors"]

    output = tmp_path / "snapshot3"
    _freeze(source, output)
    archive_path = output / identity.ARCHIVE_NAME
    with zipfile.ZipFile(archive_path) as original:
        entries = {
            name: (b"tampered" if name == "evals/scorer.py" else original.read(name))
            for name in original.namelist()
        }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100644 << 16)
            archive.writestr(info, content)
    result = identity.verify(output)
    assert result["ok"] is False
    assert "archive_hash_mismatch:evals/scorer.py" in result["errors"]

    # Editing a stored file hash and matching group hashes still conflicts with
    # the immutable snapshot ID embedded in the same manifest.
    output = tmp_path / "snapshot4"
    _freeze(source, output)
    manifest_path = output / identity.MANIFEST_NAME
    frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
    frozen_hash = "0" * 64
    frozen["files"]["evals/scorer.py"] = frozen_hash
    for group in frozen["groups"].values():
        if "evals/scorer.py" in group["files"]:
            group["files"]["evals/scorer.py"] = frozen_hash
            _rehash_group(group)
    manifest_path.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    result = identity.verify(output)
    assert "snapshot_id_mismatch" in result["errors"]
    assert any(error.startswith("archive_hash_mismatch:evals/scorer.py")
               for error in result["errors"])

    # Recomputing both group and snapshot hashes cannot hide a missing member.
    output = tmp_path / "snapshot5"
    _freeze(source, output)
    manifest_path = output / identity.MANIFEST_NAME
    frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
    del frozen["groups"]["scoring"]["files"]["evals/scorer.py"]
    _rehash_group(frozen["groups"]["scoring"])
    frozen["snapshot_id"] = "sha256:" + identity._identity_hash(frozen["groups"])
    manifest_path.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
    result = identity.verify(output)
    assert "group_membership_mismatch:scoring" in result["errors"]
    assert result["ok"] is False


def test_materialize_unpacks_verified_workspace_only_under_snapshot_root(tmp_path, monkeypatch):
    source = _candidate(tmp_path / "source")
    snapshot = tmp_path / "snapshot"
    _freeze(source, snapshot)
    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(identity, "ROOT", workspace_root)
    destination_parent = workspace_root / "tmp" / "phase7-snapshots"
    destination_parent.mkdir(parents=True)

    materialized = identity.materialize(snapshot, destination_parent / "candidate-1")

    assert materialized == destination_parent / "candidate-1"
    assert (materialized / "evals/scorer.py").read_text(encoding="utf-8") == "SCORER = 'v1'\n"
    assert (materialized / "docs/validation-cases/01-basic.md").is_file()
    assert not (materialized / "api.txt").exists()
    with pytest.raises(FileExistsError, match="materialize_destination_already_exists"):
        identity.materialize(snapshot, materialized)
    with pytest.raises(ValueError, match="materialize_destination_outside_phase7_snapshots"):
        identity.materialize(snapshot, workspace_root / "elsewhere" / "candidate")


def test_materialize_rejects_zip_path_traversal_and_symlink_entries(tmp_path, monkeypatch):
    source = _candidate(tmp_path / "source")
    snapshot = tmp_path / "snapshot"
    _freeze(source, snapshot)
    workspace_root = tmp_path / "workspace"
    monkeypatch.setattr(identity, "ROOT", workspace_root)
    (workspace_root / "tmp" / "phase7-snapshots").mkdir(parents=True)

    with zipfile.ZipFile(snapshot / identity.ARCHIVE_NAME, "w") as archive:
        archive.writestr("../escape.txt", "outside")
    with pytest.raises(ValueError, match="snapshot_verification_failed"):
        identity.materialize(snapshot, workspace_root / "tmp/phase7-snapshots/escape")

    _freeze(source, tmp_path / "snapshot2")
    snapshot2 = tmp_path / "snapshot2"
    with zipfile.ZipFile(snapshot2 / identity.ARCHIVE_NAME, "w") as archive:
        info = zipfile.ZipInfo("linked")
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "target")
    assert "archive_nonregular_entry:linked" in identity.verify(snapshot2)["errors"]
    with pytest.raises(ValueError, match="snapshot_verification_failed"):
        identity.materialize(snapshot2, workspace_root / "tmp/phase7-snapshots/symlink")

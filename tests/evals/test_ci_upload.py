import hashlib
import json
import shutil
import uuid
import zipfile
from pathlib import Path

import pytest

from evals.ci_upload import build_staging, verify_zip


@pytest.fixture
def workspace():
    root = Path.cwd() / ".tmp" / ("ci-upload-test-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _index(directory: Path, name: str):
    contents = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in directory.rglob("*") if p.is_file() and p.name != name}
    (directory / name).write_text(json.dumps(contents), encoding="utf-8")


def _fixture(root: Path, platform="linux"):
    source = root / "source"
    wrapper = source / f"{platform}-smoke-123-1"
    run = wrapper / "run"
    hidden = run / "executions/V016-1/trials/V016-x/files/.nanobot/tool-results/call.bin"
    hidden.parent.mkdir(parents=True)
    hidden.write_bytes(b"\x00\xff\x80hidden\x00")
    (run / "evidence.json").write_text("{}", encoding="utf-8")
    _index(run, "artifact-index.json")
    (wrapper / "receipt.json").write_text("local receipt", encoding="utf-8")
    _index(wrapper, "ci-artifact-index.json")
    (source / "runner-start.json").write_text("{}", encoding="utf-8")
    (source / "job-completion.json").write_text("{}", encoding="utf-8")
    (source / "source-checkout.json").write_text("{}", encoding="utf-8")
    (source / "runner").mkdir()
    (source / "runner/session.log").write_text("runner", encoding="utf-8")
    (source / "gates").mkdir()
    (source / "gates/smoke.json").write_text("{}", encoding="utf-8")
    (source / "unrelated.json").write_text("do not stage", encoding="utf-8")
    (source / ".root-secret").write_text("do not inspect", encoding="utf-8")
    (source / "api.txt").write_text("outside selected scope", encoding="utf-8")
    return source, wrapper, hidden


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_staging_preserves_hidden_binary_and_only_allowlisted_paths(workspace, platform):
    source, wrapper, hidden = _fixture(workspace, platform)
    output = workspace / "staging"

    receipt = build_staging(source, output, wrapper.name)

    staged_hidden = output / wrapper.name / hidden.relative_to(wrapper)
    assert staged_hidden.read_bytes() == hidden.read_bytes()
    assert receipt["receipt_type"] == "local_integrity_only"
    assert not receipt["execution_passed"]
    assert not (output / "unrelated.json").exists()
    assert not (output / ".root-secret").exists()
    assert not (output / "api.txt").exists()


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra"])
def test_wrapper_integrity_rejects_missing_changed_and_extra_files(workspace, mutation):
    source, wrapper, hidden = _fixture(workspace)
    if mutation == "missing":
        hidden.unlink()
    elif mutation == "changed":
        hidden.write_bytes(b"modified")
    else:
        (wrapper / "unexpected.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(ValueError):
        build_staging(source, workspace / "staging", wrapper.name)


@pytest.mark.parametrize("secret", ["api.txt", ".ENV.local", ".envrc", ".aws/key",
                                    ".nanobot/config.json"])
def test_credentials_in_wrapper_are_rejected(workspace, secret):
    source, wrapper, _ = _fixture(workspace)
    path = wrapper / "run" / secret
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret", encoding="utf-8")
    _index(wrapper / "run", "artifact-index.json")
    _index(wrapper, "ci-artifact-index.json")

    with pytest.raises(ValueError, match="credential"):
        build_staging(source, workspace / "staging", wrapper.name)


@pytest.mark.parametrize("rel", ["runner/.aws/key.log", "gates/.env.local.json"])
def test_credentials_in_selected_outer_scope_are_rejected(workspace, rel):
    source, wrapper, _ = _fixture(workspace)
    path = source / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError, match="credential"):
        build_staging(source, workspace / "staging", wrapper.name)


def test_zip_verification_and_negative_controls(workspace):
    source, wrapper, hidden = _fixture(workspace)
    stage = workspace / "stage"
    build_staging(source, stage, wrapper.name)
    zip_path = workspace / "valid.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in stage.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    assert verify_zip(zip_path, wrapper.name)["ok"]
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr(f"{wrapper.name}/unexpected.txt", "extra")
    with pytest.raises(ValueError, match="inventory_or_hash"):
        verify_zip(zip_path, wrapper.name)

    missing_zip = workspace / "missing.zip"
    with zipfile.ZipFile(missing_zip, "w") as archive:
        for path in (stage / wrapper.name).rglob("*"):
            if path.is_file() and path != stage / wrapper.name / hidden.relative_to(wrapper):
                archive.write(path, f"{wrapper.name}/{path.relative_to(stage / wrapper.name).as_posix()}")
    with pytest.raises(ValueError, match="inventory_or_hash"):
        verify_zip(missing_zip, wrapper.name)

    changed_zip = workspace / "changed.zip"
    with zipfile.ZipFile(changed_zip, "w") as archive:
        for path in stage.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(stage).as_posix()
            data = path.read_bytes()
            if name.endswith("call.bin"):
                data = b"altered bytes"
            archive.writestr(name, data)
    with pytest.raises(ValueError, match="inventory_or_hash"):
        verify_zip(changed_zip, wrapper.name)


def test_indexes_must_be_objects_and_output_must_be_new(workspace):
    source, wrapper, _ = _fixture(workspace)
    (wrapper / "ci-artifact-index.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="index_must_be_object"):
        build_staging(source, workspace / "staging", wrapper.name)
    _index(wrapper, "ci-artifact-index.json")
    (workspace / "existing").mkdir()
    with pytest.raises(ValueError, match="output_must_be_new"):
        build_staging(source, workspace / "existing", wrapper.name)


def test_complete_failed_execution_is_archived_without_rejudging(workspace):
    source, wrapper, _ = _fixture(workspace)
    original = b'{"state":"invalid","pipeline_passed":false,"quality_passed":false}'
    (wrapper / "receipt.json").write_bytes(original)
    _index(wrapper, "ci-artifact-index.json")
    stage = workspace / "stage"
    result = build_staging(source, stage, wrapper.name)
    assert result["ok"] is True and result["execution_passed"] is False
    assert (stage / wrapper.name / "receipt.json").read_bytes() == original


def test_workflow_uploads_only_validated_staging_and_includes_hidden_files():
    workflow = (Path.cwd() / ".github/workflows/evaluation.yml").read_text(encoding="utf-8")
    start = workflow.index("      - name: Prepare scoped deterministic evaluation evidence")
    end = workflow.index("\n  real-model-preflight:", start)
    block = workflow[start:end]

    assert "id: deterministic_upload" in block
    assert "python -m evals.ci_upload --source reports/evaluations --output reports/evaluation-upload --wrapper" in block
    assert "always() && steps.deterministic_upload.outcome == 'success'" in block
    assert "path: |\n            reports/evaluation-upload/" in block
    assert "include-hidden-files: true" in block
    for excluded in ("api.txt", ".env*", ".git", ".aws", ".ssh",
                     "credentials.json", "secrets.json", ".nanobot/config.json"):
        assert excluded in block

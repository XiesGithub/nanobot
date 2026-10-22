"""Executable checks for the Stage 6 basic/tools/context/memory adapters."""

import pytest

from evals.batch_basic import cases, execute


def _workspace(tmp_path, spec):
    for relative, content in spec["fixtures"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def _assertions(spec, observed):
    for row in spec["assertions"]:
        value = observed[row["key"]]
        if row["op"] == "equal":
            assert type(value) is type(row["expected"])
            assert value == row["expected"]
        elif row["op"] == "contains":
            assert row["expected"] in value
        else:
            assert value is True


def test_catalog_covers_v001_to_v040_with_explicit_gaps():
    catalog = cases()
    assert set(catalog) == {f"V{i:03}" for i in range(1, 41)}
    assert all(spec["entry"] and spec["scope"] and isinstance(spec["fixtures"], dict)
               for spec in catalog.values())
    assert all(not spec["supported"] and spec["limitations"]
               for spec in catalog.values() if not spec["assertions"])
    assert not catalog["V022"]["supported"]
    assert "VM/容器" in catalog["V022"]["limitations"][0]
    assert catalog["V021"]["supported"]
    assert catalog["V016"]["controls"]["max_tool_result_chars"] == 1000
    assert all(catalog[case_id]["supported"] for case_id in
               ("V001", "V002", "V003", "V004", "V005", "V006", "V019", "V020",
                "V025", "V026", "V027", "V028", "V033", "V034", "V035", "V039"))


SUPPORTED = [case_id for case_id, spec in cases().items() if spec["supported"]]


@pytest.mark.parametrize("case_id", SUPPORTED)
async def test_supported_module_contracts(case_id, tmp_path):
    spec = cases()[case_id]
    workspace = _workspace(tmp_path, spec)
    observed = await execute(case_id, workspace)
    if case_id == "V020":
        assert observed["fixture_hash_before_extract"] == observed["fixture_hash_after_extract"]
        assert observed["extracted_text"] == (
            "\n\n[File: budget.docx]\n项目竹桥\n\n项目\t金额\n设备\t120\n运输\t30"
        )
    _assertions(spec, observed)


async def test_v020_adapter_does_not_invent_missing_table_data(tmp_path, monkeypatch):
    spec = cases()["V020"]
    workspace = _workspace(tmp_path, spec)
    monkeypatch.setattr(
        "nanobot.utils.document.extract_documents",
        lambda text, media: ("\n\n[File: budget.docx]\n项目竹桥", []),
    )
    observed = await execute("V020", workspace)
    assert observed["paragraph_present"] is True
    assert observed["equipment_row_present"] is False
    assert observed["transport_row_present"] is False
    assert observed["total"] is None
    assert observed["fixture_unchanged"] is True


def test_supported_contracts_are_not_full_task_claims():
    for spec in cases().values():
        if spec["supported"]:
            assert spec["scope"] in {"module_contract", "runner_contract"}
            assert spec["limitations"]

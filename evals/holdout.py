"""Frozen, program-scored synthetic cases reserved for a phase 8 release profile."""

from __future__ import annotations

import copy
import random
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Mapping

from evals.common import CASE_FIELDS, ROOT, canonical, digest, inside, load_cases, read

REGISTRY_PATH = ROOT / "evals" / "holdout-registry.json"
REGISTRY_SHA256 = "a646b9dc000d376f89084e87211b0cc7fc035dc85d4adc62ce4b0757a67e167f"
VERSION = "phase8-holdout/1.2"
REGISTRY_SCHEMA = "phase8-holdout-registry/1.2"
CASE_IDS = tuple(f"H{i:03}" for i in range(101, 121))
FAMILIES = ("arithmetic", "field_extraction", "sort_dedup", "strict_boundary")
PUBLIC_IDS = {f"V{i:03}" for i in range(1, 121)}
RELEASE_SET_VERSION = "release/1.0"
RELEASE_CASE_IDS = tuple(f"R{i:03}" for i in range(1, 21))
RELEASE_FAMILY_TEMPLATES = {
    "arithmetic": "H101",
    "field_extraction": "H106",
    "sort_dedup": "H111",
    "strict_boundary": "H116",
}


def _registry():
    registry_bytes = REGISTRY_PATH.read_bytes()
    if digest(registry_bytes) != REGISTRY_SHA256:
        raise ValueError("holdout_registry_hash")
    registry = read(REGISTRY_PATH)
    if (registry.get("schema_version") != REGISTRY_SCHEMA
            or registry.get("case_version") != VERSION
            or registry.get("split") != "holdout"
            or registry.get("scope") != "full_task"
            or registry.get("construction_origin") != "prospective_synthetic"
            or registry.get("creation_source") !=
            "fresh v1.2 synthetic facts authored after phase7 v1.1 holdout results were exposed; no v1.1 facts, inputs, gold, or public input/gold copied"
            or registry.get("construction_seed") !=
            "phase8-holdout-v1.2-2026-10-10-hand-authored-after-phase7-exposure"):
        raise ValueError("holdout_registry_metadata")
    records = registry.get("cases")
    if not isinstance(records, list) or [case.get("case_id") for case in records] != list(CASE_IDS):
        raise ValueError("holdout_inventory")
    if registry.get("data_sha256") != digest(canonical(records)):
        raise ValueError("holdout_data_hash")
    counts = Counter(case.get("family") for case in records)
    if counts != Counter({family: 5 for family in FAMILIES}):
        raise ValueError("holdout_family_inventory")
    for item in records:
        family = item["family"]
        if item["input"] != _render_input(family, item["facts"]):
            raise ValueError("holdout_input_facts_mismatch")
        if not _strict_equal(item["expected"], _gold(family, item["facts"])):
            raise ValueError("holdout_gold_mismatch")
    return registry


def _render_input(family, facts):
    if family == "arithmetic":
        return (f"新造价目单：每件商品{facts['unit_price']}元，购入{facts['quantity']}件；"
                f"整单消费满{facts['threshold']}元立减{facts['discount']}元。求优惠后的支付总额，"
                "仅输出JSON字段total。")
    if family == "field_extraction":
        return (f"物流凭证记载：编号{facts['order_id']}；货品数{facts['quantity']}件；"
                f"单件标价{facts['unit_price']}元。抽取编号、货品数和标价，结果只用JSON表示，"
                "键为order_id、quantity、unit_price。")
    if family == "sort_dedup":
        values = "，".join(str(value) for value in facts["values"])
        return f"请把整数列表（{values}）整理为升序并删除重复数字，返回JSON数组。"
    if family == "strict_boundary":
        return (f"判定标准：记录金额必须严格高于{facts['threshold']}元且状态必须是approved才通过。"
                f"当前记录金额{facts['amount']}元，状态{facts['status']}。仅用JSON给出eligible布尔值。")
    raise ValueError("unsupported_holdout_family")


def _gold(family, facts):
    if family == "arithmetic":
        subtotal = facts["unit_price"] * facts["quantity"]
        total = subtotal - facts["discount"] if subtotal >= facts["threshold"] else subtotal
        return {"total": total}
    if family == "field_extraction":
        return {key: facts[key] for key in ("order_id", "quantity", "unit_price")}
    if family == "sort_dedup":
        return sorted(set(facts["values"]))
    if family == "strict_boundary":
        return {"eligible": facts["amount"] > facts["threshold"] and facts["status"] == "approved"}
    raise ValueError("unsupported_holdout_family")


def _strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_strict_equal(left[key], right[key])
                                                   for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _build_holdout_cases() -> list[dict]:
    """Build exact CASE_FIELDS records from the frozen registry and public task templates."""
    registry = _registry()
    templates = {case["case_id"]: case for case in load_cases()}
    raw_hash = digest(REGISTRY_PATH.read_bytes())
    family_templates = {
        "arithmetic": templates["V001"],
        "field_extraction": templates["V002"],
        "sort_dedup": templates["V003"],
        "strict_boundary": templates["V004"],
    }
    cases = []
    for item in registry["cases"]:
        case = copy.deepcopy(family_templates[item["family"]])
        case.update(
            case_id=item["case_id"],
            case_version=VERSION,
            title=f"保留集-{item['case_id']}-{item['family']}",
            source_ref=f"evals/holdout-registry.json#{item['case_id']}",
            source_sha256=raw_hash,
            tags=["phase8-holdout", "program", item["family"]],
            split="holdout",
        )
        case["initial_state"]["fixtures"] = []
        case["turns"] = [{"turn_id": "t1", "input": item["input"], "trigger": "start"}]
        case["requirements"][0]["expected"] = copy.deepcopy(item["expected"])
        case["requirements"][0]["source_clause"] = "保留集题目要求的结构化输出"
        case["scoring"].update(version=VERSION, strict_json_types=True,
                               semantic_review="program_gold_verified", formal_quality_eligible=False)
        if set(case) != CASE_FIELDS:
            raise ValueError("unsupported_case_schema")
        cases.append(case)
    return cases


def holdout_cases() -> list[dict]:
    return _build_holdout_cases()


def _canonical_nonce(nonce):
    if not isinstance(nonce, str):
        raise ValueError("invalid_release_nonce")
    try:
        parsed = uuid.UUID(nonce)
    except (ValueError, AttributeError):
        raise ValueError("invalid_release_nonce") from None
    if parsed.int == 0 or str(parsed) != nonce:
        raise ValueError("invalid_release_nonce")
    return parsed


def _release_facts(nonce):
    rng = random.Random(nonce.int)
    records = []
    seen = {family: set() for family in FAMILIES}
    static = {family: {canonical(item["facts"]) for item in _registry()["cases"]
                       if item["family"] == family} for family in FAMILIES}

    def fresh(family, build):
        while True:
            facts = build()
            signature = canonical(facts)
            if signature not in seen[family] and signature not in static[family]:
                seen[family].add(signature)
                return facts

    for index in range(5):
        facts = fresh("arithmetic", lambda: {
            "unit_price": rng.randint(17, 997),
            "quantity": rng.randint(2, 17),
            "threshold": rng.randint(125, 2800),
            "discount": rng.randint(7, 240),
        })
        records.append(("arithmetic", facts))
    for _ in range(5):
        def build_fields():
            letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
            return {"order_id": f"{letters}-{rng.randint(100, 999)}",
                    "quantity": rng.randint(2, 23), "unit_price": rng.randint(11, 997)}

        facts = fresh("field_extraction", build_fields)
        records.append(("field_extraction", facts))
    for _ in range(5):
        def build_sort():
            values = rng.sample(range(11, 999), 6)
            values.extend(rng.choices(values, k=2))
            rng.shuffle(values)
            return {"values": values}

        records.append(("sort_dedup", fresh("sort_dedup", build_sort)))
    for index in range(5):
        def build_boundary():
            threshold = rng.randint(125, 2800)
            amount = threshold + (1 if index in (0, 4) else 0 if index in (1, 2) else -1)
            status = "pending" if index == 2 else "approved"
            return {"amount": amount, "threshold": threshold, "status": status}

        records.append(("strict_boundary", fresh("strict_boundary", build_boundary)))
    return records


def _release_input(family, facts, nonce):
    return f"本轮标识：{nonce}；" + _render_input(family, facts)


def _build_release_cases(data, data_sha256):
    templates = {case["case_id"]: case for case in holdout_cases()}
    cases = []
    case_version = f"{RELEASE_SET_VERSION}+{data['nonce'].replace('-', '')[:12]}"
    for item in data["cases"]:
        case = copy.deepcopy(templates[RELEASE_FAMILY_TEMPLATES[item["family"]]])
        case.update(
            case_id=item["case_id"],
            case_version=case_version,
            title=f"发布保留集-{item['case_id']}-{item['family']}",
            source_ref=f"generated-release-set.json#{item['case_id']}",
            source_sha256=data_sha256,
            tags=["phase8-release-holdout", "program", item["family"]],
            split="holdout",
        )
        case["initial_state"]["fixtures"] = []
        case["turns"] = [{"turn_id": "t1", "input": item["input"], "trigger": "start"}]
        case["requirements"][0]["expected"] = copy.deepcopy(item["expected"])
        case["requirements"][0]["source_clause"] = "本轮保留集题目要求的结构化输出"
        case["scoring"].update(version=case_version, strict_json_types=True,
                               semantic_review="program_gold_verified", formal_quality_eligible=False)
        if set(case) != CASE_FIELDS:
            raise ValueError("unsupported_case_schema")
        cases.append(case)
    return cases


def create_release_set(nonce: str, public_cases) -> dict:
    """Create a nonce-bound release set without changing the static prototype registry."""
    parsed_nonce = _canonical_nonce(nonce)
    records = []
    for index, (family, facts) in enumerate(_release_facts(parsed_nonce), start=1):
        records.append({
            "case_id": RELEASE_CASE_IDS[index - 1],
            "family": family,
            "facts": facts,
            "input": _release_input(family, facts, nonce),
            "expected": _gold(family, facts),
        })
    data = {"version": RELEASE_SET_VERSION, "nonce": nonce, "cases": records}
    data_sha256 = digest(canonical(data))
    bundle = {"cases": _build_release_cases(data, data_sha256),
              "data": data, "data_sha256": data_sha256}
    bundle["validation"] = validate_release_set(bundle, public_cases)
    return bundle


def _validate_release_facts(family, facts):
    if not isinstance(facts, Mapping):
        raise ValueError("release_facts_schema")
    if family == "arithmetic":
        expected_keys = {"unit_price", "quantity", "threshold", "discount"}
        if (set(facts) != expected_keys
                or any(type(facts[key]) is not int for key in expected_keys)
                or not 17 <= facts["unit_price"] <= 997
                or not 2 <= facts["quantity"] <= 17
                or not 125 <= facts["threshold"] <= 2800
                or not 7 <= facts["discount"] <= 240):
            raise ValueError("release_facts_schema")
    elif family == "field_extraction":
        if (set(facts) != {"order_id", "quantity", "unit_price"}
                or not isinstance(facts["order_id"], str)
                or not re.fullmatch(r"[A-Z]{2}-\d{3}", facts["order_id"])
                or type(facts["quantity"]) is not int or not 2 <= facts["quantity"] <= 23
                or type(facts["unit_price"]) is not int
                or not 11 <= facts["unit_price"] <= 997):
            raise ValueError("release_facts_schema")
    elif family == "sort_dedup":
        values = facts.get("values")
        if (set(facts) != {"values"} or not isinstance(values, list)
                or len(values) != 8 or any(type(value) is not int or not 11 <= value < 999
                                           for value in values)
                or len(set(values)) != 6):
            raise ValueError("release_facts_schema")
    elif family == "strict_boundary":
        if (set(facts) != {"amount", "threshold", "status"}
                or type(facts["amount"]) is not int or not 124 <= facts["amount"] <= 2801
                or type(facts["threshold"]) is not int
                or not 125 <= facts["threshold"] <= 2800
                or not isinstance(facts["status"], str)
                or facts["status"] not in {"approved", "pending"}):
            raise ValueError("release_facts_schema")
    else:
        raise ValueError("unsupported_holdout_family")


def validate_release_set(bundle, public_cases) -> dict:
    """Rebuild task inputs and gold from nonce-bound facts; ignore supplied validation."""
    if (not isinstance(bundle, Mapping)
            or not {"cases", "data", "data_sha256"} <= set(bundle)
            or set(bundle) - {"cases", "data", "data_sha256", "validation"}):
        raise ValueError("release_bundle_schema")
    data = bundle["data"]
    if not isinstance(data, Mapping) or set(data) != {"version", "nonce", "cases"}:
        raise ValueError("release_data_schema")
    nonce = data.get("nonce")
    _canonical_nonce(nonce)
    if data.get("version") != RELEASE_SET_VERSION:
        raise ValueError("release_version")
    data_sha256 = digest(canonical(data))
    if bundle.get("data_sha256") != data_sha256:
        raise ValueError("release_data_hash")
    records = data.get("cases")
    if (not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records)
            or [item.get("case_id") for item in records] != list(RELEASE_CASE_IDS)):
        raise ValueError("release_inventory")
    counts = Counter(item.get("family") for item in records)
    if counts != Counter({family: 5 for family in FAMILIES}):
        raise ValueError("release_family_inventory")
    static_registry = _registry()
    static_records = {family: [] for family in FAMILIES}
    for item in static_registry["cases"]:
        static_records[item["family"]].append(item)
    for item in records:
        if not isinstance(item, Mapping) or set(item) != {
                "case_id", "family", "facts", "input", "expected"}:
            raise ValueError("release_case_data_schema")
        family, facts = item["family"], item["facts"]
        _validate_release_facts(family, facts)
        expected_input = _release_input(family, facts, nonce)
        expected_gold = _gold(family, facts)
        if item["input"] != expected_input:
            raise ValueError("release_input_recompute_mismatch")
        if not _strict_equal(item["expected"], expected_gold):
            raise ValueError("release_gold_recompute_mismatch")
        if any(_strict_equal(facts, old["facts"]) for old in static_records[family]):
            raise ValueError("release_static_fact_overlap")
    expected_cases = _build_release_cases(data, data_sha256)
    cases = bundle.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected_cases):
        raise ValueError("release_case_inventory")
    if any(canonical(actual) != canonical(expected)
           for actual, expected in zip(cases, expected_cases, strict=True)):
        raise ValueError("release_case_tampered")
    public_ids, public_inputs, public_fixtures, public_hashes, fixture_count, input_count = (
        _public_inventory(public_cases))
    if {item["case_id"] for item in cases} & public_ids:
        raise ValueError("release_public_id_overlap")
    static_inputs = {item["input"] for item in static_registry["cases"]}
    release_inputs = {item["input"] for item in records}
    static_input_hashes = {digest(value.encode("utf-8")) for value in static_inputs}
    release_input_hashes = {digest(value.encode("utf-8")) for value in release_inputs}
    if release_inputs & static_inputs or release_input_hashes & static_input_hashes:
        raise ValueError("release_static_input_overlap")
    public_texts = public_inputs | public_fixtures
    if any(any(_normalize(item["input"]) in text for text in public_texts)
           for item in records):
        raise ValueError("release_public_text_overlap")
    release_hashes = {digest(item["input"].encode("utf-8")) for item in records}
    release_hashes.update(digest(canonical(item)) for item in records)
    if release_hashes & public_hashes:
        raise ValueError("release_public_hash_overlap")
    return {
        "status": "pass",
        "version": RELEASE_SET_VERSION,
        "nonce": nonce,
        "data_sha256": data_sha256,
        "case_count": len(cases),
        "families": dict(counts),
        "public_case_count": len(public_ids),
        "public_input_clause_count": input_count,
        "public_fixture_count": fixture_count,
        "id_overlap_count": 0,
        "public_text_or_fixture_overlap_count": 0,
        "static_input_overlap_count": 0,
        "all_gold_recomputed": True,
        "exposure_statement": "The cases share skill families with public tasks. No claim is made that a model has never encountered similar facts or patterns.",
        "formal_quality_eligible": False,
    }


def _public_inventory(public_cases):
    if isinstance(public_cases, Mapping):
        entries = list(public_cases.items())
    else:
        entries = [(None, item) for item in public_cases]
    ids, inputs, fixtures, public_hashes = set(), set(), set(), set()
    fixture_count = 0
    input_clause_count = 0
    missing_fixture_ids = []
    for key, entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("invalid_public_catalog_entry")
        case_id = entry.get("id", entry.get("case_id", key))
        if not isinstance(case_id, str) or case_id in ids:
            raise ValueError("duplicate_public_case_id")
        ids.add(case_id)
        if "fixtures" not in entry and "initial_state" not in entry:
            missing_fixture_ids.append(case_id)
        source_text = entry.get("contract")
        if isinstance(source_text, str):
            public_hashes.add(digest(source_text.encode("utf-8")))
            for line in source_text.splitlines():
                if "用户输入" in line:
                    _, separator, clause = line.partition("：")
                    if not separator:
                        _, separator, clause = line.partition(":")
                    if separator:
                        input_clause_count += 1
                        inputs.add(_normalize(clause))
                        public_hashes.add(digest(clause.encode("utf-8")))
            public_inputs = re.findall(
                r"\*\*用户输入\*\*：\s*[“「『](.*?)[”」』]",
                source_text,
                flags=re.DOTALL,
            )
            inputs.update(_normalize(value) for value in public_inputs)
            public_hashes.update(digest(value.encode("utf-8")) for value in public_inputs)
        for turn in entry.get("turns", []):
            value = turn.get("input")
            if isinstance(value, str):
                input_clause_count += 1
                inputs.add(_normalize(value))
                public_hashes.add(digest(value.encode("utf-8")))
        value = entry.get("input")
        if isinstance(value, str):
            input_clause_count += 1
            inputs.add(_normalize(value))
            public_hashes.add(digest(value.encode("utf-8")))
        source_hash = entry.get("source_sha256")
        if isinstance(source_hash, str):
            public_hashes.add(source_hash)
        fixture_hashes = entry.get("fixture_hashes", {})
        if isinstance(fixture_hashes, Mapping):
            public_hashes.update(value for value in fixture_hashes.values()
                                 if isinstance(value, str))
        fixture_values = entry.get("fixtures", {})
        if isinstance(fixture_values, Mapping):
            for fixture in fixture_values.values():
                if isinstance(fixture, str):
                    fixture_count += 1
                    fixtures.add(_normalize(fixture))
                    public_hashes.add(digest(fixture.encode("utf-8")))
                elif isinstance(fixture, Mapping):
                    source = fixture.get("source")
                    if isinstance(source, str):
                        path = inside(ROOT / "evals", source)
                        if path.is_file():
                            content = path.read_bytes()
                            fixture_count += 1
                            public_hashes.add(digest(content))
                            try:
                                fixtures.add(_normalize(content.decode("utf-8")))
                            except UnicodeDecodeError:
                                pass
        for fixture in entry.get("initial_state", {}).get("fixtures", []):
            source = fixture.get("source")
            if isinstance(source, str):
                path = inside(ROOT / "evals", source)
                content = path.read_bytes()
                fixture_count += 1
                public_hashes.add(digest(content))
                try:
                    fixtures.add(_normalize(content.decode("utf-8")))
                except UnicodeDecodeError:
                    pass
    if ids != PUBLIC_IDS:
        raise ValueError("public_inventory_not_120")
    if missing_fixture_ids:
        from evals.batches import catalog

        adapter_specs = catalog()
        for case_id in missing_fixture_ids:
            for fixture in adapter_specs[case_id].get("fixtures", {}).values():
                if isinstance(fixture, str):
                    fixture_count += 1
                    fixtures.add(_normalize(fixture))
                    public_hashes.add(digest(fixture.encode("utf-8")))
    if input_clause_count < 120:
        raise ValueError("public_input_inventory_incomplete")
    return ids, inputs, fixtures, public_hashes, fixture_count, input_clause_count


def _normalize(text):
    return unicodedata.normalize("NFC", text.strip())


def validate_holdout(public_cases, holdout_cases) -> dict:
    """Audit inventory, frozen gold, public overlap, provenance, and task scope."""
    registry = _registry()
    cases = list(holdout_cases)
    if len(cases) != 20 or [case.get("case_id") for case in cases] != list(CASE_IDS):
        raise ValueError("holdout_case_inventory")
    frozen_cases = _build_holdout_cases()
    raw_hash = digest(REGISTRY_PATH.read_bytes())
    records = {item["case_id"]: item for item in registry["cases"]}
    ids = set()
    holdout_input_hashes, holdout_content_hashes = set(), set()
    for case, frozen in zip(cases, frozen_cases, strict=True):
        case_id = case["case_id"]
        if case_id in ids:
            raise ValueError("duplicate_holdout_case_id")
        ids.add(case_id)
        item = records[case_id]
        family = item["family"]
        if set(case) != CASE_FIELDS or case.get("schema_version") != "agent-eval/1.0":
            raise ValueError("unsupported_case_schema")
        if (case.get("case_version") != VERSION or case.get("split") != "holdout"
                or case.get("scope") != "full_task" or case.get("source_ref") !=
                f"evals/holdout-registry.json#{case_id}"
                or case.get("source_sha256") != raw_hash):
            raise ValueError("holdout_identity_or_provenance")
        if case.get("tags") != ["phase8-holdout", "program", family]:
            raise ValueError("holdout_family_tampered")
        if case.get("initial_state", {}).get("fixtures") != []:
            raise ValueError("holdout_fixture_not_empty")
        if case.get("turns", [{}])[0].get("input") != item["input"]:
            raise ValueError("holdout_input_changed")
        if not _strict_equal(case.get("requirements", [{}])[0].get("expected"), item["expected"]):
            raise ValueError("holdout_case_gold_changed")
        if case.get("scoring", {}).get("strict_json_types") is not True:
            raise ValueError("holdout_strict_types_missing")
        if case.get("scoring", {}).get("formal_quality_eligible") is not False:
            raise ValueError("holdout_formal_quality_enabled")
        if canonical(case) != canonical(frozen):
            raise ValueError("holdout_case_tampered")
        holdout_input_hashes.add(digest(item["input"].encode("utf-8")))
        holdout_content_hashes.add(digest(canonical(item)))
    (public_ids, public_inputs, public_fixtures, public_hashes, fixture_count,
     input_clause_count) = _public_inventory(public_cases)
    if ids & public_ids:
        raise ValueError("holdout_public_id_overlap")
    public_texts = public_inputs | public_fixtures
    if any(any(_normalize(item["input"]) in text for text in public_texts)
           for item in registry["cases"]):
        raise ValueError("holdout_public_text_overlap")
    if (holdout_input_hashes | holdout_content_hashes) & public_hashes:
        raise ValueError("holdout_public_hash_overlap")
    if Counter(item["family"] for item in registry["cases"]) != Counter(
            {family: 5 for family in FAMILIES}):
        raise ValueError("holdout_family_inventory")
    return {
        "status": "pass",
        "holdout_count": len(cases),
        "families": dict(Counter(item["family"] for item in registry["cases"])),
        "public_case_count": len(public_ids),
        "public_input_clause_count": input_clause_count,
        "public_fixture_count": fixture_count,
        "id_overlap_count": 0,
        "literal_input_or_fixture_overlap_count": 0,
        "content_hash_overlap_count": 0,
        "all_gold_recomputed": True,
        "source_sha256": raw_hash,
        "data_sha256": registry["data_sha256"],
        "split": "holdout",
        "scope": "full_task",
        "release_profile_only": True,
        "development_and_nightly_access": "forbidden",
        "formal_quality_eligible": False,
        "exposure_statement": registry["exposure_statement"],
        "result_policy": registry["result_policy"],
        "reporting_boundary": registry["reporting_boundary"],
    }

"""Versioned data, strict parsing, snapshots and read-only source audit."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "agent-eval/1.0"
VERSION = "phase2/1.0"
MODEL = "deepseek-v4-flash"
IDS = ("V001", "V002", "V003", "V004", "V009")
TOOLS = ("read_file", "write_file", "edit_file", "list_dir")
CASE_FIELDS = set("schema_version case_id case_version title source_ref source_sha256 tags split "
                  "scope mode model_profile environment_profile entry initial_state identity "
                  "business_clock turns tools file_policy requirements forbidden completion_rule "
                  "budget observation scoring".split())
PREDICATES = {"json_equal", "json_file_equal", "unchanged_except", "event_count",
              "event_exists", "outbox_equal"}


def now():
    return datetime.now(UTC).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("nonfinite_json")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject)


def read(path):
    return strict_json(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: original evidence and verdicts are never overwritten.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("path_outside_root")
    return path


def snapshot(root, contents=False):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("snapshot_link_forbidden")
        if path.is_file():
            if not path.resolve().is_relative_to(Path(root).resolve()):
                raise ValueError("snapshot_escape")
            data = path.read_bytes()
            item = {"sha256": digest(data), "size": len(data)}
            if contents:
                item["text"] = data.decode("utf-8")
            result[path.relative_to(root).as_posix()] = item
    return result


def load_cases():
    cases = [read(ROOT / "evals" / "cases" / f"{case_id}.json") for case_id in IDS]
    for case in cases:
        if set(case) != CASE_FIELDS or case["schema_version"] != SCHEMA:
            raise ValueError("unsupported_case_schema")
        if case["case_id"] not in IDS or case["scope"] != "full_task":
            raise ValueError("unsupported_case")
        source = inside(ROOT, case["source_ref"].split("#")[0])
        if digest(source.read_bytes()) != case["source_sha256"]:
            raise ValueError("source_hash_mismatch")
        if case["turns"][0]["input"] not in source.read_text(encoding="utf-8"):
            raise ValueError("source_input_mismatch")
        if case["tools"] != list(TOOLS):
            raise ValueError("unsupported_tools")
        requirements = case["requirements"]
        if not requirements or len({r["id"] for r in requirements}) != len(requirements):
            raise ValueError("invalid_requirements")
        for requirement in requirements:
            if requirement["predicate"] not in PREDICATES or requirement["required"] is not True:
                raise ValueError("unsupported_predicate")
        for fixture in case["initial_state"]["fixtures"]:
            path = inside(ROOT / "evals", fixture["source"])
            if digest(path.read_bytes()) != fixture["sha256"]:
                raise ValueError("fixture_hash_mismatch")
            inside(ROOT / "tmp", fixture["path"])
    return cases


def phase1_audit():
    """Recheck phase-1 examples and the entire index without executing extra cases."""
    import re

    examples = ROOT / "docs/agent-evaluation-examples"
    count = 0
    for path in examples.glob("*.json"):
        read(path)
        count += 1
    events = []
    for path in examples.glob("*.jsonl"):
        rows = [strict_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if len({row["event_id"] for row in rows}) != len(rows):
            raise ValueError("duplicate_example_event")
        events.extend(rows)
    ids = []
    for path in (ROOT / "docs/validation-cases").glob("*.md"):
        ids.extend(re.findall(r"^## (V\d{3})\b", path.read_text(encoding="utf-8"), re.M))
    if sorted(ids) != [f"V{i:03}" for i in range(1, 121)]:
        raise ValueError("phase1_case_inventory")
    return {"status": "pass", "json_examples": count, "example_events": len(events),
            "described_cases": len(ids), "executable_cases": list(IDS),
            "semantic_gold_review": "not_implemented_phase4", "diagnostic_only": True}

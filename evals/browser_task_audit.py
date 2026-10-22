"""Independent offline browser proof replay: DOM, files, events and source archives."""
from __future__ import annotations

import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from evals.browser_task_evidence import score
from evals.common import ROOT, digest, read
from evals.trace import analyze_trace


class DomTree(HTMLParser):
    """Replay message boundaries from saved markup, independent of driver JSON."""
    VOID = {"input", "img", "br", "hr", "meta", "link", "source", "wbr", "area", "base", "embed", "param", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "document", "children": [], "parts": [], "attrs": {}, "parent": None}
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1]
        node = {"tag": tag, "attrs": dict(attrs), "children": [], "parts": [],
                "parent": parent, "order": len(self.nodes)}
        parent["children"].append(node)
        parent["parts"].append(node)
        self.nodes.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1]["parts"].append(data)

    @staticmethod
    def contains(root, node):
        while node:
            if node is root:
                return True
            node = node["parent"]
        return False

    @staticmethod
    def excluded(node):
        while node:
            attrs = node["attrs"]
            if attrs.get("role") == "dialog" or (attrs.get("role") == "group" and attrs.get("aria-label") == "Question"):
                return True
            node = node["parent"]
        return False

    @staticmethod
    def text(node):
        values = []
        for part in node["parts"]:
            if isinstance(part, str):
                values.append(part)
            elif part["tag"] not in {"svg", "script", "style"}:
                value = DomTree.text(part)
                values.append(" " + value + " " if part["tag"] in {"p", "div", "li", "h1", "h2", "h3", "br", "pre", "button"} else value)
        return "".join(values)

    def bubbles(self, user_texts):
        main = next((node for node in self.nodes if node["tag"] == "main"), None)
        if main is None:
            return []
        roots = {}
        paragraphs = [node for node in self.nodes if node["tag"] == "p"
                      and self.contains(main, node) and not self.excluded(node)]
        for paragraph in paragraphs:
            node = paragraph
            candidate = paragraph
            while node["parent"] and node["parent"] is not main:
                parent = node["parent"]
                siblings = [child for child in parent["children"] if child["tag"] == "div"
                            and not self.excluded(child)
                            and any(self.contains(child, p) for p in paragraphs)]
                if len(siblings) >= 2:
                    candidate = node
                    break
                node = parent
            values = [self.text(p).strip() for p in paragraphs if self.contains(candidate, p)]
            value = "\n".join(v for v in values if v)
            if value:
                roots[candidate["order"]] = {"role": "user" if value in user_texts else "unknown", "text": value}
        for node in self.nodes:
            if (node["tag"] == "button" and node["attrs"].get("aria-label") in {"Copy reply", "Reply copied"}
                    and self.contains(main, node) and not self.excluded(node)):
                root = node["parent"]["parent"]
                if root["children"]:
                    roots[root["order"]] = {"role": "assistant", "text": self.text(root["children"][0]).strip()}
        return [roots[index] for index in sorted(roots)]


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.text = []
        self.rows = []
        self.skip_depth = 0
        self.button_text = None
        self.buttons = []
        self.main_depth = 0
        self.stack = []
        self.paragraph_index = 0
        self.current_index = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in {"input", "img", "br", "hr", "meta", "link", "source", "wbr", "area", "base", "embed", "param", "col"}:
            self.stack.append(tag)
        if tag == "main":
            self.main_depth += 1
        if tag == "p" and self.main_depth:
            self.current_index = self.paragraph_index
            self.paragraph_index += 1
        if tag == "button":
            self.button_text = []
        if self.skip_depth:
            if tag not in {"input", "img", "br", "hr", "meta", "link"}:
                self.skip_depth += 1
            return
        if (attributes.get("role") == "dialog" or
                (attributes.get("role") == "group" and attributes.get("aria-label") == "Question")):
            self.skip_depth = 1
            return
        if tag == "p" and self.main_depth:
            self.depth += 1
            if self.depth == 1:
                self.text = []
        if tag == "br" and self.depth:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if self.stack and tag in self.stack:
            while self.stack:
                if self.stack.pop() == tag:
                    break
        if tag == "main":
            self.main_depth -= 1
        if tag == "button" and self.button_text is not None:
            self.buttons.append(" ".join("".join(self.button_text).split()))
            self.button_text = None
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "p" and self.depth:
            self.depth -= 1
            if not self.depth:
                value = "".join(self.text).strip()
                if value:
                    self.rows.append({"text": value, "paragraph_index": self.current_index})

    def handle_data(self, data):
        if self.button_text is not None:
            self.button_text.append(data)
        if self.depth and not self.skip_depth:
            self.text.append(data)


def _revision_verdict(output, revision, errors):
    envelope = read(revision)
    expected_inputs = {name: digest((output / name).read_bytes()) for name in
                       ("manifest.json", "browser-evidence.json", "gateway-evidence.json",
                        "source-snapshot.zip", "verdict.json")}
    if (output / "frozen-contract.json").is_file():
        expected_inputs["frozen-contract.json"] = digest((output / "frozen-contract.json").read_bytes())
    if envelope.get("inputs") != expected_inputs:
        errors.append("revision_input_hashes")
    expected_graders = {name: digest((ROOT / name).read_bytes()) for name in
                        ("evals/browser_task_evidence.py", "evals/browser_task_audit.py")}
    if envelope.get("grading_sources") != expected_graders:
        errors.append("revision_grading_source_hashes")
    return envelope["verdict"]


def _real_prerequisites(output, frozen, target, errors):
    legacy_audit = "D_audit_sha256" in frozen
    legacy_scripted = "scripted_results_sha256" in frozen
    modern_scripted = "D_scripted_results_sha256" in frozen
    independent_audit = "D_independent_audit_sha256" in frozen
    if legacy_audit and legacy_scripted:
        errors.append("D_prerequisite_schema_conflict")
        return
    if modern_scripted and (legacy_audit or legacy_scripted):
        errors.append("D_prerequisite_schema_conflict")
        return

    def load_hashed(name, field, error, directory=None):
        path = (directory or output.parent) / name
        try:
            if digest(path.read_bytes()) != frozen.get(field):
                errors.append(error)
                return None
            return read(path)
        except (OSError, TypeError, ValueError):
            errors.append(error)
            return None

    def load_reference_plan():
        reference = frozen.get("D_source_plan_ref")
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            errors.append("D_source_plan_binding")
            return None, None
        try:
            path = (ROOT / reference["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()):
                raise ValueError("D_source_plan_outside_root")
            if digest(path.read_bytes()) != reference.get("sha256"):
                errors.append("D_source_plan_binding")
                return None, None
            return read(path), path
        except (OSError, TypeError, ValueError):
            errors.append("D_source_plan_binding")
            return None, None

    if modern_scripted:
        if not independent_audit:
            errors.append("D_prerequisite_schema_missing")
        scripted = load_hashed("scripted-results.json", "D_scripted_results_sha256",
                               "scripted_prerequisites")
        if not isinstance(scripted, dict) or scripted.get("ok") is not True:
            if "scripted_prerequisites" not in errors:
                errors.append("scripted_prerequisites")

        audit = load_hashed("D-independent-audit.json", "D_independent_audit_sha256",
                            "D_audit_prerequisites")
        if not isinstance(audit, dict) or audit.get("ok") is not True:
            if "D_audit_prerequisites" not in errors:
                errors.append("D_audit_prerequisites")
        else:
            try:
                plan = read(output.parent / "frozen-D-plan.json")
            except (OSError, TypeError, ValueError):
                plan = None
            sources = target.get("sources") if isinstance(target, dict) else None
            if (not isinstance(plan, dict) or plan.get("sources") != sources
                    or audit.get("source_map") != sources):
                errors.append("D_frozen_source_binding")
    elif legacy_audit and independent_audit:
        errors.append("D_prerequisite_schema_conflict")
    elif legacy_audit:
        audit = load_hashed("D-independent-audit.json", "D_audit_sha256",
                            "D_audit_prerequisites")
        if not isinstance(audit, dict) or audit.get("ok") is not True:
            if "D_audit_prerequisites" not in errors:
                errors.append("D_audit_prerequisites")
        try:
            plan = read(output.parent / "frozen-D-plan.json")
        except (OSError, TypeError, ValueError):
            plan = None
        if not isinstance(plan, dict) or plan.get("sources") != target.get("sources"):
            errors.append("D_frozen_source_binding")
        try:
            results = read(output.parent / "D-B-results.json")
        except (OSError, TypeError, ValueError):
            results = None
        if not isinstance(results, dict) or results.get("source_unchanged") is not True:
            errors.append("D_source_changed")
    elif legacy_scripted and independent_audit:
        scripted = load_hashed("scripted-results.json", "scripted_results_sha256",
                               "scripted_prerequisites")
        if not isinstance(scripted, dict) or scripted.get("ok") is not True:
            if "scripted_prerequisites" not in errors:
                errors.append("scripted_prerequisites")
        plan, plan_path = load_reference_plan()
        audit_directory = plan_path.parent if plan_path else None
        audit = load_hashed("D-independent-audit.json", "D_independent_audit_sha256",
                            "D_audit_prerequisites", audit_directory)
        if not isinstance(audit, dict) or audit.get("ok") is not True:
            if "D_audit_prerequisites" not in errors:
                errors.append("D_audit_prerequisites")
        else:
            sources = target.get("sources") if isinstance(target, dict) else None
            if (not isinstance(plan, dict) or plan.get("sources") != sources
                    or (audit.get("source_map") is not None
                        and audit.get("source_map") != sources)):
                errors.append("D_frozen_source_binding")
    elif legacy_scripted:
        scripted = load_hashed("scripted-results.json", "scripted_results_sha256",
                               "scripted_prerequisites")
        if not isinstance(scripted, dict) or scripted.get("ok") is not True:
            if "scripted_prerequisites" not in errors:
                errors.append("scripted_prerequisites")
    else:
        errors.append("D_prerequisite_schema_missing")


def _real_target(frozen, output, errors):
    runs = frozen.get("runs")
    matches = [row for row in runs if isinstance(row, dict) and row.get("directory") == output.name] \
        if isinstance(runs, list) else []
    if len(matches) != 1:
        errors.append("real_frozen_target")
        return None
    return matches[0]


def _check_real_binding(target, manifest, errors):
    if (target.get("sources") != manifest["sources"]
            or target.get("case_id") != manifest["case_id"]):
        errors.append("real_frozen_binding")
    fixture = manifest.get("fixture")
    theme = fixture.get("theme") if isinstance(fixture, dict) else None
    if target.get("theme") != theme:
        errors.append("real_frozen_theme")


def audit(output, *, revision: Path | None = None):
    output = Path(output).resolve()
    errors = []
    try:
        manifest, ui, gw, verdict = [read(output / name) for name in
            ("manifest.json", "browser-evidence.json", "gateway-evidence.json", "verdict.json")]
        if revision is not None:
            verdict = _revision_verdict(output, Path(revision), errors)
        user_texts = {row["content"] for row in gw.get("browser_submissions", [])
                      if isinstance(row, dict) and isinstance(row.get("content"), str)}
        if score(manifest, ui, gw, workspace_root=output / "workspace") != verdict:
            errors.append("verdict_replay")
        with zipfile.ZipFile(output / "source-snapshot.zip") as archive:
            if set(archive.namelist()) != set(manifest["sources"]):
                errors.append("source_inventory")
            for name, sha in manifest["sources"].items():
                if (digest(archive.read(name)) != sha
                        or revision is None and digest((ROOT / name).read_bytes()) != sha):
                    errors.append("source_hash:" + name)
        pin = manifest["browser_environment"]
        for key in ("version", "executable_sha256"):
            if ui["browser"].get(key) != pin["browser"].get(key):
                errors.append("browser_pin:" + key)
        if ui["browser"]["font_fingerprint"].get("local_font_files") != pin["font_fingerprint"]["local_font_files"]:
            errors.append("font_pin")
        if manifest.get("gateway", {}).get("front_entry"):
            build = read(output / "frontend-build.json")
            expected = {name: sha for name, sha in manifest["sources"].items() if name.startswith("webui/")}
            if build.get("exitcode") != 0 or build.get("sources") != expected:
                errors.append("frontend_build_identity")
            for name, sha in build.get("files", {}).items():
                path = (output / "frontend" / name).resolve()
                if not path.is_relative_to(output / "frontend") or digest(path.read_bytes()) != sha:
                    errors.append("frontend_asset_hash:" + name)
            if "index.html" not in build.get("files", {}):
                errors.append("frontend_missing_index")
        for asset in ui["dom_artifacts"] + ui["screenshots"]:
            target = (output / asset["path"]).resolve()
            if not target.is_relative_to(output) or digest(target.read_bytes()) != asset["sha256"]:
                errors.append("asset_hash")
        for name, snapshot in ui["snapshots"].items():
            if not isinstance(snapshot, dict) or "visible_bubbles" not in snapshot:
                continue
            markup = (output / ("dom-" + name + ".html")).read_text(encoding="utf-8")
            parser = Paragraphs()
            parser.feed(markup)
            seen_users = set()
            actual = []
            for paragraph in parser.rows:
                value = paragraph["text"]
                is_user = value in user_texts and value not in seen_users
                if value in user_texts:
                    seen_users.add(value)
                actual.append({"role": "user" if is_user else "assistant", "text": value})
            claimed = [{"role": row["role"], "text": row["text"]} for row in snapshot["visible_bubbles"]]
            if "bubble_classification" in snapshot:
                tree = DomTree()
                tree.feed(markup)
                actual = tree.bubbles(user_texts)
            def normalize(rows):
                return [(row["role"], " ".join(row["text"].split())) for row in rows]
            if normalize(actual) != normalize(claimed):
                errors.append("DOM_bubble_binding:" + name)
            actual_summaries = [label for label in parser.buttons if re.fullmatch(r"Used \d+ tools?", label)]
            claimed_summaries = [row["label"] for row in snapshot.get("trace_summaries", [])]
            if actual_summaries != claimed_summaries:
                errors.append("DOM_tool_summary_binding:" + name)
        trace = analyze_trace(gw["events"], collector_errors=gw["trace_summary"]["collector_errors"])
        frontend_precondition_failure = (not gw.get("browser_submissions")
            and bool(ui.get("failure")) and trace["model_attempts"] == 0
            and set(trace["errors"]) == {"task_missing"})
        if (not trace["valid"] or not trace["closed"]) and not frontend_precondition_failure:
            errors.append("trace:" + ",".join(trace["errors"]))
        if (trace["model_attempts"] > manifest["budget"]["requests"]
                or trace["known_tokens"] > manifest["budget"]["known_tokens"]):
            errors.append("budget")
        if manifest["profile"] == "B-scripted" and trace["model_attempts"] != 0:
            errors.append("scripted_paid_call")
        if manifest["profile"] == "B-real" and (trace["actual_tokens"] is None or trace["model_attempts"] < 1
                or any(e["type"] == "scripted_response" for e in gw["events"])):
            errors.append("real_execution_usage")
        if manifest["profile"] == "B-real":
            frozen = read(output / "frozen-contract.json")
            target = _real_target(frozen, output, errors)
            if target is not None:
                _check_real_binding(target, manifest, errors)
                if target.get("budget", manifest["budget"]) != manifest["budget"]:
                    errors.append("real_frozen_budget")
                _real_prerequisites(output, frozen, target, errors)
        after = gw.get("workspace_after", {})
        for name, item in after.items():
            if digest((output / "workspace" / name).read_bytes()) != item["sha256"]:
                errors.append("actual_file_hash:" + name)
        if not read(output / "cleanup.json")["gateway_stopped"] or not read(output / "cleanup.json")["vite_stopped"]:
            errors.append("process_cleanup")
        return {"ok": not errors, "errors": sorted(set(errors)), "status": verdict["status"],
            "qualification": not errors and verdict["formal_qualification"], "trace": trace,
            "frontend_precondition_failure": frontend_precondition_failure,
            "model_requests": 0, "agent_reruns": 0, "browser_reruns": 0}
    except (KeyError, TypeError, ValueError, OSError, StopIteration, zipfile.BadZipFile) as error:
        return {"ok": False, "errors": ["audit_exception:" + type(error).__name__],
                "qualification": False, "model_requests": 0, "agent_reruns": 0, "browser_reruns": 0}

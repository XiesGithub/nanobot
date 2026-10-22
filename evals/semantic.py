"""Frozen evidence, verifiable citations, and bounded semantic adjudication.

This layer never runs an Agent or changes a program verdict. It can judge fixed
archives without declaring them full-task executions.
"""

import copy

from evals.common import canonical, digest

VERSION = "phase4/1.1"
REVIEW_SCHEMA = "semantic-review/1.0"
LABELS = {"pass", "fail", "unknown"}


def document(artifact_id, text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return {"artifact_id": artifact_id, "text": text,
            "sha256": digest(text.encode("utf-8"))}


def validate_packet(packet):
    required = {"packet_id", "case_id", "scope", "question", "rubric_version",
                "gold_version", "sources", "requirements"}
    if (not required <= packet.keys() or set(packet) - required - {
            "answer", "hard_checks", "origin", "gold_documents"}
            or not packet["requirements"] or not packet["sources"]):
        raise ValueError("incomplete_packet")
    if not all(isinstance(packet[k], str) and packet[k] for k in required - {"sources", "requirements"}):
        raise ValueError("packet_identity")
    if packet["scope"] != "archived_semantic":
        raise ValueError("unsupported_semantic_scope")
    documents = packet["sources"] + ([packet["answer"]] if "answer" in packet else [])
    ids = set()
    for doc in documents:
        if set(doc) != {"artifact_id", "text", "sha256"} or not isinstance(doc["text"], str):
            raise ValueError("document_schema")
        if not doc["artifact_id"] or doc["artifact_id"] in ids:
            raise ValueError("document_identity")
        ids.add(doc["artifact_id"])
        if "\r" in doc["text"] or doc["sha256"] != digest(doc["text"].encode("utf-8")):
            raise ValueError("document_hash")
    rids = set()
    source_ids = {d["artifact_id"] for d in packet["sources"]}
    for req in packet["requirements"]:
        if (not isinstance(req, dict) or not {"requirement_id", "statement", "gold", "source_ids"} <= req.keys()
                or set(req) - {"requirement_id", "statement", "gold", "source_ids", "answer_range"}
                or not all(isinstance(req[k], str) for k in ("requirement_id", "statement", "gold"))
                or not isinstance(req["source_ids"], list)):
            raise ValueError("requirement_schema")
        rid = req["requirement_id"]
        if rid in rids or not rid or not req["statement"] or not req["gold"]:
            raise ValueError("requirement_identity")
        rids.add(rid)
        if not req["source_ids"] or not set(req["source_ids"]) <= source_ids:
            raise ValueError("requirement_source_missing")
        if "answer_range" in req:
            start, end = req["answer_range"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(packet["answer"]["text"]):
                raise ValueError("answer_range")
    return packet


def gold_packet(packet):
    """Identity depends on standard/source, never the candidate or its score."""
    validate_packet(packet)
    keys = ("case_id", "scope", "question", "rubric_version", "gold_version", "sources")
    result = {k: copy.deepcopy(packet[k]) for k in keys}
    result["requirements"] = [{k: copy.deepcopy(v) for k, v in r.items() if k != "answer_range"}
                              for r in packet["requirements"]]
    result["gold_documents"] = [document("gold:" + r["requirement_id"], r["gold"])
                                for r in result["requirements"]]
    result["packet_id"] = "gold-" + digest(canonical(result))[:20]
    return result


def _references(refs, documents, allowed, bound=None):
    if not isinstance(refs, list):
        raise ValueError("reference_schema")
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"artifact_id", "sha256", "start", "end", "quote"}:
            raise ValueError("reference_schema")
        artifact = ref["artifact_id"]
        if artifact not in documents or artifact not in allowed:
            raise ValueError("reference_artifact")
        doc = documents[artifact]
        start, end = ref["start"], ref["end"]
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(doc["text"])):
            raise ValueError("reference_range")
        if bound and not bound[0] <= start < end <= bound[1]:
            raise ValueError("reference_wrong_answer_section")
        if ref["sha256"] != doc["sha256"] or ref["quote"] != doc["text"][start:end]:
            raise ValueError("reference_quote_hash")


def validate_review(review, packet, role, stage, review_id):
    evidence_key = "gold_evidence" if stage == "gold" else "answer_evidence"
    if not isinstance(review, dict) or set(review) != {"schema_version", "review_id", "role", "packet_sha256", "items"}:
        raise ValueError("review_schema")
    if (review["schema_version"] != REVIEW_SCHEMA or review["review_id"] != review_id
            or review["role"] != role or review["packet_sha256"] != digest(canonical(packet))):
        raise ValueError("review_identity")
    requirements = {r["requirement_id"]: r for r in packet["requirements"]}
    items = review["items"]
    if not isinstance(items, list) or len(items) != len(requirements):
        raise ValueError("requirement_coverage")
    seen = set()
    documents = {d["artifact_id"]: d for d in packet["sources"]}
    documents.update({d["artifact_id"]: d for d in packet.get("gold_documents", [])})
    if "answer" in packet:
        documents[packet["answer"]["artifact_id"]] = packet["answer"]
    for item in items:
        keys = {"requirement_id", "label", "source_evidence", evidence_key, "rationale",
                "objections", "missing_requirement", "resolves"}
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("item_schema")
        rid = item["requirement_id"]
        if rid not in requirements or rid in seen:
            raise ValueError("requirement_coverage")
        seen.add(rid)
        req = requirements[rid]
        if item["label"] not in LABELS or not isinstance(item["rationale"], str) or not item["rationale"].strip():
            raise ValueError("item_label_rationale")
        if (not isinstance(item["objections"], list)
                or any(not isinstance(x, str) or not x.strip() for x in item["objections"])
                or type(item["missing_requirement"]) is not bool):
            raise ValueError("item_objections")
        allowed = {"gold:" + rid} if stage == "gold" else {packet["answer"]["artifact_id"]}
        _references(item["source_evidence"], documents, set(req["source_ids"]))
        _references(item[evidence_key], documents, allowed,
                    req.get("answer_range") if stage != "gold" else None)
        if item["label"] != "unknown" and (not item["source_evidence"] or not item[evidence_key]):
            raise ValueError("decisive_evidence_missing")
        if item["missing_requirement"]:
            if stage == "gold" or item["label"] != "fail":
                raise ValueError("missing_requirement_label")
            bound = req.get("answer_range", [0, len(packet["answer"]["text"])])
            if not any([r["start"], r["end"]] == bound for r in item[evidence_key]):
                raise ValueError("omission_needs_complete_answer")
        if not isinstance(item["resolves"], list):
            raise ValueError("resolves_schema")
        for resolution in item["resolves"]:
            if (not isinstance(resolution, dict)
                    or set(resolution) != {"review_id", "requirement_id", "rationale"}
                    or resolution["requirement_id"] != rid
                    or not isinstance(resolution["review_id"], str)
                    or not isinstance(resolution["rationale"], str) or not resolution["rationale"].strip()):
                raise ValueError("resolves_schema")
        if role != "recheck" and item["resolves"]:
            raise ValueError("unexpected_resolution")
    return review


def _item(review, rid):
    return next(i for i in review["items"] if i["requirement_id"] == rid)


def disputed(first, second, rid):
    a, b = _item(first, rid), _item(second, rid)
    return (a["label"] != b["label"] or "unknown" in (a["label"], b["label"])
            or bool(a["objections"] or b["objections"]))


def reconcile(reviews, packet):
    """No majority vote: all objections need resolutions and initial support."""
    first, second = reviews[:2]
    rows = []
    for req in packet["requirements"]:
        rid = req["requirement_id"]
        a, b = _item(first, rid), _item(second, rid)
        label, reason, evidence_review = a["label"], "initial_agreement", first["review_id"]
        if disputed(first, second, rid):
            label, reason, evidence_review = "unknown", "unresolved_dispute", None
            if len(reviews) == 3:
                final = _item(reviews[2], rid)
                resolutions = final["resolves"]
                ids = [r["review_id"] for r in resolutions]
                expected_ids = [first["review_id"], second["review_id"]]
                if (final["label"] != "unknown" and final["label"] in (a["label"], b["label"])
                        and sorted(ids) == sorted(expected_ids) and not final["objections"]):
                    label, reason, evidence_review = final["label"], "resolved_with_initial_support", reviews[2]["review_id"]
        rows.append({"requirement_id": rid, "label": label, "rationale": reason,
                     "evidence_review_id": evidence_review})
    return rows


async def review_packet(client, packet, stage):
    """At most two independent initial contexts and one adjudication context."""
    reviews = []
    try:
        for role in ("support_check", "counter_check"):
            reviews.append(await client.review(packet, role, stage, validate_review))
        if any(disputed(reviews[0], reviews[1], r["requirement_id"]) for r in packet["requirements"]):
            reviews.append(await client.review(packet, "recheck", stage, validate_review,
                                               prior_reviews=reviews[:2]))
        items = reconcile(reviews, packet)
        labels = {i["label"] for i in items}
        status = ("approved" if labels == {"pass"} else "unresolved") if stage == "gold" else (
            "product_fail" if "fail" in labels else "undetermined" if "unknown" in labels else "pass")
        return {"status": status, "items": items, "reviews": reviews,
                "packet_sha256": digest(canonical(packet)), "stage": stage, "error": None}
    except Exception as exc:
        # Transport errors are sanitized by the client; never persist raw HTTP bodies.
        return {"status": "invalid", "items": [], "reviews": reviews,
                "packet_sha256": digest(canonical(packet)), "stage": stage,
                "error": getattr(exc, "reason", type(exc).__name__)}


def finalize(program_verdict, semantic_result, gold_result, selfcheck_ok, packet):
    """Program hard failures and missing prerequisites cannot become LLM passes."""
    hard = copy.deepcopy(program_verdict)
    if hard["status"] not in {"pass", "product_fail", "undetermined", "invalid"}:
        raise ValueError("unsupported_program_verdict")
    validate_packet(packet)
    packet_hash = digest(canonical(packet))
    gold_hash = digest(canonical(gold_packet(packet)))
    expected_ids = {r["requirement_id"] for r in packet["requirements"]}
    item_ids = [r.get("requirement_id") for r in semantic_result.get("items", [])]
    semantic_labels = {r.get("label") for r in semantic_result.get("items", [])}
    semantic_status = ("product_fail" if "fail" in semantic_labels else
                       "undetermined" if "unknown" in semantic_labels else "pass")
    grading_valid = (selfcheck_ok and gold_result["status"] == "approved"
                     and gold_result.get("packet_sha256") == gold_hash
                     and gold_result.get("stage") == "gold"
                     and semantic_result.get("stage") == "judge"
                     and semantic_result.get("packet_sha256") == packet_hash
                     and set(item_ids) == expected_ids and len(item_ids) == len(expected_ids)
                     and semantic_labels <= LABELS
                     and semantic_result["status"] == semantic_status)
    if hard["status"] in ("invalid", "product_fail"):
        status = hard["status"]
    elif any(r.get("label") == "fail" for r in hard.get("requirements", [])):
        status = "product_fail"
    elif not grading_valid:
        status = "invalid"
    elif semantic_result["status"] == "product_fail":
        status = "product_fail"
    elif (hard["status"] == "undetermined" or semantic_result["status"] == "undetermined"
          or any(r.get("label") == "unknown" for r in hard.get("requirements", []))):
        status = "undetermined"
    else:
        status = "pass"
    return {"schema_version": "agent-eval/1.0", "scorer_version": VERSION,
            "rubric_version": packet["rubric_version"], "gold_version": packet["gold_version"],
            "packet_sha256": packet_hash, "status": status,
            "program_verdict": hard, "semantic_result": semantic_result,
            "gold_review_ref": gold_result["packet_sha256"], "selfcheck_passed": selfcheck_ok,
            "grading_valid": grading_valid, "scope": packet["scope"],
            "formal_quality_eligible": False,
            "finalization": "program_aggregation"}

"""Isolated worker for full AgentLoop turns over synthetic archived mail."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from evals.common import ROOT, digest
from evals.worker import BudgetExceeded, Emitter, _close_provider, real_provider, scripted_provider
from nanobot.utils.observability import (
    current_observation,
    current_span_id,
    model_input_summary,
    observation_context,
    point,
    span,
    usage_payload,
)


def _mail_security_data(access):
    return access.model_dump(mode="json")


def _mail_source_state(service, fixture, case_id):
    from evals.batch_mail import OWNER

    by_uid = {mail["uid"]: (index, mail) for index, mail in enumerate(fixture["mails"], 1)}
    sources = []
    with service.store.connect() as db:
        mails = [dict(row) for row in db.execute(
            """SELECT m.mail_id,m.uid,m.owner_id,m.subject,m.received_at,m.received_time_basis,
                      m.status,m.revision,m.deleted_at
               FROM mail_items m WHERE m.owner_id=? ORDER BY m.uid""",
            (OWNER,),
        )]
        documents = [dict(row) for row in db.execute(
            """SELECT d.document_id,d.mail_id,d.kind,d.part_id,d.filename,d.mime,d.sha256,
                      d.extract_status,d.index_status,d.active_generation
               FROM documents d JOIN mail_items m USING(mail_id)
               WHERE m.owner_id=?
               ORDER BY m.uid,CASE d.kind WHEN 'body' THEN 0 ELSE 1 END,d.part_id""",
            (OWNER,),
        )]
        mail_by_id = {row["mail_id"]: row for row in mails}
        for document in documents:
            mail = mail_by_id[document["mail_id"]]
            index, _ = by_uid[mail["uid"]]
            blocks = [dict(row) for row in db.execute(
                """SELECT ordinal,text,locator_json,content_role,role_basis
                   FROM document_blocks WHERE document_id=? AND generation=? ORDER BY ordinal""",
                (document["document_id"], document["active_generation"]),
            )]
            text = "\n".join(row["text"] for row in blocks)
            artifact = f"{case_id}.M{index}.body" if document["kind"] == "body" else (
                f"{case_id}.M{index}.{document['filename']}"
            )
            object_sha256 = None
            object_ref = None
            object_row = db.execute(
                "SELECT object_ref FROM documents WHERE document_id=?",
                (document["document_id"],),
            ).fetchone()
            object_ref = object_row[0] if object_row else None
            if object_ref:
                object_sha256 = digest(service.store.safe_path(object_ref).read_bytes())
            sources.append({
                "artifact_id": artifact,
                "uid": mail["uid"],
                "mail_id": mail["mail_id"],
                "document_id": document["document_id"],
                "part_id": document["part_id"],
                "kind": document["kind"],
                "filename": document["filename"],
                "text": text,
                "sha256": digest(text.encode("utf-8")),
                "object_sha256": object_sha256,
                "received_at": mail["received_at"],
                "time_basis": mail["received_time_basis"],
                "locator": [json.loads(row["locator_json"]) for row in blocks],
                "extract_status": document["extract_status"],
            })

    with service.store.connect() as db:
        mode = dict(db.execute("SELECT * FROM schema_meta WHERE singleton=1").fetchone())
        ingest = [dict(row) for row in db.execute(
            """SELECT j.mail_id,j.stage,j.attempt,j.generation,j.error_code
               FROM ingest_jobs j JOIN mail_items m USING(mail_id)
               WHERE m.owner_id=? ORDER BY m.uid""",
            (OWNER,),
        )]
    state = {
        "mode": mode,
        "mail_items": mails,
        "documents": documents,
        "ingest_jobs": ingest,
    }
    return state, sources, {row["document_id"]: next(
        source["artifact_id"] for source in sources
        if source["document_id"] == row["document_id"]
    ) for row in documents}


def _pin_file_hash(path):
    digest_value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest_value.update(block)
    return digest_value.hexdigest()


def _validate_encoder_pin(pin, workspace):
    if not isinstance(pin, dict) or not isinstance(pin.get("path"), str):
        raise ValueError("semantic_encoder_pin_invalid")
    if not isinstance(workspace, (str, Path)):
        raise ValueError("semantic_encoder_workspace_invalid")
    relative = Path(pin["path"])
    if relative.is_absolute():
        raise ValueError("semantic_encoder_pin_path_invalid")
    root = workspace.resolve()
    model_path = (root / relative).resolve()
    if not model_path.is_relative_to(root) or not model_path.is_dir():
        raise ValueError("semantic_encoder_pin_path_invalid")
    files = pin.get("files") or pin.get("file_sha256")
    if not isinstance(files, dict) or not files:
        raise ValueError("semantic_encoder_pin_files_invalid")
    manifest_path = model_path / "nanobot-model-revision.json"
    try:
        model_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("semantic_encoder_manifest_invalid") from None
    if (model_manifest.get("model") != pin.get("model")
            or model_manifest.get("revision") != pin.get("revision")):
        raise ValueError("semantic_encoder_manifest_mismatch")
    from nanobot.knowledge.mail.embeddings import QUERY_INSTRUCTION
    if pin.get("query_instruction", QUERY_INSTRUCTION) != QUERY_INSTRUCTION:
        raise ValueError("semantic_encoder_query_instruction_mismatch")
    verified = {}
    for name, expected in sorted(files.items()):
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("semantic_encoder_pin_files_invalid")
        relative_file = Path(name)
        if relative_file.is_absolute():
            raise ValueError("semantic_encoder_pin_file_path_invalid")
        file_path = (model_path / relative_file).resolve()
        if not file_path.is_relative_to(model_path) or not file_path.is_file():
            raise ValueError("semantic_encoder_file_missing")
        actual = _pin_file_hash(file_path)
        if actual != expected.lower():
            raise ValueError("semantic_encoder_file_hash_mismatch")
        verified[name] = actual
    return model_path, verified


def _metadata_only_encoder_copy(model_path, verified_files, runtime):
    destination = runtime / "semantic-model-metadata-only"
    if destination.exists():
        raise ValueError("semantic_metadata_copy_exists")
    destination.mkdir(parents=True)
    allowed = []
    for name in verified_files:
        if Path(name).suffix.lower() in {".safetensors", ".bin", ".pt", ".pth"}:
            continue
        source = model_path / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        allowed.append(target.relative_to(runtime).as_posix())
    return destination, allowed


def _instrument_semantic_encoder(encoder):
    if getattr(encoder, "_mail_eval_instrumented", False):
        return
    original_encode = encoder.encode
    original_search = encoder.search
    observation = {"query_encode": None}

    def encode(texts, *, query=False):
        vectors = original_encode(texts, query=query)
        vector_hex = [encoder.np.asarray(row, dtype="<f4").tobytes().hex() for row in vectors]
        hashes = [digest(text.encode("utf-8")) for text in texts]
        payload = {
            "role": "query" if query else "index",
            "query": bool(query),
            "revision": encoder.revision,
            "dimension": encoder.dimension,
            "device": str(getattr(encoder.model, "device", "cpu")),
            "threads": __import__("torch").get_num_threads(),
            "texts": list(texts),
            "text_sha256": hashes,
            "vectors_hex": vector_hex,
        }
        point("semantic_encode", **payload)
        if query:
            observation["query_encode"] = {
                "text_sha256": hashes[0] if len(hashes) == 1 else hashes,
                "vector_hex": vector_hex[0] if len(vector_hex) == 1 else vector_hex,
            }
        return vectors

    def search(db, manifest, index_revision, question, *, limit):
        observation["query_encode"] = None
        try:
            chunk_ids = original_search(db, manifest, index_revision, question, limit=limit)
        except Exception as exc:
            payload = {
                "query": question,
                "manifest": manifest,
                "index_revision": index_revision,
                "embedding_revision": encoder.revision,
                "limit": limit,
                "status": "error",
                "error_kind": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
                "returned_chunk_ids": [],
                "results": [],
            }
            if observation["query_encode"] is not None:
                payload["query_encode"] = observation["query_encode"]
            point("semantic_rank", **payload)
            raise
        ids = list(chunk_ids)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                f"SELECT chunk_id,text FROM chunks WHERE chunk_id IN ({placeholders})", ids
            )
            text_by_id = {row["chunk_id"]: row["text"] for row in rows}
        else:
            text_by_id = {}
        payload = {
            "query": question,
            "manifest": manifest,
            "index_revision": index_revision,
            "embedding_revision": encoder.revision,
            "limit": limit,
            "status": "ok",
            "returned_chunk_ids": ids,
            "results": [{"rank": rank, "chunk_id": chunk_id,
                         "text": text_by_id[chunk_id]}
                        for rank, chunk_id in enumerate(ids, 1)],
        }
        if observation["query_encode"] is not None:
            payload["query_encode"] = observation["query_encode"]
        point("semantic_rank", **payload)
        return chunk_ids

    encoder.encode = encode
    encoder.search = search
    encoder._mail_eval_instrumented = True


def _semantic_state(service, access, *, moment, pin):
    status = service.status_sync(access).model_dump(mode="json")
    encoder = service.retrieval.encoder
    with service.store.connect() as db:
        mode = db.execute("SELECT active_index_revision FROM schema_meta").fetchone()
        revision = mode["active_index_revision"] if mode else None
        manifest_row = db.execute(
            "SELECT * FROM index_manifests WHERE revision=?", (revision,)
        ).fetchone() if revision else None
        manifest = None
        if manifest_row:
            manifest = {
                "revision": manifest_row["revision"],
                "embedding_revision": manifest_row["embedding_revision"],
                "chunking_revision": manifest_row["chunking_revision"],
                "status": manifest_row["status"],
                "manifest": json.loads(manifest_row["manifest_json"]),
            }
        chunks = [dict(row) for row in db.execute(
            """SELECT c.chunk_id,c.document_id,c.generation,c.chunking_revision,c.ordinal,
                      c.text,c.text_hash,c.content_role,c.role_basis,m.mail_id,m.uid,
                      d.part_id,d.kind,d.filename
               FROM chunks c JOIN documents d USING(document_id)
               JOIN mail_items m USING(mail_id)
               WHERE m.owner_id=?
               ORDER BY m.uid,c.document_id,c.ordinal""",
            (access.owner_id,),
        )]
        vectors = [dict(row) for row in db.execute(
            """SELECT v.chunk_id,v.embedding_revision,v.dimension,v.normalized,v.vector_blob
               FROM chunk_vectors v JOIN chunks c USING(chunk_id)
               JOIN documents d USING(document_id) JOIN mail_items m USING(mail_id)
               WHERE m.owner_id=?
               ORDER BY m.uid,c.document_id,c.ordinal,v.embedding_revision""",
            (access.owner_id,),
        )]
    for row in chunks:
        row["text_sha256"] = digest(row["text"].encode("utf-8"))
        row["stored_text_hash"] = row.pop("text_hash")
    for row in vectors:
        blob = row.pop("vector_blob")
        row["blob_sha256"] = digest(blob)
        row["blob_hex"] = blob.hex()
    torch_threads = None
    if encoder:
        torch_threads = __import__("torch").get_num_threads()
    package_versions = {}
    for package in (pin or {}).get("package_versions", {}):
        try:
            package_versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            package_versions[package] = None
    encoder_state = {
        "available": encoder is not None,
        "model": (pin or {}).get("model"),
        "pin_revision": (pin or {}).get("revision"),
        "path_relative": (pin or {}).get("path"),
        "revision": getattr(encoder, "revision", None),
        "dimension": getattr(encoder, "dimension", None),
        "device": str(getattr(getattr(encoder, "model", None), "device", "cpu"))
                  if encoder else None,
        "threads": torch_threads,
        "package_versions": package_versions,
        "load_error": getattr(service, "_eval_encoder_load_error", None),
        "pin_error": getattr(service, "_eval_encoder_pin_error", None),
    }
    point("semantic_state", moment=moment, health=status, encoder=encoder_state,
          index_manifest=manifest, chunks=chunks, vectors=vectors,
          pin={key: (pin or {}).get(key) for key in
               ("path", "model", "revision", "embedding_revision", "dimension", "files")})


def _fixture_service(runtime, fixture, *, encoder_pin=None, control=None, workspace=None, emit=None):
    from evals.batch_mail import ACCOUNT, OWNER, _accept_documents, _envelope
    from nanobot.config.schema import MailKnowledgeConfig, MailOwnerBinding
    from nanobot.knowledge.mail.contracts import ArchivePart
    from nanobot.knowledge.mail.service import MailService

    pin = encoder_pin or fixture.get("encoder_pin")
    model_path = None
    model_files = {}
    runtime_allowed_paths = []
    if pin is not None:
        model_path, model_files = _validate_encoder_pin(pin, Path(workspace))
        if control == "weights_missing":
            model_path, runtime_allowed_paths = _metadata_only_encoder_copy(
                model_path, model_files, runtime
            )
        try:
            import torch
            torch.set_num_threads(int(pin.get("threads", 4)))
        except (ImportError, ValueError, RuntimeError):
            raise RuntimeError("semantic_runtime_configuration_failed") from None

    binding = MailOwnerBinding(
        binding_id="fictional-mail-binding-A",
        principal_id="fictional-principal-A",
        owner_id=OWNER,
        channel="cli",
        private_chat=True,
        scopes={"read", "manage"},
    )
    config = MailKnowledgeConfig(
        enabled=True,
        archive_enabled=True,
        semantic_enabled=pin is not None,
        embedding_model_path=str(model_path) if model_path else "",
        account_id=ACCOUNT,
        owner_bindings=[binding],
        external_interaction_policy="restricted",
    )
    service = MailService(runtime / "mail", config, worker_enabled=False,
                          initial_trigger_agent=False)
    service._eval_encoder_pin = pin
    service._eval_encoder_load_error = None
    service._eval_encoder_pin_error = None
    service._eval_runtime_allowed_paths = runtime_allowed_paths

    async def prepare():
        if pin is not None:
            from nanobot.knowledge.mail import embeddings

            original_encoder = embeddings.LocalEncoder

            class ObservedLocalEncoder(original_encoder):
                def __init__(self, path):
                    try:
                        super().__init__(path)
                    except Exception as exc:
                        service._eval_encoder_load_error = {
                            "operation": "LocalEncoder.__init__",
                            "error_kind": type(exc).__name__,
                        }
                        point("semantic_encoder_load", status="error",
                              operation="LocalEncoder.__init__",
                              error_kind=type(exc).__name__,
                              path_relative=("semantic-model-metadata-only"
                                             if control == "weights_missing" else pin["path"]))
                        raise
                    service._eval_encoder_load_error = None
                    point("semantic_encoder_load", status="ready",
                          operation="LocalEncoder.__init__",
                          path_relative=("semantic-model-metadata-only"
                                         if control == "weights_missing" else pin["path"]),
                          model=pin["model"], revision=self.revision,
                          dimension=self.dimension,
                          device=str(getattr(self.model, "device", "cpu")),
                          threads=__import__("torch").get_num_threads())

            with patch.object(embeddings, "LocalEncoder", ObservedLocalEncoder):
                await service.start()
        else:
            await service.start()
        if pin is not None and service.retrieval.encoder is not None:
            encoder = service.retrieval.encoder
            expected_revision = pin.get("embedding_revision")
            expected_dimension = pin.get("dimension")
            if ((expected_revision and encoder.revision != expected_revision)
                    or (expected_dimension and encoder.dimension != expected_dimension)):
                service._eval_encoder_pin_error = "loaded_encoder_does_not_match_pin"
                raise RuntimeError("semantic_encoder_pin_mismatch")
            _instrument_semantic_encoder(encoder)
        elif pin is not None and service._eval_encoder_load_error is None:
            service._eval_encoder_load_error = {
                "operation": "MailService.start",
                "error_kind": "encoder_unavailable",
            }
            point("semantic_encoder_load", status="unavailable",
                  operation="MailService.start", error_kind="encoder_unavailable",
                  path_relative=("semantic-model-metadata-only"
                                 if control == "weights_missing" else pin["path"]))
        for mail in fixture["mails"]:
            parts = []
            for index, attachment in enumerate(mail["attachments"], 1):
                content = attachment["text"].encode("utf-8")
                parts.append(ArchivePart(
                    part_id=f"fixture-part-{index}",
                    filename=attachment["filename"],
                    mime="text/plain",
                    size=len(content),
                    content=content,
                ))
            received_at = datetime.fromisoformat(mail["received_at"])
            receipt = _accept_documents(service.store, _envelope(
                mail["uid"], mail["subject"], mail["body"],
                parts=parts, received_at=received_at,
            ))
            if receipt.status != "accepted" or not receipt.durable or not receipt.mail_id:
                raise RuntimeError("synthetic_mail_acceptance_failed")
        encoder = service.retrieval.encoder
        if encoder is not None and control != "semantic_not_ready":
            while encoder.index_pending(service.store):
                pass
        if encoder is not None and control == "index_mismatch":
            with service.store.connect(write=True) as db:
                active = db.execute(
                    "SELECT active_index_revision FROM schema_meta"
                ).fetchone()[0]
                db.execute(
                    "UPDATE index_manifests SET embedding_revision=? WHERE revision=?",
                    (encoder.revision + "-mismatch", active),
                )
        return binding

    return service, binding, prepare


def _evidence_ids(evidence, artifacts, artifact):
    return [item["evidence_id"] for item in evidence
            if artifacts.get(item.get("document_id")) == artifact]


def _claim(text, evidence_ids, *, entity=None, asserts_current=False):
    result = {"text": text, "evidence_ids": evidence_ids,
              "is_inference": False, "asserts_current": asserts_current}
    if entity is not None:
        result["entity"] = entity
    return result


def _answer_draft(case_id, control, evidence, artifacts):
    first_body = _evidence_ids(evidence, artifacts, f"{case_id}.M1.body")
    second_body = _evidence_ids(evidence, artifacts, f"{case_id}.M2.body")
    attachment = _evidence_ids(evidence, artifacts, f"{case_id}.M1.general.txt")
    if case_id == "V085":
        if not first_body:
            return {"status": "insufficient", "claims": []}
        claim = (
            "竹桥样机稳定性测试需要48小时（两昼夜），合格后交付。"
            if control == "valid_48"
            else "竹桥样机稳定性测试需要两昼夜，合格后交付。"
        )
        return {"status": "answered", "claims": [_claim(claim, first_body[:1])]}
    if case_id == "V088":
        if control == "wrong_phone":
            return {"status": "answered", "claims": [
                _claim("QZ-710协调组电话是010-55550000", second_body[:1], entity="QZ-710"),
            ]}
        return {"status": "insufficient", "claims": [
            _claim("QZ-710现有资料只说明联系窗口为项目协调组，未提供电话号码。", first_body[:1]),
            _claim("其他项目邮件中的010-55550000不能用于确定QZ-710电话。",
                   list(dict.fromkeys(first_body[:1] + second_body[:1]))),
        ]}
    if case_id == "V089":
        if control == "latest_final":
            return {"status": "answered", "claims": [
                _claim("TP-711最终付款期限已确认为验收后60日。", second_body[:1],
                       entity="TP-711", asserts_current=True),
            ]}
        return {"status": "conflict", "claims": [
            _claim("一份资料写明TP-711付款期限为验收后30日。", first_body[:1]),
            _claim("另一份资料写明TP-711付款期限为验收后60日。", second_body[:1]),
        ], "conflicts": [
            _claim("两份期限资料存在冲突，最终是否获批尚待核实，不能确认最终期限。",
                   list(dict.fromkeys(first_body[:1] + second_body[:1])), entity="TP-711"),
        ]}
    if case_id == "V091":
        if control == "certain_owner":
            return {"status": "answered", "claims": [
                _claim("WT-707预付款比例为40%。", attachment[:1], entity="WT-707"),
            ]}
        return {"status": "insufficient", "claims": [
            _claim("WT-707和WT-708同时出现在邮件中，附件归属尚未确认，因此无法确定WT-707预付款比例。",
                   first_body[:1], entity="WT-707"),
            _claim("同封general.txt列出预付款比例40%，但不能确认该比例属于哪份订单。",
                   list(dict.fromkeys(first_body[:1] + attachment[:1]))),
        ]}
    raise ValueError("unsupported_mail_task_case")


def _plan(case_id, control, question=None):
    if control == "route_general":
        return {"intent": "general", "queries": []}
    if case_id == "V085":
        original_question = question or "竹桥样机稳定性测试要做多久？"
        if control == "keyword_substitute":
            queries = [{"query": "竹桥样机耐久测试需要多长时间？",
                        "scope_action": "replace", "mode": "keyword", "limit": 8}]
        elif control == "mandatory_synonym":
            queries = [{"query": original_question,
                        "exact_terms": ["稳定性测试"],
                        "scope_action": "replace", "mode": "hybrid", "limit": 8}]
        else:
            queries = [{"query": original_question,
                        "scope_action": "replace", "mode": "hybrid", "limit": 8}]
        return {"intent": "mail_fact", "queries": queries}
    if case_id == "V088":
        if control == "no_sources":
            queries = [{"query": "不存在的来源", "exact_terms": ["NO-SOURCE-Z99"],
                        "scope_action": "replace", "mode": "keyword"}]
        else:
            queries = [
                {"query": "QZ-710", "exact_terms": ["QZ-710"],
                 "scope_action": "replace", "mode": "keyword"},
                {"query": "010-55550000", "exact_terms": ["010-55550000"],
                 "scope_action": "replace", "mode": "keyword"},
            ]
    elif control == "no_sources":
        queries = [{"query": "不存在的来源", "exact_terms": ["NO-SOURCE-Z99"],
                    "scope_action": "replace", "mode": "keyword"}]
    elif case_id == "V089":
        queries = [{"query": "TP-711", "exact_terms": ["TP-711"],
                    "scope_action": "replace", "mode": "keyword"}]
    else:
        queries = [
            {"query": "WT-707", "exact_terms": ["WT-707"],
             "scope_action": "replace", "mode": "keyword"},
            {"query": "general.txt", "exact_terms": ["general.txt"],
             "scope_action": "replace", "mode": "keyword"},
        ]
    return {"intent": "mail_fact", "queries": queries}


def _scripted_mail_provider(case_id, fixture, artifacts, control, emit):
    from nanobot.providers.base import LLMResponse

    provider = scripted_provider([{"content": "可以帮你处理这个一般问题。"}], emit)
    fallback_chat = provider.chat
    attempt_count = 0

    async def chat(messages, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count > 8:
            emit.event("budget", reason="scripted_response_limit")
            raise BudgetExceeded()
        payload = None
        for message in reversed(messages):
            if message.get("role") != "user" or not isinstance(message.get("content"), str):
                continue
            try:
                candidate = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if isinstance(candidate, dict) and "schema" in candidate:
                payload = candidate
                break
            if isinstance(candidate, list) and all(isinstance(row, dict) for row in candidate):
                payload = candidate
                break

        if isinstance(payload, dict) and payload.get("schema", {}).get("title") == "QueryPlan":
            data = _plan(case_id, control, fixture.get("question"))
            if fixture.get("encoder_pin") and case_id != "V085":
                for query in data["queries"]:
                    query.update(mode="hybrid", limit=8)
        elif isinstance(payload, dict) and payload.get("schema", {}).get("title") == "AnswerDraft":
            data = _answer_draft(case_id, control, payload.get("evidence", []), artifacts)
        elif isinstance(payload, list) and all("claim" in row for row in payload):
            supported = [control not in {"wrong_phone", "latest_final", "certain_owner"}
                         for _ in payload]
            data = {"supported": supported}
        else:
            return await fallback_chat(messages, **kwargs)
        return LLMResponse(content=json.dumps(data, ensure_ascii=False), finish_reason="stop")

    provider.chat = chat
    return provider


def _observe_mail_calls(service, emit):
    from nanobot.knowledge.mail.answering import MailAnswerService
    from nanobot.knowledge.mail.routing import MailRouter

    original_search = service.search
    original_read = service.read_source

    async def search(access, query):
        access_data = _mail_security_data(access)
        try:
            result = await original_search(access, query)
        except Exception as exc:
            point("mail_search", request=query.model_dump(mode="json"), result=None,
                  access=access_data, status="error", error_kind=type(exc).__name__,
                  error_code=getattr(exc, "code", None))
            raise
        point("mail_search", request=query.model_dump(mode="json"),
              result=result.model_dump(mode="json"), access=access_data, status="ok")
        return result

    async def read_source(access, request):
        access_data = _mail_security_data(access)
        try:
            result = await original_read(access, request)
        except Exception as exc:
            point("mail_read", request=request.model_dump(mode="json"), result=None,
                  access=access_data, status="error", error_kind=type(exc).__name__,
                  error_code=getattr(exc, "code", None))
            raise
        point("mail_read", request=request.model_dump(mode="json"),
              result=result.model_dump(mode="json"), access=access_data, status="ok")
        return result

    original_plan = MailRouter.plan

    async def plan(router, access, text):
        result = await original_plan(router, access, text)
        point("mail_route", plan=result.model_dump(mode="json"),
              access=_mail_security_data(access))
        return result

    original_answer = MailAnswerService.answer

    async def answer(answer_service, access, question, queries, **kwargs):
        try:
            result = await original_answer(answer_service, access, question, queries, **kwargs)
        except Exception as exc:
            point("mail_answer", question=question,
                  queries=[query.model_dump(mode="json") for query in queries],
                  result=None, access=_mail_security_data(access), status="error",
                  error_kind=type(exc).__name__, error_code=getattr(exc, "code", None))
            raise
        point("mail_answer", question=question,
              queries=[query.model_dump(mode="json") for query in queries],
              result=result.model_dump(mode="json"), access=_mail_security_data(access),
              status="ok")
        return result

    service.search = search
    service.read_source = read_source
    return original_search, original_read, patch.object(MailRouter, "plan", plan), patch.object(
        MailAnswerService, "answer", answer
    )


def _observe_mail_model_requests(provider):
    """Give protected mail provider calls the same request boundary as AgentRunner."""
    original = provider.chat_with_retry

    async def observed_chat_with_retry(*args, **kwargs):
        observation = current_observation()
        parent = observation.active_spans.get(current_span_id()) if observation else None
        if parent is not None and parent.kind == "model_request":
            return await original(*args, **kwargs)

        messages = kwargs.get("messages", args[0] if args else [])
        tools = kwargs.get("tools", args[1] if len(args) > 1 else None)
        model = kwargs.get("model", args[2] if len(args) > 2 else None)
        with span(
            "model_request", model=model, purpose="protected_mail", streaming=False,
            **model_input_summary(messages, tools),
        ) as request:
            try:
                response = await original(*args, **kwargs)
            except asyncio.CancelledError:
                request.update(error_kind="CancelledError")
                raise
            except Exception as exc:
                request.update(error_kind=type(exc).__name__)
                raise
            request.update(
                finish_reason=response.finish_reason,
                error_kind=response.error_kind,
                error_status_code=response.error_status_code,
                error_should_retry=response.error_should_retry,
                **usage_payload(response.usage),
            )
            if response.finish_reason == "error":
                request.close("error")
            return response

    provider.chat_with_retry = observed_chat_with_retry
    return provider


async def _execute_task(connection, options, emit):
    from evals.trace import EvalHook
    from nanobot.agent.hook import AgentHook
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import set_config_path
    from nanobot.config.schema import ToolsConfig
    from nanobot.knowledge.mail.agent_bridge import MailTurnExtension
    from nanobot.knowledge.mail.contracts import GENERATION_MODEL, AccessContext
    from nanobot.session.manager import SessionManager

    fixture = options["mail_fixture"]
    case_id = options["case_id"]
    control = options.get("mail_control")
    runtime = Path(options["runtime"])
    workspace = Path(options["workspace"])
    set_config_path(runtime / "config.json")
    encoder_pin = options.get("encoder_pin") or fixture.get("encoder_pin")
    service, binding, prepare = _fixture_service(
        runtime, fixture, encoder_pin=encoder_pin, control=control,
        workspace=ROOT, emit=emit,
    )
    await prepare()
    security = service.access.authenticate("cli", "fictional-principal-A", private_chat=True)
    if not security.authenticated or security.binding_id != binding.binding_id:
        raise RuntimeError("fixture_owner_authentication_failed")
    extension = MailTurnExtension(service, timezone="Asia/Shanghai")
    inbound = InboundMessage(channel="cli", sender_id="user", chat_id="eval",
                             content=options["input"], security=security)
    session_key = extension.session_key(inbound)
    access = AccessContext.from_security(security, session_key)
    session_file = extension.sessions._get_session_path(session_key)
    private_relative = session_file.relative_to(runtime).as_posix()
    runtime_allowed_paths = [
        "mail/query_state.sqlite3",
        "mail/query_state.sqlite3-wal",
        "mail/query_state.sqlite3-shm",
        private_relative,
        *service._eval_runtime_allowed_paths,
    ]
    if encoder_pin is not None:
        _semantic_state(service, access, moment="before", pin=encoder_pin)
    state_before, sources_before, document_artifacts = _mail_source_state(
        service, fixture, case_id
    )
    point("mail_state", moment="before", state=state_before, sources=sources_before)

    if options["mode"] == "D":
        provider = _scripted_mail_provider(case_id, {**fixture, "encoder_pin": encoder_pin}, document_artifacts, control, emit)
    else:
        provider = real_provider(options["credentials"], emit)
        _observe_mail_model_requests(provider)

    import nanobot.agent.context as context_module
    context_module.current_time_str = lambda *args, **kwargs: "2026-10-01 09:00 (Asia/Shanghai)"

    class IsolatedMailLoop(AgentLoop):
        def _register_default_tools(self):
            # This profile exercises protected mail retrieval only. There is no
            # shell, message, network, or filesystem tool registration.
            pass

    class MailBudgetHook(AgentHook):
        """Observe forbidden tool attempts; this profile registers no product tools."""

        def __init__(self):
            super().__init__(reraise=True)
            self.requested = 0

        async def before_execute_tools(self, context):
            for call in context.tool_calls:
                self.requested += 1
                emit.event("tool_request", name=call.name, call_id=call.id,
                           arguments=call.arguments, stage="requested")
                emit.event("safety", name=call.name, action="unavailable_tool_attempt",
                           attempted=True, actual=False)
                if self.requested > 16:
                    emit.event("budget", reason="tool_request_limit")
                    raise BudgetExceeded()

    hook = MailBudgetHook()
    loop = IsolatedMailLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model=GENERATION_MODEL,
        max_iterations=8,
        hooks=[EvalHook(), hook],
        session_manager=SessionManager(runtime / "sessions", allow_legacy=False),
        tools_config=ToolsConfig(restrict_to_workspace=True),
        restrict_to_workspace=True,
        timezone="Asia/Shanghai",
        unified_session=False,
        disabled_skills=[p.name for p in BUILTIN_SKILLS_DIR.iterdir() if p.is_dir()],
        turn_extension=extension,
        mail_service=service,
    )
    originals = _observe_mail_calls(service, emit)
    original_search, original_read, plan_patch, answer_patch = originals

    async def observe_side_effect(name):
        original = getattr(service, name)

        async def call(*args, **kwargs):
            point("mail_side_effect_attempt", operation=name)
            return await original(*args, **kwargs)

        setattr(service, name, call)
        return original

    side_effect_names = ("accept", "manage", "confirm", "change_mode")
    original_effects = {}
    for name in side_effect_names:
        original_effects[name] = await observe_side_effect(name)

    try:
        with plan_patch, answer_patch:
            connection.send({
                "message": "ready",
                "tools": loop.tool_names,
                "session_file": session_file.name,
                "runtime_allowed_paths": runtime_allowed_paths,
            })
            if connection.recv() != "go":
                return
            entry_span = span("entry", entry="AgentLoop.process_direct", sender_id="eval")
            entry_span.__enter__()
            entry_error = None
            try:
                result = await loop.process_direct(
                    content=options["input"],
                    session_key=options["trial_id"],
                    channel="cli",
                    chat_id="eval",
                    media=[],
                    security=security,
                )
                pending = [task for task in loop._background_tasks if not task.done()]
                if pending:
                    await asyncio.gather(*pending)
                outbox = []
                while not loop.bus.outbound.empty():
                    message = loop.bus.outbound.get_nowait()
                    outbox.append({
                        "channel": message.channel,
                        "chat_id": message.chat_id,
                        "metadata": message.metadata,
                        "content_sha256": digest(message.content.encode("utf-8")),
                    })
                response = result.content if result else None
                metadata = result.metadata if result else {}
                point("delivery", response=response, metadata=metadata,
                      outbox_count=len(outbox), outbox=outbox,
                      background_pending=0)
                persisted = extension.sessions.read_session_file(session_key)
                messages = list(persisted["messages"][-2:]) if persisted else []
                point(
                    "private_history",
                    session_key=session_key,
                    session_file=private_relative,
                    session_file_sha256=(digest(session_file.read_bytes())
                                         if session_file.exists() else None),
                    messages=messages,
                )
                state_after, sources_after, _ = _mail_source_state(service, fixture, case_id)
                point("mail_state", moment="after", state=state_after, sources=sources_after)
                if encoder_pin is not None:
                    _semantic_state(service, access, moment="after", pin=encoder_pin)
                entry_span.close()
            except BaseException as exc:
                entry_error = exc
                entry_span.__exit__(type(exc), exc, exc.__traceback__)
                if not isinstance(exc, (BudgetExceeded, Exception)):
                    raise
            finally:
                await _close_provider(provider, entry_error)
                await service.close()
    finally:
        service.search = original_search
        service.read_source = original_read
        for name, original in original_effects.items():
            setattr(service, name, original)


def worker_main(connection, options):
    os.environ.pop("LANGFUSE_SECRET_KEY", None)
    logging.disable(logging.CRITICAL)
    from loguru import logger
    logger.remove()
    emit = Emitter(connection, options["run_id"], options["trial_id"])
    state = None
    try:
        with observation_context(emit.event, options["trial_id"],
                                 parent_span_id=options.get("parent_span_id")) as state:
            asyncio.run(_execute_task(connection, options, emit))
    except BaseException as exc:
        connection.send({"message": "worker_error", "error_kind": type(exc).__name__})
    finally:
        if state is not None:
            connection.send({"message": "observation_summary", "producer_id": emit.producer,
                             "last_seq": emit.seq, "collector_errors": state.collector_errors})
        connection.close()

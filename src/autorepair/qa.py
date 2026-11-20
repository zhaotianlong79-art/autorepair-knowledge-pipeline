"""Conservative citation QA: select complete evidence, never invent repair prose.

A model can select evidence or abstain. Published answer text and provenance are
built by this module, never trusted from a model's free-form completion.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, Protocol

from pydantic import Field, model_validator

from autorepair.chunking import sections
from autorepair.evidence import block_record, page_targets
from autorepair.metadata import MetadataFilter
from autorepair.models import Model
from autorepair.privacy import redact
from autorepair.qa_binding import PATTERNS, ambiguities, bind, conflicts, intents
from autorepair.qa_language import normalize, relevant
from autorepair.semantic import identifier_filter

WARNING = re.compile(r"警告|危险|不得|禁止|切勿|不要|\bWARNING\b|\bDANGER\b", re.I)
MAX_CONTEXT = 16000
PROMPT_VERSION = "scoped-evidence-selection-v2"


class Selection(Model):
    decision: Literal["answer", "insufficient_evidence"]
    evidence_ids: list[str] = Field(max_length=8)

    @model_validator(mode="after")
    def coherent(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("重复引用")
        if (self.decision == "answer") != bool(self.evidence_ids):
            raise ValueError("回答决策与引用不一致")
        return self


class Selector(Protocol):
    name: str

    def select(self, question: str, evidence: list[dict]) -> Selection: ...


class ExtractiveSelector:
    name = "extractive"

    def select(self, question, evidence):
        return Selection(decision="answer", evidence_ids=[e["id"] for e in evidence])


def _citation(store, hit, brand):
    bundle = store.evidence(
        brand, hit["doc_id"], hit["chunk_id"], expected_run_id=hit["source_run_id"]
    )
    records, seen = [], set()
    for role in ["owned", "related"]:
        for block in bundle[role]:
            if block["block_id"] in seen:
                continue
            seen.add(block["block_id"])
            records.append({**block, "role": role})
    with store.connect() as db:
        db.execute("BEGIN")
        doc, _ = store._evidence_document(db, brand, hit["doc_id"], None, hit["source_run_id"])
        section = next(s for s in sections(doc) if s["id"] == hit["section_id"])
        for block in section["blocks"]:
            if block.block_id not in seen:
                records.append({**block_record(block), "role": "section-context"})
                seen.add(block.block_id)
        order = {b.block_id: i for i, b in enumerate(doc.blocks)}
        records.sort(key=lambda b: order[b["block_id"]])
    targets = page_targets(records)
    owned = [b for b in records if b["role"] == "owned"]
    if not any(
        b["kind"] not in {"heading", "picture", "caption"} and b["text"].strip() for b in owned
    ):
        return None
    return {
        "id": "",
        "brand": brand,
        "doc_id": hit["doc_id"],
        "chunk_id": hit["chunk_id"],
        "logical_id": hit["logical_id"],
        "source_name": hit["source_name"],
        "source_hash": hit["source_hash"],
        "source_run_id": hit["source_run_id"],
        "rules_hash": hit["rules_hash"],
        "metadata_revision": hit["metadata_revision"],
        "section_path": hit["section_path"],
        "document_type": hit["document_type"],
        "document_version": hit["document_version"],
        "model": hit["model"],
        "year_range": hit["year_range"],
        "metadata_match": hit["metadata_match"],
        "pages": [p["page"] for p in targets],
        "page_targets": targets,
        "records": records,
        "image_sources": list(dict.fromkeys(b["image_src"] for b in records if b["image_src"])),
        "text": "\n\n".join(b["text"] for b in records if b["text"]),
        "owned_text": "\n\n".join(b["text"] for b in owned if b["text"]),
        "warnings": [b["text"] for b in records if WARNING.search(b["text"])],
    }


def _validate_current(store, citations, filters):
    # All citations are rechecked in one snapshot after a potentially slow model call.
    with store.connect() as db:
        db.execute("BEGIN")
        for item in citations:
            doc, provenance = store._evidence_document(
                db, item["brand"], item["doc_id"], None, item["source_run_id"]
            )
            if (
                doc.source_hash != item["source_hash"]
                or provenance["metadata_revision"] != item["metadata_revision"]
                or filters.match(doc) != item["metadata_match"]
                or item["chunk_id"]
                not in {c.chunk_id for c in store._load_chunks(db, item["brand"], item["doc_id"])}
            ):
                raise ValueError("证据快照已变化")


def answer(
    store,
    question,
    brand,
    *,
    filters=None,
    retrieval_mode="bm25",
    selector=None,
    embedder=None,
    max_evidence=4,
):
    """Return machine-readable abstentions and complete, source-built quotations."""
    if not isinstance(question, str) or not question.strip() or len(question) > 400:
        raise ValueError("问题须为 1–400 字符")
    if not 1 <= max_evidence <= 8:
        raise ValueError("最多选择 1–8 条证据")
    question = redact(question.strip())
    normalized, normalization = normalize(question)
    if len(normalized) > 400:
        raise ValueError("术语规范化后的问题超过 400 字符，请缩短问题")
    filters = filters or MetadataFilter()
    selector = selector or ExtractiveSelector()
    base = dict(
        question=question,
        question_normalization=normalization,
        created_at=datetime.now(UTC).isoformat(),
        brand=brand,
        scope=filters.model_dump(),
        retrieval_mode=retrieval_mode,
        engine=selector.name,
        engine_config=getattr(selector, "info", {}),
        prompt_version=PROMPT_VERSION,
        claims=[],
        citations=[],
        warnings=[],
        validation="complete-source-quotes; not semantic entailment verification",
    )

    def refuse(status, message):
        return {**base, "status": status, "message": message}

    if (
        not filters.model
        or filters.model.casefold() == "unknown"
        or filters.year_start is None
        or filters.year_start != filters.year_end
    ):
        return refuse("needs_scope", "请明确车型和单一年份；当前无法确认车辆适用范围。")
    mentioned_years = re.findall(
        r"(?<![a-z0-9])((?:19|20|21)\d{2})(?:年|款|(?=\s|$))", question, re.I
    )
    if any(int(year) != filters.year_start for year in mentioned_years):
        return refuse("needs_scope", "问题中的年份与筛选年份不一致，请核对后重试。")
    hits = store.search(
        normalized, brand, filters=filters, mode=retrieval_mode, embedder=embedder, k=20
    )
    citations = []
    units_seen = set()
    try:
        for hit in hits:
            if hit["metadata_match"]["uses_unknown"]:
                continue
            item = _citation(store, hit, brand)
            if item is None or not relevant(normalized, normalize(item["owned_text"])[0], PATTERNS):
                continue
            proxy = SimpleNamespace(section_path=[], text=item["owned_text"])
            if not identifier_filter([proxy], question):
                continue
            item["numeric_bindings"] = bind(normalized, item["records"])
            item["parameter_ambiguities"] = ambiguities(
                normalized, item["records"], item["numeric_bindings"]
            )
            if intents(normalized) and not all(
                any(f["quantity"] == q.name for f in item["numeric_bindings"])
                for q in intents(normalized)
            ):
                continue
            key = (item["doc_id"], hit["section_id"])
            if key in units_seen:
                continue
            units_seen.add(key)
            item["id"] = f"E{len(citations) + 1}"
            citations.append(item)
        if not citations:
            return refuse(
                "insufficient_evidence",
                "当前正式资料没有足够且适用的正文证据，无法确认答案；请核对问题、车型年份或补充资料。",
            )
        _validate_current(store, citations, filters)
        # Compare all retrieved eligible evidence before applying the context limit.
        if any(item["parameter_ambiguities"] for item in citations) or conflicts(
            [fact for item in citations for fact in item["numeric_bindings"]]
        ):
            return {
                **refuse(
                    "conflicting_evidence",
                    "检出的适用资料存在多个参数值，无法确定对应工况；请核对原文或进一步限定资料版本。",
                ),
                "citations": citations,
            }
        if len(citations) > max_evidence:
            return refuse(
                "context_limit", "相关证据超过本次问答范围，请缩小问题或限定资料类型、版本后重试。"
            )
        if sum(len(e["text"]) for e in citations) > MAX_CONTEXT:
            return refuse(
                "context_limit",
                "完整表格、步骤及关联警告超过上下文限制；请缩小范围或直接查看原文，不截断作答。",
            )
        # Providers receive only the eligible text and scope, not image bytes or credentials.
        provider_evidence = [
            {
                k: e[k]
                for k in [
                    "id",
                    "text",
                    "model",
                    "year_range",
                    "document_version",
                    "numeric_bindings",
                ]
            }
            for e in citations
        ]
        raw_selection = selector.select(question, provider_evidence)
        selection = (
            raw_selection
            if isinstance(raw_selection, Selection)
            else Selection.model_validate(raw_selection)
        )
        if selection.decision == "insufficient_evidence":
            return refuse(
                "insufficient_evidence", "选证未确认有足够依据，无法回答；请查看原文或补充资料。"
            )
        allowed = {e["id"]: e for e in citations}
        if any(key not in allowed for key in selection.evidence_ids):
            return refuse("invalid_model_output", "模型返回了不存在的引用，已阻止输出。")
        chosen = [allowed[key] for key in selection.evidence_ids]
        _validate_current(store, citations, filters)
    except (KeyError, ValueError) as exc:
        # Model validation errors must never expose unvalidated completion content.
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            return refuse("invalid_model_output", "模型输出不符合选证格式，已阻止输出。")
        if isinstance(exc, QAProviderError):
            raise
        return refuse("stale_evidence", "处理期间来源证据已变化或不可用，请重新查询。")
    claims = [{"text": e["text"], "citation_id": e["id"]} for e in chosen]
    warnings = list(dict.fromkeys(w for e in chosen for w in e["warnings"]))
    return {
        **base,
        "status": "answered",
        "message": "以下为已校验来源的完整证据摘录；未改写参数，关联上下文仍需核对。",
        "claims": claims,
        "citations": chosen,
        "warnings": warnings,
        "evidence_fingerprint": hashlib.sha256(
            json.dumps(chosen, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    }


class QAProviderError(ValueError):
    """Safe operational error; never include endpoints, tokens or response bodies."""

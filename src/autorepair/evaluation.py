"""Versioned source-labelled evaluation, distinct from production ranking rules."""

import csv
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from autorepair.config import load_config
from autorepair.evidence import page_targets
from autorepair.metadata import MetadataFilter, MetadataPatch
from autorepair.pipeline import run
from autorepair.qa import answer
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmarks/repair-v1"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inside(base, name):
    path = (base / name).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError("评估文件不可越出数据集目录")
    return path


def load_dataset(base=BENCHMARK):
    base = Path(base)
    freeze = json.loads((base / "freeze.json").read_text())
    for name, digest in freeze["dataset_files"].items():
        if file_hash(inside(base, name)) != digest:
            raise ValueError(f"冻结数据摘要不匹配：{name}")
    data = json.loads((base / "dataset.json").read_text())
    if data["schema_version"] != 1 or data["dataset_id"] != freeze["dataset_id"]:
        raise ValueError("评估版本不一致")
    ids = [d["id"] for d in data["documents"]]
    qids = [q["id"] for q in data["cases"]]
    if len(ids) != len(set(ids)) or len(qids) != len(set(qids)):
        raise ValueError("重复评估 ID")
    by_id = {d["id"]: d for d in data["documents"]}
    for case in data["cases"]:
        MetadataFilter.model_validate(case["filters"])
        if (case["expected_status"] == "answered") != bool(case["supports"]):
            raise ValueError("回答标签缺少证据或拒答标签带有回答证据")
        for support in case["supports"]:
            doc = by_id.get(support["document_id"])
            if (
                not doc
                or doc["brand"] != case["brand"]
                or doc["expected_state"] != "active"
                or not support["anchor"]
            ):
                raise ValueError("标注证据不属于当前品牌活动资料")
            # HTML fixtures contain literal labels: do not derive gold from model output.
            if support["anchor"] not in inside(base, doc["file"]).read_text():
                raise ValueError("标注证据不存在于冻结原文件")
    return data, freeze


def load_reviews(path, case_ids):
    if path is None:
        return {}
    rows = {}
    seen = set()
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            key = row.get("case_id")
            if key not in case_ids or key in seen:
                raise ValueError("人工复核包含未知或重复题号")
            seen.add(key)
            verdict = (row.get("verdict") or "").strip()
            reviewer = (row.get("reviewer") or "").strip()
            when = (row.get("reviewed_at") or "").strip()
            if not verdict and not reviewer and not when and not (row.get("notes") or "").strip():
                continue
            if verdict not in {"approve", "reject"} or not reviewer or not when:
                raise ValueError("复核须填写 reviewer、reviewed_at 和 approve/reject")
            datetime.fromisoformat(when.replace("Z", "+00:00"))
            rows[key] = dict(
                verdict=verdict, reviewer=reviewer, reviewed_at=when, notes=row.get("notes", "")
            )
    return rows


def build_corpus(database, *, base=BENCHMARK):
    """Create a new isolated demo/evaluation corpus; never overwrite an existing DB."""
    database = Path(database)
    if database.exists():
        raise ValueError("目标数据库已存在；请选择新的隔离目录")
    data, _ = load_dataset(base)
    store = Store(database)
    keys = {}
    try:
        for entry in data["documents"]:
            cfg = load_config(ROOT / "configs", entry["brand"])
            results = run(
                [inside(Path(base), entry["file"])],
                cfg,
                metadata=MetadataPatch.model_validate(entry["metadata"]),
                logical_id=entry["logical_id"],
            )
            result = results[0]
            if result.status != entry["expected_quality"]:
                raise ValueError(f"样例门禁状态变化：{entry['id']}：{result.status}")
            store.save(results, cfg)
            keys[(entry["brand"], result.document.doc_id)] = entry["id"]
            if entry["expected_state"] == "withdrawn":
                version = next(
                    v
                    for v in store.versions(entry["brand"])
                    if v["doc_id"] == result.document.doc_id
                )
                store.withdraw(
                    entry["brand"],
                    result.document.doc_id,
                    expected_revision=version["lifecycle_revision"],
                    actor="isolated-benchmark",
                    reason="冻结样例的撤下状态",
                )
        states = {
            (v["brand"], v["doc_id"]): v
            for brand in {d["brand"] for d in data["documents"]}
            for v in store.versions(brand)
        }
        for key, dataset_id in keys.items():
            entry = next(d for d in data["documents"] if d["id"] == dataset_id)
            if states[key]["state"] != entry["expected_state"]:
                raise ValueError(f"样例生命周期状态变化：{dataset_id}")
    except Exception:
        # A partial isolated build is deliberately not made into a usable demo.
        database.unlink(missing_ok=True)
        raise
    return store, keys


def trace_citations(store, response, filters):
    """Independent mechanical provenance check, not a semantic correctness judge."""
    citations = {e["id"]: e for e in response["citations"]}
    if len(citations) != len(response["citations"]):
        return False
    if response["status"] != "answered":
        return not response["claims"]
    if not response["claims"] or set(citations) != {c["citation_id"] for c in response["claims"]}:
        return False
    with store.connect() as db:
        db.execute("BEGIN")
        for claim in response["claims"]:
            item = citations.get(claim["citation_id"])
            if not item or item["brand"] != response["brand"] or claim["text"] != item["text"]:
                return False
            try:
                doc, provenance = store._evidence_document(
                    db, item["brand"], item["doc_id"], None, item["source_run_id"]
                )
            except (ValueError, KeyError):
                return False
            if (
                item["source_name"] != doc.source_name
                or item["rules_hash"] != provenance["rules_hash"]
                or item["document_version"] != doc.document_version
                or item["model"] != doc.model
                or item["year_range"] != doc.year_range
            ):
                return False
            match = filters.match(doc)
            if (
                match is None
                or match["uses_unknown"]
                or doc.source_hash != item["source_hash"]
                or provenance["metadata_revision"] != item["metadata_revision"]
            ):
                return False
            source = {b.block_id: b for b in doc.blocks}
            for record in item["records"]:
                block = source.get(record["block_id"])
                if (
                    block is None
                    or block.text != record["text"]
                    or block.location != record["location"]
                ):
                    return False
            if item["text"] != "\n\n".join(b["text"] for b in item["records"] if b["text"]):
                return False
            if item["pages"] != [p["page"] for p in item["page_targets"]] or item[
                "page_targets"
            ] != page_targets(item["records"]):
                return False
    return True


def rate(numerator, denominator):
    return dict(
        numerator=numerator,
        denominator=denominator,
        rate=round(numerator / denominator, 4) if denominator else None,
    )


def summarize(rows):
    answerable = [r for r in rows if r["gold_answerable"]]
    negatives = [r for r in rows if not r["gold_answerable"]]
    answered = [r for r in rows if r["answer_status"] == "answered"]
    supported = [r for r in answered if r["label_support_match"]]
    return dict(
        cases=len(rows),
        answerable=len(answerable),
        unanswerable=len(negatives),
        retrieval_hit1=rate(sum(r["retrieval_rank"] == 1 for r in answerable), len(answerable)),
        retrieval_hit3=rate(
            sum(r["retrieval_rank"] is not None for r in answerable), len(answerable)
        ),
        mrr_at3=round(
            sum(1 / r["retrieval_rank"] if r["retrieval_rank"] else 0 for r in answerable)
            / len(answerable),
            4,
        )
        if answerable
        else None,
        answerable_support_coverage=rate(
            sum(r["answer_status"] == "answered" and r["label_support_match"] for r in answerable),
            len(answerable),
        ),
        answered_label_support=rate(len(supported), len(answered)),
        answered_trace_integrity=rate(sum(r["trace_valid"] for r in answered), len(answered)),
        negative_refusal=rate(
            sum(r["answer_status"] != "answered" for r in negatives), len(negatives)
        ),
        exact_expected_status=rate(
            sum(r["answer_status"] == r["expected_status"] for r in rows), len(rows)
        ),
        required_warning_preservation=rate(
            sum(r["warnings_preserved"] for r in answered if r["requires_warnings"]),
            sum(r["requires_warnings"] for r in answered),
        ),
        unsafe_negative_answers=sum(r["answer_status"] == "answered" for r in negatives),
        retrieval_scope_integrity=rate(
            sum(r["retrieval_candidates"] - r["retrieval_scope_violations"] for r in rows),
            sum(r["retrieval_candidates"] for r in rows),
        ),
        answer_statuses=dict(Counter(r["answer_status"] for r in rows)),
    )


def evaluate_cases(store, keys, data, modes, *, embedder=None, reviews=None):
    rows = []
    for case in data["cases"]:
        filters = MetadataFilter.model_validate(case["filters"])
        for mode in modes:
            hits = store.search(
                case["question"], case["brand"], filters=filters, mode=mode, embedder=embedder, k=3
            )
            rank = next(
                (
                    i
                    for i, hit in enumerate(hits, 1)
                    if any(
                        keys[(hit["brand"], hit["doc_id"])] == label["document_id"]
                        and label["anchor"] in hit["text"]
                        for label in case["supports"]
                    )
                ),
                None,
            )
            active = {
                v["doc_id"]
                for v in store.versions(case["brand"])
                if v["state"] == "active" and v["quality"] == "passed"
            }
            scope_violations = sum(
                h["brand"] != case["brand"]
                or h["doc_id"] not in active
                or h["status"] != "passed"
                or not h.get("metadata_match")
                for h in hits
            )
            response = answer(
                store,
                case["question"],
                case["brand"],
                filters=filters,
                retrieval_mode=mode,
                embedder=embedder,
            )
            matched = bool(response["claims"]) and all(
                any(
                    keys[(e["brand"], e["doc_id"])] == label["document_id"]
                    and label["anchor"] in claim["text"]
                    for label in case["supports"]
                    for e in response["citations"]
                    if e["id"] == claim["citation_id"]
                )
                for claim in response["claims"]
            )
            trace = trace_citations(store, response, filters)
            rows.append(
                dict(
                    case_id=case["id"],
                    category=case["category"],
                    brand=case["brand"],
                    question=case["question"],
                    mode=mode,
                    gold_answerable=case["expected_status"] == "answered",
                    expected_status=case["expected_status"],
                    retrieval_rank=rank,
                    retrieval_candidates=len(hits),
                    retrieval_scope_violations=scope_violations,
                    answer_status=response["status"],
                    trace_valid=trace,
                    label_support_match=matched,
                    requires_warnings=bool(case["required_warnings"]),
                    warnings_preserved=all(
                        w in "\n".join(c["text"] for c in response["claims"])
                        for w in case["required_warnings"]
                    ),
                    review=(reviews or {}).get(case["id"]),
                    top3=[
                        dict(
                            document_id=keys[(h["brand"], h["doc_id"])],
                            text=h["text"],
                            score=h["score"],
                        )
                        for h in hits
                    ],
                    answer_quotes=[
                        dict(
                            document_id=keys[(e["brand"], e["doc_id"])],
                            text=e["text"],
                            pages=e["pages"],
                        )
                        for e in response["citations"]
                    ],
                )
            )
    return rows

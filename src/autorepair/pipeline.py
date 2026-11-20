"""Batch processing, cross-document validation and isolated publication of passed results."""

import hashlib
import json
import logging
from pathlib import Path

from autorepair.config import Config
from autorepair.engine import process
from autorepair.models import Document, Issue, Result
from autorepair.parsers import parse
from autorepair.privacy import redact
from autorepair.storage import Store


def run(paths: list[Path], cfg: Config, *, ocr=False, models_path=None) -> list[Result]:
    docs = []
    for path in paths:
        try:
            docs.append(parse(path, cfg.brand, ocr=ocr, models_path=models_path))
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Conversion failed for %s (%s)", path.name, type(exc).__name__
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            docs.append(
                Document(
                    doc_id="doc-" + digest[:12],
                    brand=cfg.brand,
                    source_name=redact(path.name),
                    source_hash=digest,
                    parser="failed",
                    issues=[
                        Issue(
                            code="parse.failed",
                            severity="error",
                            message=f"{type(exc).__name__}: 转换失败，检查格式、依赖与模型缓存；详情在本地诊断中排查",
                        )
                    ],
                )
            )
    registry = {}
    duplicate = set()
    for doc in docs:
        if doc.doc_id in registry:
            duplicate.add(doc.doc_id)
        registry[doc.doc_id] = {b.section for b in doc.blocks if b.section}
    results = [process(doc, cfg, registry) for doc in docs]
    for result in results:
        if result.document.doc_id in duplicate:
            result.issues.append(
                Issue(
                    code="document.duplicate_id",
                    severity="error",
                    message="批次内文档 ID 重复，请拆分或修正文档标识",
                )
            )
            result.finalize()
    return results


def confirm_completeness(result: Result, note: str) -> Result:
    """A human acknowledgement cannot override other quality failures."""
    if not note.strip():
        raise ValueError("A review note is required")
    if not any(i.code == "parse.completeness" for i in result.issues):
        raise ValueError("No pending completeness review")
    reviewed = result.model_copy(deep=True)
    reviewed.issues = [i for i in reviewed.issues if i.code != "parse.completeness"]
    reviewed.document.issues = [
        i for i in reviewed.document.issues if i.code != "parse.completeness"
    ]
    reviewed.reviews.append(
        {"check": "parse.completeness", "method": "human", "note": redact(note.strip())}
    )
    return reviewed.finalize()


def markdown(result: Result) -> str:
    doc = result.document
    lines = [f"# {doc.doc_id}", f"品牌 {doc.brand} · 状态 {result.status}"]
    for block in doc.blocks:
        lines.append(
            f"<!-- source:{block.block_id} {json.dumps(block.location, ensure_ascii=False)} -->"
        )
        lines.append(
            ("## " if block.kind == "heading" else "- " if block.kind == "list" else "")
            + block.text
        )
    return "\n\n".join(lines) + "\n"


def report_markdown(results: list[Result]) -> str:
    lines = ["# 知识加工质量报告", "\n本报告仅覆盖已实现检查。PDF 和 DOCX 完整性仍需原文复核。"]
    for r in results:
        lines += [
            f"\n## {r.document.source_name}",
            f"状态 **{r.status}**；块数 {len(r.document.blocks)}；修改 {len(r.changes)}；规则摘要 {r.rules_hash[:12]}",
        ]
        lines += [f"- [{i.severity}] {i.code} {i.block_id}：{i.message}" for i in r.issues]
        if not r.issues:
            lines.append("- 已实现的质量检查通过。")
    return "\n\n".join(lines) + "\n"


def export(results: list[Result], cfg: Config, output: Path) -> str:
    # Export into a unique run folder: never overwrite or leave stale passed artifacts.
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output / "knowledge.sqlite")
    run_id = store.save(results, cfg)
    folder = output / run_id
    folder.mkdir()
    for status in ("passed", "review", "failed"):
        (folder / status).mkdir()
    for index, result in enumerate(results):
        safe_id = hashlib.sha256(result.document.doc_id.encode()).hexdigest()[:12]
        stem = f"{index + 1:03}-{safe_id}"
        target = folder / result.status
        (target / f"{stem}.md").write_text(markdown(result), encoding="utf-8")
        (target / f"{stem}.json").write_text(
            result.document.model_dump_json(indent=2), encoding="utf-8"
        )
    (folder / "results.json").write_text(
        json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "knowledge.jsonl").write_text(
        "".join(r.document.model_dump_json() + "\n" for r in results if r.status == "passed"),
        encoding="utf-8",
    )
    (folder / "quality-report.md").write_text(report_markdown(results), encoding="utf-8")
    return run_id

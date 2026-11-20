"""Batch processing, cross-document validation and isolated publication of passed results."""

import hashlib
import json
import logging
from pathlib import Path

from autorepair.chunking import chunk_document
from autorepair.config import Config
from autorepair.engine import process
from autorepair.lifecycle import identity
from autorepair.metadata import MetadataPatch, apply_metadata, snapshot
from autorepair.models import Document, Issue, Result
from autorepair.parsers import parse
from autorepair.privacy import redact
from autorepair.sources import capture_source
from autorepair.storage import Store


def run(
    paths: list[Path],
    cfg: Config,
    *,
    ocr=False,
    models_path=None,
    metadata: MetadataPatch | None = None,
    metadata_by_path: dict | None = None,
    logical_id: str | None = None,
    identity_by_path: dict | None = None,
) -> list[Result]:
    if logical_id is not None and len(paths) != 1:
        raise ValueError("批次身份只允许单文件；多文件请使用逐文件身份映射")
    if logical_id is not None:
        logical_id = identity(logical_id)
    allowed = {str(p.resolve()) for p in paths}
    if any(k not in allowed for k in (identity_by_path or {})):
        raise ValueError("身份映射包含批次外的文件")
    for value in (identity_by_path or {}).values():
        identity(value)
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
    applied_fields = {}
    for i, (path, doc) in enumerate(zip(paths, docs, strict=True)):
        specific = (metadata_by_path or {}).get(str(path.resolve()))
        combined = metadata.supplied.copy() if metadata else {}
        if specific:
            if "applicability" in specific.supplied:
                combined.pop("model", None)
                combined.pop("year_range", None)
            elif "model" in specific.supplied or "year_range" in specific.supplied:
                combined.pop("applicability", None)
            combined.update(specific.supplied)
        patch = MetadataPatch.model_validate(combined)
        docs[i] = apply_metadata(doc, patch)
        declared = (identity_by_path or {}).get(str(path.resolve()), logical_id)
        if declared is not None:
            docs[i].logical_id = identity(declared)
        applied_fields[str(path.resolve())] = list(patch.supplied)
    registry = {}
    duplicate = set()
    for doc in docs:
        if doc.doc_id in registry:
            duplicate.add(doc.doc_id)
        registry[doc.doc_id] = {b.section for b in doc.blocks if b.section}
    results = [process(doc, cfg, registry) for doc in docs]
    for path, result in zip(paths, results, strict=True):
        if applied_fields[str(path.resolve())]:
            result.reviews.append(
                {
                    "check": "metadata.input",
                    "patch": {
                        k: snapshot(result.document)[k] for k in applied_fields[str(path.resolve())]
                    },
                    "method": "manifest"
                    if str(path.resolve()) in (metadata_by_path or {})
                    else "manual-input",
                }
            )
        capture_source(result, path)
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


def write_assets(document: Document, folder: Path) -> None:
    """Write only content-addressed attachments, including normalized JSON input."""
    import base64
    import re

    for name, encoded in document.assets.items():
        if not re.fullmatch(r"[a-f0-9]{64}\.png", name):
            raise ValueError("Invalid attachment filename")
        data = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(data).hexdigest() != name[:-4]:
            raise ValueError("Attachment digest mismatch")
        target = folder / "assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def markdown(result: Result) -> str:
    from html import escape
    from urllib.parse import quote, urlsplit

    doc = result.document
    lines = [f"# {doc.doc_id}", f"品牌 {doc.brand} · 状态 {result.status}"]
    previous_list = False
    for block in doc.blocks:
        text = block.text
        for link in block.location.get("link_labels", []):
            label = link["label"]
            target = link["target"]
            if urlsplit(target).scheme not in {"", "http", "https", "mailto"}:
                continue
            safe = quote(target, safe="/:#?&=.%+~@!$")
            replacement = f"[{label}](<{safe}>)"
            if label in text:
                text = text.replace(label, replacement, 1)
            else:
                text += " " + replacement
        if block.kind == "heading":
            text = "#" * min(6, block.heading_level + 1) + " " + text
            if block.anchor:
                text = f'<a id="{escape(block.anchor, quote=True)}"></a>\n\n' + text
        elif block.kind == "picture":
            if block.image_src and urlsplit(block.image_src).scheme in {"", "http", "https"}:
                source = quote(block.image_src, safe="/:#?&=.%+~@!$")
                alt = block.original.splitlines()[0].replace("[", r"\[").replace("]", r"\]")
                text = f"![{alt}](<{source}>)\n\n" + text
        comment = f"<!-- source:{block.block_id} {json.dumps(block.location, ensure_ascii=False).replace('-->', '--&gt;')} -->"
        if block.kind == "list":
            marker = (
                (("-" if block.list_marker in {"·", "•", "●", "▪"} else block.list_marker) + " ")
                if block.list_marker
                else (f"{block.ordinal}. " if block.ordered else "- ")
            )
            content = "    " * block.list_depth + marker + text + " " + comment
            lines.append(("\n" if previous_list else "\n\n") + content)
            previous_list = True
        else:
            lines.append("\n\n" + comment + "\n\n" + text)
            previous_list = False
    return "\n\n".join(lines[:2]) + "".join(lines[2:]) + "\n"


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
    results = store.load_run(run_id)
    publication = {e["doc_id"]: e for e in store.list_documents(cfg.brand)}
    active = {
        d for d, e in publication.items() if e["lifecycle"] == "active" and e["run_id"] == run_id
    }
    folder = output / run_id
    folder.mkdir()
    for status in ("passed", "review", "failed"):
        (folder / status).mkdir()
    for index, result in enumerate(results):
        safe_id = hashlib.sha256(result.document.doc_id.encode()).hexdigest()[:12]
        stem = f"{index + 1:03}-{safe_id}"
        target = folder / result.status
        write_assets(result.document, target)
        (target / f"{stem}.md").write_text(markdown(result), encoding="utf-8")
        (target / f"{stem}.json").write_text(
            result.document.model_copy(
                update={"logical_id": publication[result.document.doc_id]["logical_id"]}
            ).model_dump_json(indent=2),
            encoding="utf-8",
        )
    (folder / "results.json").write_text(
        json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "knowledge.jsonl").write_text(
        "".join(
            r.document.model_copy(
                update={"logical_id": publication[r.document.doc_id]["logical_id"]}
            ).model_dump_json()
            + "\n"
            for r in results
            if r.status == "passed" and r.document.doc_id in active
        ),
        encoding="utf-8",
    )
    (folder / "knowledge-chunks.jsonl").write_text(
        "".join(
            c.model_dump_json() + "\n"
            for r in results
            if r.status == "passed" and r.document.doc_id in active
            for c in chunk_document(r.document)
        ),
        encoding="utf-8",
    )
    (folder / "metadata-history.json").write_text(
        json.dumps(
            {
                r.document.doc_id: store.metadata_history(cfg.brand, r.document.doc_id)
                for r in results
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "publication.json").write_text(
        json.dumps(
            {r.document.doc_id: publication[r.document.doc_id] for r in results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "quality-report.md").write_text(report_markdown(results), encoding="utf-8")
    return run_id

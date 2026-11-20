"""Local-only ingestion; parser failures are explicit, never silent fallbacks."""

import hashlib
import json
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

from autorepair.models import Block, Document, Issue

SUPPORTED = {".html", ".htm", ".json", ".pdf", ".docx"}


def html_blocks(content: str, prefix: str = "") -> tuple[list[Block], list[Issue]]:
    soup = BeautifulSoup(content, "lxml")
    issues = []
    if soup.find("script") or soup.find("link", rel="stylesheet") or soup.find("style"):
        issues.append(
            Issue(
                code="html.rendering",
                severity="warning",
                message="含脚本或样式表，仅提取静态 DOM；需确认可见性",
            )
        )
    # Compute paths before removing elements so source locations remain meaningful.
    paths = {}
    for tag in soup.find_all(True):
        segments = []
        current = tag
        while getattr(current, "name", None) and current.name != "[document]":
            siblings = [
                x for x in current.previous_siblings if getattr(x, "name", None) == current.name
            ]
            segments.append(f"{current.name}[{len(siblings) + 1}]")
            current = current.parent
        paths[id(tag)] = "/" + "/".join(reversed(segments))
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        style = (tag.get("style") or "").lower().replace(" ", "")
        if (
            tag.name in {"script", "style", "noscript", "nav", "header", "footer"}
            or tag.has_attr("hidden")
            or str(tag.get("aria-hidden", "")).lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            tag.decompose()
    blocks = []
    section = ""
    containers = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre", "blockquote"}
    tags = []

    def walk(node):
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                if str(child).strip():
                    # Loose text in div/body must not disappear beside a paragraph.
                    tag = soup.new_tag("p")
                    tag.string = str(child)
                    paths[id(tag)] = paths.get(id(node), "/") + f"/text()[{len(tags) + 1}]"
                    tags.append(tag)
            elif child.name in containers:
                tags.append(child)
            elif child.name not in {"head", "img", "br"}:
                walk(child)

    walk(soup.body or soup)
    for tag in tags:
        if tag.find_parent(["table", "li", "blockquote", "pre"]):
            continue
        kind = "paragraph"
        if tag.name.startswith("h") and tag.name[1:].isdigit():
            section = tag.get("id") or tag.get_text(" ", strip=True)
            kind = "heading"
        if tag.name == "table":
            if tag.find(attrs={"rowspan": True}) or tag.find(attrs={"colspan": True}):
                issues.append(
                    Issue(
                        code="html.table_span",
                        severity="warning",
                        message="合并单元格需对照原表复核",
                    )
                )
            rows = []
            for row in tag.find_all("tr"):
                rows.append([cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])])
            width = max((len(r) for r in rows), default=0)
            if not width:
                continue
            rows = [r + [""] * (width - len(r)) for r in rows]
            text = "\n".join(
                ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                + ["| " + " | ".join(r) + " |" for r in rows[1:]]
            )
            kind = "table"
        else:
            text = tag.get_text(" ", strip=True)
            if tag.name == "li":
                kind = "list"
        if not text:
            continue
        blocks.append(
            Block(
                block_id=f"b{len(blocks) + 1:04}",
                kind=kind,
                original=text,
                text=text,
                section=section,
                location={"dom": prefix + paths[id(tag)]},
                links=[str(a["href"]) for a in tag.find_all("a", href=True)],
            )
        )
    if not blocks:
        text = soup.get_text(" ", strip=True)
        if text:
            blocks.append(
                Block(block_id="b0001", original=text, text=text, location={"dom": prefix + "/"})
            )
    return blocks, issues


@lru_cache(maxsize=2)
def converter(ocr: bool, models_path: str | None):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=ocr,
        do_table_structure=True,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU),
        enable_remote_services=False,
        artifacts_path=models_path,
        ocr_options=RapidOcrOptions(),
        document_timeout=180,
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def parse(
    path: Path,
    brand: str,
    *,
    doc_id: str | None = None,
    model: str = "unknown",
    year_range: str = "unknown",
    ocr: bool = False,
    models_path: str | None = None,
) -> Document:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported input format: {suffix}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    doc = Document(
        doc_id=doc_id or "doc-" + digest[:12],
        brand=brand,
        model=model,
        year_range=year_range,
        source_name=path.name,
        source_hash=digest,
        parser="html-dom/1",
    )
    if suffix in {".html", ".htm"}:
        doc.blocks, doc.issues = html_blocks(raw.decode("utf-8-sig"))
        doc.expected_blocks = len(doc.blocks)
    elif suffix == ".json":
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("schema_version") == "1.0" and "blocks" in data:
            restored = Document.model_validate(data)
            if restored.brand != brand:
                raise ValueError("Normalized document brand mismatch")
            return restored
        doc.parser = "json-html/1"
        records = (
            data
            if isinstance(data, list)
            else data.get("records", [data])
            if isinstance(data, dict)
            else None
        )
        if not isinstance(records, list):
            raise ValueError("JSON must be an object, array or records array")
        for n, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError("Every JSON record must be an object")
            keys = [k for k in ("content_html", "html", "body", "content", "正文") if k in record]
            if len(keys) != 1:
                raise ValueError("Every record must contain exactly one supported content field")
            key = keys[0]
            if not isinstance(record[key], str):
                raise ValueError("Content fields must be strings")
            title = record.get("title", record.get("标题"))
            if title:
                text = str(title)
                doc.blocks.append(
                    Block(
                        block_id=f"b{len(doc.blocks) + 1:04}",
                        original=text,
                        text=text,
                        kind="heading",
                        section=text,
                        location={"json_path": f"records[{n}].title"},
                    )
                )
            blocks, issues = html_blocks(record[key], f"records[{n}].{key}:")
            for block in blocks:
                block.block_id = f"b{len(doc.blocks) + 1:04}"
                block.location["field_mapping"] = {key: "content"}
                # Preserve unknown fields in a source metadata record, never discard silently.
                block.location["source_fields"] = {k: v for k, v in record.items() if k != key}
                doc.blocks.append(block)
            doc.issues.extend(issues)
            unknown = set(record) - {key, "title", "标题"}
            if unknown:
                doc.issues.append(
                    Issue(
                        code="json.unknown_fields",
                        severity="warning",
                        message="保留未规范字段: " + ", ".join(sorted(unknown)),
                    )
                )
        doc.expected_blocks = len(doc.blocks)
    else:
        from docling.datamodel.base_models import ConversionStatus
        from docling_core.types.doc import TableItem, TextItem

        result = converter(ocr, models_path).convert(path)
        doc.parser = "docling/" + version("docling")
        if result.status != ConversionStatus.SUCCESS:
            doc.issues.append(
                Issue(
                    code="parse.partial",
                    severity="error",
                    message=f"转换未完整成功: {result.status.value}",
                )
            )
        section = ""
        for item, _ in result.document.iterate_items():
            if isinstance(item, TableItem):
                text = item.export_to_markdown(doc=result.document)
                kind = "table"
            elif isinstance(item, TextItem):
                text = item.text
                kind = (
                    "heading"
                    if str(item.label.value) in {"title", "section_header"}
                    else "paragraph"
                )
            else:
                continue
            if kind == "heading":
                section = text
            location = {"item_ref": item.self_ref}
            if suffix == ".pdf" and item.prov:
                location["pages"] = [p.page_no for p in item.prov]
                location["bboxes"] = [p.bbox.model_dump(mode="json") for p in item.prov]
            if suffix == ".pdf" and not item.prov:
                doc.issues.append(
                    Issue(
                        code="source.missing",
                        severity="warning",
                        message="PDF 元素缺少页码来源",
                        block_id=f"b{len(doc.blocks) + 1:04}",
                    )
                )
            doc.blocks.append(
                Block(
                    block_id=f"b{len(doc.blocks) + 1:04}",
                    kind=kind,
                    original=text,
                    text=text,
                    section=section,
                    location=location,
                )
            )
        # Block counts from the same parser cannot establish conversion completeness.
        doc.issues.append(
            Issue(
                code="parse.completeness",
                severity="warning",
                message="PDF/DOCX 完整性需对照原文复核，解析块数不代表完整率",
            )
        )
    return doc

"""Local-only ingestion; parser failures are explicit, never silent fallbacks."""

import hashlib
import json
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from autorepair.formatting import numbered_level, table_markdown
from autorepair.html_parser import html_blocks
from autorepair.media import attach_image, local_html_images
from autorepair.models import Block, Document, Issue
from autorepair.pdf_tables import recover_table

SUPPORTED = {".html", ".htm", ".json", ".pdf", ".docx"}


@lru_cache(maxsize=2)
def converter(ocr: bool, models_path: str | None):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=ocr,
        do_table_structure=True,
        generate_picture_images=True,
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
            for block in restored.blocks:
                if block.kind == "heading" and "source_level" in block.location:
                    block.heading_level = min(6, max(1, int(block.location["source_level"])))
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
        from docling_core.types.doc import ListItem, PictureItem, TableItem, TextItem

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
        heading_stack = []
        for item, depth in result.document.iterate_items():
            image_src = ""
            heading_level = 1
            location = {"item_ref": item.self_ref}
            if isinstance(item, TableItem):
                text = table_markdown([[c.text for c in row] for row in item.data.grid])
                if suffix == ".pdf":
                    try:
                        recovered = recover_table(item, result.document, path, digest)
                        if recovered:
                            text = recovered
                            location["table_method"] = "vector-cell-geometry"
                        elif any(not c.text.strip() for row in item.data.grid for c in row):
                            doc.issues.append(
                                Issue(
                                    code="table.span_unverified",
                                    severity="warning",
                                    message="表格存在空单元格且缺少可验证的合并范围，禁止猜测填充",
                                )
                            )
                    except Exception:
                        doc.issues.append(
                            Issue(
                                code="table.geometry_failed",
                                severity="warning",
                                message="无法核对原 PDF 表格几何结构，请复核共享单元格",
                            )
                        )
                kind = "table"
            elif isinstance(item, PictureItem):
                kind = "picture"
                text = "嵌入图片"
                try:
                    image = item.get_image(result.document)
                    if image is not None:
                        image_src, recognized, scores = attach_image(
                            doc, image, ocr=(suffix == ".docx" or ocr)
                        )
                        if recognized:
                            text += "\n\n图片识别文字\n" + recognized
                            location["ocr_scores"] = scores
                        else:
                            doc.issues.append(
                                Issue(
                                    code="image.ocr_empty",
                                    severity="warning",
                                    message="图片文字未识别，已保留图像资源",
                                )
                            )
                    else:
                        doc.issues.append(
                            Issue(
                                code="image.missing",
                                severity="warning",
                                message="无法读取图片，已保留占位与来源",
                            )
                        )
                except Exception:
                    doc.issues.append(
                        Issue(
                            code="image.ocr_failed",
                            severity="warning",
                            message="图像 OCR 失败，保留图片占位并要求复核",
                        )
                    )
                doc.issues.append(
                    Issue(
                        code="image.unverified",
                        severity="warning",
                        message="图像及 OCR 需核对原图，不保证文字或隐私信息已完整处理",
                    )
                )
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
                heading_level = min(6, max(1, int(getattr(item, "level", 1))))
                if suffix == ".pdf":
                    heading_level = numbered_level(text) or heading_level
                section = text
                heading_stack = [x for x in heading_stack if x[0] < heading_level] + [
                    (heading_level, text)
                ]
            if isinstance(item, ListItem):
                kind = "list"
            hyperlink = getattr(item, "hyperlink", None)
            if hyperlink:
                location["link_labels"] = [{"label": text, "target": str(hyperlink)}]
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
                    heading_level=heading_level,
                    section_path=[x[1] for x in heading_stack],
                    list_depth=max(0, depth - len(heading_stack) - 1) if kind == "list" else 0,
                    ordered=bool(getattr(item, "enumerated", False)),
                    image_src=image_src,
                    links=[str(hyperlink)] if hyperlink else [],
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
    if suffix in {".html", ".htm", ".json"}:
        local_html_images(doc, path)
    return doc

"""Source-grounded evidence, safe tables, validated attachments and PDF page geometry."""

import base64
import hashlib
import io
import math
import re
import threading
from html import escape

from markdown_it import MarkdownIt

from autorepair.chunking import sections


def block_record(block):
    return dict(
        block_id=block.block_id,
        kind=block.kind,
        original=block.original,
        text=block.text,
        location=block.location,
        image_src=block.image_src,
        section_path=block.section_path,
        marker=block.list_marker
        or (f"{block.ordinal}." if block.ordered else "-" if block.kind == "list" else ""),
        list_depth=block.list_depth,
    )


def page_targets(records):
    targets = {}
    for record in records:
        location = record["location"]
        pages = location.get("pages", [])
        boxes = location.get("bboxes", [])
        for i, page in enumerate(pages):
            if type(page) is not int or page < 1:
                continue
            target = targets.setdefault(page, dict(page=page, block_ids=[], regions=[]))
            if record["block_id"] not in target["block_ids"]:
                target["block_ids"].append(record["block_id"])
            candidates = (
                [boxes[i]] if len(boxes) == len(pages) else boxes if len(pages) == 1 else []
            )
            for box in candidates:
                region = dict(block_id=record["block_id"], bbox=box)
                if region not in target["regions"]:
                    target["regions"].append(region)
    return [targets[p] for p in sorted(targets)]


def build_evidence(document, chunk, *, context=2):
    if chunk.doc_id != document.doc_id or chunk.brand != document.brand:
        raise ValueError("证据与文档不对应")
    by_id = {b.block_id: b for b in document.blocks}
    owned = []
    for part in chunk.parts:
        block = by_id[part.block_id]
        basis = block.original if chunk.text_mode == "original" else block.text
        if (
            not 0 <= part.start <= part.end <= len(basis)
            or part.text != basis[part.start : part.end]
        ):
            raise ValueError("证据片段与当前文档不一致，请重新检索")
        if block.block_id not in [b["block_id"] for b in owned]:
            owned.append(block_record(block))
    section = next(s for s in sections(document) if s["id"] == chunk.section_id)
    indices = [i for i, b in enumerate(section["blocks"]) if b.block_id in chunk.source_block_ids]
    context = max(0, min(5, context))
    before = [
        block_record(b) for b in section["blocks"][max(0, min(indices) - context) : min(indices)]
    ]
    after = [
        block_record(b) for b in section["blocks"][max(indices) + 1 : max(indices) + 1 + context]
    ]
    links = {}
    for link in chunk.related:
        links.setdefault(link.block_id, []).append(link.reason)
    related = [
        {**block_record(b), "reasons": links[b.block_id]}
        for b in document.blocks
        if b.block_id in links
    ]
    # Source page targeting uses owned/explicitly related evidence; neighbor context is not a hit.
    targets = page_targets(owned + related)
    images = []
    for record in owned + related:
        if record["image_src"] and record["image_src"] not in [i["src"] for i in images]:
            info = image_attachment(document, record["image_src"])
            images.append(
                {
                    **{k: v for k, v in info.items() if k != "data"},
                    "block_ids": [
                        b["block_id"]
                        for b in owned + related
                        if b["image_src"] == record["image_src"]
                    ],
                }
            )
    return dict(
        doc_id=document.doc_id,
        brand=document.brand,
        source_name=document.source_name,
        source_hash=document.source_hash,
        chunk_id=chunk.chunk_id,
        section_path=chunk.section_path,
        text_mode=chunk.text_mode,
        parts=[p.model_dump() for p in chunk.parts],
        owned=owned,
        before=before,
        after=after,
        related=related,
        pages=targets,
        images=images,
        original_label="脱敏解析原文；字符偏移以 text_mode 指定的文本为准，不映射到另一版本的文字",
    )


def image_attachment(document, src):
    result = dict(
        rights_status="not_verified",
        review_required=True,
        src=src,
        status="missing",
        message="本地附件缺失，保留引用与替代文字",
        data=None,
    )
    if re.match(r"^https?://", src, re.I):
        return {**result, "status": "external", "message": "外部图片未下载；没有可核验的本地附件"}
    if not re.fullmatch(r"assets/[a-f0-9]{64}\.png", src):
        return {**result, "status": "unsupported", "message": "不是受支持的内容哈希附件引用"}
    name = src.removeprefix("assets/")
    if name not in document.assets:
        return result
    try:
        from PIL import Image

        data = base64.b64decode(document.assets[name], validate=True)
        if hashlib.sha256(data).hexdigest() + ".png" != name:
            raise ValueError("hash mismatch")
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG" or img.width * img.height > 40_000_000:
                raise ValueError("unsupported image")
            width, height = img.size
            img.verify()
        return {
            **result,
            "status": "available",
            "message": "已校验本地附件；图内内容及脱敏情况仍需人工核对",
            "data": data,
            "width": width,
            "height": height,
            "sha256": name[:-4],
        }
    except Exception:
        return {
            **result,
            "status": "invalid",
            "message": "附件哈希、编码或图像校验失败，未显示图像",
        }


def table_html(text):
    """Render only parsed table cells; source HTML, links and image URLs never execute."""
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    rows = []
    row = None
    header = False
    in_table = False
    tables = []
    for i, token in enumerate(tokens):
        if token.type == "table_open":
            in_table = True
            rows = []
        elif token.type == "tr_open" and in_table:
            row = []
        elif token.type == "inline" and in_table and row is not None:
            cells = []
            for child in token.children or []:
                if child.type in {"text", "code_inline"}:
                    cells.append(child.content)
                elif child.type in {"softbreak", "hardbreak"}:
                    cells.append("\n")
                elif child.type == "html_inline":
                    cells.append(
                        "\n" if re.fullmatch(r"<br\s*/?>", child.content, re.I) else child.content
                    )
                elif child.type == "image":
                    cells.append(child.content)
            header = tokens[i - 1].type == "th_open"
            tag = "th" if header else "td"
            row.append(
                f'<{tag} style="border:1px solid #aab4bf;padding:6px;text-align:left">'
                + escape("".join(cells)).replace("\n", "<br>")
                + f"</{tag}>"
            )
        elif token.type == "tr_close" and in_table:
            rows.append("<tr>" + "".join(row) + "</tr>")
            row = None
        elif token.type == "table_close":
            tables.append(
                '<div style="overflow-x:auto"><table style="border-collapse:collapse">'
                + "".join(rows)
                + "</table></div>"
            )
            in_table = False
    return "".join(tables)


_pdf_lock = threading.RLock()


def render_pdf_page(data, page_number, regions=()):
    with _pdf_lock:
        return _render_pdf_page(data, page_number, regions)


def _render_pdf_page(data, page_number, regions=()):
    """Return one physical PDF page, drawing only unambiguous valid source geometry."""
    import pypdfium2 as pdfium
    from PIL import ImageDraw

    if type(page_number) is not int or page_number < 1:
        raise ValueError("页码须为从 1 开始的整数")
    pdf = pdfium.PdfDocument(data)
    try:
        if page_number > len(pdf):
            raise ValueError("来源页码超出原件页数")
        page = pdf[page_number - 1]
        try:
            width, height = page.get_size()
            scale = min(1.5, 1600 / max(width, height))
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
            draw = ImageDraw.Draw(image)
            highlighted = []
            skipped = 0
            for region in regions:
                try:
                    box = region["bbox"]
                    left, t, right, b = [float(box[k]) for k in ["l", "t", "r", "b"]]
                    if box.get("coord_origin") == "BOTTOMLEFT":
                        t, b = height - t, height - b
                    elif box.get("coord_origin") != "TOPLEFT":
                        raise ValueError("unknown origin")
                    if (
                        page.get_rotation() != 0
                        or tuple(page.get_cropbox()) != (0, 0, width, height)
                        or tuple(page.get_mediabox()) != (0, 0, width, height)
                        or not all(math.isfinite(v) for v in [left, t, right, b])
                        or not (0 <= left < right <= width and 0 <= t < b <= height)
                    ):
                        raise ValueError("ambiguous geometry")
                    draw.rectangle(
                        (
                            left * image.width / width,
                            t * image.height / height,
                            right * image.width / width,
                            b * image.height / height,
                        ),
                        outline="#d92323",
                        width=3,
                    )
                    highlighted.append(region["block_id"])
                except (ValueError, TypeError, KeyError):
                    skipped += 1
            output = io.BytesIO()
            image.save(output, format="PNG")
            return dict(
                data=output.getvalue(),
                page=page_number,
                total_pages=len(pdf),
                highlighted_blocks=list(dict.fromkeys(highlighted)),
                skipped_regions=skipped,
            )
        finally:
            page.close()
    finally:
        pdf.close()

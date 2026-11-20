"""Recover PDF structure from semantic tags, typography and native image geometry.

Typography-derived hierarchy is explicitly marked for human review. Source files and
rendered crops are never fetched remotely or published by this module.
"""

import re
from collections import Counter

from autorepair.formatting import numbered_level
from autorepair.media import attach_image
from autorepair.models import Block, Issue


def block_box(block, height):
    boxes = block.location.get("bboxes", [])
    if not boxes:
        return None
    b = boxes[0]
    return (
        (b["l"], height - b["t"], b["r"], height - b["b"])
        if b["coord_origin"] == "BOTTOMLEFT"
        else (b["l"], b["t"], b["r"], b["b"])
    )


def intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def metadata_heading(text):
    return bool(
        re.fullmatch(
            r"(?:(?:January|February|March|April|May|June|July|August|September|October|November|December) "
            r"\d{1,2}, \d{4}(?: Version \d+)?|Version \d+)",
            text.strip(),
            re.I,
        )
    )


def native_headings(pdf):
    headings = []

    def walk(nodes, level=None, page=None):
        for node in nodes:
            tag = node.get("type", "")
            match = re.fullmatch(r"H([1-6])", tag)
            current = int(match[1]) if match else level
            pn = node.get("page_number", page)
            if current and pn and node.get("mcids"):
                chars = [c for c in pdf.pages[pn - 1].chars if c.get("mcid") in node["mcids"]]
                if chars:
                    text = "".join(c["text"] for c in chars).strip()
                    bbox = (
                        min(c["x0"] for c in chars),
                        min(c["top"] for c in chars),
                        max(c["x1"] for c in chars),
                        max(c["bottom"] for c in chars),
                    )
                    if not metadata_heading(text):
                        headings.append((pn, current, text, bbox))
            walk(node.get("children", []), current, pn)

    walk(pdf.structure_tree)
    return headings


def style_for(block, page):
    box = block_box(block, page.height)
    if not box:
        return (0, "", "")
    chars = [
        c
        for c in page.chars
        if box[0] - 1 <= (c["x0"] + c["x1"]) / 2 <= box[2] + 1
        and box[1] - 1 <= (c["top"] + c["bottom"]) / 2 <= box[3] + 1
        and c["text"].strip()
    ]
    counts = Counter(
        (round(c["size"], 1), c["fontname"], str(c.get("non_stroking_color"))) for c in chars
    )
    return counts.most_common(1)[0][0] if counts else (0, "", "")


def insert_on_page(doc, block, height):
    page = block.location["pages"][0]
    top = block_box(block, height)[1]
    indices = [i for i, b in enumerate(doc.blocks) if page in b.location.get("pages", [])]
    at = next(
        (i for i in indices if (box := block_box(doc.blocks[i], height)) and box[1] > top),
        indices[-1] + 1 if indices else len(doc.blocks),
    )
    doc.blocks.insert(at, block)


def restore_hierarchy(doc, pdf):
    tagged = native_headings(pdf)
    for pn, level, text, box in tagged:
        # Recover a true Hn node even if the layout model assigned it to page furniture.
        if not any(
            pn in b.location.get("pages", [])
            and re.sub(r"\s+", "", b.text).casefold() == re.sub(r"\s+", "", text).casefold()
            for b in doc.blocks
        ):
            location = {
                "pages": [pn],
                "bboxes": [dict(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin="TOPLEFT")],
                "heading_method": "pdf-tag",
            }
            block = Block(
                block_id=f"native-heading-{pn}-{len(doc.blocks)}",
                kind="heading",
                original=text,
                text=text,
                heading_level=level,
                location=location,
            )
            insert_on_page(doc, block, pdf.pages[pn - 1].height)
    styles = {}
    native_styles = {}
    body_sizes = Counter()
    for p in pdf.pages:
        body_sizes.update(round(c["size"], 1) for c in p.chars if c["text"].strip())
    body_size = body_sizes.most_common(1)[0][0] if body_sizes else 10
    for block in doc.blocks:
        pages = block.location.get("pages", [])
        if not pages:
            continue
        page = pdf.pages[pages[0] - 1]
        box = block_box(block, page.height)
        styles[block.block_id] = style_for(block, page)
        if not box:
            continue
        for pn, level, _text, tag_box in tagged:
            if (
                pn == pages[0]
                and intersection(box, tag_box) / max(1, (box[2] - box[0]) * (box[3] - box[1])) > 0.6
            ):
                block.kind = "heading"
                block.heading_level = level
                block.location["heading_method"] = "pdf-tag"
                native_styles[styles[block.block_id]] = level
                break
    # Repeated document titles are retained as running headers, not duplicate roots.
    seen = set()
    for b in doc.blocks:
        if b.kind != "heading":
            continue
        text = b.text.strip()
        if metadata_heading(text):
            b.kind = "paragraph"
            continue
        normalized = " ".join(text.split()).casefold()
        pages = b.location.get("pages", [])
        page = pdf.pages[pages[0] - 1] if pages else None
        box = block_box(b, page.height) if page else None
        if (
            normalized in seen
            and b.location.get("heading_method") != "pdf-tag"
            and box
            and box[1] < page.height * 0.16
            and "continued" not in normalized
        ):
            b.kind = "running_header"
            continue
        seen.add(normalized)
        if re.match(r"^(?:NOTE|CAUTION|WARNING|NOTICE|HINT|REMINDER)\b", text, re.I):
            b.kind = "callout"
            continue
        if re.match(r"^Figure\s+\d+", text, re.I):
            b.kind = "caption"
            continue
        font_size = styles.get(b.block_id, (0,))[0]
        if (text.endswith(".") and len(text) > 65) or 0 < font_size < body_size * 0.9:
            b.kind = "paragraph"
    candidates = [b for b in doc.blocks if b.kind == "heading"]
    sizes = sorted(
        {
            styles.get(b.block_id, (0,))[0]
            for b in candidates
            if styles.get(b.block_id, (0,))[0] > 0
        },
        reverse=True,
    )
    by_style = {}
    for b in candidates:
        by_style.setdefault(styles.get(b.block_id), []).append(b)
    inferred = False
    last_native = 1
    for b in doc.blocks:
        if b.kind != "heading":
            continue
        if b.location.get("heading_method") == "pdf-tag":
            last_native = b.heading_level
            continue
        level = numbered_level(b.text)
        method = "numbered-heading" if level else "typography"
        style = styles.get(b.block_id, (0, "", ""))
        if not level:
            if style in native_styles:
                level = native_styles[style]
            elif native_styles:
                same_size = [v for k, v in native_styles.items() if abs(k[0] - style[0]) < 0.5]
                level = min(6, max(same_size) + 1) if same_size else min(6, last_native + 1)
            else:
                level = min(6, sizes.index(style[0]) + 1) if style[0] in sizes else b.heading_level
                peers = by_style.get(style, [])
                colon_count = sum(x.text.rstrip().endswith(":") for x in peers)
                if peers and colon_count / len(peers) >= 0.6 and not b.text.rstrip().endswith(":"):
                    level = min(6, level + 1)
            inferred = True
        b.heading_level = level
        b.location["heading_method"] = method
    stack = []
    for b in doc.blocks:
        if b.kind == "heading":
            stack = [x for x in stack if x[0] < b.heading_level] + [(b.heading_level, b.text)]
            b.section = b.text
        b.section_path = [x[1] for x in stack]
    if inferred:
        doc.issues.append(
            Issue(
                code="structure.inferred",
                severity="warning",
                message="部分 PDF 层级依字体样式推断，需核对原目录；未当作原生语义标签",
            )
        )


def preserve_unclassified_images(doc, pdf):
    count = 0
    for pn, page in enumerate(pdf.pages, 1):
        rendered = None
        images = sorted(page.images, key=lambda x: -(x["x1"] - x["x0"]) * (x["bottom"] - x["top"]))
        for image in images:
            box = (
                max(0, image["x0"]),
                max(0, image["top"]),
                min(page.width, image["x1"]),
                min(page.height, image["bottom"]),
            )
            area = (box[2] - box[0]) * (box[3] - box[1])
            if area < 120 or min(box[2] - box[0], box[3] - box[1]) < 8:
                continue
            candidates = [
                block_box(b, page.height)
                for b in doc.blocks
                if b.kind == "picture" and pn in b.location.get("pages", []) and b.image_src
            ]
            if any(b and intersection(box, b) / area > 0.9 for b in candidates):
                continue
            if rendered is None:
                rendered = page.to_image(resolution=144, antialias=True).original
            sx, sy = rendered.width / page.width, rendered.height / page.height
            crop = rendered.crop(
                (int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy))
            )
            src, _, _ = attach_image(doc, crop)
            text = "原始 PDF 图像补充"
            b = Block(
                block_id=f"native-image-{pn}-{count}",
                kind="picture",
                original=text,
                text=text,
                image_src=src,
                location={
                    "pages": [pn],
                    "image_method": "native-pdf-image",
                    "bboxes": [
                        dict(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin="TOPLEFT")
                    ],
                },
            )
            insert_on_page(doc, b, page.height)
            count += 1
    if count:
        doc.issues.append(
            Issue(
                code="image.native_fallback",
                severity="warning",
                message=f"补充保留 {count} 个未分类原始图像区域，需核对原图",
            )
        )


def enrich_pdf(doc, path):
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        restore_hierarchy(doc, pdf)
        preserve_unclassified_images(doc, pdf)

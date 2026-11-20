"""Local image preservation and optional OCR; failures remain visible quality issues."""

import base64
import hashlib
import io
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from autorepair.models import Document, Issue


def join_ocr_lines(texts, boxes):
    """Deduplicate overlapping detector crops only when their image geometry overlaps."""
    lines = []
    for text, box in zip(texts, boxes, strict=True):
        x0, y0 = min(p[0] for p in box), min(p[1] for p in box)
        x1, y1 = max(p[0] for p in box), max(p[1] for p in box)
        if lines:
            previous, px0, py0, px1, py1 = lines[-1]
            same_line = min(y1, py1) - max(y0, py0) > 0.6 * min(y1 - y0, py1 - py0)
            if same_line and x0 >= px0:
                overlap = px1 - x0
                limit = (
                    min(
                        len(text),
                        len(previous),
                        max(0, int(overlap / max(1, (x1 - x0) / len(text))) + 1),
                    )
                    if overlap > 0
                    else 0
                )
                shared = next((n for n in range(limit, 0, -1) if previous[-n:] == text[:n]), 0)
                separator = "" if shared else " "
                lines[-1] = (
                    previous + separator + text[shared:],
                    px0,
                    min(py0, y0),
                    max(px1, x1),
                    max(py1, y1),
                )
                continue
        lines.append((text, x0, y0, x1, y1))
    return "\n".join(line[0] for line in lines)


def attach_image(doc: Document, image, *, ocr=False):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    data = stream.getvalue()
    name = hashlib.sha256(data).hexdigest() + ".png"
    doc.assets[name] = base64.b64encode(data).decode("ascii")
    text = ""
    scores = []
    if ocr:
        result = ocr_engine()(image)
        text = join_ocr_lines(result.txts, result.boxes) if result.txts else ""
        scores = list(result.scores or [])
    return "assets/" + name, text, scores


@lru_cache(maxsize=1)
def ocr_engine():
    from rapidocr import RapidOCR

    return RapidOCR()


def local_html_images(doc: Document, source: Path):
    from PIL import Image

    for block in doc.blocks:
        if block.kind != "picture" or not block.image_src:
            continue
        parsed = urlparse(block.image_src)
        if parsed.scheme or parsed.netloc:
            continue  # Preserve URL; never fetch third-party resources.
        path = (source.parent / unquote(parsed.path)).resolve()
        if not path.is_relative_to(source.parent.resolve()):
            doc.issues.append(
                Issue(
                    code="image.outside_source",
                    severity="warning",
                    message="图片位于文档目录之外，已保留引用但未读取",
                    block_id=block.block_id,
                )
            )
            continue
        if not path.is_file():
            doc.issues.append(
                Issue(
                    code="image.missing",
                    severity="warning",
                    message="本地图片资源缺失，已保留引用",
                    block_id=block.block_id,
                )
            )
            continue
        try:
            with Image.open(path) as image:
                block.image_src, _, _ = attach_image(doc, image.convert("RGB"))
        except Exception:
            doc.issues.append(
                Issue(
                    code="image.failed",
                    severity="warning",
                    message="图片读取失败，已保留替代文字",
                    block_id=block.block_id,
                )
            )

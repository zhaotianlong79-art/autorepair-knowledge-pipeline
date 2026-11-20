"""Page-specific independent checks. Stores only pass/fail metrics in the public report."""

import argparse
import base64
import hashlib
import io
import json
import re
from pathlib import Path

from markdown_it import MarkdownIt
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MD = MarkdownIt("commonmark").enable("table")


def norm(text):
    return re.sub(r"\s+", "", text).casefold()


def grid(text):
    rows = []
    row = None
    ts = MD.parse(text)
    for i, t in enumerate(ts):
        if t.type == "tr_open":
            row = []
        if t.type == "inline" and i and ts[i - 1].type in {"th_open", "td_open"}:
            row.append(
                "".join(c.content for c in (t.children or []) if c.type in {"text", "code_inline"})
            )
        if t.type == "tr_close":
            rows.append(row)
    return rows


def audit(stage):
    checks = json.loads((ROOT / "docs/real-documents/checks.json").read_text())
    results = []
    for brand in ["toyota", "honda", "subaru"]:
        result = json.loads(
            (ROOT / "work/real-documents" / stage / brand / "result.json").read_text()
        )
        doc = result["document"]
        entries = []
        for index, expected in enumerate(checks[brand]["headings"]):
            matches = [
                b
                for b in doc["blocks"]
                if expected["page"] in b["location"].get("pages", [])
                and norm(b["text"]) == norm(expected["text"])
            ]
            passed = any(
                b["kind"] == "heading" and b["heading_level"] == expected["level"] for b in matches
            )
            entries.append(dict(kind="heading", index=index, passed=passed))
        for index, expected in enumerate(checks[brand]["tables"]):
            matches = [
                b
                for b in doc["blocks"]
                if b["kind"] == "table"
                and expected["page"] in b["location"].get("pages", [])
                and expected["contains"] in b["text"]
            ]
            passed = False
            for block in matches:
                rows = grid(block["text"])
                if not rows or any(len(r) != expected["columns"] for r in rows):
                    continue
                if "header_contains" in expected and not all(
                    norm(x) in norm(y)
                    for x, y in zip(expected["header_contains"], rows[0], strict=True)
                ):
                    continue
                if all(
                    any(
                        all(norm(x) in norm(y) for x, y in zip(er, row, strict=True))
                        for row in rows[1:]
                    )
                    for er in expected["row_contains"]
                ):
                    passed = True
            entries.append(dict(kind="table", index=index, passed=passed))
        for index, expected in enumerate(checks[brand]["figures"]):
            found = False
            x0, y0, x1, y1 = expected["bbox"]
            area = (x1 - x0) * (y1 - y0)
            for block in doc["blocks"]:
                if block["kind"] != "picture" or expected["page"] not in block["location"].get(
                    "pages", []
                ):
                    continue
                name = block.get("image_src", "").removeprefix("assets/")
                if name not in doc["assets"]:
                    continue
                data = base64.b64decode(doc["assets"][name])
                Image.open(io.BytesIO(data)).verify()
                assert hashlib.sha256(data).hexdigest() == name.removesuffix(".png")
                for bbox in block["location"].get("bboxes", []):
                    top = 792 - bbox["t"] if bbox["coord_origin"] == "BOTTOMLEFT" else bbox["t"]
                    bottom = 792 - bbox["b"] if bbox["coord_origin"] == "BOTTOMLEFT" else bbox["b"]
                    intersection = max(0, min(x1, bbox["r"]) - max(x0, bbox["l"])) * max(
                        0, min(y1, bottom) - max(y0, top)
                    )
                    if intersection / area >= 0.9:
                        found = True
            entries.append(dict(kind="figure", index=index, passed=found))
        for index, expected in enumerate(checks[brand]["steps"]):
            matches = [
                b
                for b in doc["blocks"]
                if b["kind"] == "list" and expected["page"] in b["location"].get("pages", [])
            ]
            passed = any(
                (b.get("list_marker") or (str(b["ordinal"]) + "." if b.get("ordered") else ""))
                == expected["number"]
                for b in matches
            )
            entries.append(dict(kind="step", index=index, passed=passed))
        totals = {
            k: dict(
                passed=sum(e["passed"] for e in entries if e["kind"] == k),
                total=sum(e["kind"] == k for e in entries),
            )
            for k in ["heading", "table", "figure", "step"]
        }
        results.append(dict(id=brand, checks=entries, totals=totals))
        print(brand, totals)
    (ROOT / "docs/real-documents" / f"{stage}-checks.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="before")
    audit(ap.parse_args().stage)

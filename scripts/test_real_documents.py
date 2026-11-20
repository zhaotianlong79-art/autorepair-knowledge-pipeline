"""Evaluate public OEM PDFs locally; publish metrics, never copyrighted source/derivatives."""

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from markdown_it import MarkdownIt

from autorepair.config import Config, load_config
from autorepair.engine import process
from autorepair.parsers import parse
from autorepair.pipeline import markdown, write_assets

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work/real-documents"


def tokens(text):
    return re.findall(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*", text.casefold())


def audit(stage, download=False):
    import pdfplumber

    parser = MarkdownIt("commonmark").enable("table")
    sources = json.loads((ROOT / "docs/real-documents/sources.json").read_text())
    out = WORK / stage
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for source in sources:
        brand = source["id"]
        path = WORK / "originals" / f"{brand}.pdf"
        if download and not path.exists():
            import httpx

            response = httpx.get(source["url"], follow_redirects=True, timeout=60)
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != source["sha256"]:
                raise ValueError(f"Source hash changed: {brand}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(response.content)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Source hash mismatch: {brand}")
        start = time.perf_counter()
        doc = parse(path, brand)
        cfg = Config(brand=brand, rules=load_config(ROOT / "configs", "aurora").rules)
        result = process(doc, cfg)
        md = markdown(result)
        write_assets(result.document, out / brand)
        (out / brand).mkdir(exist_ok=True)
        (out / brand / "output.md").write_text(md)
        (out / brand / "result.json").write_text(result.model_dump_json(indent=2))
        (out / brand / "output.html").write_text(
            '<!doctype html><meta charset="utf-8"><style>body{margin:30px auto;max-width:1050px;font:16px Arial;line-height:1.6}img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:6px}</style>'
            + parser.render(md)
        )
        with pdfplumber.open(path) as pdf:
            source_pages = [p.extract_text(layout=False) or "" for p in pdf.pages]
        visible = []
        md_tokens = parser.parse(md)
        for token in md_tokens:
            if token.type == "inline":
                visible += [
                    c.content for c in token.children or [] if c.type in {"text", "code_inline"}
                ]
        visible_text = " ".join(visible)
        # Distinct native PDF words are a diagnostic, not an independent accuracy oracle.
        native = set(tokens(" ".join(source_pages)))
        rendered = set(tokens(visible_text))
        missing = sorted(native - rendered)
        (out / brand / "missing-native-words.json").write_text(json.dumps(missing, indent=2))
        record = dict(
            id=brand,
            pages=len(source_pages),
            seconds=round(time.perf_counter() - start, 2),
            status=result.status,
            blocks=len(doc.blocks),
            tables=sum(b.kind == "table" for b in doc.blocks),
            pictures=sum(b.kind == "picture" for b in doc.blocks),
            assets=len(doc.assets),
            heading_levels=[b.heading_level for b in doc.blocks if b.kind == "heading"],
            native_distinct_words=len(native),
            retained_native_distinct_words=len(native & rendered),
            native_word_difference_count=len(missing),
            issues={
                code: sum(i.code == code for i in result.issues)
                for code in sorted({i.code for i in result.issues})
            },
            source_sha256=source["sha256"],
            markdown_sha256=hashlib.sha256(md.encode()).hexdigest(),
        )
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    (out / "metrics.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="before")
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()
    audit(args.stage, args.download)

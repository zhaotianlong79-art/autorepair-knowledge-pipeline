"""Audit the production parser and Markdown exporter against source manifests.

This is an evaluation, not a regression suite expected to pass all cases. Nonzero exit
indicates a broken runner; findings are written as data for review.
"""

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from markdown_it import MarkdownIt

from autorepair.config import load_config
from autorepair.engine import process
from autorepair.parsers import parse
from autorepair.pipeline import markdown

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples/fidelity"
OUT = Path(os.environ.get("FIDELITY_OUTPUT", str(ROOT / "docs/fidelity")))
OUT.mkdir(parents=True, exist_ok=True)
MD = MarkdownIt("commonmark").enable("table")


def norm(text):
    return re.sub(r"\s+", "", text).casefold()


def inspect_md(text):
    headings = []
    tables = []
    links = []
    rendered = []
    list_depth = 0
    depth = 0
    row = None
    table = None
    tokens = MD.parse(text)
    for n, token in enumerate(tokens):
        if token.type == "heading_open":
            headings.append((int(token.tag[1:]), tokens[n + 1].content))
        if token.type == "table_open":
            table = []
        elif token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            table.append(row)
        elif token.type == "table_close":
            tables.append(table)
            table = None
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            depth += 1
            list_depth = max(list_depth, depth)
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            depth -= 1
        if token.type == "inline":
            cell = []
            for child in token.children or []:
                if child.type == "link_open":
                    links.append(child.attrGet("href"))
                if child.type in {"text", "code_inline", "image"}:
                    rendered.append(child.content)
                    cell.append(child.content)
                elif child.type in {"softbreak", "hardbreak"}:
                    rendered.append(" ")
                    cell.append(" ")
            if row is not None and n and tokens[n - 1].type in {"th_open", "td_open"}:
                row.append("".join(cell))
    return dict(
        headings=headings,
        tables=tables,
        links=links,
        list_depth=list_depth,
        rendered=" ".join(rendered),
    )


manifest = json.loads((BASE / "manifest.json").read_text())
cfg = load_config(ROOT / "configs", "aurora")
report = {
    "scope": "Self-authored source fixtures; content checkpoint recovery is not complete document accuracy. No benchmark threshold was used to hide failures.",
    "cases": [],
}
for expected in manifest:
    path = BASE / expected["file"]
    start = time.perf_counter()
    try:
        doc = parse(path, "aurora", ocr=expected.get("ocr", False))
        result = process(doc, cfg)
        from autorepair.pipeline import write_assets

        write_assets(result.document, OUT)
        md = markdown(result)
        actual = inspect_md(md)
        # Also render a parser-only result to separate extraction loss from domain rules.
        from autorepair.models import Result

        raw_md = markdown(Result(document=doc, rules_hash="parser-only"))
        raw_actual = inspect_md(raw_md)
        missing = [t for t in expected["tokens"] if norm(t) not in norm(actual["rendered"])]
        parser_missing = [
            t for t in expected["tokens"] if norm(t) not in norm(raw_actual["rendered"])
        ]
        heading_missing = [
            title
            for _, title in expected["headings"]
            if not any(norm(title) == norm(h[1]) for h in actual["headings"])
        ]
        wrong_levels = [
            {
                "title": title,
                "source_level": level,
                "expected_markdown_level": level + 1,
                "actual": [h[0] for h in actual["headings"] if norm(title) == norm(h[1])],
            }
            for level, title in expected["headings"]
            if not any(h[0] == level + 1 and norm(h[1]) == norm(title) for h in actual["headings"])
        ]
        grid_match = len(actual["tables"]) == len(expected["tables"]) and all(
            [[norm(c) for c in row] for row in table] == [[norm(c) for c in row] for row in ref]
            for table, ref in zip(actual["tables"], expected["tables"], strict=False)
        )
        if expected.get("table_mode") == "continuation":
            combined = []
            header = expected["tables"][0][0]
            for table in actual["tables"]:
                for row in table:
                    if combined and [norm(c) for c in row] == [norm(c) for c in header]:
                        continue
                    combined.append(row)
            grid_match = [[norm(c) for c in row] for row in combined] == [
                [norm(c) for c in row] for row in expected["tables"][0]
            ]
        missing_links = [url for url in expected.get("links", []) if url not in actual["links"]]
        forbidden = [t for t in expected.get("forbidden", []) if t in actual["rendered"]]
        base = dict(
            file=path.name,
            group=expected["group"],
            seconds=round(time.perf_counter() - start, 3),
            status=result.status,
            parser=doc.parser,
            checkpoints=len(expected["tokens"]),
            recovered=len(expected["tokens"]) - len(missing),
            missing=missing,
            parser_export_missing=parser_missing,
            block_text_missing=[
                t
                for t in expected["tokens"]
                if norm(t) not in norm(" ".join(b.original for b in doc.blocks).replace(r"\|", "|"))
            ],
            table_mode=expected.get("table_mode", "ordinary"),
            heading_text_missing=heading_missing,
            heading_level_mismatches=wrong_levels,
            expected_tables=expected["tables"],
            actual_tables=actual["tables"],
            table_grid_match=grid_match,
            missing_links=missing_links,
            expected_list_depth=expected.get("list_depth"),
            actual_list_depth=actual["list_depth"],
            forbidden_visible=forbidden,
            issues=[i.model_dump() for i in result.issues],
            input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        base["content_ok"] = not missing and not forbidden
        base["headings_ok"] = not heading_missing and not wrong_levels
        base["tables_ok"] = grid_match
        (OUT / (path.name + ".md")).write_text(md)
        (OUT / (path.name + ".parser-only.md")).write_text(raw_md)
        (OUT / (path.name + ".json")).write_text(result.model_dump_json(indent=2))
        css = "body{max-width:1000px;margin:40px auto;font-family:Arial,sans-serif;line-height:1.65}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px 14px}"
        (OUT / (path.name + ".html")).write_text(
            '<!doctype html><meta charset="utf-8"><style>' + css + "</style>" + MD.render(md)
        )
    except Exception as exc:
        base = dict(
            file=path.name,
            group=expected["group"],
            error=f"{type(exc).__name__}: {exc}",
            content_ok=False,
            headings_ok=False,
            tables_ok=False,
            seconds=round(time.perf_counter() - start, 3),
        )
    report["cases"].append(base)
    print(
        json.dumps(
            {
                k: v
                for k, v in base.items()
                if k
                in {
                    "file",
                    "seconds",
                    "status",
                    "missing",
                    "parser_export_missing",
                    "table_grid_match",
                    "heading_level_mismatches",
                    "error",
                }
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
summary = defaultdict(
    lambda: dict(
        cases=0, converted=0, content_ok=0, headings_ok=0, tables_ok=0, checkpoints=0, recovered=0
    )
)
for c in report["cases"]:
    s = summary[c["group"]]
    s["cases"] += 1
    s["converted"] += "error" not in c
    for key in ["content_ok", "headings_ok", "tables_ok", "checkpoints", "recovered"]:
        s[key] += c.get(key, 0)
for group, values in summary.items():
    members = [c for c in report["cases"] if c["group"] == group and "error" not in c]
    references = {c["file"]: c for c in manifest}
    values["table_cases"] = sum(bool(c["expected_tables"]) for c in members)
    values["table_cases_ok"] = sum(bool(c["expected_tables"]) and c["tables_ok"] for c in members)
    values["multi_level_cases"] = sum(
        len({level for level, _ in references[c["file"]]["headings"]}) > 1 for c in members
    )
    values["multi_level_ok"] = sum(
        c["headings_ok"] and len({level for level, _ in references[c["file"]]["headings"]}) > 1
        for c in members
    )
report["summary"] = dict(summary)
report["production_source_hashes"] = {
    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in (ROOT / "src").rglob("*.py")
}
baseline_path = OUT / "source-baseline.json"
if baseline_path.exists():
    report["production_source_unchanged"] = (
        json.loads(baseline_path.read_text()) == report["production_source_hashes"]
    )
(OUT / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print("SUMMARY", json.dumps(report["summary"], ensure_ascii=False))

if os.environ.get("FIDELITY_REQUIRE_PASS") == "1":
    failures = [
        c["file"]
        for c in report["cases"]
        if not (c["content_ok"] and c["headings_ok"] and c["tables_ok"])
        or c.get("missing_links")
        or (c.get("expected_list_depth") and c["actual_list_depth"] < c["expected_list_depth"])
    ]
    if failures:
        raise SystemExit("Fidelity failures: " + ", ".join(failures))

"""Assertions for the conversion failures observed in the published v0.1 baseline."""

import base64
import hashlib
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

from autorepair.config import load_config
from autorepair.engine import process
from autorepair.html_parser import html_blocks
from autorepair.media import join_ocr_lines
from autorepair.models import Document, Result
from autorepair.parsers import parse
from autorepair.pipeline import export, markdown, write_assets

ROOT = Path(__file__).resolve().parents[1]


def render_html(source):
    blocks, issues = html_blocks(source)
    doc = Document(
        doc_id="test",
        brand="aurora",
        source_name="test.html",
        source_hash="test",
        parser="test",
        blocks=blocks,
        issues=issues,
    )
    return MarkdownIt("commonmark").enable("table").parse(
        markdown(Result(document=doc, rules_hash="test"))
    ), doc


def table_rows(tokens):
    rows = []
    row = None
    for i, t in enumerate(tokens):
        if t.type == "tr_open":
            row = []
        if t.type == "inline" and i and tokens[i - 1].type in {"th_open", "td_open"}:
            row.append("".join(c.content for c in t.children if c.type == "text"))
        if t.type == "tr_close":
            rows.append(row)
    return rows


def test_three_heading_levels_and_anchor():
    tokens, doc = render_html('<h1 id="top">Top</h1><h2>Child</h2><h3>Leaf</h3>')
    assert [t.tag for t in tokens if t.type == "heading_open"] == ["h1", "h2", "h3", "h4"]
    assert [b.heading_level for b in doc.blocks] == [1, 2, 3]
    assert doc.blocks[-1].section_path == ["Top", "Child", "Leaf"]
    assert any('id="top"' in child.content for token in tokens for child in (token.children or []))


def test_merged_grid_and_literal_pipe():
    tokens, _ = render_html(
        '<table><tr><th>Group</th><th>Name</th><th>Value</th></tr><tr><td rowspan="2">A</td><td>X</td><td>B|C</td></tr><tr><td>Y</td><td>35 Nm</td></tr></table>'
    )
    assert table_rows(tokens) == [
        ["Group", "Name", "Value"],
        ["A", "X", "B|C"],
        ["A", "Y", "35 Nm"],
    ]


def test_colspan_and_real_blank_cell():
    tokens, _ = render_html(
        '<table><tr><th colspan="2">Joined</th><th>Third</th></tr><tr><td>A</td><td></td><td>V</td></tr></table>'
    )
    assert table_rows(tokens) == [["Joined", "Joined", "Third"], ["A", "", "V"]]


def test_nested_numbered_list_and_links():
    tokens, _ = render_html(
        '<ol><li>Parent<ul><li>Child</li></ul></li><li>Next</li></ol><p><a href="#top">Back</a> <a href="https://example.com/a(b)">External</a></p>'
    )
    depth = 0
    maximum = 0
    for t in tokens:
        if t.type in {"ordered_list_open", "bullet_list_open"}:
            depth += 1
            maximum = max(maximum, depth)
        elif t.type in {"ordered_list_close", "bullet_list_close"}:
            depth -= 1
    assert maximum == 2
    targets = [
        c.attrGet("href") for t in tokens for c in (t.children or []) if c.type == "link_open"
    ]
    assert targets == ["#top", "https://example.com/a%28b%29"]


def test_image_alt_is_retained_and_cannot_silently_pass(tmp_path):
    p = tmp_path / "x.html"
    p.write_text('<h1>Diagram</h1><img src="missing.png" alt="Visible diagram label">')
    doc = parse(p, "aurora")
    cfg = load_config(ROOT / "configs", "aurora")
    result = process(doc, cfg)
    assert result.status == "review"
    assert {"image.unverified", "image.missing"} <= {i.code for i in result.issues}
    assert "![Visible diagram label]" in markdown(result)


def test_attachments_are_written_and_round_trip(tmp_path):
    content = b"attachment bytes"
    name = hashlib.sha256(content).hexdigest() + ".png"
    doc = Document(
        doc_id="d",
        brand="aurora",
        source_name="x",
        source_hash="x",
        parser="test",
        assets={name: base64.b64encode(content).decode()},
    )
    write_assets(doc, tmp_path)
    assert (tmp_path / "assets" / name).read_bytes() == content
    restored = Document.model_validate_json(doc.model_dump_json())
    assert restored.assets == doc.assets
    doc.assets = {"a" * 64 + ".png": base64.b64encode(content).decode()}
    with pytest.raises(ValueError, match="digest"):
        write_assets(doc, tmp_path)


def test_ocr_overlapping_chunks_preserve_single_character():
    boxes = [[[0, 0], [100, 0], [100, 20], [0, 20]], [[90, 0], [220, 0], [220, 20], [90, 20]]]
    assert join_ocr_lines(["FIGURE_", "_IMAGE_808"], boxes) == "FIGURE_IMAGE_808"
    # Repeated text on a separate line is not silently collapsed.
    assert (
        join_ocr_lines(["Repeat", "Repeat"], [boxes[0], [[0, 50], [100, 50], [100, 70], [0, 70]]])
        == "Repeat\nRepeat"
    )


@pytest.mark.docling
def test_docx_picture_is_preserved_with_ocr(tmp_path):
    pytest.importorskip("docling")
    doc = parse(ROOT / "examples/fidelity/docx_picture.docx", "aurora")
    pictures = [b for b in doc.blocks if b.kind == "picture"]
    assert len(pictures) == 1 and "FIGURE_IMAGE_808" in pictures[0].text
    assert doc.assets and pictures[0].image_src.startswith("assets/")
    cfg = load_config(ROOT / "configs", "aurora")
    result = process(doc, cfg)
    run_id = export([result], cfg, tmp_path)
    assert list((tmp_path / run_id / "review/assets").glob("*.png"))


@pytest.mark.docling
def test_pdf_merged_cells_follow_vector_geometry():
    pytest.importorskip("docling")
    doc = parse(ROOT / "examples/fidelity/pdf_merged.pdf", "aurora")
    table = next(b for b in doc.blocks if b.kind == "table")
    tokens = MarkdownIt("commonmark").enable("table").parse(table.text)
    assert table_rows(tokens) == [
        ["System", "Component", "Value"],
        ["Brake", "Unit A", "A|B"],
        ["Brake", "Unit B", "LINE_C303 LINE_D404"],
    ]
    assert table.location["table_method"] == "vector-cell-geometry"

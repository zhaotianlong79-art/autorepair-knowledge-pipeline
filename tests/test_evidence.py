"""Evidence ownership, source identity, rich display and physical PDF page checks."""

import base64
import hashlib
import io
import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from PIL import Image
from streamlit.testing.v1 import AppTest

from autorepair.chunking import ChunkConfig, chunk_document
from autorepair.cli import main
from autorepair.config import load_config
from autorepair.evidence import (
    build_evidence,
    image_attachment,
    page_targets,
    render_pdf_page,
    table_html,
)
from autorepair.metadata import MetadataPatch
from autorepair.models import Block, Document, Issue, Result
from autorepair.pipeline import export, run
from autorepair.sources import source_snapshot
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cfg():
    return load_config(ROOT / "configs", "aurora")


def fixture():
    return Document.model_validate_json(
        (ROOT / "examples/knowledge/section-links.json").read_text()
    )


def test_original_context_and_related_evidence_do_not_cross_sections():
    doc = fixture()
    chunks = chunk_document(doc)
    table = next(c for c in chunks if "large-table" in c.source_block_ids)
    bundle = build_evidence(doc, table)
    assert bundle["owned"][0]["original"] == next(
        b.original for b in doc.blocks if b.block_id == "large-table"
    )
    assert [p["page"] for p in bundle["pages"]] == [1, 2, 3]
    assert bundle["images"][0]["status"] == "available"
    assert bundle["images"][0]["block_ids"] == ["picture-1"]
    assert "data" not in bundle["images"][0]
    assert not any(r["block_id"] == "check-step" for r in bundle["before"] + bundle["after"])
    assert any(r["block_id"] == "step-11" for r in bundle["related"])
    with pytest.raises(ValueError):
        build_evidence(doc.model_copy(update={"brand": "atlas"}), table)


def test_normalized_spans_are_not_mapped_to_original_offsets():
    doc = fixture().model_copy(
        update={
            "blocks": [
                Block(block_id="alias", text="氧传感器 " * 300, original="oxygen sensor " * 300)
            ]
        }
    )
    chunk = chunk_document(doc, ChunkConfig(64, 100))[1]
    b = build_evidence(doc, chunk)
    assert b["parts"][0]["start"] > 0
    assert b["owned"][0]["original"] == "oxygen sensor " * 300
    assert b["text_mode"] == "normalized"
    assert (
        b["parts"][0]["text"] == doc.blocks[0].text[b["parts"][0]["start"] : b["parts"][0]["end"]]
    )


def test_tables_keep_escaped_pipes_linebreaks_and_duplicate_headers_without_live_markup():
    source = "| Name | Name | Description |\n| --- | --- | --- |\n| A\\|B | X<br>Y | <script>alert(1)</script> ![alt](https://example.com/x.png) [label](https://example.com) |"
    soup = BeautifulSoup(table_html(source), "html.parser")
    assert [x.get_text() for x in soup.select("th")] == ["Name", "Name", "Description"]
    assert soup.select("td")[0].get_text() == "A|B"
    assert soup.select("td")[1].find("br")
    assert not soup.select("script,img,a,iframe")
    assert "<script>" in soup.get_text()
    assert "alt" in soup.get_text() and "label" in soup.get_text()
    assert table_html("plain paragraph") == ""


@pytest.mark.parametrize(
    "src,status",
    [
        ("https://example.com/remote.png", "external"),
        ("../../private.png", "unsupported"),
        ("file:///secret.png", "unsupported"),
        ("assets/" + "0" * 64 + ".png", "missing"),
    ],
)
def test_nonlocal_or_missing_images_are_not_read_or_fetched(src, status):
    assert image_attachment(fixture(), src)["status"] == status


def test_attachment_hash_and_image_encoding_are_both_checked():
    d = fixture()
    src = next(b.image_src for b in d.blocks if b.kind == "picture")
    image = image_attachment(d, src)
    assert image["status"] == "available" and image["data"]
    assert image["rights_status"] == "not_verified" and image["review_required"]
    d.assets[src.removeprefix("assets/")] = base64.b64encode(b"changed").decode()
    assert image_attachment(d, src)["status"] == "invalid"
    data = b"not a png"
    name = hashlib.sha256(data).hexdigest() + ".png"
    d.assets[name] = base64.b64encode(data).decode()
    assert image_attachment(d, "assets/" + name)["status"] == "invalid"


def test_snapshot_survives_upload_temp_file_deletion_but_is_not_in_public_json(tmp_path):
    source = tmp_path / "upload.html"
    raw = b"<h1>Example</h1><p>phone: 13800138000 oxygen sensor</p>"
    source.write_bytes(raw)
    results = run([source], cfg())
    assert source_snapshot(results[0]).data == raw
    source.unlink()
    folder = tmp_path / "out"
    run_id = export(results, cfg(), folder)
    store = Store(folder / "knowledge.sqlite")
    doc = results[0].document
    original = store.source_file("aurora", doc.doc_id)
    assert original["status"] == "available" and original["data"] == raw
    assert store.load_run(run_id) == results
    for p in (folder / run_id).rglob("*"):
        if p.is_file():
            assert b"13800138000" not in p.read_bytes()
    assert "_source" not in results[0].model_dump_json()
    assert source_snapshot(store.load_run(run_id)[0]) is None


def test_normalized_json_cannot_masquerade_as_its_original_source(tmp_path):
    doc = fixture()
    path = tmp_path / "normalized.json"
    path.write_text(doc.model_dump_json())
    result = run([path], cfg())[0]
    assert source_snapshot(result) is None


def test_current_and_explicit_history_evidence_enforce_brand_status_and_freshness(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    doc = fixture().model_copy(update={"issues": []})
    r = Result(document=doc, rules_hash=cfg().digest)
    run_id = store.save([r], cfg())
    chunk = next(c for c in store.chunks("aurora") if "large-table" in c.source_block_ids)
    assert store.evidence("aurora", doc.doc_id, chunk.chunk_id)["source"]["status"] == "missing"
    assert (
        store.attachment(
            "aurora",
            doc.doc_id,
            chunk_document(doc)[0].parts[0].image_src
            or next(b.image_src for b in doc.blocks if b.image_src),
        )["status"]
        == "available"
    )
    with pytest.raises(ValueError):
        store.evidence("atlas", doc.doc_id, chunk.chunk_id)
    with pytest.raises(ValueError):
        store.evidence("aurora", doc.doc_id, "missing")
    store.correct_metadata(
        "aurora",
        doc.doc_id,
        MetadataPatch(document_version="v2"),
        actor="tester",
        reason="new version",
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="已更新"):
        store.evidence("aurora", doc.doc_id, chunk.chunk_id, expected_run_id=run_id)
    with pytest.raises(ValueError, match="已更新"):
        store.attachment(
            "aurora",
            doc.doc_id,
            next(b.image_src for b in doc.blocks if b.image_src),
            expected_run_id=run_id,
        )
    withdrawn = r.model_copy(
        update={
            "status": "review",
            "issues": [Issue(code="test", severity="warning", message="review")],
        }
    )
    newer = store.save([withdrawn], cfg())
    with pytest.raises(ValueError, match="正式库"):
        store.evidence("aurora", doc.doc_id, chunk.chunk_id)
    bundle = store.evidence("aurora", doc.doc_id, chunk.chunk_id, run_id=newer)
    assert bundle["historical"] and bundle["status"] == "review"
    with pytest.raises(ValueError):
        store.evidence("atlas", doc.doc_id, chunk.chunk_id, run_id=run_id)


def test_legacy_attach_requires_exact_bytes_and_is_removed_from_current_access_after_withdrawal(
    tmp_path,
):
    source = tmp_path / "original.html"
    source.write_text("<p>Example</p>")
    doc = fixture().model_copy(
        update={"issues": [], "source_hash": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    store = Store(tmp_path / "db.sqlite")
    r = Result(document=doc, rules_hash=cfg().digest)
    run_id = store.save([r], cfg())
    assert store.source_file("aurora", doc.doc_id)["status"] == "missing"
    wrong = tmp_path / "wrong.html"
    wrong.write_text("<p>Wrong</p>")
    with pytest.raises(ValueError, match="哈希"):
        store.attach_source("aurora", doc.doc_id, wrong)
    store.attach_source("aurora", doc.doc_id, source)
    assert store.source_file("aurora", doc.doc_id)["data"] == source.read_bytes()
    with store.connect() as db:
        db.execute("UPDATE source_files SET data=?", (b"corrupt",))
    assert store.source_file("aurora", doc.doc_id)["status"] == "invalid"
    store.attach_source("aurora", doc.doc_id, source)
    store.save([r.model_copy(update={"status": "review", "issues": fixture().issues})], cfg())
    with pytest.raises(ValueError):
        store.source_file("aurora", doc.doc_id)
    assert store.source_file("aurora", doc.doc_id, run_id=run_id)["status"] == "available"


def test_ambiguous_multi_page_boxes_are_not_assigned_to_wrong_pages():
    targets = page_targets(
        [
            dict(
                block_id="b",
                location={
                    "pages": [1, 2],
                    "bboxes": [{"l": 0, "t": 0, "r": 10, "b": 10, "coord_origin": "TOPLEFT"}],
                },
            )
        ]
    )
    assert [t["page"] for t in targets] == [1, 2]
    assert all(not t["regions"] for t in targets)
    assert not page_targets([dict(block_id="b", location={"pages": [0, -1, True, "2"]})])


def pdf_source(tmp_path):
    from reportlab.pdfgen import canvas

    path = tmp_path / "source.pdf"
    c = canvas.Canvas(str(path), pagesize=(300, 400))
    c.drawString(40, 340, "PAGE ONE - context")
    c.showPage()
    c.drawString(40, 340, "PAGE TWO - table and figure")
    c.rect(40, 160, 220, 140)
    c.line(40, 240, 260, 240)
    c.setFillColorRGB(0.1, 0.4, 0.8)
    c.circle(150, 200, 20, fill=1)
    c.save()
    return path


@pytest.mark.docling
def test_pdf_physical_page_and_top_bottom_origins_render_the_same_region(tmp_path):
    source = pdf_source(tmp_path)
    top = dict(block_id="table", bbox=dict(l=40, t=100, r=260, b=240, coord_origin="TOPLEFT"))
    bottom = dict(block_id="table", bbox=dict(l=40, t=300, r=260, b=160, coord_origin="BOTTOMLEFT"))
    a = render_pdf_page(source.read_bytes(), 2, [top])
    b = render_pdf_page(source.read_bytes(), 2, [bottom])
    assert a["data"] == b["data"]
    assert a["page"] == 2 and a["total_pages"] == 2 and a["highlighted_blocks"] == ["table"]
    image = Image.open(io.BytesIO(a["data"]))
    assert image.getpixel((60, 150)) == (217, 35, 35)
    assert render_pdf_page(source.read_bytes(), 1)["data"] != a["data"]
    with pytest.raises(ValueError):
        render_pdf_page(source.read_bytes(), 3)
    invalid = render_pdf_page(
        source.read_bytes(),
        2,
        [dict(block_id="bad", bbox=dict(l=-1, t=0, r=20, b=20, coord_origin="TOPLEFT"))],
    )
    assert invalid["skipped_regions"] == 1 and not invalid["highlighted_blocks"]


def test_evidence_cli_and_ui_show_tables_images_context_and_unavailable_original(
    tmp_path, monkeypatch, capsys
):
    store = Store(tmp_path / "db.sqlite")
    doc = fixture().model_copy(update={"issues": []})
    store.save([Result(document=doc, rules_hash=cfg().digest)], cfg())
    chunk = next(c for c in store.chunks("aurora") if "large-table" in c.source_block_ids)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "evidence",
            "--brand",
            "aurora",
            "--doc-id",
            doc.doc_id,
            "--chunk-id",
            chunk.chunk_id,
            "--database",
            str(store.path),
        ],
    )
    main()
    bundle = json.loads(capsys.readouterr().out)
    assert bundle["owned"][0]["block_id"] == "large-table"
    assert bundle["images"][0]["status"] == "available"
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    app.text_input(key="knowledge-query").set_value("TESTPART-01").run()
    assert not app.exception
    assert any("<table " in m.value for m in app.markdown)
    assert len(app.get("image")) >= 1
    next(b for b in app.button if b.label == "查看原文、附件与来源定位").click().run()
    assert not app.exception
    assert any("原页无法预览" in info.value for info in app.info)
    assert any(e.label == "解析原文与章节前后文" for e in app.expander)


@pytest.mark.docling
def test_changing_block_on_same_page_reloads_current_geometry(tmp_path, monkeypatch):
    source = pdf_source(tmp_path)
    first = Block(
        block_id="first",
        kind="paragraph",
        original="FIRST",
        text="FIRST",
        location={"pages": [2], "bboxes": [dict(l=40, t=40, r=260, b=80, coord_origin="TOPLEFT")]},
    )
    second = Block(
        block_id="second",
        kind="table",
        original="| X | Y |\n| --- | --- |\n| A | B |",
        text="| X | Y |\n| --- | --- |\n| A | B |",
        location={
            "pages": [2],
            "bboxes": [dict(l=40, t=100, r=260, b=240, coord_origin="TOPLEFT")],
        },
    )
    doc = fixture().model_copy(
        update={
            "blocks": [first, second],
            "issues": [],
            "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
    )
    store = Store(tmp_path / "db.sqlite")
    store.save([Result(document=doc, rules_hash=cfg().digest)], cfg())
    store.attach_source("aurora", doc.doc_id, source)
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    calls = []

    def render(data, page, regions):
        calls.append((page, [r["block_id"] for r in regions]))
        return render_pdf_page(data, page, regions)

    monkeypatch.setattr("autorepair.evidence_ui.render_pdf_page", render)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    app.text_input(key="knowledge-query").set_value("FIRST").run()
    next(b for b in app.button if b.label == "查看原文、附件与来源定位").click().run()
    assert not app.exception and calls[-1] == (2, ["first"])
    next(s for s in app.selectbox if s.label == "定位到来源块").set_value(1).run()
    assert not app.exception and calls[-1] == (2, ["second"])

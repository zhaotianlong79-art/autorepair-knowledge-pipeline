import json
import sqlite3
from pathlib import Path

import pytest

from autorepair.config import load_config
from autorepair.engine import process, signature
from autorepair.models import Block, Document
from autorepair.parsers import html_blocks, parse
from autorepair.pipeline import export, run
from autorepair.privacy import redact
from autorepair.search import search
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cfg(brand="aurora"):
    return load_config(ROOT / "configs", brand)


def document(text, brand="aurora"):
    return Document(
        doc_id="sample",
        brand=brand,
        source_name="sample.html",
        source_hash="test",
        parser="test",
        blocks=[Block(block_id="b1", original=text, text=text, location={"dom": "/p[1]"})],
    )


@pytest.mark.parametrize("brand, expected", [("aurora", "制动控制模块"), ("atlas", "车身控制模块")])
def test_brand_isolation(brand, expected):
    result = process(document("BCM oxygen sensor 35 Nm PN-A100 P0300", brand), cfg(brand))
    assert expected in result.document.blocks[0].text
    assert "氧传感器" in result.document.blocks[0].text
    assert signature(result.document.blocks[0].text) == signature(
        result.document.blocks[0].original
    )


@pytest.mark.parametrize(
    "text",
    [
        "35 Nm PN-A100 P0300 WARNING not",
        "警告：不得修改 12 V",
        "never replace PN-BCM-100",
        "ABS",
        "35 mm / 50 kPa",
    ],
)
def test_critical_tokens_survive(text):
    result = process(document(text), cfg())
    assert signature(result.document.blocks[0].text) == signature(text)


def test_reject_protected_edit(monkeypatch):
    monkeypatch.setattr("autorepair.engine.clean_structure", lambda text: "36 Nm")
    r = process(document("35 Nm"), cfg())
    assert r.status == "failed"
    assert r.document.blocks[0].text == "35 Nm"
    assert "guard.protected" in [i.code for i in r.issues]


def test_idempotent():
    r = process(document("BCM  oxygen sensor 使用 torque wrench。"), cfg())
    again = process(r.document, cfg())
    assert again.document == r.document
    assert not again.changes
    assert process(document("BCM  oxygen sensor 使用 torque wrench。"), cfg()) == r


def test_substring_alias_boundary():
    r = process(document("XBCM BCM BCM123"), cfg())
    assert r.document.blocks[0].text == "XBCM 制动控制模块 BCM123"


def test_html_visibility_and_links():
    blocks, issues = html_blocks(
        '<h1 id="s1">手册</h1><div hidden><p>不应出现</p></div><p style="display: none">不能出现</p><p aria-hidden="true">隐藏</p><p><a href="#s1">正文</a></p>'
    )
    assert [b.text for b in blocks] == ["手册", "正文"]
    assert blocks[-1].links == ["#s1"]
    assert "p[3]" in blocks[-1].location["dom"] or "p[4]" in blocks[-1].location["dom"]
    assert not issues


def test_rendering_warning():
    _, issues = html_blocks("<style>.hide{display:none}</style><p>文本</p>")
    assert issues[0].code == "html.rendering"


def test_unknown_term_and_reference():
    d = document("XCU")
    d.blocks[0].links = ["doc:missing#section"]
    r = process(d, cfg())
    assert r.status == "review"
    assert {i.code for i in r.issues} == {"reference.broken", "term.unknown"}


def test_valid_cross_reference():
    d = document("正文")
    d.blocks[0].links = ["doc:other#s1"]
    assert process(d, cfg(), {"other": {"s1"}}).status == "passed"


def test_duplicate_and_missing_blocks():
    d = document("正文")
    d.blocks.append(d.blocks[0].model_copy())
    d.expected_blocks = 3
    r = process(d, cfg())
    assert r.status == "failed"
    assert {i.code for i in r.issues} >= {
        "structure.duplicate_id",
        "content.missing",
        "content.duplicate",
    }


def test_empty():
    d = document("")
    d.blocks = []
    assert process(d, cfg()).status == "failed"


@pytest.mark.parametrize("key", ["content_html", "html", "body", "content", "正文"])
def test_json_field_aliases(tmp_path, key):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps({key: "<p>oxygen sensor</p>"}))
    d = parse(path, "aurora")
    assert d.blocks[0].location["field_mapping"] == {key: "content"}
    assert process(d, cfg()).document.blocks[0].text == "氧传感器"


def test_json_invalid_field(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"html": 123}')
    r = run([p], cfg())
    assert r[0].status == "failed"


def test_export_privacy_and_persistence(tmp_path):
    d = document("电话：13800138000 车牌：粤A12345 PN-A100")
    d.blocks[0].location["meta"] = {"电话": "电话：13800138000"}
    r = process(d, cfg())
    run_id = export([r], cfg(), tmp_path)
    store = Store(tmp_path / "knowledge.sqlite")
    restored = store.load_run(run_id)
    assert restored == [r]
    assert "13800138000" not in restored[0].model_dump_json()
    assert "13800138000" not in (tmp_path / "knowledge.sqlite").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert "PN-A100" in restored[0].document.blocks[0].text
    for file in (tmp_path / run_id).rglob("*"):
        if file.is_file():
            assert "13800138000" not in file.read_text()
            assert "粤A12345" not in file.read_text()


def test_gate_and_stale_approved_invalidation(tmp_path):
    passed = process(document("正文"), cfg())
    r1 = export([passed], cfg(), tmp_path)
    review = process(document("XCU"), cfg())
    r2 = export([review], cfg(), tmp_path)
    assert (tmp_path / r1 / "knowledge.jsonl").read_text()
    assert not (tmp_path / r2 / "knowledge.jsonl").read_text()
    with sqlite3.connect(tmp_path / "knowledge.sqlite") as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_retrieval_filters_brand_and_status():
    good = process(document("oxygen sensor"), cfg())
    other = process(document("oxygen sensor", "atlas"), cfg("atlas"))
    review = process(document("oxygen sensor XCU"), cfg())
    hits = search([good, other, review], "氧传感器", "aurora")
    assert len(hits) == 1 and hits[0]["text"] == "氧传感器"
    assert not search([good], "氧传感器", "aurora", original=True)
    assert len(search([review], "氧传感器", "aurora", include_review=True)) == 1


def test_part_number_is_not_contact():
    assert redact("部件号 13800138000 PN-13800138000") == "部件号 13800138000 PN-13800138000"


def test_deterministic_unique_batch_id(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<p>正文</p>")
    assert all(r.status == "failed" for r in run([p, p], cfg()))


def test_config_errors(tmp_path):
    import shutil

    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    common = tmp_path / "configs/common.yaml"
    text = common.read_text()
    common.write_text(text + "\nunknown_option: true\n")
    with pytest.raises(ValueError):
        load_config(tmp_path / "configs", "aurora")
    common.write_text(text.replace("action: structure", "action: execute_python"))
    with pytest.raises(ValueError):
        load_config(tmp_path / "configs", "aurora")
    common.write_text(text + "\nbrand: again\n")
    with pytest.raises(ValueError):
        load_config(tmp_path / "configs", "aurora")


@pytest.mark.docling
def test_docx_table_and_provenance():
    pytest.importorskip("docling")
    d = parse(ROOT / "examples/dev/aurora/05-service.docx", "aurora")
    assert any(b.kind == "table" and "35" in b.text for b in d.blocks)
    assert all("item_ref" in b.location for b in d.blocks)
    assert process(d, cfg()).status == "review"


def test_loose_html_text_is_retained():
    blocks, _ = html_blocks("<div>前文<p>中间</p>后文</div>")
    assert [b.text for b in blocks] == ["前文", "中间", "后文"]


def test_table_parts_have_evidence():
    d = parse(ROOT / "examples/dev/aurora/01-manual.html", "aurora")
    r = process(d, cfg())
    mention = next(m for m in r.document.component_mentions if m["part_number"] == "PN-A100")
    assert mention["descriptions"] == ["氧传感器"]
    assert "35 Nm" in mention["evidence"]
    assert mention["model"] == "unknown"


def test_brand_config_requires_explicit_override(tmp_path):
    import shutil

    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    path = tmp_path / "configs/brands/aurora.yaml"
    path.write_text(path.read_text() + "\nrules:\n  - {id: structure, action: structure}\n")
    with pytest.raises(ValueError, match="override"):
        load_config(tmp_path / "configs", "aurora")
    path.write_text(
        path.read_text().replace("action: structure}", "action: structure, overrides: true}")
    )
    assert load_config(tmp_path / "configs", "aurora").rules[0].overrides


def test_bad_alias_configuration(tmp_path):
    import shutil

    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    path = tmp_path / "configs/brands/aurora.yaml"
    path.write_text(
        path.read_text()
        + """\n  - canonical: 另一部件
    aliases: [BCM]
    kind: component
    entity_id: another-part
"""
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        load_config(tmp_path / "configs", "aurora")


def test_manual_review_cannot_override_other_errors():
    from autorepair.models import Issue
    from autorepair.pipeline import confirm_completeness

    d = document("正文")
    d.issues = [Issue(code="parse.completeness", severity="warning", message="需复核")]
    r = process(d, cfg())
    assert confirm_completeness(r, "已逐页核对").status == "passed"
    assert r.status == "review"
    with pytest.raises(ValueError):
        confirm_completeness(r, " ")
    r.issues.append(Issue(code="content.missing", severity="error", message="缺失"))
    checked = confirm_completeness(r, "已核对")
    assert checked.status == "failed" and checked.reviews


def test_contact_metadata_fields_are_redacted():
    d = document("正文")
    d.blocks[0].location["source_fields"] = {
        "phone": "13800138000",
        "车牌": "粤A12345",
        "part_number": "13800138000",
    }
    r = process(d, cfg())
    fields = r.document.blocks[0].location["source_fields"]
    assert fields["phone"] == "[PHONE]" and fields["车牌"] == "[PLATE]"
    assert fields["part_number"] == "13800138000"

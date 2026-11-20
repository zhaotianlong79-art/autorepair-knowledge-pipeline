"""Independent losslessness, context boundaries and approved-index acceptance checks."""

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.chunking import DEFAULT, ChunkConfig, chunk_document
from autorepair.cli import main
from autorepair.config import load_config
from autorepair.models import Block, Document, Issue, Result
from autorepair.parsers import parse
from autorepair.pipeline import export
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cfg():
    return load_config(ROOT / "configs", "aurora")


def fixture():
    return Document.model_validate_json(
        (ROOT / "examples/knowledge/section-links.json").read_text()
    )


def assert_lossless(doc, chunks, *, original=False):
    spans = defaultdict(list)
    for chunk in chunks:
        assert chunk.parts
        assert chunk.source_block_ids == list(dict.fromkeys(p.block_id for p in chunk.parts))
        assert (
            chunk.config_hash
            == ChunkConfig(**{k: chunk.config[k] for k in ["target_chars", "max_chars"]}).digest
        )
        assert chunk.pages == sorted(
            {pn for p in chunk.parts for pn in p.location.get("pages", [])}
        )
        for p in chunk.parts:
            spans[p.block_id].append(p)
        assert all(link.block_id in {b.block_id for b in doc.blocks} for link in chunk.related)
    assert set(spans) == {b.block_id for b in doc.blocks}
    for block in doc.blocks:
        source = block.original if original else block.text
        parts = spans[block.block_id]
        assert parts[0].start == 0
        assert parts[-1].end == len(source)
        assert "".join(p.text for p in parts) == source
        for i, p in enumerate(parts):
            assert p.text == source[p.start : p.end]
            assert p.location == block.location
            assert p.image_src == block.image_src
            if i:
                assert parts[i - 1].end == p.start
    assert len({c.chunk_id for c in chunks}) == len(chunks)


@pytest.mark.parametrize(
    "config",
    [
        DEFAULT,
        ChunkConfig(target_chars=32, max_chars=64),
        ChunkConfig(target_chars=240, max_chars=320),
    ],
)
def test_fixture_has_exact_source_spans_without_loss_or_overlap(config):
    doc = fixture()
    before = doc.model_dump_json()
    chunks = chunk_document(doc, config)
    assert_lossless(doc, chunks)
    assert doc.model_dump_json() == before
    assert chunks == chunk_document(doc, config)
    for c in chunks:
        if c.oversized:
            assert len(c.parts) == 1 and c.parts[0].atomic
        else:
            assert len(c.text) <= config.max_chars


def test_short_paragraphs_merge_but_repeated_and_child_sections_stay_separate():
    chunks = chunk_document(fixture())
    assert any({"intro-a", "intro-b"} <= set(c.source_block_ids) for c in chunks)
    assert not any({"step-11", "step-12"} <= set(c.source_block_ids) for c in chunks)
    repeat = next(c for c in chunks if "repeat-text" in c.source_block_ids)
    assert repeat.section_id == "repeat-remove"
    assert repeat.section_path == ["自制维修切块验证手册", "拆卸流程"]
    assert not repeat.step_ids
    assert not any(
        x.block_id in {"section-warning", "local-warning", "step-11"} for x in repeat.related
    )


def test_steps_keep_complete_table_image_warning_and_cross_page_continuation():
    chunks = chunk_document(fixture())
    table = next(c for c in chunks if "large-table" in c.source_block_ids)
    assert table.oversized
    assert table.step_ids == ["step-11"]
    assert table.section_path == ["自制维修切块验证手册", "拆卸流程"]
    assert table.parts[0].text == next(
        b.text for b in fixture().blocks if b.block_id == "large-table"
    )
    related = {(r.block_id, r.reason) for r in table.related}
    for block_id in ["step-11", "picture-1", "figure-caption", "local-warning", "continued-detail"]:
        assert (block_id, "step-context") in related
    assert ("global-warning", "section-warning") in related
    assert not any(
        r.block_id in {"step-12", "check-step", "section-warning"} for r in table.related
    )
    continuation = next(c for c in chunks if "continued-detail" in c.source_block_ids)
    assert continuation.section_id == "remove"
    assert continuation.step_ids == ["step-11"]
    picture = next(p for c in chunks for p in c.parts if p.block_id == "picture-1")
    assert picture.image_src in ["assets/" + name for name in fixture().assets]
    check = next(c for c in chunks if "check-step" in c.source_block_ids)
    assert check.step_ids == ["check-step"]
    assert any(r.block_id == "global-warning" for r in check.related)
    assert any(r.block_id == "section-warning" for r in check.related)


def test_nested_steps_keep_parent_and_actual_numbers():
    d = fixture().model_copy(
        update={
            "blocks": [
                Block(
                    block_id="s11",
                    kind="list",
                    text="主步骤",
                    original="主步骤",
                    ordered=True,
                    ordinal=11,
                ),
                Block(
                    block_id="s11a",
                    kind="list",
                    text="子步骤",
                    original="子步骤",
                    ordered=True,
                    ordinal=1,
                    list_depth=1,
                ),
                Block(
                    block_id="s12",
                    kind="list",
                    text="下一步",
                    original="下一步",
                    ordered=True,
                    ordinal=12,
                ),
            ]
        }
    )
    chunks = chunk_document(d)
    assert chunks[0].step_ids == ["s11"]
    assert "11. 主步骤" in chunks[0].text and "    1. 子步骤" in chunks[0].text
    assert chunks[1].step_ids == ["s12"] and chunks[1].text == "12. 下一步"


def test_warning_headings_remain_context_and_are_never_split():
    doc = fixture().model_copy(
        update={
            "blocks": [
                Block(block_id="h", kind="heading", text="Procedure", original="Procedure"),
                Block(
                    block_id="w",
                    kind="heading",
                    text="WARNING: " + "keep clear " * 100,
                    original="warning",
                ),
                Block(block_id="s", kind="list", text="Check", original="Check", list_marker="7."),
            ]
        }
    )
    chunks = chunk_document(doc, ChunkConfig(32, 64))
    w = next(c for c in chunks if "w" in c.source_block_ids)
    s = next(c for c in chunks if "s" in c.source_block_ids)
    assert w.oversized and w.parts[0].is_warning
    assert s.section_path == ["Procedure"]
    assert any(r.block_id == "w" and r.reason == "section-warning" for r in s.related)


def test_original_and_normalized_modes_use_their_own_offsets_and_ids():
    doc = fixture().model_copy(
        update={
            "blocks": [Block(block_id="b", text="氧传感器 " * 80, original="oxygen sensor\n" * 80)]
        }
    )
    original = chunk_document(doc, original=True)
    processed = chunk_document(doc)
    assert_lossless(doc, original, original=True)
    assert_lossless(doc, processed)
    assert {c.chunk_id for c in original}.isdisjoint(c.chunk_id for c in processed)
    assert {c.chunk_id for c in processed}.isdisjoint(
        c.chunk_id for c in chunk_document(doc, ChunkConfig(100, 200))
    )


def test_empty_blocks_whitespace_unicode_and_unlabelled_paths_are_preserved():
    doc = fixture().model_copy(
        update={
            "blocks": [
                Block(block_id="empty", text="", original="", section_path=["A"]),
                Block(block_id="spaces", text=" \n\t" * 70, original="", section_path=["A"]),
                Block(
                    block_id="unicode",
                    text="参数🔧35 Nm无空格" * 80,
                    original="",
                    section_path=["B"],
                ),
            ]
        }
    )
    chunks = chunk_document(doc, ChunkConfig(32, 64))
    assert_lossless(doc, chunks)
    assert not any(
        "unicode" in c.source_block_ids and "spaces" in c.source_block_ids for c in chunks
    )
    assert chunk_document(doc.model_copy(update={"blocks": []})) == []
    with pytest.raises(ValueError, match="duplicate"):
        chunk_document(doc.model_copy(update={"blocks": [doc.blocks[0], doc.blocks[0]]}))
    with pytest.raises(ValueError):
        ChunkConfig(200, 100)


def test_persisted_chunks_migrate_repair_and_withdraw_atomically(tmp_path, monkeypatch):
    # A self-authored approved test document, not an approval of any OEM material.
    doc = fixture().model_copy(update={"issues": []})
    approved = Result(document=doc, rules_hash=cfg().digest)
    path = tmp_path / "db.sqlite"
    store = Store(path)
    run_id = store.save([approved], cfg())
    expected = store.chunks("aurora")
    assert expected == chunk_document(doc)
    assert store.chunks("atlas") == []
    assert store.chunks("aurora", "missing") == []
    with store.connect() as db:
        db.execute("DROP TABLE knowledge_chunks")
        db.execute("DROP TABLE document_chunk_state")
    migrated = Store(path)
    assert migrated.chunks("aurora") == expected
    assert migrated.load_run(run_id) == [approved]
    with migrated.connect() as db:
        db.execute("UPDATE document_chunk_state SET config_hash='stale'")
        db.execute(
            "DELETE FROM knowledge_chunks WHERE rowid=(SELECT min(rowid) FROM knowledge_chunks)"
        )
    assert Store(path).chunks("aurora") == expected

    # A derived-index failure must roll back the publication and catalog changes.
    def fail(*args):
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr("autorepair.storage.chunk_document", fail)
    with pytest.raises(RuntimeError):
        migrated.save([approved], cfg())
    assert migrated.chunks("aurora") == expected
    assert migrated.list_documents("aurora")[0]["run_id"] == run_id
    withdrawn = approved.model_copy(
        update={
            "status": "review",
            "issues": [Issue(code="test.review", severity="warning", message="复核")],
        }
    )
    migrated.save([withdrawn], cfg())
    assert migrated.chunks("aurora") == []
    assert migrated.search("TESTPART-01", "aurora") == []
    assert migrated.load_run(run_id) == [approved]


def test_chunk_search_returns_owned_and_related_evidence_without_cross_step_leak(tmp_path):
    doc = fixture().model_copy(update={"issues": []})
    store = Store(tmp_path / "db.sqlite")
    run_id = store.save([Result(document=doc, rules_hash=cfg().digest)], cfg())
    hits = store.search("TESTPART-01", "aurora")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["source_block_ids"] == ["large-table"]
    assert hit["source_run_id"] == run_id and hit["chunk_config_hash"] == DEFAULT.digest
    assert hit["pages"] == [2] and hit["step_ids"] == ["step-11"]
    assert any(b["block_id"] == "picture-1" and b["image_src"] for b in hit["related_blocks"])
    assert any(b["block_id"] == "global-warning" for b in hit["related_blocks"])
    assert all(b["block_id"] != "step-12" for b in hit["related_blocks"])
    assert not store.search("TESTPART-01", "atlas")
    assert not store.search("TESTPART-01", "aurora", k=0)
    assert not store.search("", "aurora")


def test_export_cli_and_ui_expose_current_chunks_and_context(tmp_path, monkeypatch, capsys):
    doc = fixture().model_copy(update={"issues": []})
    approved = Result(document=doc, rules_hash=cfg().digest)
    candidate = Result(
        document=doc.model_copy(update={"doc_id": "candidate"}),
        rules_hash=cfg().digest,
        status="review",
        issues=fixture().issues,
    )
    run_id = export([approved, candidate], cfg(), tmp_path)
    exported = [
        json.loads(line)
        for line in (tmp_path / run_id / "knowledge-chunks.jsonl").read_text().splitlines()
    ]
    assert {c["doc_id"] for c in exported} == {doc.doc_id}
    store = Store(tmp_path / "knowledge.sqlite")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "chunks",
            "--brand",
            "aurora",
            "--doc-id",
            doc.doc_id,
            "--database",
            str(store.path),
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out) == exported
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    app.text_input(key="knowledge-query").set_value("TESTPART-01").run()
    assert not app.exception
    assert any("关联警告" in e.label for e in app.expander)
    assert any("WARNING: Keep hands clear" in c.value for c in app.code)


@pytest.mark.parametrize(
    "source",
    sorted(
        p
        for p in (ROOT / "examples/fidelity").iterdir()
        if p.suffix in {".html", ".htm", ".json"} and p.name != "manifest.json"
    ),
    ids=lambda p: p.name,
)
def test_converted_html_and_json_sources_keep_all_spans(source):
    doc = parse(source, "aurora")
    assert_lossless(doc, chunk_document(doc))


def test_long_step_continuations_keep_number_and_full_step_context(tmp_path):
    doc = fixture().model_copy(
        update={
            "issues": [],
            "blocks": [
                Block(
                    block_id="step-long",
                    kind="list",
                    text="inspect. " * 220,
                    original="inspect. " * 220,
                    list_marker="21.",
                )
            ],
        }
    )
    chunks = chunk_document(doc)
    assert len(chunks) > 1
    assert chunks[1].parts[0].start > 0
    assert chunks[1].parts[0].marker == "21."
    assert any(r.block_id == "step-long" and r.reason == "step-context" for r in chunks[1].related)
    assert_lossless(doc, chunks)


def test_explicit_references_keep_local_target_but_do_not_fetch_external_sources():
    doc = fixture().model_copy(
        update={
            "blocks": [
                Block(
                    block_id="s",
                    kind="list",
                    text="See figure",
                    original="See figure",
                    list_marker="1.",
                    links=["#fig-1", "https://example.com/private#other"],
                ),
                Block(block_id="h", kind="heading", text="Illustrations", original="Illustrations"),
                Block(
                    block_id="fig",
                    kind="picture",
                    text="Figure one",
                    original="Figure one",
                    anchor="fig-1",
                    image_src="assets/figure.png",
                ),
            ]
        }
    )
    chunks = chunk_document(doc)
    assert [(r.block_id, r.reason) for r in chunks[0].related] == [("fig", "explicit-reference")]
    assert chunks[0].parts[0].links == doc.blocks[0].links
    assert chunks[1].parts[-1].anchor == "fig-1"


def test_formal_original_search_uses_original_offsets_and_approval_scope(tmp_path):
    doc = fixture().model_copy(
        update={
            "issues": [],
            "blocks": [
                Block(
                    block_id="alias",
                    text="氧传感器",
                    original="oxygen sensor",
                    location={"pages": [3]},
                )
            ],
        }
    )
    store = Store(tmp_path / "db.sqlite")
    store.save([Result(document=doc, rules_hash=cfg().digest)], cfg())
    assert not store.search("oxygen sensor", "aurora")
    hit = store.search("oxygen sensor", "aurora", original=True)[0]
    assert hit["text_mode"] == "original"
    assert hit["parts"][0]["start"] == 0 and hit["parts"][0]["end"] == len("oxygen sensor")
    assert hit["text"] == "oxygen sensor"
    assert hit["chunk_id"] != store.search("氧传感器", "aurora")[0]["chunk_id"]

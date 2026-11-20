"""Deterministic provider tests: behavior and isolation, not model quality claims."""

import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.cli import main
from autorepair.config import load_config
from autorepair.metadata import MetadataFilter
from autorepair.models import Block, Document, Issue, Result
from autorepair.semantic import (
    DEFAULT_CONFIG,
    EmbeddingConfig,
    SemanticError,
    chunk_text,
    digest,
    fuse,
    windows,
)
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class FakeEmbedder:
    def __init__(self, config=None):
        self.config = config or replace(DEFAULT_CONFIG, model="test/local", dimension=3)
        self.calls = []

    def encode(self, texts, *, query=False):
        self.calls.append((texts, query))
        output = []
        for text in texts:
            vector = [0.0] * self.config.dimension
            vector[
                0
                if any(w in text for w in ["制动", "刹车"])
                else 1
                if any(w in text for w in ["电池", "蓄电池"])
                else 2
            ] = 1.0
            output.append(vector)
        return output


def cfg(brand="aurora"):
    return load_config(ROOT / "configs", brand)


def result(
    doc_id, text="制动液检查", *, brand="aurora", model="sedan", status="passed", logical=""
):
    return Result(
        document=Document(
            doc_id=doc_id,
            brand=brand,
            logical_id=logical,
            source_name=doc_id + ".html",
            source_hash=digest(doc_id),
            parser="self-authored-test",
            model=model,
            year_range="2024",
            blocks=[
                Block(
                    block_id="b1",
                    kind="paragraph",
                    text=text,
                    original=text,
                    section_path=["维修"],
                    location={"pages": [2]},
                )
            ],
        ),
        rules_hash=cfg(brand).digest,
        status=status,
        issues=[]
        if status == "passed"
        else [Issue(code="manual", severity="warning", message="待复核")],
    )


def seed(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save([result("brake"), result("battery", "蓄电池检查")], cfg())
    return store


def test_semantic_synonym_and_provenance_are_real_chunk_evidence(tmp_path):
    store = seed(tmp_path)
    assert not store.search("刹车", "aurora")
    hits = store.search("刹车", "aurora", mode="semantic", embedder=FakeEmbedder())
    assert hits[0]["doc_id"] == "brake" and hits[0]["cosine"] == 1
    assert hits[0]["source_run_id"] and hits[0]["pages"] == [2]
    assert hits[0]["retrieval_mode"] == "semantic" and hits[0]["parts"][0]["text"] == "制动液检查"
    evidence = store.evidence("aurora", "brake", hits[0]["chunk_id"])
    assert evidence


def test_brand_quality_and_metadata_filter_before_encoding(tmp_path):
    store = seed(tmp_path)
    store.save([result("other", brand="atlas")], cfg("atlas"))
    store.save([result("pending", status="review"), result("truck", model="truck")], cfg())
    provider = FakeEmbedder()
    hits = store.search(
        "刹车",
        "aurora",
        mode="hybrid",
        embedder=provider,
        filters=MetadataFilter(model="sedan", year_start=2024, year_end=2024),
    )
    assert {h["doc_id"] for h in hits} == {"brake", "battery"}
    assert all(h["brand"] == "aurora" and h["metadata_match"] for h in hits)
    assert sum(len(texts) for texts, is_query in provider.calls if not is_query) == 2
    assert not store.search(
        "刹车",
        "atlas",
        mode="semantic",
        embedder=provider,
        filters=MetadataFilter(model="unknown-model"),
    )


def test_cache_reuse_restart_and_config_invalidation(tmp_path):
    store = seed(tmp_path)
    provider = FakeEmbedder()
    first = store.maintain_semantic_index(
        "aurora", mode="incremental", embedder=provider, actor="a", reason="r"
    )
    assert first["written"] == 2

    def rowids():
        with store.connect() as db:
            return db.execute("SELECT rowid FROM semantic_vectors ORDER BY rowid").fetchall()

    before = rowids()
    provider.calls.clear()
    store = Store(store.path)
    store.search("刹车", "aurora", mode="hybrid", embedder=provider)
    assert len(provider.calls) == 1 and provider.calls[0][1]
    assert rowids() == before
    other = FakeEmbedder(replace(provider.config, revision="a" * 40))
    report = store.maintain_semantic_index(
        "aurora", mode="incremental", embedder=other, actor="a", reason="r"
    )
    assert report["written"] == 2 and report["pruned"] == 2
    assert store.lifecycle_history("aurora")[-1]["action"] == "semantic-index-incremental"


def test_corrupt_vector_same_count_is_detected_and_repaired(tmp_path):
    store = seed(tmp_path)
    p = FakeEmbedder()
    store.search("刹车", "aurora", mode="semantic", embedder=p)
    with store.connect() as db:
        db.execute("UPDATE semantic_vectors SET payload='[]'")
    check = store.maintain_semantic_index("aurora", embedder=p)
    assert check["dirty"] == 2 and check["written"] == 0
    fixed = store.maintain_semantic_index(
        "aurora", mode="incremental", embedder=p, actor="a", reason="repair"
    )
    assert fixed["written"] == 2
    assert store.maintain_semantic_index("aurora", embedder=p)["dirty"] == 0


def test_rebuild_and_prune_is_brand_scoped(tmp_path):
    store = seed(tmp_path)
    store.save([result("other", brand="atlas")], cfg("atlas"))
    p = FakeEmbedder()
    store.search("刹车", "aurora", mode="semantic", embedder=p)
    store.search("刹车", "atlas", mode="semantic", embedder=p)
    with store.connect() as db:
        other = db.execute(
            "SELECT rowid,payload FROM semantic_vectors WHERE brand='atlas'"
        ).fetchall()
    report = store.maintain_semantic_index(
        "aurora", mode="rebuild", embedder=p, actor="a", reason="r"
    )
    assert report["written"] == 2
    with store.connect() as db:
        assert (
            other
            == db.execute(
                "SELECT rowid,payload FROM semantic_vectors WHERE brand='atlas'"
            ).fetchall()
        )


def test_replacement_withdraw_restore_never_recall_stale_vectors(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save([result("v1", logical="manual")], cfg())
    p = FakeEmbedder()
    store.search("刹车", "aurora", mode="semantic", embedder=p)
    store.save([result("v2", text="蓄电池检查", logical="manual")], cfg())
    assert {h["doc_id"] for h in store.search("刹车", "aurora", mode="hybrid", embedder=p)} == {
        "v2"
    }
    rev = store.versions("aurora")[0]["lifecycle_revision"]
    store.withdraw("aurora", "v2", expected_revision=rev, actor="a", reason="r")
    assert not store.search("刹车", "aurora", mode="semantic", embedder=p)
    report = store.maintain_semantic_index(
        "aurora", mode="incremental", embedder=p, actor="a", reason="r"
    )
    assert report["pruned"] == 2
    rev = store.versions("aurora")[0]["lifecycle_revision"]
    store.restore("aurora", "v1", expected_revision=rev, actor="a", reason="rollback")
    hits = store.search("刹车", "aurora", mode="semantic", embedder=p)
    assert [h["doc_id"] for h in hits] == ["v1"]


@pytest.mark.parametrize("query", ["P0300 怎么排查", "90919-01253 更换", "AB-123 检查"])
def test_exact_identifiers_are_not_semantic_neighbors(tmp_path, query):
    store = Store(tmp_path / "db.sqlite")
    store.save(
        [
            result("wrong", "P0301 90919-01254 AB-124 检查"),
            result("right", "P0300 90919-01253 AB-123 检查"),
        ],
        cfg(),
    )
    for mode in ["semantic", "hybrid"]:
        hits = store.search(query, "aurora", mode=mode, embedder=FakeEmbedder())
        assert [h["doc_id"] for h in hits] == ["right"]
    assert not store.search("P0302", "aurora", mode="semantic", embedder=FakeEmbedder())


def test_oversized_window_all_characters_and_tail_scored(tmp_path):
    text = "|字段|数值|\n" * 700 + "制动尾部证据"
    parts = windows(text)
    assert parts[0]["start"] == 0 and parts[-1]["end"] == len(text)
    assert all(a["end"] >= b["start"] for a, b in zip(parts, parts[1:], strict=False))
    store = Store(tmp_path / "db.sqlite")
    r = result("table", text)
    r.document.blocks[0].kind = "table"
    store.save([r], cfg())
    hits = store.search("刹车", "aurora", mode="semantic", embedder=FakeEmbedder())
    assert hits[0]["text"].endswith("制动尾部证据")
    assert hits[0]["semantic_window"]["end"] == len(chunk_text(store.chunks("aurora")[0]))


@pytest.mark.parametrize("bad", [[[0, 0, 0]], [[math.nan, 0, 1]], [[1, 0]], []])
def test_invalid_vectors_roll_back_derived_cache(tmp_path, bad):
    store = seed(tmp_path)
    p = FakeEmbedder()
    p.encode = lambda texts, **kwargs: bad
    with pytest.raises(SemanticError):
        store.maintain_semantic_index(
            "aurora", mode="incremental", embedder=p, actor="a", reason="r"
        )
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM semantic_vectors").fetchone()[0] == 0
    assert len(store.current("aurora")) == 2


def test_insertion_failure_rolls_back_all_vectors_and_audit(tmp_path):
    store = seed(tmp_path)
    before = store.lifecycle_history("aurora")
    with store.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_second BEFORE INSERT ON semantic_vectors WHEN NEW.doc_id='brake' BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.maintain_semantic_index(
            "aurora", mode="rebuild", embedder=FakeEmbedder(), actor="a", reason="r"
        )
    with store.connect() as db:
        assert not db.execute("SELECT * FROM semantic_vectors").fetchall()
    assert store.lifecycle_history("aurora") == before


def test_missing_model_error_no_silent_fallback_bm25_unaffected(tmp_path, monkeypatch):
    store = seed(tmp_path)

    def missing(config):
        raise SemanticError("模型缺失")

    monkeypatch.setattr("autorepair.semantic.local_embedder", missing)
    with pytest.raises(SemanticError, match="模型缺失"):
        store.search("制动", "aurora", mode="hybrid")
    assert store.search("制动", "aurora")[0]["retrieval_mode"] == "bm25"
    assert store.maintain_semantic_index("aurora")["dirty"] == 2


def test_modes_inputs_and_config_validation(tmp_path):
    store = seed(tmp_path)
    for kwargs in [{"mode": "bad"}, {"mode": "semantic", "original": True}]:
        with pytest.raises(ValueError):
            store.search("query", "aurora", **kwargs)
    with pytest.raises(ValueError, match="400"):
        store.search("x" * 401, "aurora", mode="hybrid")
    assert not store.search(" ", "aurora", mode="semantic")
    assert not store.search("刹车", "aurora", mode="semantic", k=0)
    with pytest.raises(ValueError):
        EmbeddingConfig(revision="main")
    with pytest.raises(ValueError):
        EmbeddingConfig(overlap_chars=256)
    with pytest.raises(ValueError):
        store.maintain_semantic_index("aurora", mode="rebuild")


def test_rrf_is_rank_based_and_preserves_component_scores():
    rows = fuse(
        [("a", 999999), ("b", 1)], [("b", {"cosine": 0.6}), ("a", {"cosine": 0.5})], mode="hybrid"
    )
    assert rows["a"]["score"] == rows["b"]["score"]
    assert rows["a"]["lexical_rank"] == 1 and rows["a"]["semantic_rank"] == 2
    assert rows["b"]["cosine"] == 0.6


def test_cli_search_index_and_historical_rejection(tmp_path, monkeypatch, capsys):
    store = seed(tmp_path)
    monkeypatch.setattr("autorepair.semantic.local_embedder", lambda config: FakeEmbedder(config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "search",
            "刹车",
            "--brand",
            "aurora",
            "--database",
            str(store.path),
            "--mode",
            "hybrid",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)[0]["retrieval_mode"] == "hybrid"
    monkeypatch.setattr(
        sys,
        "argv",
        ["autorepair", "semantic-index", "--brand", "aurora", "--database", str(store.path)],
    )
    main()
    assert json.loads(capsys.readouterr().out)["dirty"] == 0
    run_id = store.list_documents("aurora")[0]["run_id"]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "search",
            "刹车",
            "--brand",
            "aurora",
            "--database",
            str(store.path),
            "--run",
            run_id,
            "--mode",
            "semantic",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_ui_switches_mode_and_retains_evidence(tmp_path, monkeypatch):
    store = seed(tmp_path)
    monkeypatch.setattr("autorepair.semantic.local_embedder", lambda config: FakeEmbedder(config))
    app = tmp_path / "app.py"
    app.write_text(
        f"from autorepair.knowledge_ui import render_knowledge\nfrom autorepair.storage import Store\nfrom pathlib import Path\nrender_knowledge(Store(Path({str(store.path)!r})), 'aurora')\n"
    )
    at = AppTest.from_file(str(app)).run()
    at.selectbox(key="retrieval-mode").set_value("混合 RRF").run()
    at.text_input(key="knowledge-query").set_value("刹车").run()
    assert not at.exception
    assert any("brake.html" in e.value for e in at.markdown)
    assert any("语义相似度" in e.value for e in at.caption)


def test_loader_pins_revision_offline_and_rejects_overlong_tokens(monkeypatch):
    from types import SimpleNamespace

    from autorepair.semantic import LocalEmbedder

    calls = []

    class Tokenizer:
        @staticmethod
        def from_pretrained(name, **options):
            calls.append(("tokenizer", name, options))
            return lambda *args, **kwargs: {"input_ids": SimpleNamespace(shape=(1, 513))}

    class Model:
        config = SimpleNamespace(hidden_size=512, max_position_embeddings=512)

        @classmethod
        def from_pretrained(cls, name, **options):
            calls.append(("model", name, options))
            return cls()

        def to(self, device):
            assert device == "cpu"
            return self

        def eval(self):
            return self

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "transformers", SimpleNamespace(AutoTokenizer=Tokenizer, AutoModel=Model)
    )
    provider = LocalEmbedder()
    assert all(row[2]["local_files_only"] and not row[2]["trust_remote_code"] for row in calls)
    assert all(row[2]["revision"] == DEFAULT_CONFIG.revision for row in calls)
    assert calls[1][2]["use_safetensors"]
    with pytest.raises(SemanticError, match="token"):
        provider.encode(["overlong"])


def test_check_does_not_change_cache_or_audit_and_withdraw_pruning(tmp_path):
    store = seed(tmp_path)
    p = FakeEmbedder()
    store.search("刹车", "aurora", mode="semantic", embedder=p)
    before = store.lifecycle_history("aurora")
    with store.connect() as db:
        snapshot = db.execute("SELECT * FROM semantic_vectors ORDER BY doc_id").fetchall()
    assert store.maintain_semantic_index("aurora", embedder=p)["written"] == 0
    with store.connect() as db:
        assert snapshot == db.execute("SELECT * FROM semantic_vectors ORDER BY doc_id").fetchall()
    assert before == store.lifecycle_history("aurora")
    revision = next(
        v["lifecycle_revision"] for v in store.versions("aurora") if v["doc_id"] == "brake"
    )
    store.withdraw("aurora", "brake", actor="a", reason="r", expected_revision=revision)
    assert store.maintain_semantic_index("aurora", embedder=p)["stale"] == 1
    assert (
        store.maintain_semantic_index(
            "aurora", mode="incremental", embedder=p, actor="a", reason="r"
        )["pruned"]
        == 1
    )

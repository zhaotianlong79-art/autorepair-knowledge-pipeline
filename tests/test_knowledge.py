"""Acceptance checks for approved knowledge accumulated across independent runs."""

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.cli import main
from autorepair.config import load_config
from autorepair.models import Block, Document, Issue, Result
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def config(brand="aurora"):
    return load_config(ROOT / "configs", brand)


def result(doc_id, text="氧传感器维修", *, brand="aurora", status="passed"):
    return Result(
        document=Document(
            doc_id=doc_id,
            brand=brand,
            source_name=f"{doc_id}.html",
            source_hash=f"hash-{doc_id}",
            parser="test",
            model="sedan",
            year_range="2024",
            blocks=[
                Block(
                    block_id="b1",
                    text=text,
                    original=text,
                    section="维修",
                    section_path=["发动机", "维修"],
                    location={"pages": [2]},
                )
            ],
        ),
        rules_hash=config(brand).digest,
        status=status,
        issues=[]
        if status == "passed"
        else [
            Issue(
                code="test.review",
                severity="warning" if status == "review" else "error",
                message="需复核",
            )
        ],
    )


def test_across_batches_survives_restart_with_provenance_and_brand_filter(tmp_path):
    path = tmp_path / "knowledge.sqlite"
    store = Store(path)
    a = store.save([result("first")], config())
    b = store.save([result("second")], config())
    store.save([result("candidate", status="review")], config())
    store.save([result("other", brand="atlas")], config("atlas"))
    restarted = Store(path)
    hits = restarted.search("氧传感器", "aurora")
    assert {h["doc_id"] for h in hits} == {"first", "second"}
    assert {h["source_run_id"] for h in hits} == {a, b}
    assert all(h["model"] == "sedan" and h["year_range"] == "2024" for h in hits)
    assert all(
        h["location"] == {"pages": [2]} and h["section_path"] == ["发动机", "维修"] for h in hits
    )
    assert all(h["rules_hash"] == config().digest for h in hits)
    assert len(restarted.list_documents("aurora")) == 3
    assert len(restarted.list_documents("aurora", status="passed")) == 2
    assert len(restarted.current("aurora")) == 2
    assert not restarted.search("不存在的关键词", "aurora")


@pytest.mark.parametrize("status", ["review", "failed"])
def test_withdrawal_removes_current_hits_but_keeps_history(tmp_path, status):
    store = Store(tmp_path / "db.sqlite")
    old = store.save([result("same")], config())
    newer = store.save([result("same", status=status)], config())
    assert store.search("氧传感器", "aurora") == []
    assert store.current("aurora") == []
    assert store.load_run(old)[0].status == "passed"
    assert store.list_documents("aurora")[0]["run_id"] == newer
    assert store.list_documents("aurora")[0]["status"] == status
    store.save([result("same", "新维修内容")], config())
    assert len(store.current("aurora")) == 1
    assert not store.search("氧传感器", "aurora")
    assert store.search("新维修内容", "aurora")


def test_repeat_save_is_idempotent_and_updates_current_run(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save([result("same")], config())
    last = store.save([result("same")], config())
    assert len(store.list_documents("aurora")) == 1
    hits = store.search("氧传感器", "aurora")
    assert len(hits) == 1 and hits[0]["source_run_id"] == last


def test_old_database_migration_recovers_latest_status_and_provenance(tmp_path):
    path = tmp_path / "old.sqlite"
    old = Store(path)
    run_id = old.save([result("approved"), result("withdrawn")], config())
    old.save([result("withdrawn", status="review")], config())
    with old.connect() as db:
        db.execute("DROP TABLE document_catalog")
    migrated = Store(path)
    assert len(migrated.list_documents("aurora")) == 2
    assert migrated.search("氧传感器", "aurora")[0]["source_run_id"] == run_id
    assert [r.document.doc_id for r in migrated.current("aurora")] == ["approved"]
    assert len(Store(path).list_documents("aurora")) == 2
    assert migrated.load_run(run_id) == [result("approved"), result("withdrawn")]


def test_invalid_publication_is_rejected_before_writing(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    with pytest.raises(ValueError, match="brand"):
        store.save([result("other", brand="atlas")], config())
    with pytest.raises(ValueError, match="Duplicate"):
        store.save([result("same"), result("same")], config())
    inconsistent = result("bad", status="review")
    inconsistent.status = "passed"
    with pytest.raises(ValueError, match="unresolved"):
        store.save([inconsistent], config())
    assert store.list_documents("aurora") == []


def test_cli_defaults_to_current_library_and_marks_explicit_history(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    run_id = store.save([result("first")], config())
    store.save([result("second")], config())
    args = ["autorepair", "search", "氧传感器", "--brand", "aurora", "--database", str(path)]
    monkeypatch.setattr(sys, "argv", args)
    main()
    assert len(json.loads(capsys.readouterr().out)) == 2
    monkeypatch.setattr(sys, "argv", args + ["--run", run_id])
    main()
    hits = json.loads(capsys.readouterr().out)
    assert len(hits) == 1 and hits[0]["historical"] is True
    monkeypatch.setattr(sys, "argv", args + ["--include-review"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    monkeypatch.setattr(
        sys, "argv", ["autorepair", "catalog", "--brand", "aurora", "--database", str(path)]
    )
    main()
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_library_ui_search_without_processing_new_files(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    store.save([result("existing")], config())
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    assert not app.exception
    assert [m.value for m in app.metric] == ["1", "0", "0"]
    app.text_input(key="knowledge-query").set_value("氧传感器").run()
    assert not app.exception
    assert any("氧传感器维修" in c.value for c in app.code)
    assert "processed" not in app.session_state


def test_duplicate_failed_inputs_remain_auditable(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    failed = result("duplicate", status="failed")
    run_id = store.save([failed, failed], config())
    assert len(store.load_run(run_id)) == 2
    assert not store.current("aurora")

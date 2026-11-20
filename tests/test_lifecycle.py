"""Publication, version identity, index repair and raw-source retention acceptance."""

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.cli import main
from autorepair.config import load_config
from autorepair.metadata import MetadataPatch
from autorepair.models import Block, Document, Issue, Result
from autorepair.pipeline import export, run
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cfg(brand="aurora"):
    return load_config(ROOT / "configs", brand)


def result(id, text="oxygen sensor", *, logical="manual-1", status="passed", brand="aurora"):
    d = Document(
        doc_id=id,
        logical_id=logical,
        brand=brand,
        source_name=id + ".html",
        source_hash="hash-" + id,
        parser="test",
        blocks=[Block(block_id="b", text=text, original=text)],
    )
    return Result(
        document=d,
        rules_hash=cfg(brand).digest,
        status=status,
        issues=[]
        if status == "passed"
        else [
            Issue(
                code="test",
                severity="warning" if status == "review" else "error",
                message="pending",
            )
        ],
    )


def version(store, id):
    return next(v for v in store.versions("aurora") if v["doc_id"] == id)


def publish(store, r):
    return store.save([r], cfg(r.document.brand))


def test_new_approved_revision_replaces_only_its_own_logical_brand_scope(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    old = publish(store, result("old", "OLDTERM"))
    publish(store, result("unrelated", "KEEPTERM", logical="other-manual"))
    publish(store, result("foreign", "FOREIGNTERM", brand="atlas"))
    publish(store, result("new", "NEWTERM"))
    assert not store.search("OLDTERM", "aurora")
    assert store.search("NEWTERM", "aurora")[0]["logical_id"] == "manual-1"
    assert store.search("KEEPTERM", "aurora")
    assert store.search("FOREIGNTERM", "atlas")
    assert version(store, "old")["state"] == "superseded"
    assert {r.document.doc_id for r in store.current("aurora")} == {"new", "unrelated"}
    assert store.load_run(old)[0].document.blocks[0].text == "OLDTERM"
    assert not store.chunks("aurora", "old")


@pytest.mark.parametrize("status", ["review", "failed"])
def test_pending_distinct_revision_never_displaces_approved_and_cannot_restore(tmp_path, status):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("old", "OLDTERM"))
    publish(store, result("new", "NEWTERM", status=status))
    assert store.search("OLDTERM", "aurora") and not store.search("NEWTERM", "aurora")
    v = version(store, "new")
    with pytest.raises(ValueError, match="门禁"):
        store.restore(
            "aurora",
            "new",
            actor="tester",
            reason="cannot approve",
            expected_revision=v["lifecycle_revision"],
        )
    assert version(store, "old")["state"] == "active"
    # Editing metadata cannot approve or displace the active version.
    entry = next(e for e in store.list_documents("aurora") if e["doc_id"] == "new")
    store.correct_metadata(
        "aurora",
        "new",
        MetadataPatch(document_version="v2"),
        actor="tester",
        reason="label only",
        expected_revision=entry["metadata_revision"],
    )
    assert version(store, "old")["state"] == "active"


def test_failed_reprocessing_removes_active_without_falling_back_and_latest_guard(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("old", "OLDTERM"))
    publish(store, result("new", "NEWTERM"))
    publish(store, result("new", "NEWTERM", status="failed"))
    assert not store.current("aurora") and not store.chunks("aurora")
    v = version(store, "new")
    with pytest.raises(ValueError):
        store.restore(
            "aurora",
            "new",
            actor="tester",
            reason="old approval is insufficient",
            expected_revision=v["lifecycle_revision"],
        )
    old = version(store, "old")
    store.restore(
        "aurora",
        "old",
        actor="tester",
        reason="rollback approved revision",
        expected_revision=old["lifecycle_revision"],
    )
    assert store.search("OLDTERM", "aurora")
    assert version(store, "new")["state"] == "staged"


def test_withdraw_hold_persists_restart_reimport_and_metadata_edit_until_explicit_restore(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    publish(store, result("v1", "REVONE"))
    v = version(store, "v1")
    store.withdraw(
        "aurora", "v1", actor="tester", reason="withdraw", expected_revision=v["lifecycle_revision"]
    )
    publish(store, result("v1", "REVONE"))
    publish(store, result("v2", "REVTWO"))
    restarted = Store(path)
    assert not restarted.current("aurora")
    assert version(restarted, "v1")["state"] == "withdrawn"
    assert version(restarted, "v2")["state"] == "staged"
    entry = next(e for e in restarted.list_documents("aurora") if e["doc_id"] == "v1")
    restarted.correct_metadata(
        "aurora",
        "v1",
        MetadataPatch(document_version="fixed"),
        actor="tester",
        reason="no publication",
        expected_revision=entry["metadata_revision"],
    )
    assert not restarted.search("REVONE", "aurora")
    v = version(restarted, "v2")
    restarted.restore(
        "aurora",
        "v2",
        actor="tester",
        reason="publish new approved revision",
        expected_revision=v["lifecycle_revision"],
    )
    assert restarted.search("REVTWO", "aurora") and not restarted.search("REVONE", "aurora")


def test_identity_and_source_hash_bindings_are_immutable_and_batch_ambiguity_rejected(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("id"))
    changed = result("id")
    changed.document.source_hash = "other-source"
    with pytest.raises(ValueError, match="绑定"):
        publish(store, changed)
    with pytest.raises(ValueError, match="绑定"):
        publish(store, result("id", logical="different"))
    with pytest.raises(ValueError, match="同一批次"):
        store.save([result("one"), result("two")], cfg())
    assert [v["doc_id"] for v in store.versions("aurora")] == ["id"]
    with pytest.raises(ValueError):
        store.withdraw("atlas", "id", actor="tester", reason="wrong brand", expected_revision=0)


def test_explicit_replace_can_link_legacy_independent_versions_and_audits_identity(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("old", "OLDTERM", logical=""))
    target_run = publish(store, result("new", "NEWTERM", logical=""))
    old = version(store, "old")
    store.replace_version(
        "aurora",
        "old",
        "new",
        actor="tester",
        reason="verified same manual",
        expected_revision=old["lifecycle_revision"],
        expected_target_run_id=target_run,
    )
    assert version(store, "new")["logical_id"] == "old"
    assert version(store, "old")["state"] == "superseded"
    assert not store.search("OLDTERM", "aurora")
    assert store.search("NEWTERM", "aurora")
    assert any(e["action"] == "identity-link" for e in store.lifecycle_history("aurora", "old"))
    # A repeat import with no supplied identity inherits the registered grouping.
    publish(store, result("new", "NEWTERM", logical=""))
    assert version(store, "new")["logical_id"] == "old"
    publish(store, result("named", "NAMEDTERM", logical="separate-explicit-family"))
    current = version(store, "new")
    with pytest.raises(ValueError, match="其他逻辑身份"):
        store.replace_version(
            "aurora",
            "new",
            "named",
            actor="tester",
            reason="bad merge",
            expected_revision=current["lifecycle_revision"],
            expected_target_run_id=version(store, "named")["latest_run_id"],
        )


def test_stale_lifecycle_revision_and_target_snapshot_are_rejected(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("a", logical=""))
    target = publish(store, result("b", logical=""))
    v = version(store, "a")
    publish(store, result("a", logical=""))
    with pytest.raises(ValueError):
        store.withdraw(
            "aurora", "a", actor="tester", reason="stale", expected_revision=v["lifecycle_revision"]
        )
    latest = version(store, "a")
    publish(store, result("b", logical=""))
    with pytest.raises(ValueError):
        store.replace_version(
            "aurora",
            "a",
            "b",
            actor="tester",
            reason="stale target",
            expected_revision=latest["lifecycle_revision"],
            expected_target_run_id=target,
        )


def test_transition_failure_rolls_back_publication_identity_and_audit(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("old", "OLDTERM", logical=""))
    target = publish(store, result("new", "NEWTERM", logical=""))
    before = store.versions("aurora")
    events = store.lifecycle_history("aurora")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr(store, "_replace_chunks", fail)
    with pytest.raises(RuntimeError):
        store.replace_version(
            "aurora",
            "old",
            "new",
            actor="tester",
            reason="rollback",
            expected_revision=version(store, "old")["lifecycle_revision"],
            expected_target_run_id=target,
        )
    assert store.versions("aurora") == before and store.lifecycle_history("aurora") == events
    assert store.search("OLDTERM", "aurora") and store.search("NEWTERM", "aurora")


def test_repeated_import_skips_unchanged_index_and_repair_detects_same_count_corruption(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    r = result("id")
    publish(store, r)
    with store.connect() as db:
        rows = db.execute("SELECT rowid,payload FROM knowledge_chunks").fetchall()
    publish(store, r)
    with store.connect() as db:
        assert db.execute("SELECT rowid,payload FROM knowledge_chunks").fetchall() == rows
        db.execute("UPDATE knowledge_chunks SET payload='corrupt' WHERE brand='aurora'")
    assert len(store.versions("aurora")) == 1
    report = store.maintain_index("aurora")
    assert report["dirty"][0]["reasons"] == ["index_hash"]
    store.maintain_index("aurora", mode="incremental", actor="tester", reason="repair")
    assert not store.maintain_index("aurora")["dirty"]
    assert store.search("oxygen", "aurora")
    # Missing publication rows can be rebuilt only from the current active approved record.
    with store.connect() as db:
        db.execute("DELETE FROM documents WHERE brand='aurora'")
    assert "document_missing" in store.maintain_index("aurora")["dirty"][0]["reasons"]
    store.maintain_index("aurora", mode="rebuild", actor="tester", reason="rebuild")
    assert store.current("aurora")
    v = version(store, "id")
    store.withdraw(
        "aurora", "id", actor="tester", reason="withdraw", expected_revision=v["lifecycle_revision"]
    )
    store.maintain_index("aurora", mode="rebuild", actor="tester", reason="never revive")
    assert not store.current("aurora")


def test_index_repairs_are_brand_scoped_and_atomic(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("a"))
    publish(store, result("b", brand="atlas"))
    with store.connect() as db:
        db.execute("UPDATE document_chunk_state SET config_hash='old'")

    def fail(*args, **kwargs):
        raise RuntimeError("index")

    monkeypatch.setattr(store, "_replace_chunks", fail)
    before = store.lifecycle_history("aurora")
    with pytest.raises(RuntimeError):
        store.maintain_index("aurora", mode="rebuild", actor="tester", reason="rollback")
    assert store.lifecycle_history("aurora") == before
    monkeypatch.undo()
    store.maintain_index("aurora", mode="incremental", actor="tester", reason="only aurora")
    assert store.maintain_index("atlas")["dirty"]
    assert not store.maintain_index("aurora")["dirty"]


def import_file(store, path, logical="manual-source"):
    items = run([path], cfg(), logical_id=logical)
    run_id = publish(store, items[0])
    return items[0].document.doc_id, run_id


def test_source_retention_plan_tombstones_and_explicit_reattach(tmp_path):
    path = tmp_path / "one.html"
    path.write_text("<p>one</p>")
    store = Store(tmp_path / "db.sqlite")
    first, oldrun = import_file(store, path)
    path2 = tmp_path / "two.html"
    path2.write_text("<p>two</p>")
    second, _ = import_file(store, path2)
    assert store.source_file("aurora", first, run_id=oldrun)["status"] == "available"
    plan = store.source_cleanup_plan("aurora")
    assert [i["doc_id"] for i in plan["items"]] == [first]
    result = store.cleanup_sources(
        "aurora", plan_token=plan["plan_token"], actor="tester", reason="remove inactive raw copy"
    )
    assert result["files"] == 1
    assert store.source_file("aurora", first, run_id=oldrun)["status"] == "purged"
    assert store.source_file("aurora", second)["status"] == "available"
    import_file(store, path)  # no silent raw-source resurrection and no publication rollback
    assert store.source_file("aurora", first, run_id=oldrun)["status"] == "purged"
    assert version(store, second)["state"] == "active"
    v = version(store, first)
    store.restore(
        "aurora",
        first,
        actor="tester",
        reason="text-only restore",
        expected_revision=v["lifecycle_revision"],
    )
    assert store.source_file("aurora", first)["status"] == "purged"
    store.attach_source("aurora", first, path)
    assert store.source_file("aurora", first)["status"] == "available"
    assert store.load_run(oldrun)


def test_cleanup_protects_pending_and_revalidates_exact_plan(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    p = tmp_path / "a.html"
    p.write_text("<p>A</p>")
    a, run_id = import_file(store, p)
    v = version(store, a)
    store.withdraw(
        "aurora", a, actor="tester", reason="withdraw", expected_revision=v["lifecycle_revision"]
    )
    plan = store.source_cleanup_plan("aurora")
    v = version(store, a)
    store.restore(
        "aurora",
        a,
        actor="tester",
        reason="changed plan",
        expected_revision=v["lifecycle_revision"],
    )
    with pytest.raises(ValueError, match="清理范围已变化"):
        store.cleanup_sources(
            "aurora", plan_token=plan["plan_token"], actor="tester", reason="stale"
        )
    assert store.source_file("aurora", a)["status"] == "available"
    assert store.source_cleanup_plan("atlas")["files"] == 0
    candidate = result("candidate", status="review", logical="pending")
    publish(store, candidate)
    assert not store.source_cleanup_plan("aurora")["files"]


def test_withdrawn_reprocessing_does_not_export_as_formal_knowledge(tmp_path):
    p = tmp_path / "file.html"
    p.write_text("<p>oxygen sensor</p>")
    items = run([p], cfg(), logical_id="stable-manual")
    out = tmp_path / "out"
    export(items, cfg(), out)
    store = Store(out / "knowledge.sqlite")
    d = items[0].document.doc_id
    v = version(store, d)
    store.withdraw(
        "aurora", d, actor="tester", reason="withdraw", expected_revision=v["lifecycle_revision"]
    )
    run_id = export(run([p], cfg(), logical_id="stable-manual"), cfg(), out)
    assert not (out / run_id / "knowledge.jsonl").read_text()
    assert not (out / run_id / "knowledge-chunks.jsonl").read_text()
    assert (
        json.loads((out / run_id / "publication.json").read_text())[d]["lifecycle"] == "withdrawn"
    )
    with pytest.raises(ValueError):
        run([p, p], cfg(), logical_id="ambiguous")


def test_cli_versions_withdraw_restore_index_and_preview(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("id"))
    common = ["--brand", "aurora", "--database", str(store.path)]
    monkeypatch.setattr(sys, "argv", ["autorepair", "versions", *common])
    main()
    v = json.loads(capsys.readouterr().out)[0]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "withdraw",
            *common,
            "--doc-id",
            "id",
            "--expected-revision",
            str(v["lifecycle_revision"]),
            "--actor",
            "tester",
            "--reason",
            "withdraw",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["changed"]
    monkeypatch.setattr(sys, "argv", ["autorepair", "source-cleanup", *common])
    main()
    assert json.loads(capsys.readouterr().out)["files"] == 0
    monkeypatch.setattr(sys, "argv", ["autorepair", "index", *common])
    main()
    assert not json.loads(capsys.readouterr().out)["dirty"]
    v = version(store, "id")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "restore",
            *common,
            "--doc-id",
            "id",
            "--expected-revision",
            str(v["lifecycle_revision"]),
            "--actor",
            "tester",
            "--reason",
            "restore",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["active_doc_id"] == "id"


def test_ui_withdraw_and_restore_change_actual_retrieval(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("id"))
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    for field in app.text_input:
        if field.label == "生命周期操作人":
            field.set_value("tester")
        if field.label == "生命周期操作原因":
            field.set_value("withdraw demo")
    next(b for b in app.button if b.label == "执行生命周期操作").click().run()
    assert not app.exception and not store.current("aurora")
    next(s for s in app.selectbox if s.label == "生命周期操作").set_value("恢复／回退")
    for field in app.text_input:
        if field.label == "生命周期操作人":
            field.set_value("tester")
        if field.label == "生命周期操作原因":
            field.set_value("restore demo")
    next(b for b in app.button if b.label == "执行生命周期操作").click().run()
    assert not app.exception and store.search("oxygen", "aurora")


def test_cleanup_mid_batch_failure_restores_raw_files_tombstones_and_events(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    ids = []
    for number in range(3):
        p = tmp_path / f"{number}.html"
        p.write_text(f"<p>original {number}</p>")
        ids.append(import_file(store, p))
    plan = store.source_cleanup_plan("aurora")
    assert plan["files"] == 2
    history = store.lifecycle_history("aurora")
    real_event = store._event
    calls = []

    def fail(*args, **kwargs):
        if args[4] == "source-cleanup":
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("second raw delete failed")
        return real_event(*args, **kwargs)

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        store.cleanup_sources(
            "aurora", plan_token=plan["plan_token"], actor="tester", reason="rollback batch"
        )
    assert store.lifecycle_history("aurora") == history
    assert store.source_cleanup_plan("aurora") == plan
    assert all(
        store.source_file("aurora", doc_id, run_id=run_id)["status"] == "available"
        for doc_id, run_id in ids
    )
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM purged_sources").fetchone()[0] == 0


def test_pending_raw_copy_is_protected_and_orphan_index_rows_are_reported(tmp_path):
    p = tmp_path / "candidate.html"
    p.write_text("<p>XCU</p>")
    store = Store(tmp_path / "db.sqlite")
    pending = run([p], cfg(), logical_id="candidate")[0]
    assert pending.status == "review"
    run_id = publish(store, pending)
    assert (
        store.source_file("aurora", pending.document.doc_id, run_id=run_id)["status"] == "available"
    )
    assert store.source_cleanup_plan("aurora")["files"] == 0
    with store.connect() as db:
        db.execute("INSERT INTO knowledge_chunks VALUES ('aurora','orphan','orphan','bad')")
    report = store.maintain_index("aurora")
    assert report["orphan_rows"]["knowledge_chunks"] == 1
    store.maintain_index("aurora", mode="incremental", actor="tester", reason="remove orphan")
    assert store.maintain_index("aurora")["orphan_rows"]["knowledge_chunks"] == 0
    assert not store.current("aurora")


def test_index_repair_uses_approved_run_instead_of_corrupted_document_cache(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    publish(store, result("id", "APPROVEDTERM"))
    bad = result("id", "TAMPEREDTERM").document
    with store.connect() as db:
        db.execute(
            "UPDATE documents SET payload=? WHERE brand=?", (bad.model_dump_json(), "aurora")
        )
    report = store.maintain_index("aurora")
    assert "document_payload" in report["dirty"][0]["reasons"]
    store.maintain_index(
        "aurora", mode="incremental", actor="tester", reason="recover canonical approved body"
    )
    assert store.search("APPROVEDTERM", "aurora") and not store.search("TAMPEREDTERM", "aurora")


def test_publication_export_preserves_registered_identity_and_old_names_do_not_merge(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    one = a / "manual.html"
    two = b / "manual.html"
    one.write_text("<p>first</p>")
    two.write_text("<p>second</p>")
    out = tmp_path / "out"
    first = run([one], cfg())[0]
    second = run([two], cfg())[0]
    export([first, second], cfg(), out)
    store = Store(out / "knowledge.sqlite")
    assert len(store.current("aurora")) == 2
    old = version(store, first.document.doc_id)
    store.replace_version(
        "aurora",
        first.document.doc_id,
        second.document.doc_id,
        actor="tester",
        reason="explicit link",
        expected_revision=old["lifecycle_revision"],
        expected_target_run_id=version(store, second.document.doc_id)["latest_run_id"],
    )
    last = export(run([two], cfg()), cfg(), out)
    normalized = json.loads((out / last / "knowledge.jsonl").read_text())
    assert normalized["logical_id"] == old["logical_id"]
    assert store.current("aurora")[0].document.logical_id == old["logical_id"]


def test_retired_registry_blocks_corrupt_publication_cache_even_before_repair(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    r = result("id", "OLDTERM")
    publish(store, r)
    v = version(store, "id")
    store.withdraw(
        "aurora", "id", actor="tester", reason="withdraw", expected_revision=v["lifecycle_revision"]
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO documents VALUES (?, ?, ?)", ("aurora", "id", r.document.model_dump_json())
        )
        db.execute("UPDATE document_catalog SET lifecycle='active' WHERE brand='aurora'")
    assert not store.current("aurora") and not store.search("OLDTERM", "aurora")
    report = store.maintain_index("aurora")
    assert report["orphan_rows"]["documents"] == 1
    store.maintain_index(
        "aurora", mode="rebuild", actor="tester", reason="remove stale publication cache"
    )
    assert not store.current("aurora")

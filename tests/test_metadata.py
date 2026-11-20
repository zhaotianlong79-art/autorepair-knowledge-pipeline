"""Applicability boundaries, auditable edits, migration and actual entry points."""

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.cli import main
from autorepair.config import load_config
from autorepair.metadata import (
    MetadataFilter,
    MetadataPatch,
    apply_metadata,
    load_manifest,
    snapshot,
    years,
)
from autorepair.models import Applicability, Block, Document, Issue, Result
from autorepair.pipeline import export, run
from autorepair.search import search
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def cfg():
    return load_config(ROOT / "configs", "aurora")


def doc(id="sample", **kw):
    return Document(
        doc_id=id,
        brand="aurora",
        source_name=id + ".html",
        source_hash="same-source",
        parser="test",
        blocks=[
            Block(
                block_id="b",
                text="oxygen sensor",
                original="oxygen sensor",
                location={"pages": [2]},
            )
        ],
        **kw,
    )


def known():
    return apply_metadata(
        doc(),
        MetadataPatch(
            model="Model A", year_range="2020-2022", document_type="manual", document_version="v1"
        ),
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("unknown", (None, None)),
        ("2024", (2024, 2024)),
        ("2020-2022", (2020, 2022)),
        (" 2020 – 2022 ", (2020, 2022)),
    ],
)
def test_year_input(value, expected):
    assert years(value) == expected


@pytest.mark.parametrize(
    "value", ["20-22", "2025-2020", "2024/2025", "1899", "2201", "all", "2020-", "2020,2024"]
)
def test_bad_years_never_become_wildcards(value):
    with pytest.raises(ValueError):
        MetadataPatch(year_range=value)


def test_scope_validation_and_partial_correction():
    d = known()
    changed = apply_metadata(d, MetadataPatch(document_version="v2"))
    assert changed.model == d.model and changed.year_range == d.year_range
    assert d.document_version == "v1"
    with pytest.raises(ValueError):
        Applicability(model="A", year_start=2024)
    with pytest.raises(ValueError):
        Applicability(model="A", year_start=True, year_end=2024)
    with pytest.raises(ValueError):
        MetadataPatch(model="A", applicability=[])
    multi = doc(
        applicability=[
            Applicability(model="A", year_start=2020, year_end=2021),
            Applicability(model="B", year_start=2024, year_end=2025),
        ]
    )
    with pytest.raises(ValueError):
        apply_metadata(multi, MetadataPatch(year_range="2024"))
    empty = apply_metadata(multi, MetadataPatch(applicability=[]))
    assert empty.model == "unknown" and empty.year_range == "unknown"


@pytest.mark.parametrize(
    "model,start,end,include,expected",
    [
        ("A", 2020, 2020, False, True),
        ("a", 2021, 2021, False, True),
        ("A", 2022, 2024, False, False),
        ("A", 2020, 2025, False, True),
        ("B", 2024, 2024, False, True),
        ("B", 2021, 2021, False, False),
        ("C", 2024, 2024, True, False),
        ("A", 2024, 2024, True, False),
    ],
)
def test_model_year_pairs_are_not_a_cartesian_product(model, start, end, include, expected):
    d = doc(
        applicability=[
            Applicability(model="A", year_start=2020, year_end=2021),
            Applicability(model="B", year_start=2024, year_end=2025),
        ]
    )
    assert (
        MetadataFilter(model=model, year_start=start, year_end=end, include_unknown=include).match(
            d
        )
        is not None
    ) == expected


def test_unknown_and_legacy_unparseable_metadata_are_explicit():
    f = MetadataFilter(
        model="A", year_start=2024, year_end=2024, document_type="manual", document_version="v2"
    )
    assert f.match(doc()) is None
    included = f.model_copy(update={"include_unknown": True}).match(doc())
    assert included["uses_unknown"] and included["filtered"]
    assert (
        MetadataFilter(year_start=2024, year_end=2024).match(
            doc(model="A", year_range="bad legacy")
        )
        is None
    )
    assert (
        MetadataFilter(document_type="manual", include_unknown=True).match(
            doc(document_type="service_bulletin")
        )
        is None
    )
    assert (
        MetadataFilter(document_version="v2", include_unknown=True).match(
            doc(document_version="v1")
        )
        is None
    )
    with pytest.raises(ValueError):
        MetadataFilter(year_start=2024)
    with pytest.raises(ValueError):
        MetadataFilter(year_start=2025, year_end=2020)


def test_store_filters_before_ranking_and_isolates_brand_and_approval(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    a = known()
    b = apply_metadata(
        doc("other"),
        MetadataPatch(
            model="Model B",
            year_range="2024",
            document_type="service_bulletin",
            document_version="v2",
        ),
    )
    u = doc("unknown")
    candidate = doc("candidate", model="Model A", year_range="2021")
    store.save(
        [Result(document=d, rules_hash=cfg().digest) for d in [a, b, u]]
        + [
            Result(
                document=candidate,
                rules_hash=cfg().digest,
                status="review",
                issues=[Issue(code="test", severity="warning", message="review")],
            )
        ],
        cfg(),
    )
    f = MetadataFilter(
        model="model a",
        year_start=2021,
        year_end=2021,
        document_type="manual",
        document_version="v1",
    )
    assert [h["doc_id"] for h in store.search("oxygen sensor", "aurora", filters=f)] == ["sample"]
    assert [e["doc_id"] for e in store.list_documents("aurora", status="passed", filters=f)] == [
        "sample"
    ]
    assert not store.search("oxygen sensor", "atlas", filters=f)
    hits = store.search(
        "oxygen sensor", "aurora", filters=f.model_copy(update={"include_unknown": True})
    )
    assert {h["doc_id"] for h in hits} == {"sample", "unknown"}
    assert next(h for h in hits if h["doc_id"] == "unknown")["metadata_match"]["uses_unknown"]
    assert all(h["document_type"] in {"manual", "unknown"} for h in hits)
    assert [
        h["doc_id"]
        for h in search(
            [Result(document=a, rules_hash="x"), Result(document=b, rules_hash="x")],
            "oxygen",
            "aurora",
            filters=f,
        )
    ] == ["sample"]


def test_correction_history_atomicity_concurrency_and_status(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    initial = Result(document=known(), rules_hash=cfg().digest)
    old_run = store.save([initial], cfg())
    entry = store.list_documents("aurora")[0]
    revision = entry["metadata_revision"]
    changed = store.correct_metadata(
        "aurora",
        "sample",
        MetadataPatch(model="Model C", year_range="2024", document_version="v2"),
        actor="tester",
        reason="核对手册修订页",
        expected_revision=revision,
    )
    assert changed["metadata_revision"] == revision + 1 and changed["status"] == "passed"
    current = store.current("aurora")[0].document
    assert (
        current.source_hash == initial.document.source_hash
        and current.blocks == initial.document.blocks
    )
    assert current.model == "Model C" and current.document_version == "v2"
    assert store.load_run(old_run) == [initial]
    history = store.metadata_history("aurora", "sample")
    assert (
        history[-1]["before"]["model"] == "Model A" and history[-1]["after"]["model"] == "Model C"
    )
    assert history[-1]["base_run_id"] == old_run and history[-1]["source"] == "manual"
    assert not store.metadata_history("atlas", "sample")
    with pytest.raises(ValueError, match="更新"):
        store.correct_metadata(
            "aurora",
            "sample",
            MetadataPatch(document_version="v3"),
            actor="another",
            reason="stale",
            expected_revision=revision,
        )

    def fail(*args):
        raise RuntimeError("index failure")

    monkeypatch.setattr(store, "_replace_chunks", fail)
    with pytest.raises(RuntimeError):
        store.correct_metadata(
            "aurora",
            "sample",
            MetadataPatch(document_version="v3"),
            actor="tester",
            reason="rollback",
            expected_revision=changed["metadata_revision"],
        )
    assert store.metadata_history("aurora", "sample") == history
    assert store.current("aurora")[0].document == current


@pytest.mark.parametrize("status", ["review", "failed"])
def test_edit_never_approves_candidates(tmp_path, status):
    store = Store(tmp_path / "db.sqlite")
    item = Result(
        document=doc(),
        rules_hash=cfg().digest,
        status=status,
        issues=[
            Issue(
                code="test",
                severity="warning" if status == "review" else "error",
                message="pending",
            )
        ],
    )
    original = store.save([item], cfg())
    result = store.correct_metadata(
        "aurora",
        "sample",
        MetadataPatch(document_type="manual"),
        actor="tester",
        reason="add type",
        expected_revision=0,
    )
    assert result["status"] == status
    assert not store.search("oxygen", "aurora") and not store.chunks("aurora")
    assert store.load_run(original) == [item]
    with pytest.raises(ValueError):
        store.correct_metadata(
            "aurora",
            "sample",
            MetadataPatch(document_version="v2"),
            actor="",
            reason="missing actor",
            expected_revision=1,
        )


def test_ingest_manifest_defaults_and_reprocessing_preserve_edits(tmp_path):
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text("<p>oxygen sensor</p>")
    b.write_text("<p>Inspection</p>")
    manifest = tmp_path / "metadata.json"
    manifest.write_text(
        json.dumps(
            {
                "b.html": {
                    "applicability": [{"model": "B", "year_start": 2024, "year_end": 2025}],
                    "document_type": "service_bulletin",
                }
            }
        )
    )
    overrides = load_manifest(manifest, [a, b])
    batch = MetadataPatch(
        model="A", year_range="2020-2022", document_type="manual", document_version="v1"
    )
    results = run([a, b], cfg(), metadata=batch, metadata_by_path=overrides)
    assert results[0].document.model == "A" and results[1].document.model == "B"
    assert results[1].document.document_type == "service_bulletin"
    out = tmp_path / "out"
    run_id = export(results, cfg(), out)
    store = Store(out / "knowledge.sqlite")
    id = results[0].document.doc_id
    rev = next(e["metadata_revision"] for e in store.list_documents("aurora") if e["doc_id"] == id)
    store.correct_metadata(
        "aurora",
        id,
        MetadataPatch(model="C"),
        actor="tester",
        reason="correct model",
        expected_revision=rev,
    )
    export(run([a], cfg()), cfg(), out)
    assert next(r.document for r in store.current("aurora") if r.document.doc_id == id).model == "C"
    newer = export(run([a], cfg(), metadata=MetadataPatch(document_version="v2")), cfg(), out)
    d = store.load_run(newer)[0].document
    assert (
        d.model == "C"
        and d.year_range == "2020-2022"
        and d.document_type == "manual"
        and d.document_version == "v2"
    )
    assert (
        json.loads((out / newer / "metadata-history.json").read_text())[id][-1]["after"][
            "document_version"
        ]
        == "v2"
    )
    assert store.load_run(run_id) == results
    manifest.write_text('{"wrong.html":{"model":"A"}}')
    with pytest.raises(ValueError):
        load_manifest(manifest, [a, b])


def test_old_catalog_migration_keeps_existing_data_and_unknowns(tmp_path):
    path = tmp_path / "old.sqlite"
    store = Store(path)
    original = Result(document=doc(model="Legacy", year_range="2020"), rules_hash=cfg().digest)
    run_id = store.save([original], cfg())
    with store.connect() as db:
        for column in ["document_type", "document_version", "applicability", "metadata_revision"]:
            db.execute(f"ALTER TABLE document_catalog DROP COLUMN {column}")
        db.execute("DROP TABLE metadata_history")
    updated = Store(path)
    entry = updated.list_documents("aurora")[0]
    assert entry["model"] == "Legacy" and entry["document_type"] == "unknown"
    assert updated.search(
        "oxygen", "aurora", filters=MetadataFilter(year_start=2020, year_end=2020)
    )
    assert updated.load_run(run_id) == [original]
    assert not updated.metadata_history("aurora", "sample")


def test_cli_metadata_and_history(tmp_path, monkeypatch, capsys):
    path = tmp_path / "a.html"
    path.write_text("<p>oxygen sensor</p>")
    output = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "process",
            str(path),
            "--brand",
            "aurora",
            "--output",
            str(output),
            "--model",
            "A",
            "--year-range",
            "2020-2022",
            "--document-type",
            "manual",
            "--document-version",
            "v1",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    capsys.readouterr()
    database = output / "knowledge.sqlite"
    store = Store(database)
    entry = store.list_documents("aurora")[0]
    common = ["--brand", "aurora", "--database", str(database)]
    monkeypatch.setattr(
        sys, "argv", ["autorepair", "search", "氧传感器", *common, "--model", "A", "--year", "2021"]
    )
    main()
    assert len(json.loads(capsys.readouterr().out)) == 1
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autorepair",
            "metadata-correct",
            *common,
            "--doc-id",
            entry["doc_id"],
            "--expected-revision",
            str(entry["metadata_revision"]),
            "--actor",
            "tester",
            "--reason",
            "new edition",
            "--document-version",
            "v2",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "passed"
    monkeypatch.setattr(
        sys, "argv", ["autorepair", "metadata-history", *common, "--doc-id", entry["doc_id"]]
    )
    main()
    assert json.loads(capsys.readouterr().out)[-1]["after"]["document_version"] == "v2"
    monkeypatch.setattr(
        sys, "argv", ["autorepair", "search", "氧传感器", *common, "--year-range", "2025-2020"]
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2


def test_ui_metadata_input_filter_and_correction(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    store.save([Result(document=known(), rules_hash=cfg().digest)], cfg())
    monkeypatch.setattr("autorepair.storage.Store", lambda _: store)
    app = AppTest.from_file(str(ROOT / "app.py")).run()
    app.radio(key="workspace").set_value("知识库").run()
    app.text_input(key="filter-model").set_value("Model B").run()
    app.text_input(key="knowledge-query").set_value("oxygen").run()
    assert not app.exception and not app.code
    app.text_input(key="filter-model").set_value("Model A").run()
    app.text_input(key="filter-years").set_value("2021").run()
    assert not app.exception and any("oxygen sensor" in c.value for c in app.code)
    for field in app.text_input:
        if field.label == "修正资料版本":
            field.set_value("v2")
        if field.label == "操作人（本地填写）":
            field.set_value("tester")
        if field.label == "修正原因":
            field.set_value("verify edition")
    next(b for b in app.button if b.label == "保存元数据修正").click().run()
    assert not app.exception
    assert store.current("aurora")[0].document.document_version == "v2"
    assert store.metadata_history("aurora", "sample")[-1]["source"] == "manual"


def test_upload_fields_whitespace_and_contact_redaction():
    from autorepair.metadata_ui import patch_from_fields

    assert (
        patch_from_fields({"model": "   ", "year_range": "\t", "document_version": ""}).supplied
        == {}
    )
    changed = apply_metadata(
        doc(), MetadataPatch(model="电话：13800138000", document_version="phone: 13800138000")
    )
    assert "13800138000" not in json.dumps(snapshot(changed), ensure_ascii=False)


def test_unknown_query_year_cannot_silently_disable_filter(tmp_path, monkeypatch):
    from argparse import Namespace

    from autorepair.cli import read_filter

    with pytest.raises(ValueError, match="查询年份"):
        read_filter(
            Namespace(
                year_range="unknown",
                year=None,
                model=None,
                document_type=None,
                document_version=None,
                include_unknown=False,
            )
        )

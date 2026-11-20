"""Evaluation validity: frozen labels, independent trace checks and honest denominators."""

import copy
import csv
import json
import shutil

import pytest

from autorepair.evaluation import (
    BENCHMARK,
    build_corpus,
    evaluate_cases,
    inside,
    load_dataset,
    load_reviews,
    summarize,
    trace_citations,
)
from autorepair.metadata import MetadataFilter
from autorepair.qa import answer


@pytest.fixture
def corpus(tmp_path):
    store, keys = build_corpus(tmp_path / "db.sqlite")
    data, _ = load_dataset()
    return store, keys, data


def test_frozen_fixture_is_distinct_balanced_and_not_falsely_human_reviewed():
    data, freeze = load_dataset()
    assert len(data["cases"]) == 44 and len(data["documents"]) == 20
    assert {c["brand"] for c in data["cases"]} == {"aurora", "atlas"}
    assert not data["annotation"]["independent_human_review"]
    assert data["annotation"]["review_status"] == "pending"
    assert all(d["file"] in freeze["dataset_files"] for d in data["documents"])
    assert load_reviews(BENCHMARK / "review-template.csv", {c["id"] for c in data["cases"]}) == {}


def test_label_or_source_edits_fail_fingerprint_before_scoring(tmp_path):
    base = tmp_path / "benchmark"
    shutil.copytree(BENCHMARK, base)
    with (base / "corpus/a-brake.html").open("a") as stream:
        stream.write("changed")
    with pytest.raises(ValueError, match="摘要"):
        load_dataset(base)


def test_fixture_paths_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="越出"):
        inside(tmp_path, "../elsewhere")


def test_corpus_setup_enforces_expected_lifecycle_and_does_not_overwrite(corpus):
    store, keys, _ = corpus
    states = {
        keys[(brand, v["doc_id"])]: v["state"]
        for brand in ["aurora", "atlas"]
        for v in store.versions(brand)
    }
    assert states["a-filter-old"] == states["t-lamp-old"] == "superseded"
    assert states["a-filter-new"] == "active" and states["a-withdrawn"] == "withdrawn"
    assert states["a-review"] == "staged"
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        build_corpus(store.path)
    assert before == store.path.read_bytes()


def test_scorer_retains_quality_failures_as_data_and_separates_trace(corpus):
    store, keys, data = corpus
    rows = evaluate_cases(store, keys, data, ["bm25"])
    stats = summarize(rows)
    assert stats["cases"] == 44 and stats["answerable"] == 24 and stats["unanswerable"] == 20
    assert stats["answered_trace_integrity"]["denominator"] > 0
    assert all(r["trace_valid"] for r in rows)
    # Inject a policy miss into scorer inputs without requiring the production
    # engine to stay inaccurate in later releases.
    positive = copy.deepcopy(rows[0])
    positive.update(
        gold_answerable=True, answer_status="answered", label_support_match=True, trace_valid=True
    )
    negative = copy.deepcopy(rows[0])
    negative.update(
        gold_answerable=False,
        expected_status="conflicting_evidence",
        answer_status="answered",
        label_support_match=False,
        trace_valid=True,
    )
    example = summarize([positive, negative])
    assert example["answered_trace_integrity"]["rate"] == 1
    assert example["answered_label_support"]["rate"] == 0.5
    assert example["unsafe_negative_answers"] == 1
    assert all(r["review"] is None for r in rows)


def test_zero_denominators_are_unmeasured_not_perfect_or_failed():
    stats = summarize([])
    assert stats["answered_label_support"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert stats["negative_refusal"]["rate"] is None and stats["mrr_at3"] is None


@pytest.mark.parametrize(
    "mutation", ["quote", "page", "rules", "source", "scope", "extra-citation"]
)
def test_independent_trace_check_rejects_corrupted_answer(corpus, mutation):
    store, _, data = corpus
    case = data["cases"][0]
    filters = MetadataFilter.model_validate(case["filters"])
    original = answer(store, case["question"], case["brand"], filters=filters)
    assert original["status"] == "answered" and trace_citations(store, original, filters)
    edited = copy.deepcopy(original)
    if mutation == "quote":
        edited["claims"][0]["text"] = "伪造 999 Nm"
    elif mutation == "page":
        edited["citations"][0]["pages"] = [999]
    elif mutation == "rules":
        edited["citations"][0]["rules_hash"] = "forged"
    elif mutation == "source":
        edited["citations"][0]["source_name"] = "fake.pdf"
    elif mutation == "scope":
        edited["citations"][0]["model"] = "other-model"
    else:
        extra = copy.deepcopy(edited["citations"][0])
        extra["id"] = "unused"
        edited["citations"].append(extra)
    assert not trace_citations(store, edited, filters)


def write_review(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["case_id", "reviewer", "reviewed_at", "verdict", "notes"])
        writer.writerows(rows)


def test_review_import_is_explicit_and_keeps_rejected_labels(tmp_path):
    path = tmp_path / "review.csv"
    write_review(
        path,
        [
            ["a01", "test-reviewer", "2026-11-20", "approve", "测试输入，不是真实复核"],
            ["a02", "test-reviewer", "2026-11-20", "reject", "不同意标签"],
        ],
    )
    rows = load_reviews(path, {"a01", "a02"})
    assert rows["a01"]["verdict"] == "approve" and rows["a02"]["verdict"] == "reject"


@pytest.mark.parametrize(
    "rows",
    [
        [["unknown", "reviewer", "2026-11-20", "approve", ""]],
        [["a01", "", "2026-11-20", "approve", ""]],
        [["a01", "reviewer", "not-date", "approve", ""]],
        [["a01", "reviewer", "2026-11-20", "pass", ""]],
        [["a01", "", "", "", ""], ["a01", "", "", "", ""]],
    ],
)
def test_incomplete_forged_status_or_duplicate_reviews_are_rejected(tmp_path, rows):
    path = tmp_path / "review.csv"
    write_review(path, rows)
    with pytest.raises(ValueError):
        load_reviews(path, {"a01"})


def test_dataset_manifest_is_real_json_not_generated_gold_from_predictions():
    raw = json.loads((BENCHMARK / "dataset.json").read_text())
    assert all(c["supports"] or c["expected_status"] != "answered" for c in raw["cases"])
    assert {"synonym", "fault_code", "part_number", "conflict", "version", "no_answer"} <= {
        c["category"] for c in raw["cases"]
    }

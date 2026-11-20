"""General binding boundaries beyond repair-v1: scopes, units and source geometry."""

import copy
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from autorepair.config import load_config
from autorepair.evaluation import trace_citations
from autorepair.metadata import MetadataFilter, MetadataPatch
from autorepair.pipeline import run
from autorepair.qa import answer
from autorepair.qa_binding import bind, conflicts
from autorepair.qa_language import normalize
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples/qa-binding"
MANIFEST = json.loads((BASE / "manifest.json").read_text())
SCOPE = MetadataFilter(model="nova", year_start=2026, year_end=2026)


@pytest.fixture
def corpus(tmp_path):
    cfg = load_config(ROOT / "configs", "aurora")
    stores = {}
    for name in {c["file"] for c in MANIFEST["cases"]}:
        store = Store(tmp_path / (name + ".sqlite"))
        results = run(
            [BASE / name],
            cfg,
            metadata=MetadataPatch(model="nova", year_range="2026", document_type="manual"),
        )
        assert results[0].status == "passed"
        store.save(results, cfg)
        stores[name] = store
    return stores


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda c: c["id"])
def test_additional_source_cases_keep_originals_and_bind_expected_cell(corpus, case):
    response = answer(corpus[case["file"]], case["question"], "aurora", filters=SCOPE)
    assert response["status"] == case["expected_status"]
    if response["status"] == "answered":
        facts = [f for e in response["citations"] for f in e["numeric_bindings"]]
        assert sorted({f["canonical"] for f in facts}) == sorted(case["expected_values"])
        if case["expected_cell"]:
            assert all([f["row"], f["column"]] == case["expected_cell"] for f in facts)
        assert trace_citations(corpus[case["file"]], response, SCOPE)
        assert all(
            c["text"] == e["text"]
            for c, e in zip(response["claims"], response["citations"], strict=True)
        )


def record(text, kind="table", section="轮胎气压"):
    return {"block_id": "b1", "kind": kind, "text": text, "section_path": [section], "location": {}}


def facts(question, text, kind="table", section="轮胎气压"):
    return bind(normalize(question)[0], [record(text, kind, section)])


@pytest.mark.parametrize(
    "text",
    [
        "|轮胎|冷态气压|\n|---|---|\n|前轮轮胎|220|",  # no unit; do not invent kPa
        "|轮胎|冷态气压(kPa/bar)|\n|---|---|\n|前轮轮胎|220|",  # ambiguous header unit
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|2.2 bar|",  # inconsistent cell/header units
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|220/240|",  # ambiguous multi-value cell
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|1,200|",  # unsupported thousands separator
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|220|999|",  # parser must not discard extra cell
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|220±10 kPa|",
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|≤220 kPa|",
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|220/240 kPa|",
        "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|不超过220 kPa|",
    ],
)
def test_ambiguous_units_values_and_shape_never_guess(text):
    assert not facts("前轮轮胎冷态气压是多少", text)


def test_inline_formatting_and_decimal_normalization_keep_source():
    text = "|轮胎|冷态气压(kPa)|\n|---|---|\n|前轮轮胎|**220.0**|\n|前轮轮胎|220|"
    output = facts("前轮轮胎冷态气压是多少", text)
    assert {f["canonical"] for f in output} == {"220kpa"}
    assert output[0]["source_text"] == "**220.0**" and not conflicts(output)


def test_condition_in_source_title_can_supply_scope_but_cannot_contradict_row():
    text = "|位置|气压(kPa)|\n|---|---|\n|前轮轮胎|220|"
    assert facts("前轮轮胎冷态气压是多少", text, section="轮胎冷态气压")
    assert not facts("前轮轮胎冷态气压是多少", text, section="后轮轮胎冷态气压")


@pytest.mark.parametrize("question", ["非满载前轮轮胎气压是多少", "不是前轮轮胎冷态气压是多少"])
def test_negated_conditions_do_not_mean_the_positive_condition(question):
    text = "|轮胎|满载冷态气压(kPa)|\n|---|---|\n|前轮轮胎|260|"
    assert not facts(question, text)


def test_dimension_names_do_not_swap_same_unit_columns():
    text = "|部件|间隙(mm)|厚度(mm)|\n|---|---|---|\n|制动盘|0.3|14|"
    output = facts("制动盘厚度是多少", text, section="制动盘间隙与厚度")
    assert [f["canonical"] for f in output] == ["14mm"] and output[0]["column"] == 3


def test_unrelated_component_cannot_borrow_heading_subject():
    text = "|部件|扭矩(Nm)|\n|---|---|\n|氧传感器|44|"
    assert not facts("火花塞扭矩是多少", text, section="火花塞安装")
    assert not facts(
        "火花塞扭矩是多少", "氧传感器扭矩为44 Nm。", kind="paragraph", section="火花塞安装"
    )


@pytest.mark.parametrize("span", ["-15–-3", "−15到−3"])
def test_signed_temperature_range_is_one_complete_fact(span):
    output = facts(
        "冷却液温度范围是多少",
        f"冷却液温度演示范围为 {span} °C。",
        kind="paragraph",
        section="冷却液",
    )
    assert [f["canonical"] for f in output] == ["-15–-3°c"]


def test_aliases_are_auditable_and_do_not_change_identifiers_or_negation():
    query = "前胎冷车胎压多少 P0352 PN-TEST-31 禁止改动"
    normalized, info = normalize(query)
    assert "P0352 PN-TEST-31 禁止改动" in normalized
    assert info["version"] and info["config_hash"] and info["replacements"]
    assert info["retrieval_query"] == normalized


def test_original_table_and_warning_remain_even_when_one_cell_is_selected(corpus):
    response = answer(
        corpus["pressure.html"], "前轮轮胎空载冷态气压是多少", "aurora", filters=SCOPE
    )
    assert response["status"] == "answered"
    text = response["claims"][0]["text"]
    assert all(
        v in text for v in ["210", "260", "235", "230", "290", "255", "禁止把热态读数当作冷态标准"]
    )
    assert [f["canonical"] for f in response["citations"][0]["numeric_bindings"]] == ["210kpa"]


def test_ui_shows_focused_value_and_full_source(corpus, tmp_path):
    app = tmp_path / "app.py"
    database = corpus["pressure.html"].path
    app.write_text(
        f"from pathlib import Path\nfrom autorepair.qa_ui import render_qa\nfrom autorepair.storage import Store\nfrom autorepair.metadata import MetadataFilter\nrender_qa(Store(Path({str(database)!r})),'aurora','前轮轮胎满载冷态气压是多少',MetadataFilter(model='nova',year_start=2026,year_end=2026),'bm25')\n"
    )
    at = AppTest.from_file(str(app)).run()
    at.button(key="qa-answer").click().run()
    assert not at.exception and at.table
    assert at.table[0].value.iloc[0]["数值"] == "260 kPa"
    assert any("290" in e.value for e in at.code)


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", "999"),
        ("row", 2),
        ("column", 3),
        ("canonical", "999kpa"),
        ("conditions", {"axle": ["rear"]}),
    ],
)
def test_focused_parameter_tampering_fails_source_audit(corpus, field, value):
    response = answer(
        corpus["pressure.html"], "前轮轮胎空载冷态气压是多少", "aurora", filters=SCOPE
    )
    altered = copy.deepcopy(response)
    altered["citations"][0]["numeric_bindings"][0][field] = value
    assert not trace_citations(corpus["pressure.html"], altered, SCOPE)

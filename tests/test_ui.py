from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_demo_ui_process_and_brand_switch():
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    assert [m.value for m in app.metric] == ["4", "2", "2", "0"]
    assert app.session_state["processed"]["brand"] == "aurora"
    app.selectbox(key="brand").set_value("atlas").run()
    assert not app.exception
    assert any("品牌已切换" in x.value for x in app.info)
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["processed"]["brand"] == "atlas"

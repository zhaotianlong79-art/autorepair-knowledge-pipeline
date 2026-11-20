"""Run with uv run streamlit run app.py --server.address 127.0.0.1."""

import io
import json
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

from autorepair.config import load_config
from autorepair.pipeline import confirm_completeness, export, markdown, report_markdown, run
from autorepair.search import search

ROOT = Path(__file__).parent
st.set_page_config(page_title="维修知识加工工作台", page_icon="🔧", layout="wide")
st.title("维修知识加工工作台")
st.caption("品牌规则 · 来源追踪 · 质量门禁。演示材料使用虚构品牌与自制参数，不用于实际维修。")
with st.sidebar:
    st.subheader("加工设置")
    brand = st.selectbox("品牌", ["aurora", "atlas"], key="brand")
    source = st.radio("输入方式", ["内置演示", "上传文件"])
    ocr = st.checkbox(
        "扫描 PDF 识别", value=False, help="需要 Docling 与模型缓存，首次运行会下载模型。"
    )
    uploaded = (
        st.file_uploader(
            "选择文档", type=["html", "htm", "json", "pdf", "docx"], accept_multiple_files=True
        )
        if source == "上传文件"
        else []
    )
    demo = (
        st.selectbox("演示范围", ["静态 HTML 与 JSON", "全部格式含 PDF 和 DOCX"])
        if source == "内置演示"
        else ""
    )
    start = st.button("开始加工", type="primary", width="stretch")
    st.caption("只有通过门禁的文档进入正式 JSONL；待复核材料单独保存。")
if start:
    try:
        cfg = load_config(ROOT / "configs", brand)
        with st.spinner("正在解析与校验…"):
            if source == "上传文件":
                if not uploaded:
                    st.warning("请先选择文件。")
                    st.stop()
                with tempfile.TemporaryDirectory() as tmp:
                    paths = []
                    for n, item in enumerate(uploaded):
                        path = Path(tmp) / f"{n:03}-{Path(item.name).name}"
                        path.write_bytes(item.getvalue())
                        paths.append(path)
                    results = run(paths, cfg, ocr=ocr)
            else:
                paths = sorted((ROOT / "examples/dev" / brand).glob("*"))
                if demo == "静态 HTML 与 JSON":
                    paths = [p for p in paths if p.suffix in {".html", ".json"}]
                results = run(paths, cfg, ocr=ocr)
            run_id = export(results, cfg, ROOT / "artifacts")
            st.session_state["processed"] = {"brand": brand, "results": results, "run_id": run_id}
    except Exception as exc:
        st.error(f"加工失败：{type(exc).__name__}。请检查文件、配置和解析模型环境。")
state = st.session_state.get("processed")
if not state:
    st.info("选择品牌后点击「开始加工」，可直接体验内置 HTML 与 JSON 样例。")
    st.markdown(
        "#### 可以查看什么\n- 术语与部件名称如何按品牌变化\n- 哪条规则修改了内容\n- 引用、未知缩写和隐私信息的检查结果\n- 每条检索证据对应的原文位置"
    )
    st.stop()
if state["brand"] != brand:
    st.info("品牌已切换，请重新加工以查看当前品牌结果。")
    st.stop()
results = state["results"]
st.caption(f"运行记录 {state['run_id']}")
a, b, c, d = st.columns(4)
a.metric("文档", len(results))
b.metric("通过", sum(r.status == "passed" for r in results))
c.metric("待复核", sum(r.status == "review" for r in results))
d.metric("失败", sum(r.status == "failed" for r in results))
tab1, tab2, tab3 = st.tabs(["加工审阅", "质量问题", "检索对照"])
with tab1:
    index = st.selectbox(
        "选择文档",
        range(len(results)),
        format_func=lambda i: f"{results[i].document.source_name} · {results[i].status}",
    )
    selected = results[index]
    left, right = st.columns(2)
    left.subheader("原文视图 已脱敏")
    left.code("\n\n".join(b.original for b in selected.document.blocks), language=None)
    right.subheader("规范结果")
    right.code("\n\n".join(b.text for b in selected.document.blocks), language=None)
    st.markdown("#### 规则修改记录")
    if selected.changes:
        st.dataframe([c.model_dump() for c in selected.changes], width="stretch")
    else:
        st.caption("没有内容修改。")
    with st.expander("块结构与来源"):
        st.json([b.model_dump() for b in selected.document.blocks])
    if any(i.code == "parse.completeness" for i in selected.issues):
        st.markdown("#### 原文完整性复核")
        checked = st.checkbox("我已自行对照原文件核对正文、表格与关键参数", key=f"checked-{index}")
        note = st.text_input("复核说明", key=f"review-note-{index}")
        if st.button("记录完整性复核", disabled=not checked or not note.strip()):
            results[index] = confirm_completeness(selected, note)
            state["run_id"] = export(
                results, load_config(ROOT / "configs", brand), ROOT / "artifacts"
            )
            st.rerun()
    if selected.reviews:
        st.write("人工复核记录", selected.reviews)
with tab2:
    issues = [{"文档": r.document.source_name, **i.model_dump()} for r in results for i in r.issues]
    if issues:
        st.dataframe(issues, width="stretch")
    else:
        st.success("已实现的质量检查通过。")
    st.caption("PDF/DOCX 需对照原文确认完整性；未知缩写检测仅覆盖大写英文缩写。")
    st.download_button(
        "下载质量报告", report_markdown(results), "quality-report.md", mime="text/markdown"
    )
with tab3:
    query = st.text_input("输入术语、部件号或故障码", "氧传感器")
    review = st.checkbox("在演示检索中包含待复核文档", value=False)
    left, right = st.columns(2)
    for col, original, label in [(left, True, "加工前"), (right, False, "加工后")]:
        col.subheader(label)
        hits = search(results, query, brand, original=original, include_review=review)
        if not hits:
            col.info("没有匹配证据。")
        for hit in hits:
            col.write(f"{hit['doc_id']} / {hit['block_id']} · {hit['status']}")
            col.code(hit["text"], language=None)
            col.caption(json.dumps(hit["location"], ensure_ascii=False))
# Include candidates in explicit folders, never mix them into approved JSONL.
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("quality-report.md", report_markdown(results))
    archive.writestr(
        "knowledge.jsonl",
        "".join(r.document.model_dump_json() + "\n" for r in results if r.status == "passed"),
    )
    archive.writestr(
        "results.json", json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2)
    )
    for n, r in enumerate(results):
        archive.writestr(f"{r.status}/{n + 1:03}.md", markdown(r))
        archive.writestr(f"{r.status}/{n + 1:03}.json", r.document.model_dump_json(indent=2))
st.download_button(
    "下载本次加工结果", buffer.getvalue(), "knowledge-results.zip", mime="application/zip"
)

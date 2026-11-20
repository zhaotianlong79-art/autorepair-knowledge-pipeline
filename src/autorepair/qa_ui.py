"""Button-triggered citation answers; never reuse stale answers across reruns."""

import json

import streamlit as st

from autorepair.evidence_ui import evidence_panel, render_images, render_table
from autorepair.qa import answer
from autorepair.qa_provider import OllamaSelector, load_qa_config


def render_qa(store, brand, question, filters, mode):
    with st.expander("带引用问答"):
        st.caption(
            "使用上方问题与适用范围。请先填写车型和单一年份；默认摘录完整证据，不改写参数。模型选证需显式配置，仅发送问题和脱敏正式正文，不发送图片。"
        )
        engine = st.selectbox("问答方式", ["证据摘录", "Ollama 模型选证"], key="qa-engine")
        path = ""
        if engine == "Ollama 模型选证":
            path = st.text_input(
                "问答配置文件路径",
                key="qa-config-path",
                placeholder="configs/qa/ollama.example.yaml",
            )
            st.caption(
                "指定服务将接收问题和正式证据，请自行确认配置目标；密钥只从配置指定的环境变量读取。"
            )
        if not st.button("基于证据回答", key="qa-answer"):
            return
        try:
            if engine == "Ollama 模型选证" and not path.strip():
                raise ValueError("请填写问答配置文件路径")
            selector = OllamaSelector(load_qa_config(path)) if engine == "Ollama 模型选证" else None
            with st.spinner("正在检索并校验引用"):
                result = answer(
                    store, question, brand, filters=filters, retrieval_mode=mode, selector=selector
                )
        except (ValueError, OSError) as exc:
            st.error(str(exc))
            return
        if result["status"] == "answered":
            st.success(result["message"])
        else:
            st.warning(result["message"])
        st.caption(
            f"状态 {result['status']} · 引擎 {result['engine']} · 品牌 {brand} · 车型 {filters.model or '未选择'} · 年份 {filters.year_start or '未选择'}"
        )
        for citation in result["citations"]:
            st.markdown(f"**[{citation['id']}] {citation['source_name']}**")
            st.caption(
                f"章节 {' / '.join(citation['section_path']) or '未提供'} · 版本 {citation['document_version']} · 页码 {citation['pages'] or '未提供（不虚构页码）'}"
            )
            st.code(citation["text"], language=None)
            for record in citation["records"]:
                render_table(record)
            key = f"qa-{brand}-{citation['doc_id']}-{citation['chunk_id']}-{citation['source_run_id']}"
            render_images(
                store,
                brand,
                citation["doc_id"],
                citation["image_sources"],
                expected_run_id=citation["source_run_id"],
                key=key,
            )
            evidence_panel(
                store,
                brand,
                citation["doc_id"],
                citation["chunk_id"],
                expected_run_id=citation["source_run_id"],
                key=key,
            )
        st.caption(
            "引用文本经来源核对，不代表已自动证明资料与问题语义完全一致。关联依据章节和顺序，操作前仍须核对工况。结果只在本次点击显示，未保存聊天历史。"
        )
        st.download_button(
            "下载问答与引用 JSON",
            data=json.dumps(result, ensure_ascii=False, indent=2),
            file_name="citation-answer.json",
            mime="application/json",
            on_click="ignore",
            key="qa-download",
        )

"""Independent catalog and current approved corpus search for the local workbench."""

import streamlit as st

from autorepair.evidence_ui import evidence_panel, render_images, render_table
from autorepair.lifecycle_ui import lifecycle_controls
from autorepair.metadata_ui import correction_fields, filter_fields
from autorepair.storage import Store


def render_knowledge(store: Store, brand: str):
    st.subheader("知识库")
    st.caption("跨批次累计的当前文档。正式检索仅包含通过项；历史运行仍可在命令行显式查询。")
    entries = store.list_documents(brand)
    columns = st.columns(3)
    for col, status, label in zip(
        columns, ["passed", "review", "failed"], ["正式文档", "待复核", "失败"], strict=True
    ):
        col.metric(
            label,
            sum(
                e["status"] == status and (status != "passed" or e["lifecycle"] == "active")
                for e in entries
            ),
        )
    if not entries:
        st.info("当前品牌尚无入库记录。先在「文档加工」中处理文件，通过项会自动进入正式知识库。")
        return
    lifecycle_controls(store, brand)
    correction_fields(store, brand, entries)
    try:
        filters = filter_fields()
    except ValueError as exc:
        st.error(str(exc))
        return
    selected_status = st.selectbox(
        "目录状态", ["全部", "passed", "review", "failed"], key="catalog-status"
    )
    visible = store.list_documents(
        brand, status=None if selected_status == "全部" else selected_status, filters=filters
    )
    st.dataframe(
        [
            {
                "文档 ID": e["doc_id"],
                "逻辑身份": e["logical_id"],
                "发布状态": e["lifecycle"],
                "来源文件": e["source_name"],
                "车型": e["model"],
                "年份": e["year_range"],
                "状态": e["status"],
                "资料类型": e["document_type"],
                "版本": e["document_version"],
                "元数据修订": e["metadata_revision"],
                "来源运行": e["run_id"],
                "更新时间": e["updated_at"],
            }
            for e in visible
        ],
        width="stretch",
    )
    query = st.text_input(
        "查询正式知识库", key="knowledge-query", placeholder="术语、部件号、故障码或问题关键词"
    )
    if not query.strip():
        return
    hits = store.search(query, brand, filters=filters)
    if not hits:
        st.info("正式知识库没有匹配证据。请检查品牌、关键词及文档审核状态。")
    for hit in hits:
        st.markdown(f"**{hit['source_name']} · {hit['doc_id']} / {hit['chunk_id']}**")
        st.caption(" / ".join(hit["section_path"]) or hit["section"] or "未标注章节")
        if hit["metadata_match"]["uses_unknown"]:
            st.warning("此结果包含未知适用字段，不能确认适用于所选车辆。")
        st.code(hit["text"], language=None)
        for part in hit["parts"]:
            render_table(part)
        st.caption(f"类型 {hit['document_type']} · 资料版本 {hit['document_version']}")
        st.caption(
            f"车型 {hit['model']} · 年份 {hit['year_range']} · 来源运行 {hit['source_run_id']} · 相关性 {hit['score']}"
        )
        st.caption(
            f"来源块 {', '.join(hit['source_block_ids'])} · 页码 {hit['pages'] or '未提供'} · 步骤 {', '.join(hit['step_ids']) or '无'}"
        )
        if hit["oversized"]:
            st.caption("此块保留完整表格、图像或警告，超过常规长度限制。")
        if hit["related_blocks"]:
            with st.expander("关联警告、步骤及图表上下文"):
                st.caption("关联依据章节和阅读顺序推断，需对照原文确认适用关系。")
                for related in hit["related_blocks"]:
                    st.caption(
                        f"{related['block_id']} · {related['kind']} · {', '.join(related['reasons'])}"
                    )
                    st.code(related["text"], language=None)
                    render_table(related)
                    if related["image_src"]:
                        st.caption(f"图片资源 {related['image_src']}")
                    st.json(related["location"])
        view_key = f"{brand}-{hit['doc_id']}-{hit['chunk_id']}-{hit['source_run_id']}"
        render_images(
            store,
            brand,
            hit["doc_id"],
            hit["image_sources"],
            expected_run_id=hit["source_run_id"],
            key=view_key,
        )
        evidence_panel(
            store,
            brand,
            hit["doc_id"],
            hit["chunk_id"],
            expected_run_id=hit["source_run_id"],
            key=view_key,
        )
        with st.expander("来源位置与版本"):
            st.json(
                {
                    k: hit[k]
                    for k in [
                        "brand",
                        "source_hash",
                        "rules_hash",
                        "location",
                        "parts",
                        "chunk_config",
                        "chunk_config_hash",
                    ]
                }
            )
    st.caption(
        "分数用于相关性排序，不代表答案正确概率。当前采用关键词检索，语义检索和问答按计划后续实现。"
    )

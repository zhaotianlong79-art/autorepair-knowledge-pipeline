"""Metadata forms shared by upload, catalog filtering and auditable corrections."""

import json

import streamlit as st

from autorepair.metadata import TYPE_LABELS, MetadataFilter, MetadataPatch, years


def patch_from_fields(fields):
    values = {k: v.strip() if isinstance(v, str) else v for k, v in fields.items() if v is not None}
    values = {k: v for k, v in values.items() if v != ""}
    if "applicability" in values and isinstance(values["applicability"], str):
        values["applicability"] = json.loads(values["applicability"])
    return MetadataPatch.model_validate(values)


def ingest_fields(uploaded):
    with st.expander("车型、年份和资料信息（可选）"):
        st.caption("留空保留文件已有信息；未知不会解释为通用。批次值可由单文件行覆盖。")
        values = {
            "model": st.text_input("批次车型", key="input-model"),
            "year_range": st.text_input("批次年份（YYYY 或 YYYY-YYYY）", key="input-years"),
            "document_type": st.selectbox(
                "批次资料类型",
                ["", *TYPE_LABELS],
                format_func=lambda x: TYPE_LABELS.get(x, "保留原值"),
                key="input-type",
            ),
            "document_version": st.text_input("批次资料版本", key="input-version"),
            "applicability": st.text_area(
                "多车型适用范围 JSON（可选，与批次车型／年份二选一）", key="input-scopes"
            ),
        }
        rows = []
        if uploaded:
            st.caption(
                "logical_id 是稳定文档身份：同一身份的新版本通过后替换活动版本，每批每个身份仅一个文件。资料类型用 manual、service_bulletin 等代码。"
            )
            rows = st.data_editor(
                [
                    dict(
                        file=f"{i:03} / {item.name}",
                        logical_id="",
                        model="",
                        year_range="",
                        document_type="",
                        document_version="",
                        applicability="",
                    )
                    for i, item in enumerate(uploaded)
                ],
                disabled=["file"],
                hide_index=True,
                key="file-metadata",
            )
    return values, rows


def filter_fields():
    with st.expander("适用范围过滤"):
        model = st.text_input("筛选车型", key="filter-model")
        year = st.text_input("筛选年份或年份范围", key="filter-years")
        kind = st.selectbox(
            "筛选资料类型",
            ["", *TYPE_LABELS],
            format_func=lambda x: TYPE_LABELS.get(x, "全部类型"),
            key="filter-type",
        )
        version = st.text_input("筛选资料版本", key="filter-version")
        include = st.checkbox("包含过滤字段未知的资料（适用性未确认）", key="filter-unknown")
    start, end = years(year) if year.strip() else (None, None)
    if year.strip() and start is None:
        raise ValueError("筛选年份须为 YYYY 或 YYYY-YYYY，未知项请使用复选框")
    return MetadataFilter(
        model=model.strip() or None,
        year_start=start,
        year_end=end,
        document_type=kind or None,
        document_version=version.strip() or None,
        include_unknown=include,
    )


def correction_fields(store, brand, entries):
    with st.expander("元数据修正与历史"):
        doc_id = st.selectbox(
            "选择修正文档",
            [e["doc_id"] for e in entries],
            format_func=lambda v: next(
                e["source_name"] + " / " + v for e in entries if e["doc_id"] == v
            ),
            key=f"metadata-doc-{brand}",
        )
        entry = next(e for e in entries if e["doc_id"] == doc_id)
        st.json(
            {
                k: entry[k]
                for k in [
                    "model",
                    "year_range",
                    "applicability",
                    "document_type",
                    "document_version",
                    "metadata_revision",
                    "status",
                ]
            }
        )
        st.caption(
            "只修改填写的字段；单车型年份用 YYYY 或 YYYY-YYYY，多车型请提交完整适用范围。修正保留当前审核状态。"
        )
        with st.form(f"metadata-form-{brand}-{doc_id}-{entry['metadata_revision']}"):
            fields = {
                "model": st.text_input("修正车型"),
                "year_range": st.text_input("修正年份"),
                "document_type": st.selectbox(
                    "修正资料类型",
                    ["", *TYPE_LABELS],
                    format_func=lambda x: TYPE_LABELS.get(x, "保持不变"),
                ),
                "document_version": st.text_input("修正资料版本"),
                "applicability": st.text_area("修正完整适用范围 JSON"),
            }
            actor = st.text_input("操作人（本地填写）")
            reason = st.text_input("修正原因")
            submitted = st.form_submit_button("保存元数据修正")
        if submitted:
            try:
                store.correct_metadata(
                    brand,
                    doc_id,
                    patch_from_fields(fields),
                    actor=actor,
                    reason=reason,
                    expected_revision=entry["metadata_revision"],
                )
                st.success("已保存元数据修正，审核状态保持不变。")
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        history = store.metadata_history(brand, doc_id)
        if history:
            st.json(history)
            st.download_button(
                "下载元数据修正记录",
                json.dumps(history, ensure_ascii=False, indent=2),
                "metadata-history.json",
                mime="application/json",
            )
        else:
            st.caption("暂无元数据变更记录。")

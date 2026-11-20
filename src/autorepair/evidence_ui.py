"""Human-readable evidence views without rendering source HTML or fetching image URLs."""

import json

import streamlit as st

from autorepair.evidence import page_targets, render_pdf_page, table_html


def render_table(record):
    if record["kind"] == "table":
        html = table_html(record["text"])
        if html:
            st.markdown(html, unsafe_allow_html=True)
        else:
            st.info("表格未能结构化展示，完整 Markdown 保留在文本视图中。")


def render_images(store, brand, doc_id, sources, *, run_id=None, expected_run_id=None, key=""):
    for src in dict.fromkeys(sources):
        try:
            image = store.attachment(
                brand, doc_id, src, run_id=run_id, expected_run_id=expected_run_id
            )
        except ValueError as exc:
            st.warning(str(exc))
            continue
        if image["status"] == "available":
            st.image(image["data"], caption="来源附件 · " + src)
            st.caption("授权范围未核验；图片及图内个人信息需人工核对，附件校验不等于内容审核。")
            st.download_button(
                "下载图片附件",
                image["data"],
                src.rsplit("/", 1)[-1],
                mime="image/png",
                key=f"image-{key}-{src}",
            )
        else:
            st.warning(image["message"])
            st.code(src, language=None)


def evidence_panel(store, brand, doc_id, chunk_id, *, run_id=None, expected_run_id=None, key=""):
    if not st.button(
        "查看原文、附件与来源定位", key=f"open-evidence-{key}"
    ) and not st.session_state.get(f"evidence-open-{key}"):
        return
    st.session_state[f"evidence-open-{key}"] = True
    try:
        bundle = store.evidence(
            brand, doc_id, chunk_id, run_id=run_id, expected_run_id=expected_run_id
        )
    except (ValueError, KeyError) as exc:
        st.warning(str(exc))
        return
    if bundle["historical"]:
        st.info(f"历史运行证据 · {bundle['status']}；不代表当前正式库状态。")
    st.caption(bundle["original_label"])
    with st.expander("解析原文与章节前后文", expanded=True):
        for title, records in [
            ("前文", bundle["before"]),
            ("命中源块", bundle["owned"]),
            ("后文", bundle["after"]),
        ]:
            if records:
                st.text(title)
            for record in records:
                st.caption(record["block_id"] + " · " + record["kind"])
                st.code(record["original"], language=None)
        st.caption("规范化片段的字符偏移不能直接作为解析原文的字符位置。")
    try:
        source = store.source_file(brand, doc_id, run_id=run_id, expected_run_id=expected_run_id)
    except ValueError as exc:
        st.warning(str(exc))
        return
    records = list(
        {
            r["block_id"]: r
            for r in bundle["owned"] + bundle["related"] + bundle["before"] + bundle["after"]
        }.values()
    )
    selected = st.selectbox(
        "定位到来源块",
        range(len(records)),
        format_func=lambda i: records[i]["block_id"] + " · " + records[i]["kind"],
        key=f"source-block-{key}",
    )
    record = records[selected]
    st.caption("当前定位源块的脱敏解析原文")
    st.code(record["original"], language=None)
    st.json(record["location"])
    if source["status"] != "available":
        st.info(source["message"])
    else:
        st.caption("原件仅本地保留，未作脱敏；与公开的脱敏解析视图分开。")
        st.download_button(
            "下载本地原件（未脱敏）",
            source["data"],
            source["name"],
            mime=source["mime"],
            key=f"source-download-{key}",
        )
        if source["suffix"] == ".pdf":
            targets = page_targets([record])
            if not targets:
                st.info("此来源块没有可核验的 PDF 页码，不猜测原页。")
            else:
                page_number = st.selectbox(
                    "来源页码（PDF 物理页，从 1 开始）",
                    [t["page"] for t in targets],
                    format_func=lambda p: f"第 {p} 页",
                    key=f"source-page-{key}",
                )
                target = next(t for t in targets if t["page"] == page_number)
                try:
                    preview = render_pdf_page(source["data"], target["page"], target["regions"])
                    st.image(
                        preview["data"],
                        caption=f"PDF 第 {preview['page']}/{preview['total_pages']} 页 · 红框为可验证的来源区域",
                    )
                    if preview["skipped_regions"] or not preview["highlighted_blocks"]:
                        st.caption("部分或全部坐标不足以可靠标框，已显示原页，未推测命中区域。")
                except ImportError:
                    st.info("PDF 页预览需要解析扩展依赖；仍可下载原件并按物理页码核对。")
                except Exception:
                    st.warning("无法渲染此来源页，请下载原件核对页码与来源信息。")
        else:
            st.caption(
                "此格式不提供稳定 PDF 页码；使用上方 DOM／JSON 路径／解析项定位，并下载原件核对。"
            )
    st.download_button(
        "下载证据与来源信息",
        json.dumps(bundle, ensure_ascii=False, indent=2),
        "evidence.json",
        mime="application/json",
        key=f"evidence-json-{key}",
    )

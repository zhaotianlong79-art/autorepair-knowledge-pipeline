"""Version publication controls, index maintenance and concrete source cleanup previews."""

import streamlit as st


def lifecycle_controls(store, brand):
    versions = store.versions(brand)
    if not versions:
        return
    with st.expander("文档版本与发布状态"):
        st.caption("通过是质量状态；只有活动版本进入正式检索。撤下不会删除历史审计。")
        st.dataframe(
            [
                {
                    k: v[k]
                    for k in [
                        "logical_id",
                        "doc_id",
                        "source_name",
                        "document_version",
                        "quality",
                        "state",
                        "lifecycle_revision",
                    ]
                }
                for v in versions
            ],
            hide_index=True,
        )
        selected = st.selectbox(
            "选择文档版本",
            [v["doc_id"] for v in versions],
            format_func=lambda d: next(
                v["source_name"] + " / " + d for v in versions if v["doc_id"] == d
            ),
            key=f"life-doc-{brand}",
        )
        v = next(v for v in versions if v["doc_id"] == selected)
        with st.form(f"life-form-{brand}-{selected}-{v['lifecycle_revision']}"):
            action = st.selectbox("生命周期操作", ["撤下", "恢复／回退", "替换为另一版本"])
            targets = [t for t in versions if t["doc_id"] != selected]
            target = st.selectbox(
                "替换目标（仅替换操作使用）",
                [""] + [t["doc_id"] for t in targets],
                key=f"life-target-{brand}-{selected}",
            )
            actor = st.text_input("生命周期操作人")
            reason = st.text_input("生命周期操作原因")
            apply = st.form_submit_button("执行生命周期操作")
        if apply:
            try:
                if action == "撤下":
                    store.withdraw(
                        brand,
                        selected,
                        actor=actor,
                        reason=reason,
                        expected_revision=v["lifecycle_revision"],
                    )
                elif action == "恢复／回退":
                    store.restore(
                        brand,
                        selected,
                        actor=actor,
                        reason=reason,
                        expected_revision=v["lifecycle_revision"],
                    )
                else:
                    t = next((x for x in targets if x["doc_id"] == target), None)
                    if not t:
                        raise ValueError("请选择替换目标")
                    store.replace_version(
                        brand,
                        selected,
                        target,
                        actor=actor,
                        reason=reason,
                        expected_revision=v["lifecycle_revision"],
                        expected_target_run_id=t["latest_run_id"],
                    )
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
        st.json(store.lifecycle_history(brand, v["logical_id"]))
    with st.expander("索引维护"):
        if st.button("检查当前索引", key=f"index-check-{brand}"):
            st.json(store.maintain_index(brand))
        with st.form(f"index-form-{brand}"):
            mode = st.selectbox(
                "维护方式",
                ["incremental", "rebuild"],
                format_func=lambda m: "增量修复" if m == "incremental" else "重建活动版本索引",
            )
            actor = st.text_input("索引维护操作人")
            reason = st.text_input("索引维护原因")
            maintain = st.form_submit_button("执行索引维护")
        if maintain:
            try:
                st.json(store.maintain_index(brand, mode=mode, actor=actor, reason=reason))
            except ValueError as exc:
                st.error(str(exc))
    with st.expander("本地原件保留与清理"):
        st.caption(
            "默认全部保留。预览只列出已撤下／已替换版本；不清理活动版本、待审核候选、历史正文、图片附件或外部备份。"
        )
        key = f"cleanup-plan-{brand}"
        if st.button("生成原件清理预览", key=f"cleanup-preview-{brand}"):
            st.session_state[key] = store.source_cleanup_plan(brand)
        plan = st.session_state.get(key)
        if plan:
            st.json(plan)
            with st.form(f"cleanup-form-{brand}-{plan['plan_token']}"):
                actor = st.text_input("原件清理操作人")
                reason = st.text_input("原件清理原因")
                clean = st.form_submit_button("按此预览清理原件", disabled=not plan["files"])
            if clean:
                try:
                    store.cleanup_sources(
                        brand, plan_token=plan["plan_token"], actor=actor, reason=reason
                    )
                    del st.session_state[key]
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

"""Public self-authored demonstration, isolated from the user's working library."""

from pathlib import Path

import streamlit as st

from autorepair.knowledge_ui import render_knowledge
from autorepair.storage import Store

ROOT = Path(__file__).parent
DATABASE = ROOT / "work/portfolio-demo/knowledge.sqlite"
st.set_page_config(page_title="文档解析与知识库 · 领域示例", page_icon="📄", layout="wide")
st.title("文档解析与知识库 · 领域示例")
st.caption(
    "汽车领域扩展示例 · 20 份自制资料 · 跨品牌范围过滤 · 来源证据 · 版本生命周期 · 本地检索与引用摘录。仅用于软件演示，参数不可用于真实维修。"
)
with st.sidebar:
    brand = st.selectbox("品牌", ["aurora", "atlas"], key="portfolio-brand")
    st.markdown("**可复现演示**")
    st.write("Aurora：车型 aster，年份 2023；Atlas：车型 ridge，年份 2021。")
    st.write("示例：前制动卡钳支架螺栓扭矩是多少；P0118 要核查哪些线路。")
    st.caption(
        "人工盲测尚未完成。原 0.11 基线失败保留；0.12 的参数绑定修复、回归和新增工况验收见仓库报告。"
    )
if not DATABASE.is_file():
    st.info(
        "尚未创建隔离演示库。先在项目根目录执行 .venv/bin/python scripts/demo_knowledge.py，再刷新页面。"
    )
    st.stop()
render_knowledge(Store(DATABASE), brand)

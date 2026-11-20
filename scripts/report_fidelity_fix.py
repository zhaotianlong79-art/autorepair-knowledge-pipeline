"""Compare the published v0.1 baseline and v0.2 fidelity evidence."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
before = json.loads((ROOT / "docs/fidelity/results.json").read_text())
after = json.loads((ROOT / "docs/fidelity-fixed/results.json").read_text())


def totals(result):
    groups = result["summary"].values()
    return {
        key: sum(g[key] for g in groups)
        for key in [
            "cases",
            "converted",
            "content_ok",
            "checkpoints",
            "recovered",
            "table_cases",
            "table_cases_ok",
            "multi_level_cases",
            "multi_level_ok",
        ]
    }


b = totals(before)
a = totals(after)
lines = [
    "# Markdown 保真问题修复验证",
    "",
    "版本：0.2.0。日期：2026 年 11 月 20 日。修复前公开提交：`325a6ea`。",
    "",
    "同一批 22 个自制样例复测通过。修复前的输入、标注、Markdown 和问题报告保留于 `docs/fidelity/`；修复后结果独立保存于 `docs/fidelity-fixed/`，没有覆盖历史证据。",
    "",
    "## 相同样例的前后对比",
    "",
    "| 检查 | 修复前 | 修复后 |",
    "| --- | --- | --- |",
]
for label, n, d in [
    ("成功生成 Markdown", "converted", "cases"),
    ("全部指定内容检查点恢复的样例", "content_ok", "cases"),
    ("内容检查点", "recovered", "checkpoints"),
    ("表格逻辑网格", "table_cases_ok", "table_cases"),
    ("多级章节", "multi_level_ok", "multi_level_cases"),
]:
    lines.append(f"| {label} | {b[n]}/{b[d]} | {a[n]}/{a[d]} |")
lines += [
    "",
    "检查点比例只描述本组测试，不是实际手册的整体转换准确率或 OCR 准确率。",
    "",
    "## 本次修复",
    "",
    "1. 中间模型保留标题级别、章节路径和锚点。HTML 读取 h1 至 h6，DOCX 保留 Docling 层级，PDF 对明确的编号标题补充级别判断。导出不再将所有标题统一成 `##`。",
    "2. HTML 先按 rowspan 和 colspan 建立逻辑网格；Markdown 单元格转义竖线与反斜杠。保留真实空白，不按前一行内容猜测填充。",
    "3. 数字 PDF 表格增加 pdfplumber 矢量单元格几何核对。源文件有明确合并边框时按其覆盖范围展开；无法确认合并范围的表格继续待复核。",
    "4. 嵌套列表保留深度与编号，链接保留可点击目标，原章节锚点进入 Markdown。",
    "5. HTML 保留图片替代文字和引用；本地目录内可读取的图片作为带摘要的附件导出。DOCX 图片保留原图，增加本地识别；按图像坐标处理重叠识别片段，避免重复边界字符。",
    "6. 图片未处理、读取失败、识别失败或仍需核对时产生问题项，避免在内容未处理的情况下静默通过。图片与识别结果始终待复核。",
    "",
    "## 逐格式验证",
    "",
    "| 格式 | 样例 | 内容检查点恢复 | 表格网格 | 多级章节 |",
    "| --- | --- | --- | --- | --- |",
]
for group, g in after["summary"].items():
    lines.append(
        f"| {group} | {g['cases']} | {g['recovered']}/{g['checkpoints']} | {g['table_cases_ok']}/{g['table_cases']} | {g['multi_level_ok']}/{g['multi_level_cases']} |"
    )
lines += [
    "",
    "HTML 嵌套列表复测为两层，两个链接均保留；DOCX 图片中的 `FIGURE_IMAGE_808` 已识别并随原图导出。跨页连续表格仍输出两张表，36 行逻辑数据保持一致。",
    "",
    "## 对照证据",
    "",
    "- [修复前质量报告](全格式转Markdown质量测试报告.md)",
    "- [修复后机器结果](fidelity-fixed/results.json)",
    "- [章节修复后 Markdown](fidelity-fixed/html_basic.html.md)",
    "- [HTML 合并表格修复后 Markdown](fidelity-fixed/html_complex.html.md)",
    "- [PDF 合并表格修复后 Markdown](fidelity-fixed/pdf_merged.pdf.md)",
    "- [图片识别修复后 Markdown](fidelity-fixed/docx_picture.docx.md)",
    "- [列表与链接修复后 Markdown](fidelity-fixed/html_nested.html.md)",
    "",
    "## 回归验证与复现",
    "",
    "本机 45 项测试通过，包括新增的章节、合并网格、竖线、列表、链接、图片复核、附件完整性和 OCR 重叠片段测试。Ruff 检查和格式检查通过。完整保真评测可以要求任一断言失败时返回非零退出码：",
    "",
    '```bash\nuv sync --frozen --extra docling\nHF_HOME="$PWD/work/huggingface" HF_HUB_OFFLINE=1 uv run --extra docling pytest -q\nFIDELITY_REQUIRE_PASS=1 FIDELITY_OUTPUT="$PWD/docs/fidelity-fixed" \\\n  HF_HOME="$PWD/work/huggingface" HF_HUB_OFFLINE=1 \\\n  uv run --extra docling python scripts/test_markdown_fidelity.py\nuv run python scripts/report_fidelity_fix.py\n```',
    "",
    "离线命令需要已下载模型。首次运行应先联网准备模型；本次使用已有缓存完成验证。GitHub Actions 的默认检查包含无需大模型下载的回归测试。",
    "",
    "## 保留的限制",
    "",
    "- 对没有明确层级编号、且解析模型未识别层级的 PDF 标题，不声称已恢复真实目录树。",
    "- 无边框、扫描合并单元格和更复杂的嵌套表仍需按真实文件复核；不能把本次有边框样例外推到所有 PDF。",
    "- 图像识别文字不保证正确，也没有自动进行图像像素脱敏；资源仍按原文复核流程处理。HTML 不会自动请求外部图片，上传时缺失的相对资源会提示。",
    "- 全部 22 份材料都是自制样例，没有替代真实授权维修手册上的验收。",
    "",
]
(ROOT / "docs/Markdown保真修复验证.md").write_text("\n".join(lines))
print(a)

"""Write the Chinese review report from the fidelity runner's factual results."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
report = json.loads((ROOT / "docs/fidelity/results.json").read_text())
cases = report["cases"]
summary = report["summary"]
total = sum(s["checkpoints"] for s in summary.values())
recovered = sum(s["recovered"] for s in summary.values())
lines = [
    "# 全格式转 Markdown 保真测试报告",
    "",
    "日期：2026 年 11 月 20 日。对象：AutoRepair Knowledge Pipeline 0.1.0 当前本地实现。",
    "",
    "结论：22 个样例均能生成 Markdown，但当前实现不能称为无损转换。基础正文和普通表格可用；多级章节被扁平化，HTML 复杂表格存在列错位和渲染内容丢失，嵌入图像内容未保留。建议先修复结构与静默丢失问题，再用真实手册验收。",
    "",
    "## 测试范围与方法",
    "",
    "- 覆盖所有当前入口：`.html`、`.htm`、`.json`、`.docx`、数字 PDF、扫描 PDF。未把尚不支持的旧版 `.doc`、Excel 或 PPT 算作支持范围。",
    "- 22 份自制源文档，涵盖三级章节、普通表格、合并单元格、含竖线的单元格、长表跨页、正文首尾、嵌套列表、链接和嵌入图片。JSON 覆盖五种内容字段名、数组、records 封装、规范 JSON 回读。",
    "- 核对的是实际 Markdown 渲染后的文字、标题标签和表格网格，不只搜索原始 Markdown 字符串。源码里存在 `A|B` 不代表渲染出来还在。",
    "- 标题允许因外层文档 ID 标题统一下移一级，但必须保留原有相对层级。连续表格允许按页拆成多张表；去除重复表头后逐行逐列对照。",
    "- PDF 与 DOCX 源文件已渲染检查；源表格、图片标记和章节均可见。扫描样例为清晰的 180 DPI 中英文页面，无旋转、污损或复杂背景。",
    "- 同时保存解析后块结构、只解析的 Markdown、经过品牌规则后的 Markdown。业务模块 `src/` 的哈希与测试前一致，本轮没有修改转换逻辑。",
    "",
    "## 按格式的实际结果",
    "",
    "“内容通过”表示该样例的全部指定检查点在渲染正文中恢复，不表示整篇文档绝对无遗漏。“表格通过”要求完整的行列对应关系，不只要求文字存在。",
    "",
    "| 格式 | 样例数 | 成功生成 MD | 内容通过 | 表格通过 | 多级章节通过 |",
    "| --- | --- | --- | --- | --- | --- |",
]
for group, s in summary.items():
    lines.append(
        f"| {group} | {s['cases']} | {s['converted']}/{s['cases']} | {s['content_ok']}/{s['cases']} | {s['table_cases_ok']}/{s['table_cases']} | {s['multi_level_ok']}/{s['multi_level_cases']} |"
    )
lines += [
    "",
    f"共恢复 {recovered}/{total} 个指定内容检查点，3 个未恢复项分别是 HTML 表格中渲染后的 `A|B`、HTML 图片替代文字 `FIGURE_ALT_707`、DOCX 图片文字 `FIGURE_IMAGE_808`。这个比值是测试检查点统计，不是转换完整率或 OCR 准确率。",
    "",
    "## 发现的问题与可复现证据",
    "",
    "### 一 多级章节层级丢失",
    "",
    "19 个包含多级章节的样例均未保留层级。标题文字没有缺失，但原本一级、二级、三级章节都输出为 `##`。因此目录树、父子章节关系和按章节分块会出错。含单级标题的另外 3 个样例不计入这个失败分母。",
    "",
    "预期允许外层文档标题后输出 `##`、`###`、`####`，实际为：",
    "",
    "```markdown\n## Chapter 1 Service\n## 1.1 Inspection\n## 1.1.1 Torque\n```",
    "",
    "HTML 在提取时未保留 h1 至 h6 级别；DOCX/PDF 适配层将标题统一为 heading，导出器再统一写 `##`。这是当前项目的中间模型与导出链路问题，不能据此认定 Docling 底座本身没有层级能力。",
    "",
    "[实际 MD](fidelity/html_basic.html.md) · [渲染证据](fidelity/evidence/headings.png)",
    "",
    "### 二 HTML 合并单元格与竖线导致表格错误",
    "",
    "源表格中 Brake 跨两行，Value 的第一行是 `A|B`。实际 Markdown 未转义竖线，也未展开 rowspan：",
    "",
    "| 行 | 预期 System | 预期 Component | 预期 Value | 实际 System | 实际 Component | 实际 Value |",
    "| --- | --- | --- | --- | --- | --- | --- |",
    "| 第一行 | Brake | Unit A | A 与 B 及中间竖线 | Brake | Unit A | A |",
    "| 第二行 | Brake | Unit B | LINE_C303 LINE_D404 | Unit B | LINE_C303 LINE_D404 | 空 |",
    "",
    "这里同时发生了渲染内容截断和字段关联错位。现有门禁发出了合并单元格待复核警告，但没有定位到竖线导致的渲染截断。",
    "",
    "[实际 MD](fidelity/html_complex.html.md) · [渲染证据](fidelity/evidence/html-table.png)",
    "",
    "### 三 PDF 合并单元格的关联关系丢失",
    "",
    "`pdf_merged.pdf` 中的共享单元格 Brake 适用于 Unit A 和 Unit B。输出第一行 System 为空，第二行才出现 Brake。所有文字检查点都存在，但第一行与共享类别的关系没有保住，因此该表格判定不通过。DOCX 的对应合并表格则正确展开为两行 Brake，说明不同格式需要分别验证。",
    "",
    "[PDF 源文件](../examples/fidelity/pdf_merged.pdf) · [实际 MD](fidelity/pdf_merged.pdf.md)",
    "",
    "### 四 图像内容和替代文字遗漏",
    "",
    '- HTML 中图片的 `alt="FIGURE_ALT_707"` 没有进入解析块和 Markdown。',
    "- DOCX 嵌入图片中清晰可见的 `FIGURE_IMAGE_808` 没有进入解析块和 Markdown；正文与表格保留。",
    "- 当前 DOCX 路径没有覆盖嵌入图片 OCR；这不影响本轮扫描 PDF 的正向结果，两者是不同处理路径。",
    "- HTML 图像遗漏没有触发警告，该样例仍标记为 passed。这是质量门禁的静默遗漏，应优先补齐提醒。",
    "",
    "[HTML 实际 MD](fidelity/html_nested.html.md) · [DOCX 实际 MD](fidelity/docx_picture.docx.md)",
    "",
    "### 五 嵌套列表和链接语义没有保留",
    "",
    "HTML 的两层列表输出成一层，子项文字并入父项。两个链接的显示文字存在，但实际 Markdown 链接数为零；href 仍保存在 JSON 来源元数据中，没有进入可点击的 Markdown 正文。HTML 内部章节引用与外部链接均受影响。",
    "",
    "## 通过的专项检查",
    "",
    "- 普通三列表格的单元格值、部件号、整数和小数扭矩保持；DOCX 合并表格及竖线单元格在本样例中保留正确。",
    "- 数字 PDF 的普通英文、中文正文与表格通过；两页不同表格的正文首尾检查点也在。",
    "- 连续跨页表格的 36 行、108 个数据单元格全部按顺序恢复。输出为两张 Markdown 表格，去掉重复表头后逻辑网格一致，尚未合并成单张表。",
    "- 本轮两份清晰中英文扫描 PDF 的标题文字、正文检查点、表格和值均恢复，章节层级仍扁平。这个结果不能外推到低清、倾斜或受损扫描件。",
    "- JSON 的五种内容字段、数组、records 封装与规范 JSON 输入均成功；标题文字与普通表格内容保留，但多级层级也丢失。JSON 中嵌入复杂 HTML 表格未额外构造样例；因共用 HTML 解析函数，复杂表格风险属于代码路径推断。",
    "- 有意隐藏的 `HIDDEN_EXCLUDED` 没有进入正文，属于预期排除，不算内容遗漏。",
    "",
    "## 修复顺序建议",
    "",
    "1. 保留标题级别和章节路径，按层级导出；验证所有 19 个多级样例。",
    "2. HTML 表格先建立带 rowspan 和 colspan 的逻辑网格，再导出并转义竖线；对 PDF 合并表格保留共享关系或强制复核。",
    "3. HTML 图片至少输出替代文字与资源引用；DOCX 图片保留占位及来源，明确是否 OCR；未处理图像必须产生质量提醒。",
    "4. 保留列表深度、编号、链接目标及章节锚点。",
    "5. 将这些保真断言加入回归验收，再加入真实授权手册、复杂表头和低清扫描测试。",
    "",
    "## 逐文件结果",
    "",
    "| 文件 | 内容检查点 | 标题文字 | 层级 | 表格网格 | 当前门禁状态 |",
    "| --- | --- | --- | --- | --- | --- |",
]
for c in cases:
    levels = "通过" if c["headings_ok"] else "丢失层级"
    tables = "通过" if c["tables_ok"] else "失败"
    if not c["expected_tables"]:
        tables = "不涉及"
    lines.append(
        f"| [{c['file']}](fidelity/{c['file']}.md) | {c['recovered']}/{c['checkpoints']} | {'保留' if not c['heading_text_missing'] else '缺失'} | {levels} | {tables} | {c['status']} |"
    )
lines += [
    "",
    "## 复现与边界",
    "",
    "在原项目根目录使用已安装的 Python 3.12 环境：",
    "",
    '```bash\nHF_HOME="$PWD/work/huggingface" HF_HUB_OFFLINE=1 .venv/bin/python scripts/test_markdown_fidelity.py\n.venv/bin/python scripts/report_markdown_fidelity.py\n```',
    "",
    "`examples/fidelity/manifest.json` 是源内容与结构标注；`docs/fidelity/results.json` 是逐项实际结果；同目录的 `.json`、`.md`、`.parser-only.md` 和 `.html` 分别保留处理结果、正式 Markdown、仅解析 Markdown 和实际渲染预览。扫描测试依赖已经下载的模型；首次使用需先准备依赖与模型。",
    "",
    "已有 36 项回归测试仍全部通过，新建评测脚本通过 Ruff 检查。原回归测试覆盖的简单机制不能证明章节和复杂内容无损，本轮专项结果以逐文件证据为准。所有材料都是自制样例，没有使用真实厂商手册，不能以本次检查点统计替代真实业务完整率。",
    "",
    f"业务源文件哈希与测试前一致：{report['production_source_unchanged']}。本次新增测试材料、评测脚本与报告，没有修复或替换解析器。",
    "",
]
(ROOT / "docs/全格式转Markdown质量测试报告.md").write_text("\n".join(lines))
print("Report written")

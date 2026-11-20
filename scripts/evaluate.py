"""Fixed self-authored acceptance cases, with per-query evidence and denominators."""

import json
from pathlib import Path

from autorepair.config import load_config
from autorepair.engine import signature
from autorepair.pipeline import run
from autorepair.search import search

ROOT = Path(__file__).resolve().parents[1]
# Expected source filename and literal evidence were authored separately from the engine.
QUESTIONS = [
    ("氧传感器", "01-manual.html", "氧传感器"),
    ("扭矩扳手", "01-manual.html", "扭矩扳手"),
    ("故障诊断码", "01-manual.html", "P0300"),
    ("间歇性接触不良", "01-manual.html", "间歇性接触不良"),
    ("42 Nm", "01-manual.html", "42 Nm"),
    ("P0300", "01-manual.html", "P0300"),
    ("返回演示维修手册", "01-manual.html", "返回演示维修手册"),
    ("氧传感器 扭矩扳手", "03-procedure.json", "氧传感器"),
    ("12 V", "03-procedure.json", "12 V"),
    ("传感器更换流程", "03-procedure.json", "传感器更换流程"),
    ("扩展维修记录", "04-extra.json", "扩展维修记录"),
    ("警告不得忽略", "04-extra.json", "警告不得忽略"),
    ("XCU", "02-review.html", "XCU"),
    ("连接检查", "02-review.html", "连接检查"),
    ("查看缺失章节", "02-review.html", "查看缺失章节"),
]
report = {
    "scope": "24 original demonstration fixtures exist; retrieval metrics below cover only the 8 HTML/JSON holdout files. Candidate retrieval includes review documents and does not imply approval for ingestion.",
    "queries": [],
    "brands": {},
    "protected_blocks": {"unchanged": 0, "total": 0},
    "detected_issue_codes": {},
}
for brand in ["aurora", "atlas"]:
    paths = sorted(
        p for p in (ROOT / "examples/holdout" / brand).glob("*") if p.suffix in {".html", ".json"}
    )
    results = run(paths, load_config(ROOT / "configs", brand))
    by_id = {r.document.doc_id: r.document.source_name for r in results}
    totals = {
        "questions": len(QUESTIONS),
        "before_hits": 0,
        "after_hits": 0,
        "passed_documents": sum(r.status == "passed" for r in results),
        "review_documents": sum(r.status == "review" for r in results),
    }
    for query, source, needle in QUESTIONS:
        row = {
            "brand": brand,
            "query": query,
            "expected_source": source,
            "expected_evidence": needle,
        }
        for original, label in [(True, "before"), (False, "after")]:
            hits = search(results, query, brand, original=original, include_review=True, k=5)
            equivalents = {
                "氧传感器": ["氧传感器", "oxygen sensor", "O₂ sensor"],
                "扭矩扳手": ["扭矩扳手", "torque wrench"],
                "间歇性接触不良": ["间歇性接触不良", "intermittent poor contact"],
            }.get(needle, [needle])
            matched = [
                h
                for h in hits
                if by_id[h["doc_id"]] == source and any(n in h["text"] for n in equivalents)
            ]
            row[label + "_hit"] = bool(matched)
            row[label + "_top5"] = [
                {"source": by_id[h["doc_id"]], "block_id": h["block_id"], "text": h["text"]}
                for h in hits
            ]
            totals[label + "_hits"] += bool(matched)
        report["queries"].append(row)
    for r in results:
        for b in r.document.blocks:
            report["protected_blocks"]["total"] += 1
            report["protected_blocks"]["unchanged"] += signature(b.original) == signature(b.text)
        for issue in r.issues:
            report["detected_issue_codes"][issue.code] = (
                report["detected_issue_codes"].get(issue.code, 0) + 1
            )
    report["brands"][brand] = totals
out = ROOT / "docs/acceptance-results.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
lines = [
    "# 第一版验证结果",
    "",
    "本报告由 `uv run python scripts/evaluate.py` 生成。样例为自制演示材料，开发集和验收集共享结构，仅更换部分参数；这些结果不能代表真实维修手册的泛化能力。",
    "",
    "## 固定题集检索",
    "",
    "每题标注一个文件及一个应出现的证据片段。命中要求前五条同时满足文件与证据片段；原文英文别名与规范中文同等计为正确证据。加工前按原文术语匹配，规范化查询的收益包含术语统一的影响，不等于通用检索算法提升。检索包含待复核文档，正式导出仍执行门禁。",
    "",
    "| 品牌 | 题数 | 加工前命中 | 加工后命中 | 通过文档 | 待复核文档 |",
    "| --- | --- | --- | --- | --- | --- |",
]
for brand, t in report["brands"].items():
    lines.append(
        f"| {brand} | {t['questions']} | {t['before_hits']}/{t['questions']} | {t['after_hits']}/{t['questions']} | {t['passed_documents']} | {t['review_documents']} |"
    )
a = report["protected_blocks"]
lines += [
    "",
    "## 参数与警告词保护",
    "",
    f"对照已解析的 {a['total']} 个块，{a['unchanged']} 个块的参数与警告词保护签名保持一致。该结果验证加工阶段不误改，不验证上游解析是否完整。",
    "",
    "## 检出的样例问题",
    "",
]
lines += [
    f"- `{code}`：{count} 次" for code, count in sorted(report["detected_issue_codes"].items())
]
lines += [
    "",
    "## 尚需真实材料验证",
    "",
    "- 独立人工标注的转换完整率、术语 precision 和 recall 尚未测得，不能据此声称已达到决策文档目标。",
    "- 当前样例仅验证英文扫描内容，中文 OCR 质量与跨页复杂表格仍需专项评估。",
    "- 所有题目、来源和前五条证据见 `acceptance-results.json`。",
    "",
]
(ROOT / "docs/第一版验证结果.md").write_text("\n".join(lines))
print(json.dumps(report["brands"], ensure_ascii=False, indent=2))
print("Protected:", report["protected_blocks"])

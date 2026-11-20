"""Frozen source-labelled benchmark. Quality misses are reported, not relabelled."""

import argparse
import importlib.metadata
import json
import platform
import tempfile
from collections import Counter
from pathlib import Path

from autorepair.evaluation import (
    BENCHMARK,
    ROOT,
    build_corpus,
    evaluate_cases,
    file_hash,
    load_dataset,
    load_reviews,
    summarize,
)


def report_markdown(report):
    lines = [
        "# 冻结题集评估结果",
        "",
        f"数据集 repair-v1；版本 {report['runtime']['project_version']}。标签由助手基于原文件预先编写，外部人工复核尚未完成；不是人工盲测或真实维修效果。",
        "",
        f"{report['dataset']['documents']} 份自制资料，{report['dataset']['cases']} 个场景（{report['dataset']['distinct_questions']} 种不同问题文本）。开发引擎冻结基线 `{report['baseline_commit'][:7]}`；本次引擎与基线一致：{report['engine_matches_baseline']}。",
        "",
        "引用可追溯性是逐字来源与发布状态核对；标注证据匹配是文件及锚点匹配，两者都不能替代人工语义正确性判断。",
        "",
        "| 模式 | 前三正文召回 | 可答题有依据作答 | 已答标注证据匹配 | 已答引用可追溯 | 应拒答题拒答 | 负例误答 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def fraction(metric):
        return (
            f"{metric['numerator']}/{metric['denominator']}"
            if metric["denominator"]
            else "未测（分母为0）"
        )

    for mode, stats in report["metrics"].items():
        lines.append(
            f"| {mode} | {fraction(stats['retrieval_hit3'])} | {fraction(stats['answerable_support_coverage'])} | {fraction(stats['answered_label_support'])} | {fraction(stats['answered_trace_integrity'])} | {fraction(stats['negative_refusal'])} | {stats['unsafe_negative_answers']} |"
        )
    lines += [
        "",
        "所有分母按该模式的场景计数；同一场景的多个模式不是独立样本。负例误答表示违反预标注拒答策略，不必然表示虚构参数。无模型模式不伪造语义分数。",
        "",
        "## 未通过或需要人工核对的场景",
        "",
        "| 模式 | 题号 | 类别 | 预期状态 | 实际状态 | 正文召回排名 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        if row["answer_status"] != row["expected_status"] or (
            row["gold_answerable"] and not row["label_support_match"]
        ):
            lines.append(
                f"| {row['mode']} | {row['case_id']} | {row['category']} | {row['expected_status']} | {row['answer_status']} | {row['retrieval_rank'] or '未进前三'} |"
            )
    lines += [
        "",
        "## 复核与限制",
        "",
        f"已提交复核记录 {report['reviews']['submitted_cases']} 题，认可标签 {report['reviews']['approved_cases']} 题，否决标签 {report['reviews']['rejected_cases']} 题。记录为自行署名，不是身份认证。",
        "",
        "人工认可标签子集的指标单列于 JSON；没有认可记录时分母为 0、比率为 null，不声称人工验证通过。"
        + (
            "生产引擎与冻结基线一致。"
            if report["engine_matches_baseline"]
            else "生产引擎已变化，本次为已见题回归，不是独立盲测。原 0.11.0 基线报告保留不覆盖。"
        ),
        "",
        "未运行真实 LLM 选证，没有自由生成答案准确率。本轮只使用自制 HTML 正式证据，不验证真实 PDF 的维修语义或维修安全。该结果不可作实际车辆操作依据。",
        "",
        "复现：`.venv/bin/python scripts/evaluate_benchmark.py --modes bm25`；准备本地 BGE 缓存后可用 `--modes bm25 semantic hybrid`。详见同目录 JSON 逐题结果、标签和复核表。",
        "",
    ]
    return "\n".join(lines)


def evaluate(output, markdown_path, modes, *, reviews_path=None):
    if not modes or len(modes) != len(set(modes)) or set(modes) - {"bm25", "semantic", "hybrid"}:
        raise ValueError("检索模式须唯一且为 bm25、semantic 或 hybrid")
    data, freeze = load_dataset()
    reviews = load_reviews(reviews_path, {c["id"] for c in data["cases"]})
    engine = {name: file_hash(ROOT / name) for name in freeze["baseline_engine_files"]}
    embedder = None
    model = None
    if set(modes) - {"bm25"}:
        from autorepair.semantic import DEFAULT_CONFIG, local_embedder

        embedder = local_embedder()
        model = {**DEFAULT_CONFIG.__dict__, "config_hash": DEFAULT_CONFIG.key}
    with tempfile.TemporaryDirectory() as tmp:
        store, keys = build_corpus(Path(tmp) / "knowledge.sqlite")
        rows = evaluate_cases(store, keys, data, modes, embedder=embedder, reviews=reviews)
    metrics = {mode: summarize([r for r in rows if r["mode"] == mode]) for mode in modes}
    approved = {k for k, v in reviews.items() if v["verdict"] == "approve"}
    report = dict(
        schema_version=1,
        dataset=dict(
            id=data["dataset_id"],
            documents=len(data["documents"]),
            cases=len(data["cases"]),
            distinct_questions=len({c["question"] for c in data["cases"]}),
            categories=dict(Counter(c["category"] for c in data["cases"])),
            annotation=data["annotation"],
            dataset_hash=file_hash(BENCHMARK / "dataset.json"),
        ),
        baseline_commit=freeze["baseline_commit"],
        engine_files=engine,
        engine_matches_baseline=engine == freeze["baseline_engine_files"],
        runtime=dict(
            python=platform.python_version(),
            platform=platform.platform(),
            project_version=importlib.metadata.version("autorepair-knowledge-pipeline"),
            uv_lock_hash=file_hash(ROOT / "uv.lock"),
        ),
        evaluator_files={
            name: file_hash(ROOT / name)
            for name in ["src/autorepair/evaluation.py", "scripts/evaluate_benchmark.py"]
        },
        model=model,
        additional_engine_files={
            name: file_hash(ROOT / name)
            for name in [
                "src/autorepair/qa_language.py",
                "src/autorepair/qa_binding.py",
                "configs/qa/query-terms.yaml",
            ]
            if (ROOT / name).exists()
        },
        reviews=dict(
            submitted_cases=len(reviews),
            approved_cases=len(approved),
            rejected_cases=len(reviews) - len(approved),
            identity_verified=False,
        ),
        metrics=metrics,
        approved_label_subset_metrics={
            mode: summarize([r for r in rows if r["mode"] == mode and r["case_id"] in approved])
            for mode in modes
        },
        per_category={
            mode: {
                category: summarize(
                    [r for r in rows if r["mode"] == mode and r["category"] == category]
                )
                for category in sorted({c["category"] for c in data["cases"]})
            }
            for mode in modes
        },
        integrity_passed=all(
            r["trace_valid"] and r["retrieval_scope_violations"] == 0 for r in rows
        ),
        rows=rows,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    markdown_path.write_text(report_markdown(report))
    print(
        json.dumps(
            {
                "metrics": metrics,
                "integrity_passed": report["integrity_passed"],
                "submitted_review_records": len(reviews),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["integrity_passed"]:
        raise ValueError("评估发现来源或范围完整性问题，请检查逐题结果")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--modes", nargs="+", default=["bm25"], choices=["bm25", "semantic", "hybrid"]
    )
    parser.add_argument("--output", type=Path, default=ROOT / "docs/benchmark-results.json")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/冻结题集评估结果.md")
    parser.add_argument("--reviews", type=Path)
    args = parser.parse_args()
    evaluate(args.output, args.report, args.modes, reviews_path=args.reviews)

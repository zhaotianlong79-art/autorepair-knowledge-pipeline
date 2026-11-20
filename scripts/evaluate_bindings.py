"""Self-authored row/column/condition acceptance with frozen source fixtures."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from autorepair.config import load_config
from autorepair.metadata import MetadataFilter, MetadataPatch
from autorepair.pipeline import run
from autorepair.qa import answer
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples/qa-binding"


def evaluate(output, modes):
    freeze = json.loads((BASE / "freeze.json").read_text())
    for name, digest in freeze.items():
        if hashlib.sha256((BASE / name).read_bytes()).hexdigest() != digest:
            raise ValueError("新增绑定样例摘要不匹配")
    manifest = json.loads((BASE / "manifest.json").read_text())
    embedder = None
    if set(modes) - {"bm25"}:
        from autorepair.semantic import local_embedder

        embedder = local_embedder()
    cfg = load_config(ROOT / "configs", "aurora")
    filters = MetadataFilter(model="nova", year_start=2026, year_end=2026)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        stores = {}
        for name in {c["file"] for c in manifest["cases"]}:
            results = run(
                [BASE / name],
                cfg,
                metadata=MetadataPatch(
                    model="nova", year_range="2026", document_type="manual", document_version="B1"
                ),
            )
            if results[0].status != "passed":
                raise ValueError(f"新增样例门禁状态变化：{name}")
            store = Store(Path(tmp) / (name + ".sqlite"))
            store.save(results, cfg)
            stores[name] = store
        for case in manifest["cases"]:
            for mode in modes:
                result = answer(
                    stores[case["file"]],
                    case["question"],
                    "aurora",
                    filters=filters,
                    retrieval_mode=mode,
                    embedder=embedder,
                )
                facts = (
                    [f for c in result["citations"] for f in c.get("numeric_bindings", [])]
                    if result["status"] == "answered"
                    else []
                )
                values = sorted({f["canonical"] for f in facts})
                cell = case["expected_cell"]
                valid = result["status"] == case["expected_status"] and values == sorted(
                    case["expected_values"]
                )
                if cell:
                    valid = (
                        valid
                        and bool(facts)
                        and all([f["row"], f["column"]] == cell for f in facts)
                    )
                rows.append(
                    dict(
                        case_id=case["id"],
                        mode=mode,
                        question=case["question"],
                        expected_status=case["expected_status"],
                        actual_status=result["status"],
                        expected_values=case["expected_values"],
                        actual_values=values,
                        bindings=facts,
                        passed=valid,
                    )
                )
    report = dict(
        scope=manifest["scope"],
        cases=len(manifest["cases"]),
        executions=len(rows),
        passed=all(r["passed"] for r in rows),
        modes=modes,
        rows=rows,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "cases": report["cases"],
                "executions": len(rows),
                "passed": report["passed"],
                "failures": [
                    {
                        k: r[k]
                        for k in [
                            "case_id",
                            "mode",
                            "expected_status",
                            "actual_status",
                            "actual_values",
                        ]
                    }
                    for r in rows
                    if not r["passed"]
                ],
            },
            ensure_ascii=False,
        )
    )
    if not report["passed"]:
        raise ValueError("参数绑定验收未通过，详见逐项结果")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs/qa-binding-results.json")
    parser.add_argument(
        "--modes", nargs="+", choices=["bm25", "semantic", "hybrid"], default=["bm25"]
    )
    args = parser.parse_args()
    evaluate(args.output, args.modes)

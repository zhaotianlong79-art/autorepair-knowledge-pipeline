"""Command line entry points. Exit 2 = failed, 1 = needs review, 0 = all passed."""

import argparse
import json
from pathlib import Path

from autorepair.config import load_config
from autorepair.pipeline import export, run
from autorepair.search import search
from autorepair.storage import Store


def main():
    parser = argparse.ArgumentParser(description="汽车维修知识加工与质量校验")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("process", help="加工本地文件或目录")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--brand", required=True)
    ingest.add_argument("--config", type=Path, default=Path("configs"))
    ingest.add_argument("--output", type=Path, default=Path("artifacts"))
    ingest.add_argument("--ocr", action="store_true")
    ingest.add_argument("--models", help="Docling 预下载模型目录")
    lookup = commands.add_parser("search", help="默认检索正式知识库；--run 可查询历史批次")
    lookup.add_argument("query")
    lookup.add_argument("--run", help="历史运行 ID；不填则查询当前正式知识库")
    lookup.add_argument("--brand", required=True)
    lookup.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    lookup.add_argument("--include-review", action="store_true")
    lookup.add_argument("--original", action="store_true")
    catalog = commands.add_parser("catalog", help="列出品牌知识库文档及最近状态")
    catalog.add_argument("--brand", required=True)
    catalog.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    catalog.add_argument("--status", choices=["passed", "review", "failed"])
    args = parser.parse_args()
    if args.command == "process":
        try:
            cfg = load_config(args.config, args.brand)
            from autorepair.parsers import SUPPORTED

            paths = (
                sorted(
                    p
                    for p in args.input.rglob("*")
                    if p.is_file() and p.suffix.lower() in SUPPORTED
                )
                if args.input.is_dir()
                else [args.input]
            )
            if not paths:
                parser.error("没有找到支持的文件")
            results = run(paths, cfg, ocr=args.ocr, models_path=args.models)
            run_id = export(results, cfg, args.output)
            print(
                json.dumps(
                    {
                        "run_id": run_id,
                        "folder": str(args.output / run_id),
                        "documents": len(results),
                        "passed": sum(r.status == "passed" for r in results),
                        "review": sum(r.status == "review" for r in results),
                        "failed": sum(r.status == "failed" for r in results),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(
                2
                if any(r.status == "failed" for r in results)
                else 1
                if any(r.status == "review" for r in results)
                else 0
            )
        except (ValueError, FileNotFoundError) as exc:
            parser.error(str(exc))
    else:
        try:
            store = Store(args.database)
            if args.command == "catalog":
                output = store.list_documents(args.brand, status=args.status)
            elif args.run:
                output = search(
                    store.load_run(args.run),
                    args.query,
                    args.brand,
                    original=args.original,
                    include_review=args.include_review,
                )
                for hit in output:
                    hit["source_run_id"] = args.run
                    hit["historical"] = True
            else:
                if args.include_review:
                    parser.error("正式知识库不包含待复核文档；演示查询请显式指定 --run")
                output = store.search(args.query, args.brand, original=args.original)
            print(json.dumps(output, ensure_ascii=False, indent=2))
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))

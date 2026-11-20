"""Command line entry points. Exit 2 = failed, 1 = needs review, 0 = all passed."""

import argparse
import json
from pathlib import Path

from autorepair.config import load_config
from autorepair.metadata import TYPE_LABELS, MetadataFilter, MetadataPatch, load_manifest, years
from autorepair.pipeline import export, run
from autorepair.search import search
from autorepair.storage import Store


def patch_args(parser):
    parser.add_argument("--model")
    parser.add_argument("--year-range", help="YYYY、YYYY-YYYY 或 unknown")
    parser.add_argument("--document-type", choices=list(TYPE_LABELS))
    parser.add_argument("--document-version")
    parser.add_argument("--applicability", help="JSON 数组，每项为 model/year_start/year_end")


def read_patch(args):
    return MetadataPatch(
        model=args.model,
        year_range=args.year_range,
        document_type=args.document_type,
        document_version=args.document_version,
        applicability=json.loads(args.applicability) if args.applicability is not None else None,
    )


def filter_args(parser):
    parser.add_argument("--model")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--year", type=int)
    group.add_argument("--year-range")
    parser.add_argument("--document-type", choices=list(TYPE_LABELS))
    parser.add_argument("--document-version")
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="显式包含过滤字段未知的材料，仍排除已知不适用资料",
    )


def read_filter(args):
    start, end = years(args.year_range) if args.year_range is not None else (args.year, args.year)
    if args.year_range is not None and start is None:
        raise ValueError("查询年份须为 YYYY 或 YYYY-YYYY；未知项使用 --include-unknown")
    return MetadataFilter(
        model=args.model,
        year_start=start,
        year_end=end,
        document_type=args.document_type,
        document_version=args.document_version,
        include_unknown=args.include_unknown,
    )


def main():
    parser = argparse.ArgumentParser(description="多格式文档解析、Markdown 转换与质量校验")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("process", help="加工本地文件或目录")
    ingest.add_argument("input", type=Path)
    ingest.add_argument("--brand", required=True)
    ingest.add_argument("--config", type=Path, default=Path("configs"))
    ingest.add_argument("--output", type=Path, default=Path("artifacts"))
    ingest.add_argument("--ocr", action="store_true")
    ingest.add_argument("--models", help="Docling 预下载模型目录")
    patch_args(ingest)
    ingest.add_argument("--metadata-manifest", type=Path)
    ingest.add_argument("--logical-id", help="单文件的稳定逻辑文档身份；不按文件名推断")
    lookup = commands.add_parser("search", help="默认检索正式知识库；--run 可查询历史批次")
    lookup.add_argument("query")
    lookup.add_argument("--run", help="历史运行 ID；不填则查询当前正式知识库")
    lookup.add_argument("--brand", required=True)
    lookup.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    lookup.add_argument("--include-review", action="store_true")
    lookup.add_argument("--original", action="store_true")
    lookup.add_argument("--mode", choices=["bm25", "semantic", "hybrid"], default="bm25")
    filter_args(lookup)
    catalog = commands.add_parser("catalog", help="列出品牌知识库文档及最近状态")
    catalog.add_argument("--brand", required=True)
    catalog.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    catalog.add_argument("--status", choices=["passed", "review", "failed"])
    filter_args(catalog)
    chunks = commands.add_parser("chunks", help="查看当前正式知识库的章节切块与关联")
    chunks.add_argument("--brand", required=True)
    chunks.add_argument("--doc-id")
    chunks.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    correction = commands.add_parser(
        "metadata-correct", help="修正文档元数据并生成审计运行，保持审核状态"
    )
    correction.add_argument("--brand", required=True)
    correction.add_argument("--doc-id", required=True)
    correction.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    correction.add_argument("--expected-revision", type=int, required=True)
    correction.add_argument("--actor", required=True)
    correction.add_argument("--reason", required=True)
    patch_args(correction)
    history = commands.add_parser("metadata-history", help="查询元数据修改前后值与来源")
    history.add_argument("--brand", required=True)
    history.add_argument("--doc-id", required=True)
    history.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    evidence = commands.add_parser("evidence", help="查看章节块的解析原文、上下文、附件与页码定位")
    evidence.add_argument("--brand", required=True)
    evidence.add_argument("--doc-id", required=True)
    evidence.add_argument("--chunk-id", required=True)
    evidence.add_argument("--run", help="明确查看历史／候选运行；默认只允许当前正式资料")
    evidence.add_argument("--original", action="store_true")
    evidence.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    attach = commands.add_parser("source-attach", help="为已有资料补挂哈希一致的本地原件")
    attach.add_argument("input", type=Path)
    attach.add_argument("--brand", required=True)
    attach.add_argument("--doc-id", required=True)
    attach.add_argument("--run")
    attach.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    versions = commands.add_parser("versions", help="列出文档身份、内容版本与发布状态")
    events = commands.add_parser("lifecycle-history", help="查看生命周期及维护审计")
    withdraw = commands.add_parser("withdraw", help="撤下版本，保留审计和默认保留原件")
    restore = commands.add_parser("restore", help="恢复或回退到最近结果仍通过的版本")
    replace = commands.add_parser("version-replace", help="显式替换已有独立文档或同组版本")
    index = commands.add_parser("index", help="检查、增量修复或重建当前活动版本索引")
    cleanup = commands.add_parser(
        "source-cleanup", help="默认预览；按预览令牌清理已撤下／替换版本的原件"
    )
    for p in [versions, events, withdraw, restore, replace, index, cleanup]:
        p.add_argument("--brand", required=True)
        p.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    for p in [versions, events]:
        p.add_argument("--logical-id")
    for p in [withdraw, restore, replace]:
        p.add_argument("--doc-id", required=True)
        p.add_argument("--expected-revision", type=int, required=True)
    for p in [withdraw, restore, replace, index, cleanup]:
        p.add_argument("--actor", default="")
        p.add_argument("--reason", default="")
    replace.add_argument("--replacement", required=True)
    replace.add_argument("--expected-target-run", required=True)
    index.add_argument("--mode", choices=["check", "incremental", "rebuild"], default="check")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--plan-token")
    semantic_index = commands.add_parser(
        "semantic-index", help="检查、增量更新或重建当前品牌的本地向量缓存"
    )
    semantic_index.add_argument("--brand", required=True)
    semantic_index.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    semantic_index.add_argument(
        "--mode", choices=["check", "incremental", "rebuild"], default="check"
    )
    semantic_index.add_argument("--actor", default="")
    semantic_index.add_argument("--reason", default="")
    model = commands.add_parser("semantic-model", help="查看固定模型配置，显式下载到本地缓存")
    model.add_argument("--download", action="store_true")
    ask = commands.add_parser("ask", help="基于当前正式证据摘录回答，附来源和页码")
    ask.add_argument("question")
    ask.add_argument("--brand", required=True)
    ask.add_argument("--database", type=Path, default=Path("artifacts/knowledge.sqlite"))
    ask.add_argument("--mode", choices=["bm25", "semantic", "hybrid"], default="bm25")
    ask.add_argument("--engine", choices=["extractive", "ollama"], default="extractive")
    ask.add_argument("--qa-config", type=Path, help="显式 Ollama 配置文件；不允许在文件中填写密钥")
    filter_args(ask)
    args = parser.parse_args()
    if args.command == "semantic-model":
        from dataclasses import asdict

        from autorepair.semantic import DEFAULT_CONFIG, LocalEmbedder, SemanticError

        try:
            if args.download:
                LocalEmbedder(download=True)
            print(
                json.dumps(
                    {
                        **asdict(DEFAULT_CONFIG),
                        "config_hash": DEFAULT_CONFIG.key,
                        "downloaded": args.download,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        except SemanticError as exc:
            parser.error(str(exc))
        return

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
            metadata = read_patch(args)
            by_path = (
                load_manifest(args.metadata_manifest, paths) if args.metadata_manifest else None
            )
            results = run(
                paths,
                cfg,
                ocr=args.ocr,
                models_path=args.models,
                metadata=metadata,
                metadata_by_path=by_path,
                logical_id=args.logical_id,
            )
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
            store = (
                Store(args.database, auto_repair=False)
                if args.command == "index"
                or (args.command == "semantic-index" and args.mode == "check")
                else Store(args.database)
            )
            if args.command == "ask":
                from autorepair.qa import answer
                from autorepair.qa_provider import OllamaSelector, load_qa_config

                if args.engine == "ollama" and args.qa_config is None:
                    parser.error("Ollama 问答须显式提供 --qa-config")
                if args.engine == "extractive" and args.qa_config is not None:
                    parser.error("--qa-config 仅用于显式选择的 Ollama 模式")
                selector = (
                    OllamaSelector(load_qa_config(args.qa_config))
                    if args.engine == "ollama"
                    else None
                )
                output = answer(
                    store,
                    args.question,
                    args.brand,
                    filters=read_filter(args),
                    retrieval_mode=args.mode,
                    selector=selector,
                )
            elif args.command == "semantic-index":
                output = store.maintain_semantic_index(
                    args.brand, mode=args.mode, actor=args.actor, reason=args.reason
                )
            elif args.command == "versions":
                output = store.versions(args.brand, args.logical_id)
            elif args.command == "lifecycle-history":
                output = store.lifecycle_history(args.brand, args.logical_id)
            elif args.command in {"withdraw", "restore"}:
                output = getattr(store, args.command)(
                    args.brand,
                    args.doc_id,
                    actor=args.actor,
                    reason=args.reason,
                    expected_revision=args.expected_revision,
                )
            elif args.command == "version-replace":
                output = store.replace_version(
                    args.brand,
                    args.doc_id,
                    args.replacement,
                    actor=args.actor,
                    reason=args.reason,
                    expected_revision=args.expected_revision,
                    expected_target_run_id=args.expected_target_run,
                )
            elif args.command == "index":
                output = store.maintain_index(
                    args.brand, mode=args.mode, actor=args.actor, reason=args.reason
                )
            elif args.command == "source-cleanup":
                output = (
                    store.cleanup_sources(
                        args.brand, plan_token=args.plan_token, actor=args.actor, reason=args.reason
                    )
                    if args.apply
                    else store.source_cleanup_plan(args.brand)
                )
            elif args.command == "catalog":
                output = store.list_documents(
                    args.brand, status=args.status, filters=read_filter(args)
                )
            elif args.command == "chunks":
                output = [c.model_dump() for c in store.chunks(args.brand, args.doc_id)]
            elif args.command == "metadata-correct":
                output = store.correct_metadata(
                    args.brand,
                    args.doc_id,
                    read_patch(args),
                    actor=args.actor,
                    reason=args.reason,
                    expected_revision=args.expected_revision,
                )
            elif args.command == "metadata-history":
                output = store.metadata_history(args.brand, args.doc_id)
            elif args.command == "evidence":
                output = store.evidence(
                    args.brand, args.doc_id, args.chunk_id, run_id=args.run, original=args.original
                )
            elif args.command == "source-attach":
                output = store.attach_source(args.brand, args.doc_id, args.input, run_id=args.run)
            elif args.run:
                if args.mode != "bm25":
                    parser.error("历史批次仅支持 BM25；语义及混合检索只查询当前正式知识库")
                output = search(
                    store.load_run(args.run),
                    args.query,
                    args.brand,
                    original=args.original,
                    include_review=args.include_review,
                    filters=read_filter(args),
                )
                for hit in output:
                    hit["source_run_id"] = args.run
                    hit["historical"] = True
            else:
                if args.include_review:
                    parser.error("正式知识库不包含待复核文档；演示查询请显式指定 --run")
                output = store.search(
                    args.query,
                    args.brand,
                    original=args.original,
                    filters=read_filter(args),
                    mode=args.mode,
                )
            print(json.dumps(output, ensure_ascii=False, indent=2))
        except (KeyError, ValueError, OSError) as exc:
            parser.error(str(exc))

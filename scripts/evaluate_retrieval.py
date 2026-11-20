"""Reproducible real-model retrieval comparison on public self-authored fixtures.

No score assertion is tuned to the answers. General no-answer questions report
candidate returns rather than pretending cosine scores certify answerability.
"""

import argparse
import importlib.metadata
import json
import platform
import resource
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from autorepair.config import load_config
from autorepair.metadata import MetadataFilter, MetadataPatch
from autorepair.pipeline import run
from autorepair.semantic import DEFAULT_CONFIG, local_embedder, validate_vectors, windows
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def evaluate(output):
    corpus = json.loads((ROOT / "examples/retrieval/corpus.json").read_text())
    start = perf_counter()
    embedder = local_embedder()
    model_load = perf_counter() - start
    report = {
        "scope": "Fixed self-authored development set, not independent evaluation or real repair guidance. All brands share fixture wording. Scores measure evidence retrieval, not answer correctness.",
        "config": {**DEFAULT_CONFIG.__dict__, "config_hash": DEFAULT_CONFIG.key},
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            **{p: importlib.metadata.version(p) for p in ["torch", "transformers", "tokenizers"]},
        },
        "model_load_seconds": round(model_load, 4),
        "model_parameters": sum(p.numel() for p in embedder.model.parameters()),
        "model_weight_bytes": sum(
            p.numel() * p.element_size() for p in embedder.model.parameters()
        ),
        "index": {},
        "metrics": {},
        "queries": [],
        "no_answer": [],
    }
    long_table = "|项目|数值|\n|检查项|演示数据|\n" * 180 + "尾部警告：先释放压力"
    segments = windows(long_table)
    actual_vectors = embedder.encode([p["text"] for p in segments])
    validate_vectors(actual_vectors, len(segments), DEFAULT_CONFIG.dimension)
    assert segments[0]["start"] == 0 and segments[-1]["end"] == len(long_table)
    assert all(a["end"] >= b["start"] for a, b in zip(segments, segments[1:], strict=False))
    report["long_table_check"] = {
        "characters": len(long_table),
        "windows": len(segments),
        "tail_covered": True,
        "all_vectors_valid": True,
    }
    times = {mode: [] for mode in ["bm25", "semantic", "hybrid"]}
    totals = {mode: dict(questions=0, hit1=0, hit3=0, reciprocal_rank_at3=0.0) for mode in times}
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        store = Store(folder / "knowledge.sqlite")
        sources = {}
        for brand in ["aurora", "atlas"]:
            paths = []
            for doc in corpus["documents"]:
                path = folder / f"{brand}-{doc['id']}.html"
                path.write_text(
                    f"<html><body><h1>{doc['title']}</h1><h2>检查说明</h2><p>{doc['text']}</p></body></html>"
                )
                paths.append(path)
            results = run(
                paths,
                load_config(ROOT / "configs", brand),
                metadata=MetadataPatch(
                    model="sedan",
                    year_range="2024",
                    document_type="manual",
                    document_version="demo-1",
                ),
            )
            if any(r.status != "passed" for r in results):
                raise ValueError(
                    [
                        (r.document.source_name, r.status, [i.code for i in r.issues])
                        for r in results
                        if r.status != "passed"
                    ]
                )
            store.save(results, load_config(ROOT / "configs", brand))
            sources[brand] = {r.document.doc_id: r.document.source_name for r in results}
            start = perf_counter()
            report["index"][brand] = store.maintain_semantic_index(
                brand,
                mode="incremental",
                embedder=embedder,
                actor="self-authored-evaluation",
                reason="固定开发题集的真实模型比较",
            )
            report["index"][brand]["build_seconds"] = round(perf_counter() - start, 4)
        filters = MetadataFilter(model="sedan", year_start=2024, year_end=2024)
        for brand in sources:
            for doc in corpus["documents"]:
                for query in doc["queries"]:
                    row = dict(
                        brand=brand,
                        query=query,
                        expected=f"{brand}-{doc['id']}.html",
                        expected_evidence=doc["text"],
                        modes={},
                    )
                    for mode in times:
                        start = perf_counter()
                        hits = store.search(
                            query, brand, mode=mode, embedder=embedder, k=3, filters=filters
                        )
                        elapsed = (perf_counter() - start) * 1000
                        times[mode].append(elapsed)
                        assert all(
                            h["brand"] == brand and h["status"] == "passed" and h["source_run_id"]
                            for h in hits
                        )
                        rank = next(
                            (
                                i
                                for i, h in enumerate(hits, 1)
                                if h["source_name"] == row["expected"]
                                and row["expected_evidence"] in h["text"]
                            ),
                            None,
                        )
                        total = totals[mode]
                        total["questions"] += 1
                        total["hit1"] += rank == 1
                        total["hit3"] += rank is not None
                        total["reciprocal_rank_at3"] += 1 / rank if rank else 0
                        row["modes"][mode] = dict(
                            rank=rank,
                            milliseconds=round(elapsed, 3),
                            top3=[
                                dict(
                                    source=h["source_name"],
                                    chunk_id=h["chunk_id"],
                                    score=h["score"],
                                    evidence_match=h["source_name"] == row["expected"]
                                    and row["expected_evidence"] in h["text"],
                                )
                                for h in hits
                            ],
                        )
                    report["queries"].append(row)
            for query in corpus["no_answer_queries"]:
                report["no_answer"].append(
                    dict(
                        brand=brand,
                        query=query,
                        returned={
                            mode: [
                                h["source_name"]
                                for h in store.search(
                                    query, brand, mode=mode, embedder=embedder, k=3
                                )
                            ]
                            for mode in times
                        },
                    )
                )
        with store.connect() as db:
            report["vector_cache"] = dict(
                rows=db.execute("SELECT count(*) FROM semantic_vectors").fetchone()[0],
                payload_bytes=db.execute(
                    "SELECT sum(length(CAST(payload AS BLOB))) FROM semantic_vectors"
                ).fetchone()[0],
            )
        for mode, total in totals.items():
            report["metrics"][mode] = {
                **total,
                "hit1_rate": round(total["hit1"] / total["questions"], 4),
                "hit3_rate": round(total["hit3"] / total["questions"], 4),
                "mrr_at3": round(total["reciprocal_rank_at3"] / total["questions"], 4),
                "median_ms": round(statistics.median(times[mode]), 3),
                "p95_ms": round(sorted(times[mode])[int(0.95 * (len(times[mode]) - 1))], 3),
            }
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report["process_peak_rss_mib"] = round(
        peak / (1024**2 if sys.platform == "darwin" else 1024), 2
    )
    report["decision"] = (
        "Keep BM25 default. Offer semantic and RRF explicitly. No reranker yet; this small development set cannot justify another model. No global similarity cutoff or claim of general no-answer detection."
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "metrics": report["metrics"],
                "peak_rss_mib": report["process_peak_rss_mib"],
                "no_answer_cases": len(report["no_answer"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs/retrieval-results.json")
    evaluate(parser.parse_args().output)

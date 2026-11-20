"""Audit chunk coverage using existing conversion results; publish metrics only."""

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from autorepair.chunking import DEFAULT, chunk_document
from autorepair.config import load_config
from autorepair.engine import process
from autorepair.models import Document, Result
from autorepair.parsers import parse

ROOT = Path(__file__).resolve().parents[1]


def check(document):
    modes = []
    for original in [False, True]:
        chunks = chunk_document(document, original=original)
        spans = defaultdict(list)
        for c in chunks:
            for p in c.parts:
                spans[p.block_id].append(p)
        coverage = set(spans) == {b.block_id for b in document.blocks}
        exact = True
        count = 0
        for b in document.blocks:
            text = b.original if original else b.text
            parts = spans[b.block_id]
            offset = 0
            for p in parts:
                exact &= (
                    p.start == offset
                    and p.text == text[p.start : p.end]
                    and p.location == b.location
                    and p.image_src == b.image_src
                )
                offset = p.end
            exact &= offset == len(text)
            exact &= "".join(p.text for p in parts) == text
            count += len(text)
        atomic = all(
            sum(p.block_id == b.block_id for c in chunks for p in c.parts) == 1
            for b in document.blocks
            if b.kind in {"table", "picture", "caption", "callout"}
        )
        bound = all(not c.oversized or (len(c.parts) == 1 and c.parts[0].atomic) for c in chunks)
        refs = all(r.block_id in spans for c in chunks for r in c.related)
        modes.append(
            dict(
                mode="original" if original else "normalized",
                chunks=len(chunks),
                source_characters=count,
                owned_characters=sum(len(p.text) for c in chunks for p in c.parts),
                source_blocks=len(document.blocks),
                covered_blocks=len(spans),
                exact_spans=bool(exact and coverage),
                atomic_blocks_intact=atomic,
                bounded_or_explicit_atomic_exception=bound,
                links_resolve=refs,
                context_links=sum(len(c.related) for c in chunks),
                oversized_chunks=sum(c.oversized for c in chunks),
            )
        )
    return modes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--converted-cache", type=Path, default=ROOT / "work/fidelity-v050")
    parser.add_argument("--real-cache", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/chunking-results.json")
    args = parser.parse_args()
    rows = []
    fixture = Document.model_validate_json(
        (ROOT / "examples/knowledge/section-links.json").read_text()
    )
    rows.append(dict(source="self-authored-linked-repair", status="review", modes=check(fixture)))
    manifest = json.loads((ROOT / "examples/fidelity/manifest.json").read_text())
    # The manifest is a list of source cases frozen before this chunker was authored.
    cases = manifest if isinstance(manifest, list) else manifest["cases"]
    for case in cases:
        name = case["file"]
        source = ROOT / "examples/fidelity" / name
        cached = args.converted_cache / (name + ".json")
        result = Result.model_validate_json(cached.read_text())
        input_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if input_hash != result.document.source_hash:
            # Canonical JSON deliberately retains the original document's source hash.
            data = json.loads(source.read_text()) if source.suffix == ".json" else {}
            if (
                data.get("schema_version") != "1.0"
                or process(parse(source, "aurora"), load_config(ROOT / "configs", "aurora"))
                != result
            ):
                raise ValueError(f"Conversion cache source mismatch: {name}")
        rows.append(
            dict(
                source=name,
                source_sha256=input_hash,
                document_source_hash=result.document.source_hash,
                status=result.status,
                modes=check(result.document),
            )
        )
    if args.real_cache:
        for source in json.loads((ROOT / "docs/real-documents/sources.json").read_text()):
            result = Result.model_validate_json(
                (args.real_cache / source["id"] / "result.json").read_text()
            )
            if result.document.source_hash != source["sha256"]:
                raise ValueError("OEM cache source mismatch")
            rows.append(
                dict(
                    source=source["id"],
                    source_sha256=source["sha256"],
                    status=result.status,
                    input="previously verified OEM conversion cache; not newly approved",
                    modes=check(result.document),
                )
            )
    passed = all(
        all(
            m[k]
            for k in [
                "exact_spans",
                "atomic_blocks_intact",
                "bounded_or_explicit_atomic_exception",
                "links_resolve",
            ]
        )
        for row in rows
        for m in row["modes"]
    )
    report = dict(
        scope="Chunking-stage coverage only. Owned spans exclude repeated related context. Character coverage does not establish upstream conversion completeness or semantic linkage correctness.",
        config=DEFAULT.metadata,
        config_hash=DEFAULT.digest,
        passed=passed,
        documents=len(rows),
        rows=rows,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            dict(
                passed=passed,
                documents=len(rows),
                blocks=sum(r["modes"][0]["source_blocks"] for r in rows),
                chunks=sum(r["modes"][0]["chunks"] for r in rows),
            ),
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

"""Evidence-first retrieval with brand filtering and source location."""

import re

from rank_bm25 import BM25Okapi

from autorepair.metadata import MetadataFilter, snapshot
from autorepair.models import Result


def tokenize(text: str) -> list[str]:
    latin = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.casefold())
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    return latin + [run[i : i + 2] for run in chinese for i in range(max(1, len(run) - 1))]


def search(
    results: list[Result],
    query: str,
    brand: str,
    *,
    original=False,
    include_review=False,
    k=5,
    filters=None,
):
    rows = []
    for result in results:
        if (
            result.document.brand != brand
            or result.status == "failed"
            or (result.status == "review" and not include_review)
        ):
            continue
        match = (filters or MetadataFilter()).match(result.document)
        if match is None:
            continue
        for block in result.document.blocks:
            text = block.original if original else block.text
            rows.append(
                {
                    **snapshot(result.document),
                    "metadata_match": match,
                    "doc_id": result.document.doc_id,
                    "brand": result.document.brand,
                    "model": result.document.model,
                    "year_range": result.document.year_range,
                    "source_name": result.document.source_name,
                    "source_hash": result.document.source_hash,
                    "section_path": block.section_path,
                    "image_src": block.image_src,
                    "block_id": block.block_id,
                    "text": text,
                    "section": block.section,
                    "location": block.location,
                    "status": result.status,
                }
            )
    if not rows or not query.strip():
        return []
    texts = [r["section"] + " " + r["text"] for r in rows]
    tokens = [tokenize(t) or [""] for t in texts]
    bm25 = BM25Okapi(tokens)
    scores = bm25.get_scores(tokenize(query))
    ranked = []
    for i, row in enumerate(rows):
        overlap = set(tokenize(query)) & set(tokens[i])
        if not overlap:
            continue
        # Exact matches get a fixed boost; BM25 scores are relevance, not probability.
        exact = query.casefold() in texts[i].casefold()
        ranked.append(dict(row, score=round(float(scores[i]) + (10 if exact else 0), 5)))
    return sorted(ranked, key=lambda r: (-r["score"], r["doc_id"], r["block_id"]))[:k]


def search_chunks(documents, chunks, query, *, k=5):
    """Rank owned chunk text, then attach related evidence without inflating scores."""
    if not query.strip() or not chunks or k <= 0:
        return []
    texts = [" / ".join(c.section_path) + " " + c.text for c in chunks]
    tokens = [tokenize(t) or [""] for t in texts]
    query_tokens = tokenize(query)
    scores = BM25Okapi(tokens).get_scores(query_tokens)
    ranked = []
    for i, chunk in enumerate(chunks):
        if not set(query_tokens) & set(tokens[i]):
            continue
        doc = documents[(chunk.brand, chunk.doc_id)]
        by_id = {b.block_id: b for b in doc.blocks}
        related = {}
        for link in chunk.related:
            block = by_id[link.block_id]
            if link.block_id not in related:
                related[link.block_id] = {
                    "block_id": block.block_id,
                    "kind": block.kind,
                    "text": block.original if chunk.text_mode == "original" else block.text,
                    "location": block.location,
                    "image_src": block.image_src,
                    "marker": block.list_marker
                    or (
                        f"{block.ordinal}."
                        if block.ordered
                        else "-"
                        if block.kind == "list"
                        else ""
                    ),
                    "list_depth": block.list_depth,
                    "reasons": [],
                }
            related[link.block_id]["reasons"].append(link.reason)
        related_blocks = [related[b.block_id] for b in doc.blocks if b.block_id in related]
        owned_images = [p.image_src for p in chunk.parts if p.image_src]
        rows = {
            **snapshot(doc),
            "chunk_id": chunk.chunk_id,
            "doc_id": doc.doc_id,
            "brand": doc.brand,
            "model": doc.model,
            "year_range": doc.year_range,
            "source_name": doc.source_name,
            "source_hash": doc.source_hash,
            "section_path": chunk.section_path,
            "section": chunk.section_path[-1] if chunk.section_path else "",
            "section_id": chunk.section_id,
            "block_id": chunk.source_block_ids[0],
            "source_block_ids": chunk.source_block_ids,
            "step_ids": chunk.step_ids,
            "text": chunk.text,
            "parts": [p.model_dump() for p in chunk.parts],
            "location": chunk.parts[0].location
            if len(chunk.parts) == 1
            else {
                "pages": chunk.pages,
                "sources": [{"block_id": p.block_id, "location": p.location} for p in chunk.parts],
            },
            "pages": chunk.pages,
            "image_src": owned_images[0] if owned_images else "",
            "image_sources": list(
                dict.fromkeys(
                    owned_images + [b["image_src"] for b in related_blocks if b["image_src"]]
                )
            ),
            "related_blocks": related_blocks,
            "chunk_config": chunk.config,
            "chunk_config_hash": chunk.config_hash,
            "text_mode": chunk.text_mode,
            "oversized": chunk.oversized,
            "status": "passed",
            "score": round(
                float(scores[i]) + (10 if query.casefold() in texts[i].casefold() else 0), 5
            ),
        }
        ranked.append(rows)
    return sorted(ranked, key=lambda r: (-r["score"], r["doc_id"], r["chunk_id"]))[:k]

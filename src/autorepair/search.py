"""Evidence-first retrieval with brand filtering and source location."""

import re

from rank_bm25 import BM25Okapi

from autorepair.models import Result


def tokenize(text: str) -> list[str]:
    latin = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.casefold())
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    return latin + [run[i : i + 2] for run in chinese for i in range(max(1, len(run) - 1))]


def search(
    results: list[Result], query: str, brand: str, *, original=False, include_review=False, k=5
):
    rows = []
    for result in results:
        if (
            result.document.brand != brand
            or result.status == "failed"
            or (result.status == "review" and not include_review)
        ):
            continue
        for block in result.document.blocks:
            text = block.original if original else block.text
            rows.append(
                {
                    "doc_id": result.document.doc_id,
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

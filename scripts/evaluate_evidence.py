"""Inspect previously converted OEM evidence locally; publish metrics, not source copies."""

import argparse
import json
import tempfile
from pathlib import Path

from PIL import Image

from autorepair.chunking import chunk_document
from autorepair.config import Config, load_config
from autorepair.evidence import (
    block_record,
    image_attachment,
    page_targets,
    render_pdf_page,
    table_html,
)
from autorepair.models import Result
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "work/real-documents/fixed")
    parser.add_argument("--originals", type=Path, default=ROOT / "work/real-documents/originals")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence-results.json")
    args = parser.parse_args()
    sources = json.loads((ROOT / "docs/real-documents/sources.json").read_text())
    rows = []
    for source in sources:
        result = Result.model_validate_json((args.cache / source["id"] / "result.json").read_text())
        document = result.document
        if document.source_hash != source["sha256"] or result.status != "review":
            raise ValueError("Source hash/status mismatch")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "audit.sqlite")
            cfg = Config(brand=document.brand, rules=load_config(ROOT / "configs", "aurora").rules)
            run_id = store.save([result], cfg)
            store.attach_source(
                document.brand,
                document.doc_id,
                args.originals / (source["id"] + ".pdf"),
                run_id=run_id,
            )
            chunks = chunk_document(document)
            bundles = [
                store.evidence(document.brand, document.doc_id, c.chunk_id, run_id=run_id)
                for c in chunks
            ]
            refs = sorted({b.image_src for b in document.blocks if b.image_src})
            images = [image_attachment(document, ref) for ref in refs]
            tables = [b for b in document.blocks if b.kind == "table"]
            targets = page_targets([block_record(b) for b in document.blocks])
            retained = store.source_file(document.brand, document.doc_id, run_id=run_id)
            rendered = 0
            skipped = 0
            for target in targets:
                preview = render_pdf_page(retained["data"], target["page"], target["regions"])
                import io

                Image.open(io.BytesIO(preview["data"])).verify()
                rendered += 1
                skipped += preview["skipped_regions"]
            selected = 3 if source["id"] == "honda" else 2 if source["id"] == "toyota" else 1
            focus = [
                b
                for b in document.blocks
                if b.kind == "table" and selected in b.location.get("pages", [])
            ] or [
                b
                for b in document.blocks
                if b.kind == "picture" and selected in b.location.get("pages", [])
            ]
            focus_targets = page_targets([block_record(b) for b in focus])
            regions = next((t["regions"] for t in focus_targets if t["page"] == selected), [])
            preview = render_pdf_page(retained["data"], selected, regions)
            out = ROOT / "work/evidence-real"
            out.mkdir(parents=True, exist_ok=True)
            (out / (source["id"] + f"-page{selected}.png")).write_bytes(preview["data"])
            row = dict(
                source=source["id"],
                sha256=document.source_hash,
                status=result.status,
                evidence_bundles=len(bundles),
                retained_source_verified=retained["status"] == "available",
                pages=[t["page"] for t in targets],
                pages_rendered=rendered,
                tables=len(tables),
                tables_rendered=sum(bool(table_html(b.text)) for b in tables),
                distinct_images=len(images),
                valid_images=sum(i["status"] == "available" for i in images),
                skipped_ambiguous_regions=skipped,
                approved_documents=len(store.current(document.brand)),
            )
            rows.append(row)
    passed = all(
        r["retained_source_verified"]
        and r["pages_rendered"] == len(r["pages"])
        and r["tables"] == r["tables_rendered"]
        and r["distinct_images"] == r["valid_images"]
        and r["approved_documents"] == 0
        for r in rows
    )
    args.output.write_text(
        json.dumps(
            dict(
                scope="Evidence display of verified local conversion caches, not source conversion or semantic accuracy. No OEM originals/images are redistributed. Ambiguous geometry is skipped rather than guessed.",
                passed=passed,
                rows=rows,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        json.dumps(
            dict(
                passed=passed,
                documents=len(rows),
                pages=sum(r["pages_rendered"] for r in rows),
                tables=sum(r["tables_rendered"] for r in rows),
                distinct_images=sum(r["valid_images"] for r in rows),
            )
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

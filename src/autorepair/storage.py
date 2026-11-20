"""SQLite persistence with per-brand upserts and run-scoped provenance."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autorepair.config import Config
from autorepair.models import Result


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, brand TEXT NOT NULL,
              rules_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
              brand TEXT, doc_id TEXT, payload TEXT NOT NULL, PRIMARY KEY (brand, doc_id));
            CREATE TABLE IF NOT EXISTS terms (
              brand TEXT, entity_id TEXT, canonical TEXT, kind TEXT, aliases TEXT,
              PRIMARY KEY (brand, entity_id));
            CREATE TABLE IF NOT EXISTS refs (
              run_id TEXT, doc_id TEXT, block_id TEXT, target TEXT,
              PRIMARY KEY (run_id, doc_id, block_id, target));
            CREATE TABLE IF NOT EXISTS parts (
              run_id TEXT, brand TEXT, doc_id TEXT, part_number TEXT, block_id TEXT,
              payload TEXT NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.path)

    def save(self, results: list[Result], cfg: Config) -> str:
        run_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    datetime.now(UTC).isoformat(),
                    cfg.brand,
                    cfg.digest,
                    json.dumps([r.model_dump() for r in results], ensure_ascii=False),
                ),
            )
            db.execute("DELETE FROM terms WHERE brand = ?", (cfg.brand,))
            for term in cfg.terms:
                db.execute(
                    "INSERT INTO terms VALUES (?, ?, ?, ?, ?)",
                    (
                        cfg.brand,
                        term.entity_id,
                        term.canonical,
                        term.kind,
                        json.dumps(term.aliases, ensure_ascii=False),
                    ),
                )
            for result in results:
                doc = result.document
                for mention in doc.component_mentions:
                    db.execute(
                        "INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            doc.brand,
                            doc.doc_id,
                            mention["part_number"],
                            mention["block_id"],
                            json.dumps(mention, ensure_ascii=False),
                        ),
                    )
                if result.status == "passed":
                    db.execute(
                        "INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                        (doc.brand, doc.doc_id, doc.model_dump_json()),
                    )
                else:
                    # A failed reprocessing run must invalidate its stale approved version.
                    db.execute(
                        "DELETE FROM documents WHERE brand = ? AND doc_id = ?",
                        (doc.brand, doc.doc_id),
                    )
                for block in doc.blocks:
                    for target in set(block.links):
                        db.execute(
                            "INSERT INTO refs VALUES (?, ?, ?, ?)",
                            (run_id, doc.doc_id, block.block_id, target),
                        )
        return run_id

    def load_run(self, run_id: str) -> list[Result]:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return [Result.model_validate(x) for x in json.loads(row[0])]

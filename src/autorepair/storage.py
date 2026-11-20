"""SQLite persistence with per-brand upserts and run-scoped provenance."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autorepair.chunking import DEFAULT, Chunk, chunk_document
from autorepair.config import Config
from autorepair.models import Document, Result


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            had_catalog = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_catalog'"
            ).fetchone()
            db.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
              brand TEXT NOT NULL, doc_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY (brand, doc_id, chunk_id));
            CREATE TABLE IF NOT EXISTS document_chunk_state (
              brand TEXT NOT NULL, doc_id TEXT NOT NULL, document_hash TEXT NOT NULL,
              config_hash TEXT NOT NULL, chunk_count INTEGER NOT NULL,
              PRIMARY KEY (brand, doc_id));
            CREATE TABLE IF NOT EXISTS document_catalog (
              brand TEXT NOT NULL, doc_id TEXT NOT NULL, source_name TEXT NOT NULL,
              source_hash TEXT NOT NULL, model TEXT NOT NULL, year_range TEXT NOT NULL,
              status TEXT NOT NULL, run_id TEXT NOT NULL, rules_hash TEXT NOT NULL,
              updated_at TEXT NOT NULL, reviews TEXT NOT NULL,
              PRIMARY KEY (brand, doc_id));
            CREATE INDEX IF NOT EXISTS catalog_brand_status ON document_catalog(brand, status);
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

            if not had_catalog:
                # Replay metadata only, oldest to newest; keep historical payloads untouched.
                for run_id, created, rules, payload in db.execute(
                    "SELECT run_id, created_at, rules_hash, payload FROM runs ORDER BY created_at, rowid"
                ).fetchall():
                    for data in json.loads(payload):
                        self._catalog(db, Result.model_validate(data), run_id, rules, created)
                for brand, doc_id, payload in db.execute("SELECT * FROM documents").fetchall():
                    if not db.execute(
                        "SELECT 1 FROM document_catalog WHERE brand=? AND doc_id=?", (brand, doc_id)
                    ).fetchone():
                        self._catalog(
                            db,
                            Result(document=Document.model_validate_json(payload), rules_hash=""),
                            "",
                            "",
                            "",
                        )

            self._ensure_chunks(db)

    @staticmethod
    def _replace_chunks(db, doc, payload):
        chunks = chunk_document(doc)
        db.execute(
            "DELETE FROM knowledge_chunks WHERE brand=? AND doc_id=?", (doc.brand, doc.doc_id)
        )
        db.executemany(
            "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?)",
            [(doc.brand, doc.doc_id, c.chunk_id, c.model_dump_json()) for c in chunks],
        )
        db.execute(
            "INSERT OR REPLACE INTO document_chunk_state VALUES (?, ?, ?, ?, ?)",
            (
                doc.brand,
                doc.doc_id,
                hashlib.sha256(payload.encode()).hexdigest(),
                DEFAULT.digest,
                len(chunks),
            ),
        )

    def _ensure_chunks(self, db):
        # Rebuild only derived data; never alter approval state or historical payloads.
        approved = "SELECT 1 FROM documents d JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id WHERE d.brand={table}.brand AND d.doc_id={table}.doc_id AND c.status='passed'"
        for table in ["knowledge_chunks", "document_chunk_state"]:
            db.execute(f"DELETE FROM {table} WHERE NOT EXISTS ({approved.format(table=table)})")
        rows = db.execute(
            "SELECT d.brand, d.doc_id, d.payload, s.document_hash, s.config_hash, s.chunk_count "
            "FROM documents d JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
            "LEFT JOIN document_chunk_state s ON s.brand=d.brand AND s.doc_id=d.doc_id "
            "WHERE c.status='passed'"
        ).fetchall()
        for brand, doc_id, payload, digest, config_hash, count in rows:
            actual = db.execute(
                "SELECT count(*) FROM knowledge_chunks WHERE brand=? AND doc_id=?", (brand, doc_id)
            ).fetchone()[0]
            if (
                digest != hashlib.sha256(payload.encode()).hexdigest()
                or config_hash != DEFAULT.digest
                or actual != count
            ):
                self._replace_chunks(db, Document.model_validate_json(payload), payload)

    @staticmethod
    def _catalog(db, result, run_id, rules_hash, created):
        doc = result.document
        db.execute(
            "INSERT OR REPLACE INTO document_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc.brand,
                doc.doc_id,
                doc.source_name,
                doc.source_hash,
                doc.model,
                doc.year_range,
                result.status,
                run_id,
                rules_hash,
                created,
                json.dumps(result.reviews, ensure_ascii=False),
            ),
        )

    def connect(self):
        return sqlite3.connect(self.path)

    def save(self, results: list[Result], cfg: Config) -> str:
        if any(r.document.brand != cfg.brand for r in results):
            raise ValueError("Result brand does not match configuration")
        if len({r.document.doc_id for r in results}) != len(results) and any(
            r.status != "failed" for r in results
        ):
            raise ValueError("Duplicate document IDs in one save")
        if any(r.status == "passed" and r.issues for r in results):
            raise ValueError("Passed documents cannot contain unresolved issues")
        run_id = uuid4().hex
        created = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    created,
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
                self._catalog(db, result, run_id, cfg.digest, created)
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
                    payload = doc.model_dump_json()
                    db.execute(
                        "INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                        (doc.brand, doc.doc_id, payload),
                    )
                    self._replace_chunks(db, doc, payload)
                else:
                    # A failed reprocessing run must invalidate its stale approved version.
                    db.execute(
                        "DELETE FROM documents WHERE brand = ? AND doc_id = ?",
                        (doc.brand, doc.doc_id),
                    )
                if result.status != "passed":
                    for table in ["knowledge_chunks", "document_chunk_state"]:
                        db.execute(
                            f"DELETE FROM {table} WHERE brand=? AND doc_id=?",
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

    def list_documents(self, brand: str, *, status: str | None = None) -> list[dict]:
        """Latest known state per document, including excluded candidates for review."""
        if status is not None and status not in {"passed", "review", "failed"}:
            raise ValueError("Unknown document status")
        where = "brand = ?" + (" AND status = ?" if status else "")
        params = (brand, status) if status else (brand,)
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"SELECT * FROM document_catalog WHERE {where} ORDER BY updated_at DESC, doc_id",
                params,
            ).fetchall()
        return [{**dict(row), "reviews": json.loads(row["reviews"])} for row in rows]

    def current(self, brand: str) -> list[Result]:
        """Load only current approved knowledge; never fall back to historical runs."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.payload, c.rules_hash, c.reviews FROM documents d "
                "JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
                "WHERE d.brand=? AND c.status='passed' ORDER BY d.doc_id",
                (brand,),
            ).fetchall()
        return [
            Result(
                document=Document.model_validate_json(payload),
                rules_hash=rules,
                reviews=json.loads(reviews),
            )
            for payload, rules, reviews in rows
        ]

    def chunks(self, brand: str, doc_id: str | None = None) -> list[Chunk]:
        with self.connect() as db:
            return self._load_chunks(db, brand, doc_id)

    @staticmethod
    def _load_chunks(db, brand, doc_id=None):
        rows = db.execute(
            "SELECT k.payload FROM knowledge_chunks k JOIN documents d ON d.brand=k.brand AND d.doc_id=k.doc_id "
            "JOIN document_catalog c ON c.brand=k.brand AND c.doc_id=k.doc_id "
            "WHERE k.brand=? AND c.status='passed'"
            + (" AND k.doc_id=?" if doc_id is not None else "")
            + " ORDER BY k.doc_id, k.rowid",
            (brand, doc_id) if doc_id is not None else (brand,),
        ).fetchall()
        return [Chunk.model_validate_json(payload) for (payload,) in rows]

    def search(self, query: str, brand: str, *, original=False, k=5) -> list[dict]:
        from autorepair.search import search_chunks

        # Explicit transaction gives document content, derived chunks and approval provenance one snapshot.
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT d.payload, c.run_id, c.rules_hash FROM documents d "
                "JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
                "WHERE d.brand=? AND c.status='passed' ORDER BY d.doc_id",
                (brand,),
            ).fetchall()
            chunks = self._load_chunks(db, brand)
        docs = [Document.model_validate_json(payload) for payload, _, _ in rows]
        documents = {(d.brand, d.doc_id): d for d in docs}
        provenance = {
            doc.doc_id: dict(source_run_id=row[1], rules_hash=row[2])
            for doc, row in zip(docs, rows, strict=True)
        }
        if original:
            chunks = [c for doc in docs for c in chunk_document(doc, original=True)]
        return [
            {**hit, **provenance[hit["doc_id"]]}
            for hit in search_chunks(documents, chunks, query, k=k)
        ]

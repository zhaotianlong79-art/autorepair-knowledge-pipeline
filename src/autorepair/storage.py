"""SQLite persistence with per-brand upserts and run-scoped provenance."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autorepair.chunking import DEFAULT, Chunk, chunk_document
from autorepair.config import Config
from autorepair.metadata import FIELDS, MetadataFilter, MetadataPatch, apply_metadata, snapshot
from autorepair.models import Document, Result
from autorepair.privacy import redact


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            had_catalog = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_catalog'"
            ).fetchone()
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata_history (
              event_id TEXT PRIMARY KEY, brand TEXT NOT NULL, doc_id TEXT NOT NULL,
              revision INTEGER NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
              reason TEXT NOT NULL, source TEXT NOT NULL, before_json TEXT NOT NULL,
              after_json TEXT NOT NULL, base_run_id TEXT NOT NULL, run_id TEXT NOT NULL,
              UNIQUE (brand, doc_id, revision));
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

            columns = {row[1] for row in db.execute("PRAGMA table_info(document_catalog)")}
            migrate_metadata = "document_type" not in columns
            for name, default in [
                ("document_type", "unknown"),
                ("document_version", "unknown"),
                ("applicability", "[]"),
            ]:
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE document_catalog ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                    )
            if "metadata_revision" not in columns:
                db.execute(
                    "ALTER TABLE document_catalog ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 0"
                )
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

            if migrate_metadata and had_catalog:
                for brand, doc_id, run_id in db.execute(
                    "SELECT brand, doc_id, run_id FROM document_catalog"
                ).fetchall():
                    row = db.execute(
                        "SELECT payload FROM documents WHERE brand=? AND doc_id=?", (brand, doc_id)
                    ).fetchone()
                    if row:
                        doc = Document.model_validate_json(row[0])
                    else:
                        doc = (
                            next(
                                (
                                    r.document
                                    for r in self._load_run(db, run_id)
                                    if r.document.doc_id == doc_id and r.document.brand == brand
                                ),
                                None,
                            )
                            if run_id
                            else None
                        )
                    if doc:
                        db.execute(
                            "UPDATE document_catalog SET document_type=?, document_version=?, applicability=? WHERE brand=? AND doc_id=?",
                            (
                                doc.document_type,
                                doc.document_version,
                                json.dumps(snapshot(doc)["applicability"]),
                                brand,
                                doc_id,
                            ),
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
    def _catalog(db, result, run_id, rules_hash, created, revision=None):
        doc = result.document
        if revision is None:
            old = db.execute(
                "SELECT metadata_revision FROM document_catalog WHERE brand=? AND doc_id=?",
                (doc.brand, doc.doc_id),
            ).fetchone()
            last = (
                db.execute(
                    "SELECT max(revision) FROM metadata_history WHERE brand=? AND doc_id=?",
                    (doc.brand, doc.doc_id),
                ).fetchone()[0]
                or 0
            )
            revision = max(old[0] if old else 0, last)
        db.execute(
            "INSERT OR REPLACE INTO document_catalog (brand,doc_id,source_name,source_hash,model,year_range,status,run_id,rules_hash,updated_at,reviews,document_type,document_version,applicability,metadata_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                doc.document_type,
                doc.document_version,
                json.dumps(snapshot(doc)["applicability"], ensure_ascii=False),
                revision,
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
            db.execute("BEGIN IMMEDIATE")
            prepared = []
            seen_metadata = set()
            for incoming in results:
                doc = incoming.document
                old = db.execute(
                    "SELECT run_id FROM document_catalog WHERE brand=? AND doc_id=?",
                    (doc.brand, doc.doc_id),
                ).fetchone()
                prior = (
                    next(
                        (
                            r
                            for r in self._load_run(db, old[0])
                            if r.document.doc_id == doc.doc_id and r.document.brand == doc.brand
                        ),
                        None,
                    )
                    if old and old[0]
                    else None
                )
                input_event = next(
                    (r for r in reversed(incoming.reviews) if r.get("check") == "metadata.input"),
                    None,
                )
                if prior and input_event and input_event.get("patch"):
                    doc = apply_metadata(
                        doc.model_copy(update={k: getattr(prior.document, k) for k in FIELDS}),
                        MetadataPatch.model_validate(input_event["patch"]),
                    )
                    incoming = incoming.model_copy(update={"document": doc})
                elif (
                    prior
                    and not any(r.get("check") == "metadata.input" for r in incoming.reviews)
                    and snapshot(doc)
                    == snapshot(
                        Document(
                            doc_id="unknown",
                            brand=doc.brand,
                            source_name="",
                            source_hash="",
                            parser="",
                        )
                    )
                ):
                    doc = doc.model_copy(update={k: getattr(prior.document, k) for k in FIELDS})
                    incoming = incoming.model_copy(update={"document": doc})
                prepared.append(incoming)
                if doc.doc_id not in seen_metadata:
                    self._metadata_event(
                        db,
                        prior.document if prior else None,
                        doc,
                        run_id,
                        created,
                        "local-ingest",
                        "初次录入" if prior is None else "重新加工元数据",
                        "ingest",
                    )
                    seen_metadata.add(doc.doc_id)
            results = prepared
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
            self._publish(db, results, run_id, cfg.digest, created)
        return run_id

    def _publish(self, db, results, run_id, rules_hash, created):
        for result in results:
            doc = result.document
            last_revision = db.execute(
                "SELECT max(revision) FROM metadata_history WHERE brand=? AND doc_id=?",
                (doc.brand, doc.doc_id),
            ).fetchone()[0]
            self._catalog(db, result, run_id, rules_hash, created, revision=last_revision)
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

    def load_run(self, run_id: str) -> list[Result]:
        with self.connect() as db:
            return self._load_run(db, run_id)

    @staticmethod
    def _load_run(db, run_id):
        row = db.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return [Result.model_validate(x) for x in json.loads(row[0])]

    def list_documents(
        self, brand: str, *, status: str | None = None, filters: MetadataFilter | None = None
    ) -> list[dict]:
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
        entries = [
            {
                **dict(row),
                "reviews": json.loads(row["reviews"]),
                "applicability": json.loads(row["applicability"]),
            }
            for row in rows
        ]
        if filters:
            entries = [
                {**entry, "metadata_match": match}
                for entry in entries
                if (
                    match := filters.match(
                        Document.model_validate(
                            {
                                k: entry[k]
                                for k in ["brand", "doc_id", "source_name", "source_hash", *FIELDS]
                            }
                            | {"parser": "catalog"}
                        )
                    )
                )
                is not None
            ]
        return entries

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

    def search(
        self, query: str, brand: str, *, original=False, k=5, filters: MetadataFilter | None = None
    ) -> list[dict]:
        from autorepair.search import search_chunks

        # Explicit transaction gives document content, derived chunks and approval provenance one snapshot.
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT d.payload, c.run_id, c.rules_hash, c.metadata_revision FROM documents d "
                "JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
                "WHERE d.brand=? AND c.status='passed' ORDER BY d.doc_id",
                (brand,),
            ).fetchall()
            chunks = self._load_chunks(db, brand)
        docs = [Document.model_validate_json(row[0]) for row in rows]
        filters = filters or MetadataFilter()
        matches = {d.doc_id: filters.match(d) for d in docs}
        docs = [d for d in docs if matches[d.doc_id] is not None]
        documents = {(d.brand, d.doc_id): d for d in docs}
        chunks = [c for c in chunks if (c.brand, c.doc_id) in documents]
        provenance = {
            doc.doc_id: dict(source_run_id=row[1], rules_hash=row[2], metadata_revision=row[3])
            for doc, row in zip(
                [Document.model_validate_json(row[0]) for row in rows], rows, strict=True
            )
        }
        if original:
            chunks = [c for doc in docs for c in chunk_document(doc, original=True)]
        return [
            {**hit, **provenance[hit["doc_id"]], "metadata_match": matches[hit["doc_id"]]}
            for hit in search_chunks(documents, chunks, query, k=k)
        ]

    @staticmethod
    def _metadata_event(db, before_doc, after_doc, run_id, created, actor, reason, source):
        before = (
            snapshot(before_doc)
            if before_doc
            else snapshot(
                Document(
                    doc_id="", brand=after_doc.brand, source_name="", source_hash="", parser=""
                )
            )
        )
        after = snapshot(after_doc)
        row = db.execute(
            "SELECT metadata_revision,run_id FROM document_catalog WHERE brand=? AND doc_id=?",
            (after_doc.brand, after_doc.doc_id),
        ).fetchone()
        last = (
            db.execute(
                "SELECT max(revision) FROM metadata_history WHERE brand=? AND doc_id=?",
                (after_doc.brand, after_doc.doc_id),
            ).fetchone()[0]
            or 0
        )
        revision = max(row[0] if row else 0, last)
        if before == after:
            return revision
        revision += 1
        db.execute(
            "INSERT INTO metadata_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                after_doc.brand,
                after_doc.doc_id,
                revision,
                created,
                redact(actor.strip()),
                redact(reason.strip()),
                source,
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                row[1] if row else "",
                run_id,
            ),
        )
        if row:
            db.execute(
                "UPDATE document_catalog SET metadata_revision=? WHERE brand=? AND doc_id=?",
                (revision, after_doc.brand, after_doc.doc_id),
            )
        return revision

    def metadata_history(self, brand, doc_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM metadata_history WHERE brand=? AND doc_id=? ORDER BY revision",
                (brand, doc_id),
            ).fetchall()
        return [
            {
                **{k: v for k, v in dict(r).items() if k not in {"before_json", "after_json"}},
                "before": json.loads(r["before_json"]),
                "after": json.loads(r["after_json"]),
            }
            for r in rows
        ]

    def correct_metadata(
        self, brand, doc_id, patch: MetadataPatch, *, actor, reason, expected_revision
    ):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("修订号须为非负整数")
        if not actor.strip() or not reason.strip() or not patch.supplied:
            raise ValueError("修正须填写操作人、原因和至少一个元数据字段")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT run_id,metadata_revision FROM document_catalog WHERE brand=? AND doc_id=?",
                (brand, doc_id),
            ).fetchone()
            if not row:
                raise KeyError(doc_id)
            if row[1] != expected_revision:
                raise ValueError("元数据已被其他操作更新，请刷新后重试")
            prior = next(
                r
                for r in self._load_run(db, row[0])
                if r.document.doc_id == doc_id and r.document.brand == brand
            )
            doc = apply_metadata(prior.document, patch)
            if snapshot(doc) == snapshot(prior.document):
                raise ValueError("元数据没有变化，无需重复修正")
            run_id = uuid4().hex
            created = datetime.now(UTC).isoformat()
            revision = self._metadata_event(
                db, prior.document, doc, run_id, created, actor, reason, "manual"
            )
            result = prior.model_copy(deep=True)
            result.document = doc
            result.reviews.append(
                {
                    "check": "metadata.correction",
                    "actor": redact(actor.strip()),
                    "reason": redact(reason.strip()),
                    "revision": revision,
                    "base_run_id": row[0],
                }
            )
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    created,
                    brand,
                    result.rules_hash,
                    json.dumps([result.model_dump()], ensure_ascii=False),
                ),
            )
            self._publish(db, [result], run_id, result.rules_hash, created)
            return {"run_id": run_id, "metadata_revision": revision, "status": result.status}

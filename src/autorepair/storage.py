"""SQLite persistence with per-brand upserts and run-scoped provenance."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autorepair.chunking import DEFAULT, Chunk, chunk_document
from autorepair.config import Config
from autorepair.lifecycle import LifecycleMixin
from autorepair.metadata import FIELDS, MetadataFilter, MetadataPatch, apply_metadata, snapshot
from autorepair.models import Document, Result
from autorepair.privacy import redact
from autorepair.sources import MIMES, SourceFile, read_source, source_snapshot


class Store(LifecycleMixin):
    def __init__(self, path: Path, *, auto_repair=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            had_catalog = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_catalog'"
            ).fetchone()
            db.executescript("""
            CREATE TABLE IF NOT EXISTS source_files (
              brand TEXT NOT NULL, doc_id TEXT NOT NULL, source_hash TEXT NOT NULL,
              suffix TEXT NOT NULL, name TEXT NOT NULL, data BLOB NOT NULL,
              PRIMARY KEY(brand,doc_id,source_hash));
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
            state_columns = {r[1] for r in db.execute("PRAGMA table_info(document_chunk_state)")}
            if "index_hash" not in state_columns:
                db.execute(
                    "ALTER TABLE document_chunk_state ADD COLUMN index_hash TEXT NOT NULL DEFAULT ''"
                )
            from autorepair.semantic import init_cache

            init_cache(db)
            self._init_lifecycle(db, sync=auto_repair)
            if auto_repair:
                self._ensure_chunks(db)

    @staticmethod
    def _index_digest(rows):
        return hashlib.sha256(json.dumps(sorted(rows), ensure_ascii=False).encode()).hexdigest()

    def _replace_chunks(self, db, doc, payload, *, force=False):
        digest = hashlib.sha256(payload.encode()).hexdigest()
        state = db.execute(
            "SELECT document_hash,config_hash,chunk_count,index_hash FROM document_chunk_state WHERE brand=? AND doc_id=?",
            (doc.brand, doc.doc_id),
        ).fetchone()
        stored = db.execute(
            "SELECT chunk_id,payload FROM knowledge_chunks WHERE brand=? AND doc_id=?",
            (doc.brand, doc.doc_id),
        ).fetchall()
        if (
            not force
            and state
            and state[0] == digest
            and state[1] == DEFAULT.digest
            and state[2] == len(stored)
            and state[3] == self._index_digest(stored)
        ):
            return False
        chunks = chunk_document(doc)
        rows = [(c.chunk_id, c.model_dump_json()) for c in chunks]
        db.execute(
            "DELETE FROM knowledge_chunks WHERE brand=? AND doc_id=?", (doc.brand, doc.doc_id)
        )
        db.executemany(
            "INSERT INTO knowledge_chunks VALUES (?, ?, ?, ?)",
            [(doc.brand, doc.doc_id, k, p) for k, p in rows],
        )
        db.execute(
            "INSERT OR REPLACE INTO document_chunk_state(brand,doc_id,document_hash,config_hash,chunk_count,index_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (doc.brand, doc.doc_id, digest, DEFAULT.digest, len(rows), self._index_digest(rows)),
        )
        return True

    def _ensure_chunks(self, db, *, brand=None, repair=True, force=False):
        approved = "SELECT 1 FROM document_versions v JOIN document_families f ON f.brand=v.brand AND f.logical_id=v.logical_id JOIN document_catalog c ON c.brand=v.brand AND c.doc_id=v.doc_id WHERE v.brand={table}.brand AND v.doc_id={table}.doc_id AND v.state='active' AND v.quality='passed' AND f.active_doc_id=v.doc_id AND f.blocked=0 AND c.status='passed'"
        orphan = {}
        for table in ["documents", "knowledge_chunks", "document_chunk_state"]:
            where = (
                "brand=? AND " if brand is not None else ""
            ) + f"NOT EXISTS ({approved.format(table=table)})"
            params = (brand,) if brand is not None else ()
            orphan[table] = db.execute(
                f"SELECT count(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
            if repair:
                db.execute(f"DELETE FROM {table} WHERE {where}", params)
        rows = db.execute(
            "SELECT v.brand,v.doc_id,v.latest_run_id,d.payload,s.document_hash,s.config_hash,s.chunk_count,s.index_hash FROM document_versions v JOIN document_families f ON f.brand=v.brand AND f.logical_id=v.logical_id JOIN document_catalog c ON c.brand=v.brand AND c.doc_id=v.doc_id LEFT JOIN documents d ON d.brand=v.brand AND d.doc_id=v.doc_id LEFT JOIN document_chunk_state s ON s.brand=v.brand AND s.doc_id=v.doc_id WHERE v.state='active' AND v.quality='passed' AND f.active_doc_id=v.doc_id AND f.blocked=0 AND c.status='passed'"
            + (" AND v.brand=?" if brand is not None else ""),
            (brand,) if brand is not None else (),
        ).fetchall()
        dirty = []
        repaired = 0
        for owner, doc_id, run_id, payload, digest, config_hash, count, index_hash in rows:
            missing = payload is None
            payload_damaged = False
            if run_id:
                result = next(
                    (
                        r
                        for r in self._load_run(db, run_id)
                        if r.document.brand == owner and r.document.doc_id == doc_id
                    ),
                    None,
                )
                if result is None or result.status != "passed" or result.issues:
                    raise ValueError("活动版本的审核记录不一致，停止索引维护")
                try:
                    payload_damaged = (
                        payload is not None
                        and Document.model_validate_json(payload) != result.document
                    )
                except ValueError:
                    payload_damaged = True
                if missing or payload_damaged:
                    payload = result.document.model_dump_json()
            elif missing:
                raise ValueError("旧资料缺少可恢复的已审核运行正文")
            stored = db.execute(
                "SELECT chunk_id,payload FROM knowledge_chunks WHERE brand=? AND doc_id=?",
                (owner, doc_id),
            ).fetchall()
            reasons = []
            if missing:
                reasons.append("document_missing")
            if payload_damaged:
                reasons.append("document_payload")
            if digest != hashlib.sha256(payload.encode()).hexdigest():
                reasons.append("document_hash")
            if config_hash != DEFAULT.digest:
                reasons.append("config_hash")
            if count != len(stored):
                reasons.append("chunk_count")
            if index_hash != self._index_digest(stored):
                reasons.append("index_hash")
            if reasons or force:
                dirty.append(
                    dict(brand=owner, doc_id=doc_id, reasons=reasons or ["forced_rebuild"])
                )
                if repair:
                    if missing or payload_damaged:
                        db.execute(
                            "INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                            (owner, doc_id, payload),
                        )
                    self._replace_chunks(
                        db, Document.model_validate_json(payload), payload, force=force
                    )
                    repaired += 1
        return dict(
            brand=brand,
            checked=len(rows),
            dirty=dirty,
            repaired=repaired,
            orphan_rows=orphan,
            config_hash=DEFAULT.digest,
        )

    def maintain_index(self, brand, *, mode="check", actor="", reason=""):
        if mode not in {"check", "incremental", "rebuild"}:
            raise ValueError("未知索引维护模式")
        if mode != "check":
            self._operation(actor, reason, 0)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE" if mode != "check" else "BEGIN")
            report = self._ensure_chunks(
                db, brand=brand, repair=mode != "check", force=mode == "rebuild"
            )
            if mode != "check":
                self._event(db, brand, "", "", f"index-{mode}", {}, report, actor, reason)
            return {**report, "mode": mode}

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
        originals = results
        run_id = uuid4().hex
        created = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prepared = []
            batch_families = {}
            seen_metadata = set()
            for incoming in results:
                doc = incoming.document
                registered = db.execute(
                    "SELECT logical_id FROM document_versions WHERE brand=? AND doc_id=?",
                    (doc.brand, doc.doc_id),
                ).fetchone()
                family = (
                    doc.logical_id.strip()
                    if doc.logical_id
                    else registered[0]
                    if registered
                    else doc.doc_id
                )
                if family in batch_families and batch_families[family] != doc.doc_id:
                    raise ValueError("同一批次不能提交同一逻辑文档的多个版本，请逐个处理")
                batch_families[family] = doc.doc_id
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
            for result in originals:
                source = source_snapshot(result)
                if source and result.status != "failed":
                    self._write_source(db, result.document, source)
        return run_id

    def _publish(self, db, results, run_id, rules_hash, created, *, auto_activate=True):
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
            publish = self._register_version(
                db, result, run_id, created, auto_activate=auto_activate
            )
            if publish:
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
            if not publish:
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
        versions = {v["doc_id"]: v for v in self.versions(brand)}
        return [
            {**e, "lifecycle_revision": versions[e["doc_id"]]["lifecycle_revision"]}
            for e in entries
        ]

    def current(self, brand: str) -> list[Result]:
        """Load only current approved knowledge; never fall back to historical runs."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.payload, c.rules_hash, c.reviews,c.logical_id FROM documents d "
                "JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
                "WHERE d.brand=? AND c.status='passed' AND c.lifecycle='active' AND EXISTS (SELECT 1 FROM document_versions av JOIN document_families af ON af.brand=av.brand AND af.logical_id=av.logical_id WHERE av.brand=c.brand AND av.doc_id=c.doc_id AND av.state='active' AND av.quality='passed' AND af.active_doc_id=av.doc_id AND af.blocked=0) ORDER BY d.doc_id",
                (brand,),
            ).fetchall()
        return [
            Result(
                document=Document.model_validate_json(payload).model_copy(
                    update={"logical_id": logical}
                ),
                rules_hash=rules,
                reviews=json.loads(reviews),
            )
            for payload, rules, reviews, logical in rows
        ]

    def chunks(self, brand: str, doc_id: str | None = None) -> list[Chunk]:
        with self.connect() as db:
            return self._load_chunks(db, brand, doc_id)

    @staticmethod
    def _load_chunks(db, brand, doc_id=None):
        rows = db.execute(
            "SELECT k.payload FROM knowledge_chunks k JOIN documents d ON d.brand=k.brand AND d.doc_id=k.doc_id "
            "JOIN document_catalog c ON c.brand=k.brand AND c.doc_id=k.doc_id "
            "WHERE k.brand=? AND c.status='passed' AND c.lifecycle='active' AND EXISTS (SELECT 1 FROM document_versions av JOIN document_families af ON af.brand=av.brand AND af.logical_id=av.logical_id WHERE av.brand=c.brand AND av.doc_id=c.doc_id AND av.state='active' AND av.quality='passed' AND af.active_doc_id=av.doc_id AND af.blocked=0)"
            + (" AND k.doc_id=?" if doc_id is not None else "")
            + " ORDER BY k.doc_id, k.rowid",
            (brand, doc_id) if doc_id is not None else (brand,),
        ).fetchall()
        return [Chunk.model_validate_json(payload) for (payload,) in rows]

    def search(
        self,
        query: str,
        brand: str,
        *,
        original=False,
        k=5,
        filters: MetadataFilter | None = None,
        mode="bm25",
        embedder=None,
    ) -> list[dict]:
        from autorepair.search import lexical_ranking, search_chunks
        from autorepair.semantic import (
            LazyEmbedder,
            cached_vectors,
            fuse,
            identifier_filter,
            rank_semantic,
        )

        if mode not in {"bm25", "semantic", "hybrid"}:
            raise ValueError("未知检索模式")
        if original and mode != "bm25":
            raise ValueError("原文对照查询目前仅支持 BM25；语义检索使用正式规范正文")
        if not query.strip() or k <= 0:
            return []
        if mode != "bm25" and len(query) > 400:
            raise ValueError("语义查询最多 400 字符，防止超过模型输入上限")
        # Explicit transaction gives document content, derived chunks and approval provenance one snapshot.
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT d.payload, c.run_id, c.rules_hash, c.metadata_revision,c.logical_id FROM documents d "
                "JOIN document_catalog c ON c.brand=d.brand AND c.doc_id=d.doc_id "
                "WHERE d.brand=? AND c.status='passed' AND c.lifecycle='active' AND EXISTS (SELECT 1 FROM document_versions av JOIN document_families af ON af.brand=av.brand AND af.logical_id=av.logical_id WHERE av.brand=c.brand AND av.doc_id=c.doc_id AND av.state='active' AND av.quality='passed' AND af.active_doc_id=av.doc_id AND af.blocked=0) ORDER BY d.doc_id",
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
            doc.doc_id: dict(
                source_run_id=row[1], rules_hash=row[2], metadata_revision=row[3], logical_id=row[4]
            )
            for doc, row in zip(
                [Document.model_validate_json(row[0]) for row in rows], rows, strict=True
            )
        }
        if original:
            chunks = [c for doc in docs for c in chunk_document(doc, original=True)]
        if mode != "bm25":
            chunks = identifier_filter(chunks, query)
        ranking = None
        config_info = {}
        if mode != "bm25" and chunks:
            embedder = embedder or LazyEmbedder()
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                active = {c.chunk_id for c in self._load_chunks(db, brand)}
                if any(c.chunk_id not in active for c in chunks):
                    raise ValueError("检索期间发布内容发生变化，请重试")
                vectors, report = cached_vectors(db, chunks, embedder)
            semantic = rank_semantic(chunks, vectors, query, embedder)
            ranking = fuse(lexical_ranking(chunks, query), semantic, mode=mode, pool=max(50, k))
            config_info = dict(
                embedding_config=report["config"], embedding_config_hash=report["config_hash"]
            )
        return [
            {
                **hit,
                **provenance[hit["doc_id"]],
                "metadata_match": matches[hit["doc_id"]],
                "retrieval_mode": mode,
                **config_info,
            }
            for hit in search_chunks(documents, chunks, query, k=k, ranking=ranking)
        ]

    def maintain_semantic_index(self, brand, *, mode="check", embedder=None, actor="", reason=""):
        from autorepair.semantic import LazyEmbedder, cached_vectors

        if mode != "check":
            self._operation(actor, reason, 0)
        embedder = embedder or LazyEmbedder()
        with self.connect() as db:
            db.execute("BEGIN" if mode == "check" else "BEGIN IMMEDIATE")
            chunks = self._load_chunks(db, brand)
            _, report = cached_vectors(db, chunks, embedder, mode=mode)
            active = {(c.doc_id, c.chunk_id) for c in chunks}
            stale_rows = [
                rowid
                for rowid, doc_id, chunk_id, config_hash in db.execute(
                    "SELECT rowid,doc_id,chunk_id,config_hash FROM semantic_vectors WHERE brand=?",
                    (brand,),
                )
                if config_hash != embedder.config.key or (doc_id, chunk_id) not in active
            ]
            stale = len(stale_rows)
            if mode != "check":
                db.executemany(
                    "DELETE FROM semantic_vectors WHERE rowid=?", [(r,) for r in stale_rows]
                )
                self._event(
                    db,
                    brand,
                    "",
                    "",
                    f"semantic-index-{mode}",
                    {},
                    {**report, "stale": stale},
                    actor,
                    reason,
                )
            return {
                **report,
                "brand": brand,
                "mode": mode,
                "stale": stale,
                "pruned": stale if mode != "check" else 0,
            }

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
            self._publish(db, [result], run_id, result.rules_hash, created, auto_activate=False)
            return {"run_id": run_id, "metadata_revision": revision, "status": result.status}

    @staticmethod
    def _write_source(db, document, source, *, explicit=False):
        if (
            not explicit
            and db.execute(
                "SELECT 1 FROM purged_sources WHERE brand=? AND doc_id=? AND source_hash=?",
                (document.brand, document.doc_id, document.source_hash),
            ).fetchone()
        ):
            return
        if explicit:
            db.execute(
                "DELETE FROM purged_sources WHERE brand=? AND doc_id=? AND source_hash=?",
                (document.brand, document.doc_id, document.source_hash),
            )
        if source.digest != document.source_hash:
            raise ValueError("原件哈希与文档来源不符")
        db.execute(
            "INSERT OR REPLACE INTO source_files VALUES (?, ?, ?, ?, ?, ?)",
            (
                document.brand,
                document.doc_id,
                document.source_hash,
                source.suffix,
                source.name,
                source.data,
            ),
        )

    def _evidence_document(self, db, brand, doc_id, run_id=None, expected_run_id=None):
        if run_id:
            result = next(
                (
                    r
                    for r in self._load_run(db, run_id)
                    if r.document.brand == brand and r.document.doc_id == doc_id
                ),
                None,
            )
            if result is None or result.status == "failed":
                raise ValueError("历史运行没有此品牌文档的可用证据")
            return result.document, dict(
                source_run_id=run_id,
                rules_hash=result.rules_hash,
                status=result.status,
                historical=True,
            )
        row = db.execute(
            "SELECT d.payload,c.run_id,c.rules_hash,c.metadata_revision,c.logical_id FROM documents d JOIN document_catalog c ON d.brand=c.brand AND d.doc_id=c.doc_id WHERE d.brand=? AND d.doc_id=? AND c.status='passed' AND c.lifecycle='active' AND EXISTS (SELECT 1 FROM document_versions av JOIN document_families af ON af.brand=av.brand AND af.logical_id=av.logical_id WHERE av.brand=c.brand AND av.doc_id=c.doc_id AND av.state='active' AND av.quality='passed' AND af.active_doc_id=av.doc_id AND af.blocked=0)",
            (brand, doc_id),
        ).fetchone()
        if row is None:
            raise ValueError("文档不在当前正式库中，请刷新检索结果或明确选择历史运行")
        if expected_run_id and expected_run_id != row[1]:
            raise ValueError("文档已更新，请重新检索后查看证据")
        return Document.model_validate_json(row[0]).model_copy(update={"logical_id": row[4]}), dict(
            source_run_id=row[1],
            rules_hash=row[2],
            metadata_revision=row[3],
            logical_id=row[4],
            status="passed",
            historical=False,
        )

    def evidence(
        self, brand, doc_id, chunk_id, *, run_id=None, expected_run_id=None, original=False
    ):
        from autorepair.evidence import build_evidence

        with self.connect() as db:
            db.execute("BEGIN")
            doc, provenance = self._evidence_document(db, brand, doc_id, run_id, expected_run_id)
            chunks = (
                chunk_document(doc, original=original)
                if run_id or original
                else self._load_chunks(db, brand, doc_id)
            )
            chunk = next((c for c in chunks if c.chunk_id == chunk_id), None)
            if chunk is None:
                raise ValueError("证据块不存在或已变化，请重新检索")
            bundle = build_evidence(doc, chunk)
            source = self._source_row(db, doc)
        return {**bundle, **provenance, "source": {k: v for k, v in source.items() if k != "data"}}

    @staticmethod
    def _source_row(db, doc):
        row = db.execute(
            "SELECT suffix,name,data FROM source_files WHERE brand=? AND doc_id=? AND source_hash=?",
            (doc.brand, doc.doc_id, doc.source_hash),
        ).fetchone()
        if row is None:
            if db.execute(
                "SELECT 1 FROM purged_sources WHERE brand=? AND doc_id=? AND source_hash=?",
                (doc.brand, doc.doc_id, doc.source_hash),
            ).fetchone():
                return dict(
                    status="purged", message="原件已按计划清理；可显式补挂同哈希原件", data=None
                )
            return dict(
                status="missing", message="未保留原件；可核对脱敏解析原文，原页无法预览", data=None
            )
        source = SourceFile(bytes(row[2]), row[0], row[1])
        if source.digest != doc.source_hash or source.suffix not in MIMES:
            return dict(status="invalid", message="原件校验失败，原页未显示", data=None)
        return dict(
            status="available",
            message="本地校验原件，未作脱敏",
            suffix=source.suffix,
            name=source.name,
            mime=MIMES[source.suffix],
            data=source.data,
        )

    def source_file(self, brand, doc_id, *, run_id=None, expected_run_id=None):
        with self.connect() as db:
            db.execute("BEGIN")
            doc, _ = self._evidence_document(db, brand, doc_id, run_id, expected_run_id)
            return self._source_row(db, doc)

    def attachment(self, brand, doc_id, src, *, run_id=None, expected_run_id=None):
        from autorepair.evidence import image_attachment

        with self.connect() as db:
            doc, _ = self._evidence_document(db, brand, doc_id, run_id, expected_run_id)
        if not any(b.image_src == src for b in doc.blocks):
            raise ValueError("该图片引用不属于当前文档")
        return image_attachment(doc, src)

    def attach_source(self, brand, doc_id, path, *, run_id=None):
        source = read_source(path)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            doc, _ = self._evidence_document(db, brand, doc_id, run_id)
            self._write_source(db, doc, source, explicit=True)
            v = self._version(db, brand, doc_id)
            self._event(
                db,
                brand,
                v[0],
                doc_id,
                "source-attach",
                {},
                dict(source_hash=source.digest),
                "local-source-attach",
                "显式补挂哈希一致原件",
            )
        return dict(source_hash=source.digest, source_name=source.name, status="available")

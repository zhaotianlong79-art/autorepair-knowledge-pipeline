"""Brand-scoped logical identities, immutable version bindings and publication transitions."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from autorepair.privacy import redact


def identity(value):
    value = value.strip()
    if not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise ValueError("逻辑文档身份须为 1–160 个可见字符")
    return value


class LifecycleMixin:
    def _init_lifecycle(self, db, *, sync=True):
        db.executescript("""
        CREATE TABLE IF NOT EXISTS document_families (
          brand TEXT, logical_id TEXT, active_doc_id TEXT, revision INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(brand,logical_id));
        CREATE TABLE IF NOT EXISTS document_versions (
          brand TEXT, doc_id TEXT, logical_id TEXT NOT NULL, source_hash TEXT NOT NULL,
          latest_run_id TEXT NOT NULL, quality TEXT NOT NULL, state TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(brand,doc_id));
        CREATE TABLE IF NOT EXISTS lifecycle_events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,brand TEXT NOT NULL,
          logical_id TEXT NOT NULL,doc_id TEXT NOT NULL,action TEXT NOT NULL,created_at TEXT NOT NULL,
          actor TEXT NOT NULL,reason TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS purged_sources (
          brand TEXT,doc_id TEXT,source_hash TEXT,created_at TEXT NOT NULL,
          PRIMARY KEY(brand,doc_id,source_hash));
        """)
        columns = {r[1] for r in db.execute("PRAGMA table_info(document_catalog)")}
        for name, default in [("logical_id", ""), ("lifecycle", "staged")]:
            if name not in columns:
                db.execute(
                    f"ALTER TABLE document_catalog ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                )
        for brand, doc_id, source_hash, run_id, status, created in db.execute(
            "SELECT brand,doc_id,source_hash,run_id,status,updated_at FROM document_catalog"
        ).fetchall():
            existing = db.execute(
                "SELECT logical_id,state FROM document_versions WHERE brand=? AND doc_id=?",
                (brand, doc_id),
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE document_catalog SET logical_id=?,lifecycle=? WHERE brand=? AND doc_id=?",
                    (existing[0], existing[1], brand, doc_id),
                )
                continue
            # Legacy docs are separate families; names and version labels never imply replacement.
            active = status == "passed" and bool(
                db.execute(
                    "SELECT 1 FROM documents WHERE brand=? AND doc_id=?", (brand, doc_id)
                ).fetchone()
            )
            db.execute(
                "INSERT OR IGNORE INTO document_families VALUES (?, ?, ?, 0, 0)",
                (brand, doc_id, doc_id if active else None),
            )
            db.execute(
                "INSERT INTO document_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brand,
                    doc_id,
                    doc_id,
                    source_hash,
                    run_id,
                    status,
                    "active" if active else "staged",
                    created,
                ),
            )
            db.execute(
                "UPDATE document_catalog SET logical_id=?,lifecycle=? WHERE brand=? AND doc_id=?",
                (doc_id, "active" if active else "staged", brand, doc_id),
            )
        if sync:
            self._sync_publication(db)

    @staticmethod
    def _event(db, brand, logical_id, doc_id, action, before, after, actor, reason):
        db.execute(
            "INSERT INTO lifecycle_events(event_id,brand,logical_id,doc_id,action,created_at,actor,reason,before_json,after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                brand,
                logical_id,
                doc_id,
                action,
                datetime.now(UTC).isoformat(),
                redact(actor.strip()),
                redact(reason.strip()),
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _sync_publication(db):
        for table in ["documents", "knowledge_chunks", "document_chunk_state"]:
            db.execute(
                f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM document_versions v JOIN document_families f ON f.brand=v.brand AND f.logical_id=v.logical_id WHERE v.brand={table}.brand AND v.doc_id={table}.doc_id AND v.state='active' AND v.quality='passed' AND f.active_doc_id=v.doc_id AND f.blocked=0)"
            )

    def _register_version(self, db, result, run_id, created, *, auto_activate=True):
        doc = result.document
        old = db.execute(
            "SELECT logical_id,source_hash,state,latest_run_id FROM document_versions WHERE brand=? AND doc_id=?",
            (doc.brand, doc.doc_id),
        ).fetchone()
        if not doc.doc_id.strip():
            raise ValueError("内容版本 ID 不能为空")
        logical = identity(doc.logical_id) if doc.logical_id else old[0] if old else doc.doc_id
        if old and (old[0] != logical or old[1] != doc.source_hash):
            raise ValueError(
                "版本 ID 已绑定其他身份或来源；请使用新版本 ID，替换已有资料请显式选择版本替换"
            )
        db.execute(
            "INSERT OR IGNORE INTO document_families VALUES (?, ?, NULL, 0, 0)",
            (doc.brand, logical),
        )
        active, revision, blocked = db.execute(
            "SELECT active_doc_id,revision,blocked FROM document_families WHERE brand=? AND logical_id=?",
            (doc.brand, logical),
        ).fetchone()
        previous_state = old[2] if old else "staged"
        publish = (
            result.status == "passed"
            and not blocked
            and (previous_state == "active" or (auto_activate and previous_state == "staged"))
        )
        state = (
            "active"
            if publish
            else previous_state
            if previous_state in {"withdrawn", "superseded"}
            else "staged"
        )
        before = dict(active_doc_id=active, state=old[2] if old else None, revision=revision)
        if publish and active and active != doc.doc_id:
            db.execute(
                "UPDATE document_versions SET state='superseded' WHERE brand=? AND doc_id=?",
                (doc.brand, active),
            )
            db.execute(
                "UPDATE document_catalog SET lifecycle='superseded' WHERE brand=? AND doc_id=?",
                (doc.brand, active),
            )
        if publish:
            active = doc.doc_id
        elif active == doc.doc_id:
            active = None
        db.execute(
            "INSERT INTO document_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(brand,doc_id) DO UPDATE SET latest_run_id=excluded.latest_run_id,quality=excluded.quality,state=excluded.state",
            (
                doc.brand,
                doc.doc_id,
                logical,
                doc.source_hash,
                run_id,
                result.status,
                state,
                created,
            ),
        )
        db.execute(
            "UPDATE document_families SET active_doc_id=?,revision=revision+1 WHERE brand=? AND logical_id=?",
            (active, doc.brand, logical),
        )
        db.execute(
            "UPDATE document_catalog SET logical_id=?,lifecycle=? WHERE brand=? AND doc_id=?",
            (logical, state, doc.brand, doc.doc_id),
        )
        self._event(
            db,
            doc.brand,
            logical,
            doc.doc_id,
            "ingest" if auto_activate else "metadata-update",
            before,
            dict(active_doc_id=active, state=state, revision=revision + 1, run_id=run_id),
            "local-ingest",
            "处理结果更新，保持人工撤下／已替换状态",
        )
        self._sync_publication(db)
        return publish

    def versions(self, brand, logical_id=None):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT v.*,f.active_doc_id,f.revision AS lifecycle_revision,f.blocked,c.source_name,c.document_version FROM document_versions v JOIN document_families f ON f.brand=v.brand AND f.logical_id=v.logical_id JOIN document_catalog c ON c.brand=v.brand AND c.doc_id=v.doc_id WHERE v.brand=?"
                + (" AND v.logical_id=?" if logical_id else "")
                + " ORDER BY v.logical_id,v.created_at,v.doc_id",
                (brand, logical_id) if logical_id else (brand,),
            ).fetchall()
        return [dict(r) for r in rows]

    def lifecycle_history(self, brand, logical_id=None):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM lifecycle_events WHERE brand=?"
                + (" AND logical_id=?" if logical_id else "")
                + " ORDER BY seq",
                (brand, logical_id) if logical_id else (brand,),
            ).fetchall()
        return [
            {
                **{k: v for k, v in dict(r).items() if k not in {"before_json", "after_json"}},
                "before": json.loads(r["before_json"]),
                "after": json.loads(r["after_json"]),
            }
            for r in rows
        ]

    @staticmethod
    def _operation(actor, reason, revision):
        if not actor.strip() or not reason.strip() or type(revision) is not int or revision < 0:
            raise ValueError("操作须填写署名、原因和当前生命周期修订号")

    def _version(self, db, brand, doc_id):
        row = db.execute(
            "SELECT v.logical_id,v.latest_run_id,v.quality,v.state,f.active_doc_id,f.revision,f.blocked FROM document_versions v JOIN document_families f ON f.brand=v.brand AND f.logical_id=v.logical_id WHERE v.brand=? AND v.doc_id=?",
            (brand, doc_id),
        ).fetchone()
        if not row:
            raise ValueError("当前品牌下不存在此版本")
        return row

    def withdraw(self, brand, doc_id, *, actor, reason, expected_revision):
        self._operation(actor, reason, expected_revision)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            v = self._version(db, brand, doc_id)
            if v[5] != expected_revision:
                raise ValueError("生命周期已更新，请刷新后重试")
            if v[3] == "withdrawn":
                return dict(changed=False, logical_id=v[0], lifecycle_revision=v[5])
            blocked = 1 if v[4] in {None, doc_id} else v[6]
            active = None if v[4] == doc_id else v[4]
            db.execute(
                "UPDATE document_versions SET state='withdrawn' WHERE brand=? AND doc_id=?",
                (brand, doc_id),
            )
            db.execute(
                "UPDATE document_catalog SET lifecycle='withdrawn' WHERE brand=? AND doc_id=?",
                (brand, doc_id),
            )
            db.execute(
                "UPDATE document_families SET active_doc_id=?,blocked=?,revision=revision+1 WHERE brand=? AND logical_id=?",
                (active, blocked, brand, v[0]),
            )
            self._sync_publication(db)
            self._event(
                db,
                brand,
                v[0],
                doc_id,
                "withdraw",
                dict(active_doc_id=v[4], state=v[3], revision=v[5]),
                dict(active_doc_id=active, state="withdrawn", blocked=blocked, revision=v[5] + 1),
                actor,
                reason,
            )
            return dict(changed=True, logical_id=v[0], lifecycle_revision=v[5] + 1)

    def _activate(self, db, brand, doc_id, actor, reason, action):
        v = self._version(db, brand, doc_id)
        result = next(
            (
                r
                for r in self._load_run(db, v[1])
                if r.document.brand == brand and r.document.doc_id == doc_id
            ),
            None,
        )
        if result is None or result.status != "passed" or result.issues or v[2] != "passed":
            raise ValueError("该版本最近处理结果未通过门禁，不能恢复或替换")
        if v[4] and v[4] != doc_id:
            db.execute(
                "UPDATE document_versions SET state='superseded' WHERE brand=? AND doc_id=?",
                (brand, v[4]),
            )
            db.execute(
                "UPDATE document_catalog SET lifecycle='superseded' WHERE brand=? AND doc_id=?",
                (brand, v[4]),
            )
        db.execute(
            "UPDATE document_versions SET state='active' WHERE brand=? AND doc_id=?",
            (brand, doc_id),
        )
        db.execute(
            "UPDATE document_catalog SET lifecycle='active' WHERE brand=? AND doc_id=?",
            (brand, doc_id),
        )
        db.execute(
            "UPDATE document_families SET active_doc_id=?,blocked=0,revision=revision+1 WHERE brand=? AND logical_id=?",
            (doc_id, brand, v[0]),
        )
        self._sync_publication(db)
        payload = result.document.model_dump_json()
        db.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?)", (brand, doc_id, payload))
        self._replace_chunks(db, result.document, payload)
        self._event(
            db,
            brand,
            v[0],
            doc_id,
            action,
            dict(active_doc_id=v[4], state=v[3], revision=v[5]),
            dict(active_doc_id=doc_id, state="active", revision=v[5] + 1),
            actor,
            reason,
        )
        return dict(
            changed=True, logical_id=v[0], active_doc_id=doc_id, lifecycle_revision=v[5] + 1
        )

    def restore(self, brand, doc_id, *, actor, reason, expected_revision):
        self._operation(actor, reason, expected_revision)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            v = self._version(db, brand, doc_id)
            if v[5] != expected_revision:
                raise ValueError("生命周期已更新，请刷新后重试")
            if v[3] == "active":
                return dict(
                    changed=False, logical_id=v[0], active_doc_id=doc_id, lifecycle_revision=v[5]
                )
            return self._activate(db, brand, doc_id, actor, reason, "restore")

    def replace_version(
        self,
        brand,
        current_doc_id,
        replacement_doc_id,
        *,
        actor,
        reason,
        expected_revision,
        expected_target_run_id,
    ):
        self._operation(actor, reason, expected_revision)
        if current_doc_id == replacement_doc_id:
            raise ValueError("替换目标须为不同的内容版本")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._version(db, brand, current_doc_id)
            target = self._version(db, brand, replacement_doc_id)
            if (
                old[4] != current_doc_id
                or old[5] != expected_revision
                or target[1] != expected_target_run_id
            ):
                raise ValueError("当前版本或替换目标已变化，请刷新后重试")
            if target[0] != old[0]:
                count = db.execute(
                    "SELECT count(*) FROM document_versions WHERE brand=? AND logical_id=?",
                    (brand, target[0]),
                ).fetchone()[0]
                if count != 1 or target[0] != replacement_doc_id:
                    raise ValueError("替换目标已绑定其他逻辑身份，不能隐式合并文档组")
                self._event(
                    db,
                    brand,
                    old[0],
                    replacement_doc_id,
                    "identity-link",
                    dict(logical_id=target[0], latest_run_id=target[1]),
                    dict(logical_id=old[0]),
                    actor,
                    reason,
                )
                db.execute(
                    "UPDATE document_versions SET logical_id=? WHERE brand=? AND doc_id=?",
                    (old[0], brand, replacement_doc_id),
                )
                db.execute(
                    "UPDATE document_catalog SET logical_id=? WHERE brand=? AND doc_id=?",
                    (old[0], brand, replacement_doc_id),
                )
                db.execute(
                    "DELETE FROM document_families WHERE brand=? AND logical_id=?",
                    (brand, target[0]),
                )
            return self._activate(db, brand, replacement_doc_id, actor, reason, "replace")

    def source_cleanup_plan(self, brand):
        with self.connect() as db:
            db.execute("BEGIN")
            return self._cleanup_plan(db, brand)

    @staticmethod
    def _cleanup_plan(db, brand):
        rows = db.execute(
            "SELECT s.doc_id,s.source_hash,length(s.data),v.logical_id,v.state FROM source_files s JOIN document_versions v ON v.brand=s.brand AND v.doc_id=s.doc_id AND v.source_hash=s.source_hash WHERE s.brand=? AND v.state IN ('withdrawn','superseded') ORDER BY s.doc_id,s.source_hash",
            (brand,),
        ).fetchall()
        items = [
            dict(doc_id=r[0], source_hash=r[1], bytes=r[2], logical_id=r[3], state=r[4])
            for r in rows
        ]
        seq = db.execute(
            "SELECT COALESCE(max(seq),0) FROM lifecycle_events WHERE brand=?", (brand,)
        ).fetchone()[0]
        token = hashlib.sha256(json.dumps([brand, seq, items], sort_keys=True).encode()).hexdigest()
        total = db.execute("SELECT count(*) FROM source_files WHERE brand=?", (brand,)).fetchone()[
            0
        ]
        return dict(
            protected_files=total - len(items),
            policy="explicit-inactive-only",
            brand=brand,
            items=items,
            files=len(items),
            bytes=sum(r[2] for r in rows),
            plan_token=token,
        )

    def cleanup_sources(self, brand, *, plan_token, actor, reason):
        self._operation(actor, reason, 0)
        if not plan_token:
            raise ValueError("请先预览清理范围并提供计划令牌")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._cleanup_plan(db, brand)
            if plan["plan_token"] != plan_token:
                raise ValueError("清理范围已变化，请重新预览清理计划")
            for item in plan["items"]:
                db.execute(
                    "DELETE FROM source_files WHERE brand=? AND doc_id=? AND source_hash=?",
                    (brand, item["doc_id"], item["source_hash"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO purged_sources VALUES (?, ?, ?, ?)",
                    (brand, item["doc_id"], item["source_hash"], datetime.now(UTC).isoformat()),
                )
                self._event(
                    db,
                    brand,
                    item["logical_id"],
                    item["doc_id"],
                    "source-cleanup",
                    item,
                    dict(source_hash=item["source_hash"], source_status="purged"),
                    actor,
                    reason,
                )
            return {**plan, "applied": True}

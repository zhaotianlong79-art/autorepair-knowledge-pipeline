"""Reproducible lifecycle workflow against self-authored sources in a temporary database."""

import json
import tempfile
from pathlib import Path

from autorepair.config import load_config
from autorepair.pipeline import export, run
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]


def main():
    cfg = load_config(ROOT / "configs", "aurora")
    checks = []

    def record(name, passed):
        checks.append(dict(check=name, passed=bool(passed)))

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "artifacts"
        store = Store(out / "knowledge.sqlite")

        def ingest(name):
            source = ROOT / "examples/lifecycle" / (name + ".html")
            result = run([source], cfg, logical_id="demo-manual")
            run_id = export(result, cfg, out)
            return result[0].document.doc_id, run_id

        def version(id):
            return next(v for v in store.versions("aurora") if v["doc_id"] == id)

        first, first_run = ingest("first")
        second, second_run = ingest("second")
        candidate, _ = ingest("candidate")
        record(
            "approved_new_version_replaces_old",
            not store.search("first-version", "aurora")
            and bool(store.search("second-version", "aurora")),
        )
        record(
            "candidate_never_displaces_active",
            version(candidate)["quality"] == "review" and version(second)["state"] == "active",
        )
        record(
            "raw_inactive_only_preview",
            store.source_cleanup_plan("aurora")["files"] == 1
            and store.source_cleanup_plan("aurora")["protected_files"] == 2,
        )
        v = version(second)
        store.withdraw(
            "aurora",
            second,
            actor="self-authored-demo",
            reason="test withdrawal",
            expected_revision=v["lifecycle_revision"],
        )
        ingest("second")
        record(
            "withdrawal_survives_repeat_ingest",
            not Store(store.path).current("aurora") and version(second)["state"] == "withdrawn",
        )
        v = version(first)
        store.restore(
            "aurora",
            first,
            actor="self-authored-demo",
            reason="test rollback",
            expected_revision=v["lifecycle_revision"],
        )
        record(
            "rollback_returns_only_selected_approved_version",
            bool(store.search("first-version", "aurora"))
            and not store.search("second-version", "aurora"),
        )
        before = store.load_run(first_run)
        store.maintain_index(
            "aurora", mode="rebuild", actor="self-authored-demo", reason="test index rebuild"
        )
        with store.connect() as db:
            db.execute("UPDATE knowledge_chunks SET payload='corrupt' WHERE brand='aurora'")
        record(
            "same_count_payload_corruption_detected", bool(store.maintain_index("aurora")["dirty"])
        )
        store.maintain_index(
            "aurora",
            mode="incremental",
            actor="self-authored-demo",
            reason="test incremental repair",
        )
        record(
            "repair_and_history_preserved",
            bool(store.search("first-version", "aurora")) and store.load_run(first_run) == before,
        )
        plan = store.source_cleanup_plan("aurora")
        store.cleanup_sources(
            "aurora",
            plan_token=plan["plan_token"],
            actor="self-authored-demo",
            reason="test inactive raw cleanup",
        )
        record(
            "inactive_raw_copy_removed",
            store.source_file("aurora", second, run_id=second_run)["status"] == "purged",
        )
        record(
            "active_raw_copy_protected", store.source_file("aurora", first)["status"] == "available"
        )
        ingest("second")
        record(
            "raw_tombstone_prevents_automatic_refill",
            store.source_file("aurora", second, run_id=second_run)["status"] == "purged",
        )
        v = version(second)
        store.restore(
            "aurora",
            second,
            actor="self-authored-demo",
            reason="test restore without raw",
            expected_revision=v["lifecycle_revision"],
        )
        record(
            "restore_search_without_claiming_raw_availability",
            bool(store.search("second-version", "aurora"))
            and store.source_file("aurora", second)["status"] == "purged",
        )
        store.attach_source("aurora", second, ROOT / "examples/lifecycle/second.html")
        record(
            "explicit_verified_reattach",
            store.source_file("aurora", second)["status"] == "available",
        )
        record("no_duplicate_versions", len(store.versions("aurora")) == 3)
        record(
            "lifecycle_audit_retained",
            {
                "ingest",
                "withdraw",
                "restore",
                "source-cleanup",
                "source-attach",
                "index-rebuild",
                "index-incremental",
            }
            <= {e["action"] for e in store.lifecycle_history("aurora")},
        )
        report = dict(
            scope="Self-authored isolated temporary workflow only; no user originals are cleaned. Cleanup deletes database source copies, not audit payloads, extracted images, source filesystem files, exports or backups.",
            passed=all(c["passed"] for c in checks),
            checks=checks,
            versions=[
                {k: v[k] for k in ["logical_id", "doc_id", "quality", "state"]}
                for v in store.versions("aurora")
            ],
        )
    (ROOT / "docs/lifecycle-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(dict(passed=report["passed"], checks=len(checks))))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()

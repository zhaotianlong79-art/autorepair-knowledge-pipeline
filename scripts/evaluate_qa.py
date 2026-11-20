"""End-to-end self-authored citation QA checks, with an explicitly simulated LLM.

The local HTTP fixture tests the Ollama protocol, not inference/model quality.
No user corpus is mutated and no external endpoint is called.
"""

import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autorepair.config import load_config
from autorepair.metadata import MetadataFilter, MetadataPatch
from autorepair.pipeline import run
from autorepair.qa import Selection, answer
from autorepair.qa_provider import OllamaConfig, OllamaSelector
from autorepair.storage import Store

ROOT = Path(__file__).resolve().parents[1]
SCOPE = MetadataFilter(model="sedan", year_start=2024, year_end=2024)
QUESTION = "火花塞安装扭矩是多少"


def evaluate(output):
    rows = []

    def record(name, passed, **details):
        rows.append(dict(name=name, passed=bool(passed), **details))
        if not passed:
            raise AssertionError(name)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "knowledge.sqlite")
        for brand in ["aurora", "atlas"]:
            cfg = load_config(ROOT / "configs", brand)
            results = run(
                [ROOT / "examples/qa/manual.html", ROOT / "examples/qa/review.html"],
                cfg,
                metadata=MetadataPatch(
                    model="sedan",
                    year_range="2024",
                    document_type="manual",
                    document_version="demo-1",
                ),
            )
            assert [r.status for r in results] == ["passed", "review"]
            store.save(results, cfg)
            record(
                brand + "-pending-image-not-published",
                results[1].status == "review" and bool(results[1].document.assets),
            )
            response = answer(store, QUESTION, brand, filters=SCOPE)
            record(
                brand + "-numeric-source-quote",
                response["status"] == "answered"
                and "42 Nm" in response["claims"][0]["text"]
                and "99 Nm" not in json.dumps(response, ensure_ascii=False),
                status=response["status"],
            )
            record(
                brand + "-scope-and-provenance",
                all(
                    e["brand"] == brand
                    and e["model"] == "sedan"
                    and e["source_run_id"]
                    and e["rules_hash"]
                    for e in response["citations"]
                ),
            )
            record(
                brand + "-warning-table-steps",
                response["warnings"]
                and all(
                    t in "\n".join(e["text"] for e in response["citations"])
                    for t in ["先用手旋入火花塞", "0.8 mm", "禁止强行拧入"]
                ),
            )
            record(
                brand + "-html-no-invented-page", all(not e["pages"] for e in response["citations"])
            )
            record(
                brand + "-no-answer",
                answer(store, "电饭锅煮饭夹生怎么修", brand, filters=SCOPE)["status"]
                == "insufficient_evidence",
            )
        record(
            "require-vehicle-scope", answer(store, QUESTION, "aurora")["status"] == "needs_scope"
        )
        record(
            "wrong-model-excluded",
            answer(
                store,
                QUESTION,
                "aurora",
                filters=MetadataFilter(model="truck", year_start=2024, year_end=2024),
            )["status"]
            == "insufficient_evidence",
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert (
                    self.path == "/api/chat"
                    and body["stream"] is False
                    and body["format"]["additionalProperties"] is False
                )
                content = json.dumps({"decision": "answer", "evidence_ids": ["E1"]})
                raw = json.dumps(
                    {"done": True, "done_reason": "stop", "message": {"content": content}}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            selector = OllamaSelector(
                OllamaConfig(
                    endpoint=f"http://127.0.0.1:{server.server_port}/api/chat",
                    model="explicitly-simulated-selection",
                )
            )
            response = answer(store, QUESTION, "aurora", filters=SCOPE, selector=selector)
            record(
                "ollama-http-protocol-simulation",
                response["status"] == "answered" and response["engine"] == "ollama-selection",
                real_model=False,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        class Invalid:
            name = "invalid-reference-fixture"

            def select(self, *args):
                return Selection(decision="answer", evidence_ids=["E999"])

        record(
            "invalid-reference-blocked",
            answer(store, QUESTION, "aurora", filters=SCOPE, selector=Invalid())["status"]
            == "invalid_model_output",
        )
        cfg = load_config(ROOT / "configs", "aurora")
        conflict = run(
            [ROOT / "examples/qa/conflict.html"],
            cfg,
            metadata=MetadataPatch(
                model="sedan",
                year_range="2024",
                document_type="manual",
                document_version="conflict-demo",
            ),
        )
        assert conflict[0].status == "passed"
        store.save(conflict, cfg)
        record(
            "conflicting-parameters-refused",
            answer(store, QUESTION, "aurora", filters=SCOPE)["status"] == "conflicting_evidence",
        )
        for version in store.versions("aurora"):
            if version["quality"] == "passed":
                store.withdraw(
                    "aurora",
                    version["doc_id"],
                    expected_revision=version["lifecycle_revision"],
                    actor="self-authored-evaluation",
                    reason="隔离库撤下验收",
                )
        record(
            "withdrawn-no-answer",
            answer(store, QUESTION, "aurora", filters=SCOPE)["status"] == "insufficient_evidence",
        )
    report = dict(
        scope="Self-authored pipeline and protocol acceptance; no real LLM quality claim, no user data cleanup.",
        passed=all(r["passed"] for r in rows),
        checks=len(rows),
        results=rows,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": len(rows), "real_llm_tested": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs/qa-results.json")
    evaluate(parser.parse_args().output)

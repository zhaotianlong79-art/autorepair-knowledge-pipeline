"""CPU Docling smoke evaluation. Models must already be downloaded for offline use."""

import json
import platform
import threading
import time
from pathlib import Path

import psutil

from autorepair.config import load_config
from autorepair.engine import process
from autorepair.parsers import parse

ROOT = Path(__file__).resolve().parents[1]
report = {"platform": platform.platform(), "cpu_only": True, "cases": [], "peak_rss_mib": 0}
stop = threading.Event()


def sample():
    proc = psutil.Process()
    while not stop.wait(0.1):
        report["peak_rss_mib"] = max(
            report["peak_rss_mib"], round(proc.memory_info().rss / 1024**2, 1)
        )


thread = threading.Thread(target=sample, daemon=True)
thread.start()
try:
    for split in ["dev", "holdout"]:
        for brand in ["aurora", "atlas"]:
            cfg = load_config(ROOT / "configs", brand)
            for name in ["05-service.docx", "06-service.pdf"]:
                path = ROOT / "examples" / split / brand / name
                start = time.perf_counter()
                doc = parse(path, brand, ocr=(split == "holdout" and path.suffix == ".pdf"))
                result = process(doc, cfg)
                text = "\n".join(b.text for b in doc.blocks)
                anchors = [
                    "WARNING",
                    "oxygen sensor",
                    "35" if split == "dev" else "42",
                    "PN-A100" if brand == "aurora" else "PN-B200",
                ]
                row = {
                    "file": str(path.relative_to(ROOT)),
                    "parser": doc.parser,
                    "seconds": round(time.perf_counter() - start, 3),
                    "blocks": len(doc.blocks),
                    "tables": sum(b.kind == "table" for b in doc.blocks),
                    "anchor_checks": {a: a in text for a in anchors},
                    "status": result.status,
                    "issues": [i.code for i in result.issues],
                    "text": text,
                }
                report["cases"].append(row)
                print(
                    json.dumps({k: v for k, v in row.items() if k != "text"}, ensure_ascii=False),
                    flush=True,
                )
finally:
    stop.set()
    thread.join()
(ROOT / "docs/converter-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
assert all(all(c["anchor_checks"].values()) for c in report["cases"]), (
    "Some source anchors were not recovered; see converter-results.json"
)
assert all(
    c["tables"] >= 1 for c in report["cases"] if c["file"].endswith(".docx") or "/dev/" in c["file"]
), "Expected structured table missing"
print("PEAK_RSS_MIB", report["peak_rss_mib"])

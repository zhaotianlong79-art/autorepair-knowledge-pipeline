"""Create an isolated, reproducible corpus for the portfolio workbench."""

import argparse
import json
from pathlib import Path

from autorepair.evaluation import ROOT, build_corpus


def create_demo(folder):
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("演示目录非空；请选择新的目录，不覆盖已有文件")
    folder.mkdir(parents=True, exist_ok=True)
    store, keys = build_corpus(folder / "knowledge.sqlite")
    summary = dict(
        dataset="repair-v1",
        documents=len(keys),
        brands={
            brand: dict(versions=len(store.versions(brand)), active=len(store.current(brand)))
            for brand in ["aurora", "atlas"]
        },
        scope="Self-authored demonstration only; external human review pending.",
    )
    (folder / "demo-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "work/portfolio-demo")
    create_demo(parser.parse_args().output)

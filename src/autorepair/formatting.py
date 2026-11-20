"""Markdown-safe table and heading serialization shared by all adapters."""

import re


def table_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(map(len, rows))

    def cell(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("|", r"\|")
            .replace("\r", "")
            .replace("\n", "<br>")
        )

    normalized = [[cell(c) for c in row] + [""] * (width - len(row)) for row in rows]
    return "\n".join(
        ["| " + " | ".join(normalized[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        + ["| " + " | ".join(row) + " |" for row in normalized[1:]]
    )


def numbered_level(text: str) -> int | None:
    match = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.、]?\s+|(?=[\u4e00-\u9fff]))", text)
    return min(5, match[1].count(".") + 1) if match else None

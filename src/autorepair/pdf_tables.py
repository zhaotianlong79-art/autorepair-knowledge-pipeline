"""Recover ruled PDF table spans from actual vector cell geometry, never forward-fill blanks."""

from functools import lru_cache
from pathlib import Path

from autorepair.formatting import table_markdown


@lru_cache(maxsize=32)
def ruled_tables(path: str, source_hash: str, page_number: int):
    import pdfplumber

    with pdfplumber.open(Path(path)) as pdf:
        page = pdf.pages[page_number - 1]
        tables = []
        for table in page.find_tables():
            xs = sorted(
                {round(c[0], 2) for c in table.cells} | {round(c[2], 2) for c in table.cells}
            )
            ys = sorted(
                {round(c[1], 2) for c in table.cells} | {round(c[3], 2) for c in table.cells}
            )
            if len(xs) < 2 or len(ys) < 2:
                continue
            grid = [[""] * (len(xs) - 1) for _ in range(len(ys) - 1)]
            for cell in table.cells:
                x0, y0, x1, y1 = cell
                text = page.crop(cell).extract_text(x_tolerance=2, y_tolerance=2) or ""
                text = " ".join(text.splitlines())
                for ri in range(len(ys) - 1):
                    cy = (ys[ri] + ys[ri + 1]) / 2
                    for ci in range(len(xs) - 1):
                        cx = (xs[ci] + xs[ci + 1]) / 2
                        if x0 <= cx <= x1 and y0 <= cy <= y1:
                            grid[ri][ci] = text
            if any(c.strip() for row in grid for c in row):
                tables.append((table.bbox, grid))
        return tables


def recover_table(item, document, path: Path, digest: str):
    if not item.prov:
        return None
    prov = item.prov[0]
    height = document.pages[prov.page_no].size.height
    bbox = prov.bbox.to_top_left_origin(page_height=height)
    for (x0, y0, x1, y1), grid in ruled_tables(str(path.resolve()), digest, prov.page_no):
        intersection = max(0, min(x1, bbox.r) - max(x0, bbox.l)) * max(
            0, min(y1, bbox.b) - max(y0, bbox.t)
        )
        area = (x1 - x0) * (y1 - y0)
        other_area = max(1, (bbox.r - bbox.l) * (bbox.b - bbox.t))
        if area and intersection / max(area, other_area) > 0.75:
            # A sparse outer box cannot override a richer layout-model grid.
            if len(grid[0]) < item.data.num_cols:
                continue
            return header_markdown(grid)
    return None


def header_markdown(grid):
    """Flatten observed multirow headers without discarding group captions."""
    rows = [list(row) for row in grid]
    caption = ""
    if len(rows) > 1 and len(set(rows[0])) == 1 and len(set(rows[1])) > 1:
        caption = rows.pop(0)[0] + "\n\n"
    elif len(rows) > 1:
        # Only collapse when each second-row cell is either an unchanged label
        # or a child of a repeated merged parent in the first row.
        first, second = rows[:2]
        repeated = {x for x in first if first.count(x) > 1 and x}
        if repeated and all(a == b or a in repeated for a, b in zip(first, second, strict=True)):
            rows[:2] = [
                [b if a == b else b + " / " + a for a, b in zip(first, second, strict=True)]
            ]
    return caption + table_markdown(rows)

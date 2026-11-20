"""Build a fixed conversion-fidelity corpus with independent expected content/structure.

These are test artifacts, never repair instructions. Run with the bundled document runtime.
"""

import argparse
import json
import subprocess
from pathlib import Path

from docx import Document
from docx.shared import Inches
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as PDFImage,
)
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "examples/fidelity"
BASE.mkdir(parents=True, exist_ok=True)
MANIFEST = []
HEADINGS = [(1, "Chapter 1 Service"), (2, "1.1 Inspection"), (3, "1.1.1 Torque")]
ROWS = [
    ["Component", "Part number", "Torque"],
    ["Sensor Alpha", "PN-F101", "35 Nm"],
    ["Valve Beta", "PN-F202", "12.5 Nm"],
]
TOKENS = ["INTRO_A101", "BODY_B202", "TAIL_Z909", "PN-F101", "PN-F202", "35 Nm", "12.5 Nm"]


def add(name, group, tokens=None, headings=None, tables=None, **extra):
    MANIFEST.append(
        dict(
            file=name,
            group=group,
            tokens=tokens or [],
            headings=headings or [],
            tables=tables or [],
            **extra,
        )
    )


def html_table(rows):
    return (
        "<table>"
        + "".join(
            "<tr>"
            + "".join(f"<{'th' if n == 0 else 'td'}>{x}</{'th' if n == 0 else 'td'}>" for x in row)
            + "</tr>"
            for n, row in enumerate(rows)
        )
        + "</table>"
    )


basic_html = (
    '<!doctype html><html lang="en"><body><h1>Chapter 1 Service</h1><p>INTRO_A101</p><h2>1.1 Inspection</h2><p>BODY_B202</p><h3>1.1.1 Torque</h3>'
    + html_table(ROWS)
    + "<p>TAIL_Z909</p></body></html>"
)
for name in ["html_basic.html", "htm_basic.htm"]:
    (BASE / name).write_text(basic_html)
    add(name, "HTML" if name.endswith(".html") else "HTM", TOKENS, HEADINGS, [ROWS])
complex_rows = [
    ["System", "Component", "Value"],
    ["Brake", "Unit A", "A|B"],
    ["Brake", "Unit B", "LINE_C303 LINE_D404"],
]
complex_html = '<h1>Complex table</h1><table><tr><th>System</th><th>Component</th><th>Value</th></tr><tr><td rowspan="2">Brake</td><td>Unit A</td><td>A|B</td></tr><tr><td>Unit B</td><td>LINE_C303<br>LINE_D404</td></tr></table><p>COMPLEX_END</p>'
(BASE / "html_complex.html").write_text(complex_html)
add(
    "html_complex.html",
    "HTML",
    ["Brake", "Unit A", "Unit B", "A|B", "LINE_C303", "LINE_D404", "COMPLEX_END"],
    [(1, "Complex table")],
    [complex_rows],
)
nested_html = '<h1 id="top">Nested document</h1><div>LOOSE_FRONT<p>PARA_MIDDLE</p>LOOSE_TAIL</div><h2 id="sub">Nested list</h2><ol><li>STEP_PARENT<ul><li>STEP_CHILD</li></ul></li><li>STEP_SECOND</li></ol><p><a href="#top">LINK_BACK</a> and <a href="https://example.com/manual">LINK_EXTERNAL</a></p><p hidden>HIDDEN_EXCLUDED</p><img src="figure.png" alt="FIGURE_ALT_707">'
(BASE / "html_nested.html").write_text(nested_html)
add(
    "html_nested.html",
    "HTML",
    [
        "LOOSE_FRONT",
        "PARA_MIDDLE",
        "LOOSE_TAIL",
        "STEP_PARENT",
        "STEP_CHILD",
        "STEP_SECOND",
        "LINK_BACK",
        "LINK_EXTERNAL",
        "FIGURE_ALT_707",
    ],
    [(1, "Nested document"), (2, "Nested list")],
    [],
    links=["#top", "https://example.com/manual"],
    list_depth=2,
    forbidden=["HIDDEN_EXCLUDED"],
)
for field in ["content_html", "html", "body", "content", "正文"]:
    name = "json_" + ("zh" if field == "正文" else field) + ".json"
    (BASE / name).write_text(json.dumps({field: basic_html}, ensure_ascii=False))
    add(name, "JSON", TOKENS, HEADINGS, [ROWS], json_field=field)
for name, data in [
    (
        "json_array.json",
        [{"html": basic_html}, {"body": "<h1>Second record</h1><p>RECORD_TWO_END</p>"}],
    ),
    (
        "json_records.json",
        {
            "records": [
                {"content": basic_html},
                {"正文": "<h1>Second record</h1><p>RECORD_TWO_END</p>"},
            ]
        },
    ),
]:
    (BASE / name).write_text(json.dumps(data, ensure_ascii=False))
    add(name, "JSON", TOKENS + ["RECORD_TWO_END"], HEADINGS + [(1, "Second record")], [ROWS])
# Standard normalized JSON round trip tests declared input structure independently of HTML.
blocks = []
for i, (level, title) in enumerate(HEADINGS):
    blocks.append(
        dict(
            block_id=f"b{i}",
            kind="heading",
            original=title,
            text=title,
            location={"source_level": level},
            section=title,
            links=[],
        )
    )
blocks.append(
    dict(
        block_id="bt",
        kind="table",
        original="\n".join(
            ["| " + " | ".join(ROWS[0]) + " |", "| --- | --- | --- |"]
            + ["| " + " | ".join(r) + " |" for r in ROWS[1:]]
        ),
        text="\n".join(
            ["| " + " | ".join(ROWS[0]) + " |", "| --- | --- | --- |"]
            + ["| " + " | ".join(r) + " |" for r in ROWS[1:]]
        ),
        location={"item": "table1"},
        section=HEADINGS[-1][1],
        links=[],
    )
)
(BASE / "json_normalized.json").write_text(
    json.dumps(
        dict(
            schema_version="1.0",
            doc_id="normalized-fidelity",
            brand="aurora",
            source_name="normalized-fidelity",
            source_hash="fixture",
            parser="fixture",
            blocks=blocks,
        ),
        ensure_ascii=False,
    )
)
add("json_normalized.json", "JSON", ["PN-F101", "PN-F202", "35 Nm", "12.5 Nm"], HEADINGS, [ROWS])

for name, merged, picture in [
    ("docx_basic.docx", False, False),
    ("docx_merged.docx", True, False),
    ("docx_picture.docx", False, True),
]:
    doc = Document()
    for style in ["Normal", "Heading 1", "Heading 2", "Heading 3"]:
        doc.styles[style].font.name = "Arial"
    doc.add_heading(HEADINGS[0][1], level=1)
    doc.add_paragraph("INTRO_A101")
    doc.add_heading(HEADINGS[1][1], level=2)
    doc.add_paragraph("BODY_B202")
    doc.add_heading(HEADINGS[2][1], level=3)
    rows = complex_rows if merged else ROWS
    table = doc.add_table(rows=len(rows), cols=3)
    table.style = "Table Grid"
    for n, row in enumerate(rows):
        for j, value in enumerate(row):
            table.cell(n, j).text = value
    if merged:
        table.cell(1, 0).merge(table.cell(2, 0))
        table.cell(1, 0).text = "Brake"
    if picture:
        image = Image.new("RGB", (900, 180), "white")
        draw = ImageDraw.Draw(image)
        fontpath = "/System/Library/Fonts/Supplemental/Arial.ttf"
        font = (
            ImageFont.truetype(fontpath, 40)
            if Path(fontpath).exists()
            else ImageFont.load_default(size=40)
        )
        draw.text((20, 55), "FIGURE_IMAGE_808", font=font, fill="black")
        image.save(BASE / "figure.png")
        doc.add_picture(str(BASE / "figure.png"), width=Inches(5.4))
    doc.add_paragraph("TAIL_Z909")
    doc.save(BASE / name)
    add(
        name,
        "DOCX",
        (
            ["INTRO_A101", "BODY_B202", "TAIL_Z909"]
            + (
                ["Brake", "Unit A", "Unit B", "A|B", "LINE_C303", "LINE_D404"]
                if merged
                else TOKENS[3:]
            )
        )
        + (["FIGURE_IMAGE_808"] if picture else []),
        HEADINGS,
        [rows],
    )

parser = argparse.ArgumentParser()
parser.add_argument("--pdftoppm", required=True)
args = parser.parse_args()
fontpath = "/Library/Fonts/Arial Unicode.ttf"
pdfmetrics.registerFont(TTFont("FidelityCJK", fontpath))
normal = ParagraphStyle("Body", fontName="FidelityCJK", fontSize=11, leading=16, spaceAfter=8)
styles = {
    level: ParagraphStyle(
        f"H{level}",
        parent=normal,
        fontSize=20 - level * 2,
        leading=25 - level * 2,
        spaceBefore=12,
        spaceAfter=8,
    )
    for level in [1, 2, 3]
}


def make_table(rows):
    t = Table(rows, colWidths=[160, 160, 160])
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "FidelityCJK"),
                ("FONTSIZE", (0, 0), (-1, -1), 11),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]
        )
    )
    return t


def pdf(name, cjk=False, multi=False):
    headings = (
        [(1, "第一章 维修准备"), (2, "1.1 检查步骤"), (3, "1.1.1 扭矩参数")] if cjk else HEADINGS
    )
    rows = (
        [
            ["部件", "部件号", "扭矩"],
            ["前轮传感器", "PN-F101", "35 Nm"],
            ["后轮阀门", "PN-F202", "12.5 Nm"],
        ]
        if cjk
        else ROWS
    )
    story = []
    for i, (level, title) in enumerate(headings):
        story.append(Paragraph(title, styles[level]))
        if i < 2:
            story.append(Paragraph(["INTRO_A101", "BODY_B202"][i], normal))
    story.append(make_table(rows))
    story.append(Spacer(1, 15))
    story.append(Paragraph("TAIL_Z909", normal))
    tables = [rows]
    tokens = list(TOKENS)
    if cjk:
        tokens += ["前轮传感器", "后轮阀门"]
    if multi:
        story += [
            PageBreak(),
            Paragraph("Chapter 2 Continuation", styles[1]),
            Paragraph("PAGE_TWO_222", normal),
        ]
        rows2 = [ROWS[0], ["Pump Gamma", "PN-F303", "88 Nm"], ["Relay Delta", "PN-F404", "5 Nm"]]
        story += [make_table(rows2), Paragraph("FINAL_PAGE_END_999", normal)]
        tokens += ["PAGE_TWO_222", "PN-F303", "88 Nm", "PN-F404", "5 Nm", "FINAL_PAGE_END_999"]
        tables.append(rows2)
        headings = headings + [(1, "Chapter 2 Continuation")]
    SimpleDocTemplate(
        str(BASE / name),
        pagesize=(595, 842),
        leftMargin=48,
        rightMargin=48,
        topMargin=40,
        bottomMargin=40,
    ).build(story)
    add(name, "数字PDF", tokens, headings, tables)


pdf("pdf_digital.pdf")
pdf("pdf_multipage.pdf", multi=True)
pdf("pdf_cjk.pdf", cjk=True)
for source, name in [("pdf_digital.pdf", "pdf_scan_en.pdf"), ("pdf_cjk.pdf", "pdf_scan_zh.pdf")]:
    imgprefix = BASE / (name.replace(".pdf", "_raster"))
    subprocess.run(
        [
            args.pdftoppm,
            "-f",
            "1",
            "-singlefile",
            "-r",
            "180",
            "-png",
            str(BASE / source),
            str(imgprefix),
        ],
        check=True,
        capture_output=True,
    )
    image = Image.open(str(imgprefix) + ".png")
    width, height = image.size
    SimpleDocTemplate(
        str(BASE / name),
        pagesize=(595, 842),
        leftMargin=0,
        rightMargin=0,
        topMargin=0,
        bottomMargin=0,
    ).build([PDFImage(str(imgprefix) + ".png", width=585, height=585 * height / width)])
    expected = next(c for c in MANIFEST if c["file"] == source)
    add(name, "扫描PDF", expected["tokens"], expected["headings"], expected["tables"], ocr=True)
# One logical table split by the PDF layout engine, with a repeated column header.
continued_rows = [ROWS[0]] + [
    [f"Component {n:02}", f"PN-X{n:03}", f"{n}.5 Nm"] for n in range(1, 37)
]
continued = make_table(continued_rows)
continued.repeatRows = 1
SimpleDocTemplate(
    str(BASE / "pdf_crosspage.pdf"),
    pagesize=(595, 842),
    leftMargin=48,
    rightMargin=48,
    topMargin=40,
    bottomMargin=40,
).build(
    [
        Paragraph("Cross-page table", styles[1]),
        Paragraph("TABLE_START_001", normal),
        continued,
        Paragraph("TABLE_END_036", normal),
    ]
)
add(
    "pdf_crosspage.pdf",
    "数字PDF",
    ["TABLE_START_001", "TABLE_END_036"] + [cell for row in continued_rows[1:] for cell in row],
    [(1, "Cross-page table")],
    [continued_rows],
    table_mode="continuation",
)
merged_table = make_table(complex_rows)
merged_table.setStyle(TableStyle([("SPAN", (0, 1), (0, 2))]))
SimpleDocTemplate(
    str(BASE / "pdf_merged.pdf"),
    pagesize=(595, 842),
    leftMargin=48,
    rightMargin=48,
    topMargin=40,
    bottomMargin=40,
).build(
    [Paragraph("Merged PDF table", styles[1]), merged_table, Paragraph("MERGED_END_555", normal)]
)
add(
    "pdf_merged.pdf",
    "数字PDF",
    ["Brake", "Unit A", "Unit B", "A|B", "LINE_C303", "LINE_D404", "MERGED_END_555"],
    [(1, "Merged PDF table")],
    [complex_rows],
)
(BASE / "manifest.json").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2))
print("Fixture cases:", len(MANIFEST))

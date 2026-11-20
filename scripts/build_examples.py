"""Generate original demonstration documents. Every repair value is fictional."""

import io
import json
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parents[1]
for split in ["dev", "holdout"]:
    for brand in ["aurora", "atlas"]:
        folder = ROOT / "examples" / split / brand
        folder.mkdir(parents=True, exist_ok=True)
        value = "35" if split == "dev" else "42"
        part = "PN-A100" if brand == "aurora" else "PN-B200"
        name = "制动控制模块" if brand == "aurora" else "车身控制模块"
        clean = f"""<!doctype html><html lang="zh"><body><h1 id="s1">演示维修手册</h1>
        <p>警告：不得带电拆卸 BCM。使用 torque wrench。</p>
        <table><tr><th>部件</th><th>扭矩</th><th>部件号</th></tr><tr><td>oxygen sensor</td><td>{value} Nm</td><td>{part}</td></tr></table>
        <p hidden>隐藏内容 不应进入正文</p><p style="display:none">营销内容</p>
        <p>检查 diagnostic trouble code P0300。</p><p>检测 intermittent poor contact。</p>
        <p><a href="#s1">返回演示维修手册</a></p></body></html>"""
        (folder / "01-manual.html").write_text(clean)
        (folder / "02-review.html").write_text(
            '<h1 id="inspect">连接检查</h1><p>XCU 需要人工确认。</p><p><a href="doc:missing#s9">查看缺失章节</a></p><p>重复说明</p><p>重复说明</p>'
        )
        (folder / "03-procedure.json").write_text(
            json.dumps(
                {
                    "标题": "传感器更换流程",
                    "正文": f"<p>O₂ sensor 应使用 torque wrench。</p><p>部件号 {part}。检测电压 12 V。</p><p>电话：13800138000 车牌：粤A12345</p>",
                },
                ensure_ascii=False,
            )
        )
        (folder / "04-extra.json").write_text(
            json.dumps(
                {
                    "title": "扩展维修记录",
                    "content_html": f"<p>BCM 对应 {name}，警告不得忽略。</p>",
                    "legacy_id": "demo-001",
                },
                ensure_ascii=False,
            )
        )
        doc = Document()
        doc.add_heading("Demonstration repair data", 0)
        doc.add_paragraph(f"WARNING: Never remove BCM while powered. Part {part}.")
        table = doc.add_table(rows=1, cols=3)
        for c, t in zip(table.rows[0].cells, ["Component", "Torque", "Tool"], strict=True):
            c.text = t
        for c, t in zip(
            table.add_row().cells, ["oxygen sensor", value + " Nm", "torque wrench"], strict=True
        ):
            c.text = t
        doc.save(folder / "05-service.docx")
        stream = io.BytesIO()
        canvas = Canvas(stream, pagesize=(595, 842))
        if split == "holdout":
            img = Image.new("RGB", (1190, 1684), "white")
            draw = ImageDraw.Draw(img)
            # DejaVu is optional; use the Mac font only during fixture generation.
            candidates = [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
            font = next(
                (ImageFont.truetype(p, 32) for p in candidates if Path(p).exists()),
                ImageFont.load_default(size=32),
            )
            for y, line in enumerate(
                [
                    "SCANNED DEMONSTRATION",
                    "WARNING: Never remove BCM while powered.",
                    f"oxygen sensor torque: {value} Nm",
                    f"Part number: {part}",
                    "Tool: torque wrench",
                ]
            ):
                draw.text((70, 130 + y * 85), line, font=font, fill="black")
            canvas.drawImage(ImageReader(img), 0, 0, 595, 842)
        else:
            canvas.setFont("Helvetica-Bold", 18)
            canvas.drawString(50, 770, "DEMONSTRATION SERVICE MANUAL")
            canvas.setFont("Helvetica", 12)
            canvas.drawString(50, 720, "WARNING: Never remove BCM while powered.")
            for x, text in [(50, "Component"), (280, "Torque"), (420, "Part number")]:
                canvas.drawString(x, 665, text)
            for x, text in [(50, "oxygen sensor"), (280, value + " Nm"), (420, part)]:
                canvas.drawString(x, 625, text)
            for y in [690, 650, 610]:
                canvas.line(45, y, 550, y)
            for x in [45, 270, 410, 550]:
                canvas.line(x, 610, x, 690)
        canvas.showPage()
        canvas.save()
        (folder / "06-service.pdf").write_bytes(stream.getvalue())
print("Created 24 self-authored fixtures across development and holdout splits.")

"""Sample files built in memory (no fixture files needed)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw


def png(w: int = 400, h: int = 240, mode: str = "RGB") -> bytes:
    img = Image.new(mode, (w, h), (240, 244, 242) if mode == "RGB" else (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((30, 30, w - 30, h - 30), fill=(61, 122, 111) if mode == "RGB" else (61, 122, 111, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def docx_bytes() -> bytes:
    import docx
    from docx.shared import Inches

    d = docx.Document()
    d.core_properties.title = "Biology notes"
    d.add_heading("Photosynthesis", 0)
    d.add_heading("Overview", 1)
    p = d.add_paragraph("Plants turn ")
    p.add_run("light energy").bold = True
    p.add_run(" into ")
    p.add_run("chemical energy").italic = True
    p.add_run(". Café and € test Unicode.")
    d.add_paragraph("Chlorophyll absorbs light", style="List Bullet")
    d.add_paragraph("Oxygen is released", style="List Bullet 2")
    d.add_paragraph("Collect leaves", style="List Number")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Stage", "Output"
    t.cell(1, 0).text, t.cell(1, 1).text = "Calvin cycle", "Glucose"
    d.add_picture(io.BytesIO(png()), width=Inches(2))
    d.add_page_break()
    d.add_heading("Equation", 1)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Cell Biology"
    s.placeholders[1].text = "Unit 3 revision"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Organelles"
    tf = s.placeholders[1].text_frame
    tf.text = "Nucleus: stores DNA"
    p = tf.add_paragraph()
    p.text = "ATP"
    p.level = 1
    s.shapes.add_picture(io.BytesIO(png()), Inches(6), Inches(4), width=Inches(2))
    s.notes_slide.notes_text_frame.text = "Mention the powerhouse joke."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def xlsx_bytes() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Marks"
    ws.append(["Name", "Maths", "Roll no"])
    ws.append(["Asha", 91.5, "007"])
    ws.append(["Ravi", 78, "012"])
    wb.create_sheet("Empty")
    ws3 = wb.create_sheet("Attendance")
    ws3.append(["Week", "Present"])
    ws3.append([1, 5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pdf_bytes() -> bytes:
    """A PDF with headings, a list, a table and a picture (made by our own writer)."""
    from app.convert import convert

    return convert(docx_bytes(), "notes.docx", "pdf").data


MARKDOWN = (
    "# Study plan\n\nRevise **chemistry** and *physics*.\n\n- Monday: atoms\n- Tuesday: forces\n"
    "  - Newton's laws\n\n1. First\n2. Second\n\n| Day | Topic |\n|---|---|\n| Mon | Atoms |\n\n"
    "> Stay calm.\n\n```\nprint('hi')\n```\n"
).encode()

HTML = (
    b"<html><head><title>Lab report</title><style>p{}</style></head><body><h1>Lab report</h1>"
    b"<p align='center'>Some <b>bold</b>, <i>italic</i> and <a href='https://example.com'>a link</a>.</p>"
    b"<ul><li>One<ul><li>Nested</li></ul></li><li>Two</li></ul><ol><li>First</li></ol>"
    b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    b"<img src='https://example.com/x.png'><script>alert(1)</script></body></html>"
)

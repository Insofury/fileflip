"""Document -> Word (.docx), using python-docx."""

from __future__ import annotations

import io

from ..model import Document, Image, ListItem, PageBreak, Paragraph, Run, Table

STYLES = {"title": "Title", "h1": "Heading 1", "h2": "Heading 2", "h3": "Heading 3", "quote": "Quote"}
PAGE_SIZES_MM = {"A4": (210, 297), "Letter": (215.9, 279.4)}


def _add_hyperlink(paragraph, run: Run) -> None:
    """python-docx has no hyperlink API, so build the XML: <w:hyperlink r:id=...><w:r>...</w:r>."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import RGBColor

    r_id = paragraph.part.relate_to(run.link, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    new_run = paragraph.add_run(run.text)
    new_run.font.underline = True
    new_run.font.bold = run.bold or None
    new_run.font.italic = run.italic or None
    new_run.font.color.rgb = RGBColor(0x1F, 0x5F, 0xA8)
    link.append(new_run._r)  # move the run inside the hyperlink element
    paragraph._p.append(link)


def _add_runs(paragraph, runs: list[Run]) -> None:
    for run in runs:
        if run.link:
            _add_hyperlink(paragraph, run)
            continue
        r = paragraph.add_run(run.text)  # "\n" and "\t" become line breaks and tabs
        r.bold = run.bold or None
        r.italic = run.italic or None
        r.underline = run.underline or None
        if run.code:
            r.font.name = "Consolas"


def write_docx(doc: Document, page_size: str = "A4") -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Mm, Pt

    out = docx.Document()
    section = out.sections[0]
    width_mm, height_mm = PAGE_SIZES_MM.get(page_size, PAGE_SIZES_MM["A4"])
    section.page_width, section.page_height = Mm(width_mm), Mm(height_mm)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Mm(22))
    usable_inches = (width_mm - 44) / 25.4
    out.styles["Normal"].font.name = "Calibri"
    out.styles["Normal"].font.size = Pt(11)
    if doc.title:
        out.core_properties.title = doc.title

    align = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT, "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}
    for block in doc.blocks:
        if isinstance(block, Paragraph):
            if block.style == "code":
                p = out.add_paragraph()
                for line_run in block.runs:
                    r = p.add_run(line_run.text)
                    r.font.name = "Consolas"
                    r.font.size = Pt(9.5)
                continue
            p = out.add_paragraph(style=STYLES.get(block.style))
            _add_runs(p, block.runs)
            if block.align in align:
                p.alignment = align[block.align]
        elif isinstance(block, ListItem):
            base = "List Number" if block.ordered else "List Bullet"
            style = base if block.level == 0 else f"{base} {min(block.level + 1, 3)}"
            p = out.add_paragraph(style=style)
            _add_runs(p, block.runs)
        elif isinstance(block, Table):
            if not block.rows:
                continue
            width = max(len(r) for r in block.rows)
            table = out.add_table(rows=len(block.rows), cols=width)
            table.style = "Table Grid"
            for i, row in enumerate(block.rows):
                for j in range(width):
                    cell = table.cell(i, j)
                    cell.text = row[j] if j < len(row) else ""
                    if i == 0 and block.header:
                        for r in cell.paragraphs[0].runs:
                            r.bold = True
            out.add_paragraph()
        elif isinstance(block, Image):
            width_in = min(block.width / 96, usable_inches)
            try:
                out.add_picture(io.BytesIO(block.data), width=Inches(width_in))
            except Exception:  # noqa: BLE001 - skip a picture Word can't take rather than fail
                doc.note("A picture couldn't be added to the Word file and was left out.")
        elif isinstance(block, PageBreak):
            out.add_page_break()

    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()

"""Document -> PDF, using fpdf2 with the bundled DejaVu fonts.

Layout: A4 or Letter pages with comfortable margins; headings, rich paragraphs (bold,
italic, underline, links), bullet and numbered lists with indents, tables with a light
header, pictures scaled to fit, page breaks, and page numbers in the footer.
"""

from __future__ import annotations

import io

from .. import fonts
from ..model import Document, Image, ListItem, PageBreak, Paragraph, Run, Table, text_of

SIZES = {"title": 22, "h1": 18, "h2": 15, "h3": 12.5, "normal": 10.5, "quote": 10.5, "code": 9}
SPACE_BEFORE = {"title": 2, "h1": 6, "h2": 5, "h3": 4, "normal": 0, "quote": 1, "code": 1}
INK = (33, 41, 46)
MUTED = (96, 108, 112)
ACCENT = (47, 111, 102)
LINK = (31, 95, 168)
MARGIN = 20  # mm


def _make_pdf(page_size: str, title: str):
    from ..compat import fpdf

    FPDF = fpdf()

    class _PDF(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("Body", size=8)
            self.set_text_color(*MUTED)
            self.cell(0, 6, str(self.page_no()), align="C")

    pdf = _PDF(format="Letter" if page_size == "Letter" else "A4", unit="mm")
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(True, margin=MARGIN)
    for style in ("", "B", "I", "BI"):
        pdf.add_font("Body", style, fonts.path(style))
    pdf.add_font("Mono", "", fonts.path("mono"))
    pdf.set_title(title or "Converted with FileFlip")
    pdf.set_creator("FileFlip")
    pdf.add_page()
    pdf.set_text_color(*INK)
    return pdf


class _Writer:
    def __init__(self, doc: Document, page_size: str):
        self.doc = doc
        self.pdf = _make_pdf(page_size, doc.title)
        self.numbers: dict[int, int] = {}  # list level -> current number
        self.prev = None

    # ------------------------------------------------ text helpers
    def font(self, run: Run | None, size: float, style_bold: bool = False) -> None:
        pdf = self.pdf
        if run is not None and run.code:
            pdf.set_font("Mono", size=size * 0.92)
            return
        bold = style_bold or (run.bold if run else False)
        italic = run.italic if run else False
        pdf.set_font("Body", ("B" if bold else "") + ("I" if italic else ""), size=size)
        pdf.set_text_color(*(LINK if run is not None and run.link else INK))

    def rich(self, runs: list[Run], size: float, indent: float = 0, align: str = "left", bold: bool = False) -> None:
        pdf = self.pdf
        line_h = size * 0.5
        plain = all(r.same_format(runs[0]) for r in runs) and not runs[0].link
        left = MARGIN + indent
        if plain or align in ("center", "right", "justify"):
            self.font(runs[0], size, bold)
            pdf.set_x(left)
            pdf.multi_cell(pdf.w - MARGIN - left, line_h, text_of(runs), align={"center": "C", "right": "R", "justify": "J"}.get(align, "L"), new_x="LMARGIN", new_y="NEXT")
            return
        pdf.set_left_margin(left)
        pdf.set_x(left)
        for run in runs:
            self.font(run, size, bold)
            if run.underline or run.link:
                pdf.set_font(style=pdf.font_style + "U")
            pdf.write(line_h, run.text, link=run.link or "")
        pdf.ln(line_h)
        pdf.set_left_margin(MARGIN)

    # ------------------------------------------------ blocks
    def paragraph(self, block: Paragraph) -> None:
        pdf = self.pdf
        size = SIZES.get(block.style, SIZES["normal"])
        pdf.ln(SPACE_BEFORE.get(block.style, 0))
        if block.style in ("title", "h1", "h2", "h3"):
            if pdf.get_y() > pdf.h - MARGIN - 25:  # keep headings with the text below them
                pdf.add_page()
            self.font(None, size, True)
            pdf.set_text_color(*(ACCENT if block.style in ("title", "h1") else INK))
            align = {"center": "C", "right": "R"}.get(block.align, "L")
            pdf.multi_cell(0, size * 0.5, text_of(block.runs).replace("\n", " "), align=align, new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*INK)
            pdf.ln(1.5)
            return
        if block.style == "code":
            pdf.set_fill_color(242, 244, 242)
            pdf.set_font("Mono", size=size)
            pdf.multi_cell(0, size * 0.48, text_of(block.runs), fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2.5)
            return
        if block.style == "quote":
            start_y, page = pdf.get_y(), pdf.page_no()
            self.rich([Run(r.text, r.bold, True, r.underline, r.code, r.link) for r in block.runs], size, indent=5)
            if pdf.page_no() == page:
                pdf.set_draw_color(*ACCENT)
                pdf.set_line_width(0.6)
                pdf.line(MARGIN + 1.5, start_y + 0.5, MARGIN + 1.5, pdf.get_y() - 0.5)
            pdf.ln(2.5)
            return
        self.rich(block.runs, size, align=block.align)
        pdf.ln(2.5)

    def list_item(self, block: ListItem) -> None:
        pdf = self.pdf
        size = SIZES["normal"]
        indent = 6 + block.level * 6
        self.numbers = {lvl: n for lvl, n in self.numbers.items() if lvl < block.level or (lvl == block.level and block.ordered)}
        if block.ordered:
            self.numbers[block.level] = self.numbers.get(block.level, 0) + 1
            marker = f"{self.numbers[block.level]}."
        else:
            marker = "•" if block.level % 2 == 0 else "◦"
        self.font(None, size)
        pdf.set_x(MARGIN + indent - 5)
        pdf.cell(5, size * 0.5, marker)
        self.rich(block.runs, size, indent=indent)
        pdf.ln(1)

    def table(self, block: Table) -> None:
        pdf = self.pdf
        if not block.rows:
            return
        width = max(len(r) for r in block.rows)
        rows = [r + [""] * (width - len(r)) for r in block.rows]
        size = max(6.5, 9.5 - max(0, width - 5) * 0.4)
        pdf.set_font("Body", size=size)
        pdf.set_draw_color(214, 220, 216)
        pdf.set_line_width(0.2)
        pdf.ln(1)
        from fpdf.fonts import FontFace

        heading = FontFace(emphasis="BOLD", color=INK, fill_color=(231, 240, 237))
        with pdf.table(
            width=pdf.epw,
            line_height=size * 0.55,
            text_align="LEFT",
            first_row_as_headings=block.header,
            headings_style=heading,
            borders_layout="SINGLE_TOP_LINE" if not block.header else "HORIZONTAL_LINES",
            padding=1.2,
        ) as table:
            for row in rows:
                cells = table.row()
                for value in row:
                    cells.cell(value)
        pdf.ln(4)

    def image(self, block: Image) -> None:
        pdf = self.pdf
        width_mm = min(block.width * 25.4 / 96, pdf.epw)
        height_mm = width_mm * block.height / max(block.width, 1)
        max_h = pdf.h - 2 * MARGIN - 10
        if height_mm > max_h:  # very tall picture: fit the page height instead
            height_mm, width_mm = max_h, max_h * block.width / max(block.height, 1)
        if pdf.get_y() + height_mm > pdf.h - MARGIN:
            pdf.add_page()
        x = MARGIN + (pdf.epw - width_mm) / 2
        pdf.image(io.BytesIO(block.data), x=x, w=width_mm, h=height_mm)
        pdf.ln(4)

    def run(self) -> bytes:
        for block in self.doc.blocks:
            if not isinstance(block, ListItem):
                self.numbers = {}
            if isinstance(block, Paragraph):
                self.paragraph(block)
            elif isinstance(block, ListItem):
                self.list_item(block)
            elif isinstance(block, Table):
                self.table(block)
            elif isinstance(block, Image):
                self.image(block)
            elif isinstance(block, PageBreak):
                if not isinstance(self.prev, PageBreak):
                    self.pdf.add_page()
            self.prev = block
        return bytes(self.pdf.output())


def _check_characters(doc: Document) -> None:
    supported = fonts.supported_chars()
    missing = set()
    for block in doc.blocks:
        texts = []
        if isinstance(block, (Paragraph, ListItem)):
            texts.append(text_of(block.runs))
        elif isinstance(block, Table):
            texts.extend(c for row in block.rows for c in row)
        for text in texts:
            missing.update(ch for ch in text if ord(ch) > 0x7F and ord(ch) not in supported and not ch.isspace())
    if missing:
        sample = " ".join(sorted(missing)[:6])
        doc.note(f"Some characters ({sample}) aren't in the PDF font and may show as blanks, e.g. emoji, Hindi or Chinese text.")


def write_pdf(doc: Document, page_size: str = "A4") -> bytes:
    _check_characters(doc)
    return _Writer(doc, page_size).run()

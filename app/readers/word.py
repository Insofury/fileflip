"""Word (.docx) -> Document, using python-docx.

Walks the document body in order (paragraphs and tables), keeping headings, bold /
italic / underline, links, bullet and numbered lists, alignment, tables, pictures and
page breaks.
"""

from __future__ import annotations

import io

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, normalize_image, strip_runs

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
STYLE_MAP = {"title": "title", "subtitle": "h2", "heading 1": "h1", "heading 2": "h2", "quote": "quote", "intense quote": "quote"}
ALIGN = {1: "center", 2: "right", 3: "justify"}


def _style(paragraph) -> str:
    name = (paragraph.style.name if paragraph.style is not None else "").lower()
    if name in STYLE_MAP:
        return STYLE_MAP[name]
    if name.startswith("heading"):
        return "h3"
    return "normal"


def _list_info(paragraph, numbering) -> tuple[bool, bool, int]:
    """(is a list item, ordered, level) from the paragraph's numbering or its style."""
    style = (paragraph.style.name if paragraph.style is not None else "").lower()
    p_pr = paragraph._p.pPr
    num_pr = p_pr.numPr if p_pr is not None else None
    if num_pr is None or num_pr.numId is None:
        if style.startswith("list bullet"):
            return True, False, _level_from_style(style)
        if style.startswith("list number"):
            return True, True, _level_from_style(style)
        return False, False, 0
    num_id = num_pr.numId.val
    level = num_pr.ilvl.val if num_pr.ilvl is not None else 0
    if num_id == 0:  # numbering explicitly removed
        return False, False, 0
    ordered = "number" in style
    if numbering is not None:
        fmt = numbering.xpath(
            f'./w:num[@w:numId="{num_id}"]/w:abstractNumId/@w:val'
        )
        if fmt:
            levels = numbering.xpath(f'./w:abstractNum[@w:abstractNumId="{fmt[0]}"]/w:lvl[@w:ilvl="{level}"]/w:numFmt/@w:val')
            if levels:
                ordered = levels[0] not in ("bullet", "none")
    return True, ordered, min(int(level), 3)


def _level_from_style(style: str) -> int:
    last = style.rsplit(" ", 1)[-1]
    return min(int(last) - 1, 3) if last.isdigit() else 0


def _run_format(run, paragraph) -> dict:
    def flag(value, style_value):
        return bool(value if value is not None else style_value)

    style_font = paragraph.style.font if paragraph.style is not None else None
    return {
        "bold": flag(run.bold, style_font.bold if style_font else None),
        "italic": flag(run.italic, style_font.italic if style_font else None),
        "underline": bool(run.underline),
        "code": (run.font.name or "").lower() in ("consolas", "courier new", "courier", "menlo", "monaco"),
    }


def _images_in(element, part) -> list:
    images = []
    for rid in element.xpath(".//a:blip/@r:embed"):
        try:
            blob = part.related_parts[rid].blob
        except KeyError:
            continue
        images.append(blob)
    return images


def read_docx(data: bytes) -> Document:
    import docx
    from docx.table import Table as DocxTable
    from docx.text.hyperlink import Hyperlink
    from docx.text.paragraph import Paragraph as DocxParagraph

    try:
        source = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises many kinds of errors for bad files
        raise ConvertError("This Word file couldn't be opened. It may be damaged or password-protected.") from exc

    doc = Document(title=(source.core_properties.title or "").strip())
    part = source.part
    try:
        numbering = part.numbering_part.element
    except (NotImplementedError, KeyError, AttributeError):
        numbering = None
    skipped = 0

    for child in source.element.body.iterchildren():
        tag = child.tag.replace(W, "")
        if tag == "tbl":
            table = DocxTable(child, source)
            rows = []
            for row in table.rows:
                cells, previous = [], None
                for cell in row.cells:
                    # python-docx repeats merged cells; keep the text once
                    cells.append("" if cell._tc is previous else cell.text.strip())
                    previous = cell._tc
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                doc.add(Table([r + [""] * (width - len(r)) for r in rows]))
            continue
        if tag != "p":
            continue

        paragraph = DocxParagraph(child, source)
        run_list: list[Run] = []
        page_break = False
        for item in paragraph.iter_inner_content():
            if isinstance(item, Hyperlink):
                url = item.url or None
                for r in item.runs:
                    run_list.append(Run(r.text, **_run_format(r, paragraph), link=url))
                continue
            run_list.append(Run(item.text, **_run_format(item, paragraph)))
            if item._r.xpath('./w:br[@w:type="page"]'):
                page_break = True

        is_list, ordered, level = _list_info(paragraph, numbering)
        clean = strip_runs(run_list)
        if clean:
            if is_list:
                doc.add(ListItem(clean, ordered=ordered, level=level))
            else:
                align = ALIGN.get(int(paragraph.alignment)) if paragraph.alignment is not None else None
                doc.add(Paragraph(clean, style=_style(paragraph), align=align or "left"))

        for blob in _images_in(child, part):
            img = normalize_image(blob)
            if img is None:
                skipped += 1
            else:
                doc.add(img)
        if page_break:
            doc.add(PageBreak())

    if skipped:
        doc.note(f"{skipped} drawing(s) in a format FileFlip can't read (like EMF/WMF) were left out.")
    if not doc.blocks:
        raise ConvertError("This Word document is empty.")
    return doc

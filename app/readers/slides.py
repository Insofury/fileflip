"""PowerPoint (.pptx) -> Document, using python-pptx.

Each slide becomes a heading ("Slide 3: Title"), its text as bullet points (keeping the
indent level), its tables and pictures, and the speaker notes. Slides are separated by
page breaks, so a PDF or Word export reads like a handout / study notes.
"""

from __future__ import annotations

import io

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, normalize_image, runs, strip_runs


def _shape_blocks(shape, doc: Document, title_shape, counters: dict) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if title_shape is not None and shape.shape_id == title_shape.shape_id:
        return
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for inner in _ordered(shape.shapes):
            _shape_blocks(inner, doc, title_shape, counters)
        return
    if getattr(shape, "has_table", False) and shape.has_table:
        rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
        if rows:
            doc.add(Table(rows))
        return
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        try:
            img = normalize_image(shape.image.blob, shape.name)
        except (AttributeError, ValueError):
            img = None
        if img is None:
            counters["skipped"] += 1
        else:
            doc.add(img)
        return
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        from pptx.enum.shapes import PP_PLACEHOLDER

        paragraphs = [p for p in shape.text_frame.paragraphs if "".join(r.text for r in p.runs).strip()]
        body_placeholder = shape.is_placeholder and shape.placeholder_format.type in (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT)
        as_list = len(paragraphs) > 1 or (body_placeholder and bool(paragraphs))
        for p in paragraphs:
            run_list = strip_runs(
                [Run(r.text, bold=bool(r.font.bold), italic=bool(r.font.italic), underline=bool(r.font.underline)) for r in p.runs]
            )
            if not run_list:
                continue
            if as_list:
                doc.add(ListItem(run_list, level=min(p.level, 3)))
            else:
                doc.add(Paragraph(run_list))


def _ordered(shapes):
    """Top-to-bottom, then left-to-right - roughly the reading order."""
    return sorted(shapes, key=lambda s: ((s.top or 0) // 20000, s.left or 0))


def read_pptx(data: bytes) -> Document:
    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ConvertError("This PowerPoint file couldn't be opened. It may be damaged or password-protected.") from exc

    doc = Document(title=(prs.core_properties.title or "").strip())
    counters = {"skipped": 0}
    for number, slide in enumerate(prs.slides, start=1):
        if number > 1:
            doc.add(PageBreak())
        title_shape = slide.shapes.title
        title = title_shape.text.strip().replace("\n", " ") if title_shape is not None and title_shape.has_text_frame else ""
        doc.add(Paragraph(runs(f"Slide {number}" + (f": {title}" if title else "")), style="h2"))
        for shape in _ordered(slide.shapes):
            _shape_blocks(shape, doc, title_shape, counters)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                doc.add(Paragraph([Run("Speaker notes: ", bold=True), Run(notes)], style="quote"))
    if counters["skipped"]:
        doc.note(f"{counters['skipped']} picture(s) in a format FileFlip can't read were left out.")
    if not doc.blocks:
        raise ConvertError("This presentation has no slides.")
    return doc

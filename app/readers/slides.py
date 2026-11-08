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


def _read_with_python_pptx(data: bytes) -> Document:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    doc = Document(title=(prs.core_properties.title or "").strip())
    counters = {"skipped": 0}
    for number, slide in enumerate(prs.slides, start=1):
        if number > 1:
            doc.add(PageBreak())
        try:
            title_shape = slide.shapes.title
            title = title_shape.text.strip().replace("\n", " ") if title_shape is not None and title_shape.has_text_frame else ""
        except Exception:  # noqa: BLE001
            title_shape, title = None, ""
        doc.add(Paragraph(runs(f"Slide {number}" + (f": {title}" if title else "")), style="h2"))
        for shape in _ordered(slide.shapes):
            try:
                _shape_blocks(shape, doc, title_shape, counters)
            except Exception:  # noqa: BLE001 - one odd shape (chart, SmartArt, OLE) must not stop the rest
                counters["skipped_shapes"] = counters.get("skipped_shapes", 0) + 1
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    doc.add(Paragraph([Run("Speaker notes: ", bold=True), Run(notes)], style="quote"))
        except Exception:  # noqa: BLE001
            pass
    if counters["skipped"]:
        doc.note(f"{counters['skipped']} picture(s) in a format FileFlip can't read were left out.")
    if counters.get("skipped_shapes"):
        doc.note(f"{counters['skipped_shapes']} item(s) like charts or diagrams couldn't be converted.")
    return doc


# ------------------------------------------------------------------ fallback
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _read_with_xml(data: bytes) -> Document:
    """Read slide text straight from the XML (works for .pptm/.ppsx/.potx and files
    python-pptx refuses)."""
    import posixpath
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        pres = ET.fromstring(zf.read("ppt/presentation.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))}
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ConvertError("This PowerPoint file couldn't be opened. It may be damaged or password-protected.") from exc
    slide_ids = pres.find(P + "sldIdLst")
    paths = [posixpath.normpath(posixpath.join("ppt", rels.get(s.get(REL + "id"), ""))) for s in (slide_ids if slide_ids is not None else [])]
    doc = Document()
    for number, path in enumerate(paths, start=1):
        try:
            root = ET.fromstring(zf.read(path))
        except (KeyError, ET.ParseError):
            continue
        if number > 1:
            doc.add(PageBreak())
        title, body = "", []
        for sp in root.iter(P + "sp"):
            ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
            is_title = ph is not None and ph.get("type") in ("title", "ctrTitle")
            for p in sp.iter(A + "p"):
                text = "".join(t.text or "" for t in p.iter(A + "t")).strip()
                if not text:
                    continue
                if is_title and not title:
                    title = text
                else:
                    ppr = p.find(A + "pPr")
                    body.append((text, int(ppr.get("lvl", "0")) if ppr is not None else 0))
        doc.add(Paragraph(runs(f"Slide {number}" + (f": {title}" if title else "")), style="h2"))
        for text, level in body:
            doc.add(ListItem([Run(text)], level=min(level, 3)))
    return doc


def read_pptx(data: bytes) -> Document:
    try:
        doc = _read_with_python_pptx(data)
    except Exception:  # noqa: BLE001 - e.g. .pptm content types or odd XML: read it directly
        doc = _read_with_xml(data)
        doc.note("This file was read in a simpler way, so pictures, tables and notes may be missing.")
    if not doc.blocks:
        raise ConvertError("This presentation has no slides.")
    return doc

"""PDF -> Document, using pdfminer.six (text with positions and fonts) and pypdf (images).

A PDF has no paragraphs or headings - only characters placed on a page. We rebuild the
structure the way a reader would:

* pdfminer groups characters into lines and text boxes in reading order;
* the most common font size is the body text; noticeably bigger text becomes a heading;
* bold / italic come from the font names ("Arial-BoldMT", "Times-Italic");
* lines starting with "•", "-", "1." become list items;
* lines broken with a hyphen are joined back ("exam-" + "ple" -> "example");
* page numbers in the top/bottom margin are dropped;
* pictures are placed between the text boxes at their position on the page.
"""

from __future__ import annotations

import io
import re
from collections import Counter

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, merge_runs, normalize_image, strip_runs

MAX_PAGES = 400
MIN_IMAGE_SIDE = 24  # ignore tiny decorations
BULLET = re.compile(r"^\s*(?:[•●○◦▪▫■□‣⁃∙·\-–—*]|(\d{1,3})[.)]|\(?([a-z])\))\s+")
PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)


def _is_bold(font: str) -> bool:
    f = font.lower()
    return any(k in f for k in ("bold", "black", "heavy", "semibold", "demi"))


def _is_italic(font: str) -> bool:
    f = font.lower()
    return "italic" in f or "oblique" in f


def _line_runs(line) -> tuple[list[Run], list[float]]:
    """Characters of one text line -> formatted runs, plus the character sizes."""
    from pdfminer.layout import LTChar

    out: list[Run] = []
    sizes: list[float] = []
    bold = italic = False
    for item in line:
        text = item.get_text()
        if isinstance(item, LTChar):
            bold, italic = _is_bold(item.fontname), _is_italic(item.fontname)
            sizes.append(item.size)
        out.append(Run(text.replace("\n", ""), bold=bold, italic=italic))
    return merge_runs(out), sizes


def _join(parts: list[list[Run]]) -> list[Run]:
    """Join the lines of a paragraph with spaces, undoing end-of-line hyphenation."""
    result: list[Run] = []
    for line in parts:
        line = strip_runs(line)
        if not line:
            continue
        if result:
            prev = result[-1]
            if prev.text.endswith("-") and not prev.text.endswith(" -") and line[0].text[:1].islower():
                result[-1] = Run(prev.text[:-1], prev.bold, prev.italic)
            else:
                result.append(Run(" ", prev.bold and line[0].bold, prev.italic and line[0].italic))
        result.extend(line)
    return merge_runs(result)


def _heading_style(size: float, body: float, text: str, all_bold: bool, n_lines: int) -> str | None:
    if size >= body * 1.6:
        return "h1"
    if size >= body * 1.3:
        return "h2"
    short = len(text) <= 90 and not text.rstrip().endswith((".", ",", ";", ":"))
    if n_lines <= 2 and short and (size >= body * 1.12 or (all_bold and len(text) <= 70)):
        return "h3"
    return None


def _box_lines(box) -> list[tuple[list[Run], float, str, bool, float]]:
    """(runs, typical character size, text, whole line bold, left x) for each line of a box."""
    from pdfminer.layout import LTTextLine

    lines = []
    for line in box:
        if isinstance(line, LTTextLine):
            run_list, sizes = _line_runs(line)
            text = "".join(r.text for r in run_list).strip()
            if text:
                size = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
                bold = all(r.bold for r in run_list if r.text.strip())
                lines.append((run_list, size, text, bold, line.x0))
    return lines


def _box_blocks(box, body: float, list_x: dict) -> list:
    """One pdfminer text box -> headings / paragraphs / list items.

    A box can hold several of these (e.g. a heading sitting right above its paragraph),
    so lines are first split into segments wherever the font size changes, a bullet
    starts, or a short all-bold line is followed by normal text. `list_x` collects the
    left edge of each list item so nesting levels can be worked out per page."""
    lines = _box_lines(box)
    if not lines:
        return []

    segments: list[dict] = []
    for run_list, size, text, bold, x0 in lines:
        m = BULLET.match(text)
        prev = segments[-1] if segments else None
        new = (
            prev is None
            or m is not None
            or abs(size - prev["size"]) > max(0.8, prev["size"] * 0.12)
            or (len(prev["lines"]) == 1 and prev["bold"] and not bold and len(prev["text"]) <= 70)
        )
        if new:
            segments.append({"lines": [], "size": size, "bold": bold, "text": "", "bullet": m, "raw": text, "x0": x0})
        seg = segments[-1]
        seg["lines"].append(run_list)
        seg["text"] = (seg["text"] + " " + text).strip()
        seg["bold"] = seg["bold"] and bold

    blocks: list = []
    for seg in segments:
        style = _heading_style(seg["size"], body, seg["text"], seg["bold"], len(seg["lines"]))
        if style:
            blocks.append(Paragraph([Run(seg["text"])], style=style))
            continue
        m = seg["bullet"]
        if m:
            first = _strip_prefix(seg["lines"][0], len(m.group(0)) - (len(seg["raw"]) - len(seg["raw"].lstrip())))
            joined = _join([first] + seg["lines"][1:])
            if joined:
                item = ListItem(joined, ordered=bool(m.group(1) or m.group(2)))
                list_x[id(item)] = seg["x0"]
                blocks.append(item)
            continue
        joined = _join(seg["lines"])
        if joined:
            blocks.append(Paragraph(joined))
    return blocks


# ------------------------------------------------------------------ tables
def _find_tables(boxes: list, body: float) -> list[tuple[float, set[int], list[list[str]]]]:
    """Spot tables: short text boxes that line up in at least 2 rows x 2 columns.

    Returns [(top y, indexes of the boxes used, rows of cell text)]."""
    tol = max(2.0, body * 0.6)
    cands = []
    for i, box in enumerate(boxes):
        text = " ".join(box.get_text().split())
        n_lines = sum(1 for _ in box)
        if text and n_lines <= 3 and len(text) <= 80:
            cands.append((i, box, text))
    # group into rows by their top edge
    rows: list[list[tuple[int, object, str]]] = []
    for item in sorted(cands, key=lambda c: -c[1].y1):
        if rows and abs(rows[-1][0][1].y1 - item[1].y1) <= tol:
            rows[-1].append(item)
        else:
            rows.append([item])
    rows = [sorted(r, key=lambda c: c[1].x0) for r in rows]

    def aligned(a, b) -> bool:
        if len(a) < 2 or len(b) < 2 or abs(len(a) - len(b)) > 1:
            return False
        xs = [c[1].x0 for c in a]
        hits = sum(1 for c in b if min(abs(c[1].x0 - x) for x in xs) <= 14)
        gap = min(c[1].y0 for c in a) - max(c[1].y1 for c in b)
        return hits >= min(len(a), len(b)) - (0 if min(len(a), len(b)) == 2 else 1) and gap <= body * 2.6

    tables = []
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and aligned(rows[j], rows[j + 1]):
            j += 1
        group = rows[i : j + 1]
        if len(group) >= 2:
            widest = max(group, key=len)
            columns = [c[1].x0 for c in widest]
            table_rows = []
            used = set()
            for row in group:
                cells = [""] * len(columns)
                for idx, box, text in row:
                    col = min(range(len(columns)), key=lambda k: abs(columns[k] - box.x0))
                    cells[col] = (cells[col] + " " + text).strip()
                    used.add(idx)
                table_rows.append(cells)
            tables.append((group[0][0][1].y1, used, table_rows))
        i = j + 1
    return tables


def _strip_prefix(run_list: list[Run], n: int) -> list[Run]:
    """Remove the first n characters (the bullet) from a list of runs."""
    run_list = strip_runs(run_list)
    out = []
    for r in run_list:
        if n <= 0:
            out.append(r)
        elif len(r.text) <= n:
            n -= len(r.text)
        else:
            out.append(Run(r.text[n:], r.bold, r.italic))
            n = 0
    return strip_runs(out)


def _pdf_images(data: bytes) -> list[dict]:
    """page index -> {name: Image} using pypdf (handles JPEG, Flate, masks, CMYK...)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        reader.decrypt("")
    pages = []
    for page in reader.pages[:MAX_PAGES]:
        found = {}
        try:
            for img in page.images:
                try:
                    pil = img.image
                except Exception:  # noqa: BLE001 - unusual image encodings are skipped
                    continue
                if pil is None or min(pil.size) < MIN_IMAGE_SIDE:
                    continue
                buf = io.BytesIO()
                if pil.mode in ("RGBA", "LA", "P"):
                    pil.convert("RGBA").save(buf, "PNG")
                else:
                    pil.convert("RGB").save(buf, "JPEG", quality=90)
                normal = normalize_image(buf.getvalue(), img.name)
                if normal:
                    found[img.name.rsplit(".", 1)[0]] = normal
        except Exception:  # noqa: BLE001
            pass
        pages.append(found)
    return pages


def _layout_images(element, out: list) -> None:
    """Collect (name, top y) of every image on a page, including those inside figures."""
    from pdfminer.layout import LTFigure, LTImage

    if isinstance(element, LTImage):
        out.append((element.name, element.y1))
    elif isinstance(element, LTFigure):
        for child in element:
            _layout_images(child, out)


def read_pdf(data: bytes) -> Document:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFSyntaxError

    try:
        pages = []
        for i, page in enumerate(extract_pages(io.BytesIO(data), laparams=LAParams(line_margin=0.6, boxes_flow=0.5))):
            if i >= MAX_PAGES:
                break
            pages.append(page)
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise ConvertError("This PDF is password-protected. Remove the password first, then try again.") from exc
    except PDFSyntaxError as exc:
        raise ConvertError("This PDF couldn't be read. It may be damaged.") from exc
    except Exception as exc:  # noqa: BLE001 - pdfminer raises many error types on broken files
        raise ConvertError("This PDF couldn't be read. It may be damaged or use an unusual format.") from exc
    if not pages:
        raise ConvertError("This PDF has no pages.")

    # The most common character size is the body text size.
    sizes: Counter = Counter()
    for page in pages:
        for el in page:
            if isinstance(el, LTTextContainer):
                for line in el:
                    if isinstance(line, LTTextLine):
                        for ch in line:
                            if isinstance(ch, LTChar):
                                sizes[round(ch.size, 1)] += 1
    body = sizes.most_common(1)[0][0] if sizes else 11.0

    try:
        page_images = _pdf_images(data)
    except Exception:  # noqa: BLE001 - images are a bonus; never fail the conversion for them
        page_images = []

    doc = Document()
    for index, page in enumerate(pages):
        if index:
            doc.add(PageBreak())
        height = page.height or 842
        items: list[tuple[float, list]] = []  # (top y, blocks), in pdfminer's reading order
        positions: list[tuple[str, float]] = []
        boxes = []
        for el in page:
            if isinstance(el, LTTextContainer):
                text = el.get_text().strip()
                in_margin = el.y0 > height * 0.92 or el.y1 < height * 0.08
                if text and not (in_margin and PAGE_NUMBER.match(text)):  # drop page numbers
                    boxes.append(el)
            else:
                _layout_images(el, positions)

        tables = _find_tables(boxes, body)
        in_table = set().union(*(used for _y, used, _rows in tables)) if tables else set()
        placed: set[int] = set()
        list_x: dict[int, float] = {}
        for index_box, box in enumerate(boxes):
            if index_box in in_table:
                for t, (top, used, rows) in enumerate(tables):
                    if index_box in used and t not in placed:
                        placed.add(t)
                        items.append((top, [Table(rows, header=True)]))
                continue
            blocks = _box_blocks(box, body, list_x)
            if blocks:
                items.append((box.y1, blocks))

        # Nesting: list items indented further than the page's leftmost list item.
        if list_x:
            base = min(list_x.values())
            for _y, blocks in items:
                for block in blocks:
                    if isinstance(block, ListItem) and id(block) in list_x:
                        block.level = max(0, min(3, round((list_x[id(block)] - base) / (body * 1.5))))

        images = dict(page_images[index]) if index < len(page_images) else {}
        # Put each picture before the first text box that starts below its top edge.
        for name, top in sorted(positions, key=lambda p: -p[1]):
            img = images.pop(name, None)
            if img is None:
                continue
            spot = next((i for i, (y, _b) in enumerate(items) if y < top), len(items))
            items.insert(spot, (top, [img]))
        items.extend((0, [img]) for img in images.values())  # pictures we couldn't place

        for _y, blocks in items:
            for block in blocks:
                doc.add(block)

    if not sizes:
        doc.note("This PDF has no selectable text (it looks like a scan), so only its pictures were kept. "
                 "Use an OCR tool to turn scanned pages into editable text.")
    if not doc.has_text() and not doc.images():
        raise ConvertError("This PDF has no text or pictures that can be converted.")
    if len(pages) >= MAX_PAGES:
        doc.note(f"Only the first {MAX_PAGES} pages were converted.")
    return doc

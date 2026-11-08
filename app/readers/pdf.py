"""PDF -> Document, using pdfminer.six (text with positions and fonts) and pypdf (images,
and a simpler fallback reader).

A PDF has no paragraphs or headings - only characters placed on a page. We rebuild the
structure the way a reader would:

* pdfminer groups characters into lines and text boxes. Text inside "figures" (form
  XObjects - Canva, design tools and many print drivers wrap whole pages in them) is
  laid out too, and boxes from different figures are merged top-to-bottom;
* the most common font size is the body text; noticeably bigger text becomes a heading;
* bold / italic come from the font names ("Arial-BoldMT", "Times-Italic");
* lines starting with "•", "-", "1." - or next to a small drawn dot - become list items;
* paragraphs inside one box are split at a short line ending a sentence, or a gap;
* lines broken with a hyphen are joined back ("exam-" + "ple" -> "example");
* letter-spaced titles ("R E S U M E") and doubled spaces are tidied up;
* page numbers in the top/bottom margin are dropped;
* short boxes that line up in rows and columns become tables;
* pictures are placed between the text boxes at their position on the page.

If pdfminer can't read a file, or finds no text, pypdf's text extraction is tried too.
"""

from __future__ import annotations

import io
import re
from collections import Counter

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, merge_runs, normalize_image, strip_runs

MAX_PAGES = 400
MIN_IMAGE_SIDE = 24  # ignore tiny decorations
BULLET = re.compile(r"^\s*(?:[•●○◦▪▫■□‣⁃∙·\-–—*➢►▶✓✔]|(\d{1,3})[.)]|\(?([a-z])\))\s+")
PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)
CID = re.compile(r"\(cid:\d+\)")
LETTER_SPACED = re.compile(r"\S(?: \S){2,}(?: {2,}\S(?: \S)*)*")
SENTENCE_END = (".", "!", "?", ":", ";", "”", '"', ")")
NO_TEXT = (
    "FileFlip couldn't find any text in this PDF. It's probably a scan (pictures of pages) or its text "
    "was saved as shapes. Use an OCR tool to make the text selectable, or export the PDF again."
)


def _is_bold(font: str) -> bool:
    f = font.lower()
    return any(k in f for k in ("bold", "black", "heavy", "semibold", "demi"))


def _is_italic(font: str) -> bool:
    f = font.lower()
    return "italic" in f or "oblique" in f


# ------------------------------------------------------------------ lines
ORDINALS = {"st", "nd", "rd", "th"}


def _superscripts(boxes: list, body: float) -> tuple[dict, set]:
    """Find ordinal superscripts ("10th", "2nd") that pdfminer split off into their own
    little line, and work out which character they belong after.

    Returns ({id(line): {char index: suffix}}, {id(fragment line)})."""
    from pdfminer.layout import LTChar, LTTextLine

    lines = [l for box in boxes for l in box if isinstance(l, LTTextLine)]
    inserts: dict[int, dict[int, str]] = {}
    fragments: set[int] = set()
    for frag in lines:
        text = frag.get_text().strip()
        chars = [c for c in frag if isinstance(c, LTChar)]
        if text not in ORDINALS or not chars or max(c.size for c in chars) > body * 0.85:
            continue
        for line in lines:
            if line is frag or not (line.y0 - 2 <= frag.y0 <= line.y1):
                continue
            for i, ch in enumerate(line):
                if isinstance(ch, LTChar) and ch.get_text().isdigit() and abs(ch.x1 - frag.x0) <= 2.5:
                    inserts.setdefault(id(line), {})[i] = text
                    fragments.add(id(frag))
                    break
            if id(frag) in fragments:
                break
    return inserts, fragments


def _line(line, stats: Counter, inserts: dict | None = None) -> dict | None:
    """One pdfminer text line -> {runs, size, text, bold, x0, x1, y0, y1}."""
    from pdfminer.layout import LTChar

    extra = (inserts or {}).get(id(line), {})
    items = list(line)
    # "10 th" where "th" is a smaller superscript in the same line -> "10th"
    drop: set[int] = set()
    for i, item in enumerate(items):
        if isinstance(item, LTChar) and item.get_text().isdigit():
            j, spaces = i + 1, []
            while j < len(items) and items[j].get_text() == " ":
                spaces.append(j)
                j += 1
            if spaces and j + 1 < len(items):
                a, b = items[j], items[j + 1]
                after = items[j + 2].get_text() if j + 2 < len(items) else ""
                if (
                    isinstance(a, LTChar)
                    and isinstance(b, LTChar)
                    and (a.get_text() + b.get_text()) in ORDINALS
                    and a.size < item.size * 0.85
                    and not after.isalpha()
                ):
                    drop.update(spaces)
    out: list[Run] = []
    sizes: list[float] = []
    bold = italic = False
    for i, item in enumerate(items):
        if i in drop:
            continue
        text = item.get_text()
        if isinstance(item, LTChar):
            bold, italic = _is_bold(item.fontname), _is_italic(item.fontname)
            sizes.append(item.size)
        out.append(Run(text.replace("\n", "") + extra.get(i, ""), bold=bold, italic=italic))
    run_list = merge_runs(out)

    text = "".join(r.text for r in run_list)
    if "(cid:" in text:  # glyphs from a font without a character map can't be read
        stats["cid"] += len(CID.findall(text))
        run_list = [Run(CID.sub("", r.text), r.bold, r.italic) for r in run_list]
        text = "".join(r.text for r in run_list)
    stripped = text.strip()
    if not stripped:
        return None
    if LETTER_SPACED.fullmatch(stripped):  # "A S H I S H   C H A H A R" -> "ASHISH CHAHAR"
        words = re.split(r" {2,}", stripped)
        first = run_list[0] if run_list else Run("")
        run_list = [Run(" ".join(w.replace(" ", "") for w in words), first.bold, first.italic)]
        stripped = run_list[0].text
    size = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
    return {
        "runs": run_list,
        "size": size,
        "text": stripped,
        "bold": all(r.bold for r in run_list if r.text.strip()),
        "x0": line.x0,
        "x1": line.x1,
        "y0": line.y0,
        "y1": line.y1,
    }


def _join(parts: list[list[Run]]) -> list[Run]:
    """Join the lines of a paragraph with spaces, undoing end-of-line hyphenation and
    collapsing doubled spaces (common in justified text)."""
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
    tidy: list[Run] = []
    for r in merge_runs(result):
        text = re.sub(r" {2,}", " ", r.text)
        if tidy and tidy[-1].text.endswith(" ") and text.startswith(" "):
            text = text[1:]
        if text:
            tidy.append(Run(text, r.bold, r.italic))
    return tidy


def _heading_style(size: float, body: float, text: str, all_bold: bool, n_lines: int) -> str | None:
    if size >= body * 1.6:
        return "h1"
    if size >= body * 1.3:
        return "h2"
    short = len(text) <= 90 and not text.rstrip().endswith((".", ",", ";", ":"))
    if n_lines <= 2 and short and (size >= body * 1.12 or (all_bold and len(text) <= 70)):
        return "h3"
    return None


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


def _drawn_bullet(line: dict, marks: list[tuple[float, float, float]], body: float) -> bool:
    """Is there a small drawn dot/square just left of this line (a bullet drawn as a shape)?"""
    for mx0, mx1, my in marks:
        if line["y0"] - 1.5 <= my <= line["y1"] + 1.5 and mx1 <= line["x0"] + 1 and mx0 >= line["x0"] - body * 3:
            return True
    return False


# ------------------------------------------------------------------ boxes -> blocks
def _box_blocks(box, ctx: dict) -> list:
    """One pdfminer text box -> headings / paragraphs / list items.

    A box can hold several of these (a heading above its paragraph, several bullet points),
    so lines are split into segments where: the font size changes; a bullet starts (a
    bullet character or a drawn dot); a short all-bold line is followed by normal text;
    a short line ends a sentence (end of a paragraph); or there's a vertical gap."""
    from pdfminer.layout import LTTextLine

    body = ctx["body"]
    lines = [
        info
        for info in (
            _line(l, ctx["stats"], ctx["inserts"])
            for l in box
            if isinstance(l, LTTextLine) and id(l) not in ctx["fragments"]
        )
        if info
    ]
    if not lines:
        return []
    widest = max(l["x1"] - l["x0"] for l in lines)

    segments: list[dict] = []
    prev_line = None
    for line in lines:
        m = BULLET.match(line["text"])
        drawn = _drawn_bullet(line, ctx["marks"], body)
        seg = segments[-1] if segments else None
        new = (
            seg is None
            or m is not None
            or drawn
            or abs(line["size"] - seg["size"]) > max(0.8, seg["size"] * 0.12)
            or (len(seg["lines"]) == 1 and seg["bold"] and not line["bold"] and len(seg["text"]) <= 70)
        )
        if not new and prev_line is not None:
            gap = prev_line["y0"] - line["y1"]
            short_end = (
                prev_line["text"].endswith(SENTENCE_END)
                and (prev_line["x1"] - prev_line["x0"]) < widest * 0.82
                and line["text"][:1].isupper()
            )
            new = short_end or gap > max(line["size"], body) * 0.9
        if new:
            segments.append({"lines": [], "size": line["size"], "bold": line["bold"], "text": "", "bullet": m,
                             "drawn": drawn, "raw": line["text"], "x0": line["x0"]})
        seg = segments[-1]
        seg["lines"].append(line["runs"])
        seg["text"] = (seg["text"] + " " + line["text"]).strip()
        seg["bold"] = seg["bold"] and line["bold"]
        prev_line = line

    blocks: list = []
    for seg in segments:
        style = _heading_style(seg["size"], body, seg["text"], seg["bold"], len(seg["lines"]))
        m = seg["bullet"]
        if style in ("h1", "h2") or (style and not m and not seg["drawn"]):
            blocks.append(Paragraph([Run(re.sub(r"\s{2,}", " ", seg["text"]))], style=style))
            continue
        if m or seg["drawn"]:
            first = seg["lines"][0]
            if m:
                first = _strip_prefix(first, len(m.group(0)) - (len(seg["raw"]) - len(seg["raw"].lstrip())))
            joined = _join([first] + seg["lines"][1:])
            if joined:
                item = ListItem(joined, ordered=bool(m and (m.group(1) or m.group(2))))
                ctx["list_x"][id(item)] = seg["x0"]
                blocks.append(item)
            continue
        joined = _join(seg["lines"])
        if joined:
            blocks.append(Paragraph(joined))
    return blocks


# ------------------------------------------------------------------ tables
def _box_text(box, ctx: dict) -> str:
    from pdfminer.layout import LTTextLine

    parts = []
    for line in box:
        if isinstance(line, LTTextLine) and id(line) not in ctx["fragments"]:
            info = _line(line, ctx["stats"], ctx["inserts"])
            if info:
                parts.append(info["text"])
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """How much two horizontal ranges overlap, as a share of the narrower one."""
    inter = min(a1, b1) - max(a0, b0)
    return inter / max(1.0, min(a1 - a0, b1 - b0)) if inter > 0 else 0.0


def _find_tables(boxes: list, ctx: dict) -> list[tuple[float, set[int], list[list[str]]]]:
    """Spot tables: short text boxes that line up in at least 2 rows x 2 columns.

    Cells in neighbouring rows belong to the same column when they overlap horizontally
    (so an indented cell, e.g. one with a bullet dot, still lines up).
    Returns [(top y, indexes of the boxes used, rows of cell text)]."""
    body = ctx["body"]
    tol = max(2.0, body * 0.6)
    cands = []
    for i, box in enumerate(boxes):
        text = _box_text(box, ctx)
        n_lines = sum(1 for _ in box)
        if text and n_lines <= 3 and len(text) <= 80:
            cands.append({"i": i, "x0": box.x0, "x1": box.x1, "y0": box.y0, "y1": box.y1, "text": text})
    rows: list[list[dict]] = []
    for cell in sorted(cands, key=lambda c: -c["y1"]):
        if rows and abs(rows[-1][0]["y1"] - cell["y1"]) <= tol:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    merged_rows = []
    for row in rows:  # cells of one row that overlap each other are one cell
        row = sorted(row, key=lambda c: c["x0"])
        out: list[dict] = []
        for cell in row:
            if out and _overlap(out[-1]["x0"], out[-1]["x1"], cell["x0"], cell["x1"]) > 0.3:
                last = out[-1]
                out[-1] = {**last, "x1": max(last["x1"], cell["x1"]), "y0": min(last["y0"], cell["y0"]),
                           "text": f"{last['text']} {cell['text']}", "ids": last.get("ids", [last["i"]]) + [cell["i"]]}
            else:
                out.append(cell)
        merged_rows.append(out)

    def aligned(a, b) -> bool:
        if len(a) < 2 or len(b) < 2 or abs(len(a) - len(b)) > 1:
            return False
        hits = sum(1 for c in b if any(_overlap(c["x0"], c["x1"], d["x0"], d["x1"]) >= 0.3 for d in a))
        gap = min(c["y0"] for c in a) - max(c["y1"] for c in b)
        return hits >= min(len(a), len(b)) and gap <= body * 2.6

    tables = []
    i = 0
    while i < len(merged_rows):
        j = i
        while j + 1 < len(merged_rows) and aligned(merged_rows[j], merged_rows[j + 1]):
            j += 1
        group = merged_rows[i : j + 1]
        if len(group) >= 2:
            columns = [(c["x0"], c["x1"]) for c in max(group, key=len)]
            table_rows = []
            used: set[int] = set()
            for row in group:
                cells = [""] * len(columns)
                for cell in row:
                    col = max(range(len(columns)), key=lambda k: (_overlap(*columns[k], cell["x0"], cell["x1"]), -abs(columns[k][0] - cell["x0"])))
                    cells[col] = (cells[col] + " " + cell["text"]).strip()
                    used.update(cell.get("ids", [cell["i"]]))
                table_rows.append(cells)
            tables.append((group[0][0]["y1"], used, table_rows))
        i = j + 1
    return tables


# ------------------------------------------------------------------ pictures
def _pdf_images(data: bytes) -> list[dict]:
    """page index -> {name: Image} using pypdf (handles JPEG, Flate, masks, CMYK...)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
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


# ------------------------------------------------------------------ page layout
def _collect(element, group: list, groups: list, images: list, marks: list) -> None:
    """Walk a page: text boxes (grouped per figure, in pdfminer's reading order), image
    positions and small drawn shapes that may be bullets."""
    from pdfminer.layout import LTCurve, LTFigure, LTImage, LTTextContainer

    if isinstance(element, LTTextContainer):
        group.append(element)
    elif isinstance(element, LTImage):
        images.append((element.name, element.y1))
    elif isinstance(element, LTFigure):
        inner: list = []
        groups.append(inner)
        for child in element:
            _collect(child, inner, groups, images, marks)
    elif isinstance(element, LTCurve):  # includes rectangles and lines
        w, h = element.width, element.height
        if 1.0 <= w <= 9 and 1.0 <= h <= 9 and max(w, h) <= min(w, h) * 1.8:
            marks.append((element.x0, element.x1, (element.y0 + element.y1) / 2))


def _merge_groups(groups: list[list]) -> list:
    """Each group (the page itself, or one figure) is already in reading order. Start from
    the biggest group and insert every other group as a whole at the point where its top
    edge falls - so a block of text in its own figure (a summary, a sidebar) stays together
    and lands in the right place."""
    groups = [g for g in groups if g]
    if not groups:
        return []
    groups.sort(key=len, reverse=True)
    out = list(groups[0])
    for group in sorted(groups[1:], key=lambda g: -max(b.y1 for b in g)):
        top = max(b.y1 for b in group)
        spot = next((i for i, b in enumerate(out) if b.y1 < top - 0.5), len(out))
        out[spot:spot] = group
    return out


def _set_list_levels(items: list[tuple[float, list]], list_x: dict, body: float) -> None:
    """Nesting from indentation, within each run of consecutive list items: an item
    indented further than the one before goes one level deeper."""
    stack: list[tuple[float, int]] = []
    prev_was_item = False
    for _y, blocks in items:
        for block in blocks:
            if not (isinstance(block, ListItem) and id(block) in list_x):
                prev_was_item = isinstance(block, ListItem)
                if not prev_was_item:
                    stack = []
                continue
            x = list_x[id(block)]
            if not prev_was_item or not stack:
                stack = [(x, 0)]
            elif x > stack[-1][0] + body * 0.9:
                stack.append((x, min(stack[-1][1] + 1, 3)))
            else:
                while len(stack) > 1 and x < stack[-1][0] - body * 0.9:
                    stack.pop()
            block.level = stack[-1][1]
            prev_was_item = True


def _read_with_pdfminer(data: bytes) -> Document:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTTextLine
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

    # all_texts=True: also lay out text inside figures (form XObjects).
    params = LAParams(line_margin=0.6, boxes_flow=0.5, all_texts=True)
    pages = []
    try:
        for i, page in enumerate(extract_pages(io.BytesIO(data), laparams=params)):
            if i >= MAX_PAGES:
                break
            pages.append(page)
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise ConvertError("This PDF is password-protected. Remove the password first, then try again.") from exc
    if not pages:
        raise ConvertError("This PDF has no pages.")

    contents = []  # per page: (boxes in reading order, image positions, bullet marks)
    for page in pages:
        top: list = []
        groups: list = [top]
        images: list = []
        marks: list = []
        for el in page:
            _collect(el, top, groups, images, marks)
        contents.append((_merge_groups(groups), images, marks))

    sizes: Counter = Counter()
    for boxes, _images, _marks in contents:
        for box in boxes:
            for line in box:
                if isinstance(line, LTTextLine):
                    for ch in line:
                        if isinstance(ch, LTChar) and ch.get_text().strip():
                            sizes[round(ch.size, 1)] += 1
    body = sizes.most_common(1)[0][0] if sizes else 11.0

    try:
        page_images = _pdf_images(data)
    except Exception:  # noqa: BLE001 - pictures are a bonus; never fail the conversion for them
        page_images = []

    doc = Document()
    stats: Counter = Counter()
    for index, page in enumerate(pages):
        if index:
            doc.add(PageBreak())
        height = page.height or 842
        all_boxes, positions, marks = contents[index]
        boxes = []
        for el in all_boxes:
            text = el.get_text().strip()
            in_margin = el.y0 > height * 0.92 or el.y1 < height * 0.08
            if text and not (in_margin and PAGE_NUMBER.match(text)):  # drop page numbers
                boxes.append(el)

        inserts, fragments = _superscripts(boxes, body)
        ctx = {"body": body, "marks": marks, "list_x": {}, "stats": stats, "inserts": inserts, "fragments": fragments}
        items: list[tuple[float, list]] = []  # (top y, blocks), in reading order
        tables = _find_tables(boxes, ctx)
        in_table = set().union(*(used for _y, used, _rows in tables)) if tables else set()
        placed: set[int] = set()
        last_item = None  # (list item, its box) - for lines that continue a bullet point
        for index_box, box in enumerate(boxes):
            if index_box in in_table:
                for t, (top, used, rows) in enumerate(tables):
                    if index_box in used and t not in placed:
                        placed.add(t)
                        items.append((top, [Table(rows, header=True)]))
                last_item = None
                continue
            blocks = _box_blocks(box, ctx)
            if not blocks:
                continue
            # A plain line right under a bullet point, starting where its text starts, is
            # the rest of that bullet point (e.g. a second line in its own text box).
            if (
                last_item is not None
                and len(blocks) == 1
                and type(blocks[0]) is Paragraph
                and blocks[0].style == "normal"
                and abs(box.x0 - ctx["list_x"].get(id(last_item[0]), -99)) <= 3
                and 0 <= last_item[1].y0 - box.y1 <= body * 1.2
            ):
                last_item[0].runs = _join([last_item[0].runs, blocks[0].runs])
                last_item = (last_item[0], box)
                continue
            last = blocks[-1]
            last_item = (last, box) if isinstance(last, ListItem) else None
            items.append((box.y1, blocks))

        _set_list_levels(items, ctx["list_x"], body)

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

    if stats["cid"]:
        doc.note("Some characters use a font without a text map, so they couldn't be read and were left out.")
    if len(pages) >= MAX_PAGES:
        doc.note(f"Only the first {MAX_PAGES} pages were converted.")
    return doc


# ------------------------------------------------------------------ fallback
def _read_with_pypdf(data: bytes) -> Document:
    """A simpler reader: pypdf's text extraction, one paragraph per block of lines."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ConvertError("This PDF is password-protected. Remove the password first, then try again.")
    doc = Document()
    for index, page in enumerate(reader.pages[:MAX_PAGES]):
        if index:
            doc.add(PageBreak())
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - skip a page pypdf can't read rather than give up
            continue
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                doc.add(Paragraph([Run(re.sub(r"\s+", " ", " ".join(paragraph)).strip())]))
                paragraph.clear()

        for raw in text.splitlines():
            line = CID.sub("", raw).strip()
            if not line:
                flush()
                continue
            if PAGE_NUMBER.match(line):
                continue
            m = BULLET.match(line)
            if m:
                flush()
                doc.add(ListItem([Run(line[m.end():].strip())], ordered=bool(m.group(1) or m.group(2))))
                continue
            paragraph.append(line)
            if line.endswith(SENTENCE_END):
                flush()
        flush()
    try:
        for found in _pdf_images(data):
            for img in found.values():
                doc.add(img)
    except Exception:  # noqa: BLE001
        pass
    return doc


def read_pdf(data: bytes) -> Document:
    try:
        doc = _read_with_pdfminer(data)
    except ConvertError as exc:
        if "password" in str(exc):
            raise
        doc = None
    except Exception:  # noqa: BLE001 - pdfminer can choke on unusual or damaged files
        doc = None

    if doc is None or not doc.has_text():
        try:
            simple = _read_with_pypdf(data)
        except ConvertError:
            raise
        except Exception:  # noqa: BLE001
            simple = None
        if simple is not None and simple.has_text():
            simple.note("This PDF was read with a simpler method, so some headings and tables may be plain text.")
            return simple
        if doc is None and simple is None:
            raise ConvertError("This PDF couldn't be read. It may be damaged or use an unusual format.")
        doc = doc if doc is not None else simple

    if not doc.has_text():
        if not doc.images():
            raise ConvertError(NO_TEXT, status=422)
        doc.note("This PDF has no selectable text (it looks like a scan), so only its pictures were kept. "
                 "Use an OCR tool to turn scanned pages into editable text.")
    return doc

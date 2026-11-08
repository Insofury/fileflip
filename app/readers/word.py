"""Word (.docx, .docm, .dotx) -> Document.

The main reader uses python-docx and walks the document body in order, keeping headings,
bold / italic / underline, links, bullet and numbered lists, alignment, tables, pictures
and page breaks. It also reads text that python-docx's simple API skips:

* content controls (<w:sdt>, the "click here to enter text" fields in many templates);
* text boxes (<w:txbxContent>, common for CV sidebars);
* tracked insertions (<w:ins>), smart tags and simple fields;
* equations (<m:oMath>), as plain text.

If python-docx can't open the file at all (macro-enabled .docm, templates, slightly
damaged files), a fallback reads the XML directly with the standard library.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, normalize_image, strip_runs

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
STYLE_MAP = {"title": "title", "subtitle": "h2", "heading 1": "h1", "heading 2": "h2", "quote": "quote", "intense quote": "quote"}
ALIGN = {1: "center", 2: "right", 3: "justify"}
CONTAINERS = {W + "ins", W + "smartTag", W + "customXml", W + "fldSimple", W + "bdo", W + "dir"}
MONO_FONTS = ("consolas", "courier new", "courier", "menlo", "monaco")


def _style(paragraph) -> str:
    try:
        name = (paragraph.style.name if paragraph.style is not None else "").lower()
    except Exception:  # noqa: BLE001 - a broken style reference just means "normal"
        return "normal"
    if name in STYLE_MAP:
        return STYLE_MAP[name]
    if name.startswith("heading"):
        return "h3"
    return "normal"


def _list_info(paragraph, numbering) -> tuple[bool, bool, int]:
    """(is a list item, ordered, level) from the paragraph's numbering or its style."""
    try:
        style = (paragraph.style.name if paragraph.style is not None else "").lower()
    except Exception:  # noqa: BLE001
        style = ""
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
        abstract = numbering.xpath(f'./w:num[@w:numId="{num_id}"]/w:abstractNumId/@w:val')
        if abstract:
            fmts = numbering.xpath(f'./w:abstractNum[@w:abstractNumId="{abstract[0]}"]/w:lvl[@w:ilvl="{level}"]/w:numFmt/@w:val')
            if fmts:
                ordered = fmts[0] not in ("bullet", "none")
    return True, ordered, min(int(level), 3)


def _level_from_style(style: str) -> int:
    last = style.rsplit(" ", 1)[-1]
    return min(int(last) - 1, 3) if last.isdigit() else 0


def _run_format(run, paragraph) -> dict:
    try:
        style_font = paragraph.style.font if paragraph.style is not None else None
    except Exception:  # noqa: BLE001
        style_font = None

    def flag(value, style_value):
        return bool(value if value is not None else style_value)

    return {
        "bold": flag(run.bold, style_font.bold if style_font else None),
        "italic": flag(run.italic, style_font.italic if style_font else None),
        "underline": bool(run.underline),
        "code": (run.font.name or "").lower() in MONO_FONTS,
    }


def _link_url(element, part) -> str | None:
    rid = element.get(R + "id")
    if not rid:
        return None
    try:
        rel = part.rels[rid]
        return rel.target_ref if rel.is_external else None
    except Exception:  # noqa: BLE001
        return None


def _runs(element, paragraph, part, link: str | None = None) -> list[Run]:
    """All the text runs inside a paragraph (or inside a container within it), in order."""
    from docx.text.run import Run as DocxRun

    out: list[Run] = []
    for child in element.iterchildren():
        tag = child.tag
        if tag == W + "r":
            r = DocxRun(child, paragraph)
            out.append(Run(r.text, **_run_format(r, paragraph), link=link))
        elif tag == W + "hyperlink":
            out += _runs(child, paragraph, part, _link_url(child, part) or link)
        elif tag in CONTAINERS:
            out += _runs(child, paragraph, part, link)
        elif tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                out += _runs(content, paragraph, part, link)
        elif tag in (M + "oMath", M + "oMathPara"):
            text = "".join(t.text or "" for t in child.iter(M + "t"))
            if text:
                out.append(Run(text, italic=True))
        # w:del (deleted text in tracked changes) and properties are skipped on purpose
    return out


def _text_boxes(element) -> list:
    """<w:txbxContent> elements inside a paragraph, skipping the duplicate copies Word
    keeps in <mc:Fallback> for old versions."""
    found = []
    for box in element.iter(W + "txbxContent"):
        parent, fallback = box.getparent(), False
        while parent is not None:
            if parent.tag == MC_FALLBACK:
                fallback = True
                break
            parent = parent.getparent()
        if not fallback:
            found.append(box)
    return found


class _Reader:
    def __init__(self, source):
        self.source = source
        self.part = source.part
        try:
            self.numbering = self.part.numbering_part.element
        except Exception:  # noqa: BLE001 - documents without (or with broken) numbering
            self.numbering = None
        self.doc = Document(title=(source.core_properties.title or "").strip())
        self.skipped_images = 0

    def walk(self, elements) -> None:
        for child in elements:
            tag = child.tag
            if tag == W + "p":
                self.paragraph(child)
            elif tag == W + "tbl":
                self.table(child)
            elif tag == W + "sdt":
                content = child.find(W + "sdtContent")
                if content is not None:
                    self.walk(content.iterchildren())
            elif tag in (W + "customXml", W + "ins"):
                self.walk(child.iterchildren())

    def table(self, element) -> None:
        from docx.table import Table as DocxTable

        try:
            table = DocxTable(element, self.source)
            rows = []
            for row in table.rows:
                cells, previous = [], None
                for cell in row.cells:
                    # python-docx repeats merged cells; keep the text once
                    cells.append("" if cell._tc is previous else cell.text.strip())
                    previous = cell._tc
                rows.append(cells)
        except Exception:  # noqa: BLE001 - unusual tables: read the cell text directly
            rows = [
                ["".join(t.text or "" for t in tc.iter(W + "t")).strip() for tc in tr.iter(W + "tc")]
                for tr in element.iter(W + "tr")
            ]
        rows = [r for r in rows if r]
        if rows:
            width = max(len(r) for r in rows)
            self.doc.add(Table([r + [""] * (width - len(r)) for r in rows]))

    def paragraph(self, element) -> None:
        from docx.text.paragraph import Paragraph as DocxParagraph

        paragraph = DocxParagraph(element, self.source)
        try:
            run_list = _runs(element, paragraph, self.part)
        except Exception:  # noqa: BLE001 - fall back to the paragraph's plain text
            run_list = [Run("".join(t.text or "" for t in element.iter(W + "t")))]

        is_list, ordered, level = _list_info(paragraph, self.numbering)
        clean = strip_runs(run_list)
        if clean:
            if is_list:
                self.doc.add(ListItem(clean, ordered=ordered, level=level))
            else:
                try:
                    align = ALIGN.get(int(paragraph.alignment)) if paragraph.alignment is not None else None
                except (TypeError, ValueError):
                    align = None
                self.doc.add(Paragraph(clean, style=_style(paragraph), align=align or "left"))

        for box in _text_boxes(element):
            self.walk(box.iterchildren())
        for rid in element.xpath(".//a:blip/@r:embed"):
            try:
                img = normalize_image(self.part.related_parts[rid].blob)
            except Exception:  # noqa: BLE001
                img = None
            if img is None:
                self.skipped_images += 1
            else:
                self.doc.add(img)
        if element.xpath('.//w:br[@w:type="page"]'):
            self.doc.add(PageBreak())


def _read_with_python_docx(data: bytes) -> Document:
    import docx

    source = docx.Document(io.BytesIO(data))
    reader = _Reader(source)
    reader.walk(source.element.body.iterchildren())
    if reader.skipped_images:
        reader.doc.note(f"{reader.skipped_images} drawing(s) in a format FileFlip can't read (like EMF/WMF) were left out.")
    return reader.doc


# ------------------------------------------------------------------ fallback
def _main_part(zf: zipfile.ZipFile) -> str:
    """Find the main document XML through [Content_Types].xml (works for .docm/.dotx)."""
    try:
        types = ET.fromstring(zf.read("[Content_Types].xml"))
        for override in types:
            if "wordprocessingml" in override.get("ContentType", "") and ".main+xml" in override.get("ContentType", ""):
                return override.get("PartName", "").lstrip("/")
            if override.get("ContentType", "").endswith(("document.main+xml", "template.main+xml")):
                return override.get("PartName", "").lstrip("/")
    except (KeyError, ET.ParseError):
        pass
    return "word/document.xml"


def _xml_paragraph(p) -> tuple[str, str, bool]:
    """(text, style id, has numbering) for a raw <w:p>."""
    parts = []
    for node in p.iter():
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
        elif node.tag == M + "t" and node.text:
            parts.append(node.text)
    style = p.find(f"{W}pPr/{W}pStyle")
    numbered = p.find(f"{W}pPr/{W}numPr") is not None
    return "".join(parts), (style.get(W + "val") if style is not None else "") or "", numbered


def _read_with_xml(data: bytes) -> Document:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        root = ET.fromstring(zf.read(_main_part(zf)))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ConvertError("This Word file couldn't be opened. It may be damaged or password-protected.") from exc
    body = root.find(W + "body")
    if body is None:
        raise ConvertError("This Word file couldn't be opened. It may be damaged.")
    doc = Document()

    def walk(elements) -> None:
        for el in elements:
            if el.tag == W + "p":
                text, style, numbered = _xml_paragraph(el)
                text = text.strip()
                if text:
                    s = style.lower().replace(" ", "")
                    if numbered or s.startswith("listbullet") or s.startswith("listparagraph"):
                        doc.add(ListItem([Run(text)], ordered=s.startswith("listnumber")))
                    elif s == "title":
                        doc.add(Paragraph([Run(text)], style="title"))
                    elif re.fullmatch(r"heading[1-9]", s):
                        doc.add(Paragraph([Run(text)], style={"1": "h1", "2": "h2"}.get(s[-1], "h3")))
                    else:
                        doc.add(Paragraph([Run(text)]))
                if any(br.get(W + "type") == "page" for br in el.iter(W + "br")):
                    doc.add(PageBreak())
            elif el.tag == W + "tbl":
                rows = []
                for tr in el.iter(W + "tr"):
                    rows.append([_xml_paragraph_text(tc) for tc in tr.findall(W + "tc")])
                rows = [r for r in rows if r]
                if rows:
                    width = max(len(r) for r in rows)
                    doc.add(Table([r + [""] * (width - len(r)) for r in rows]))
            elif el.tag in (W + "sdt",):
                content = el.find(W + "sdtContent")
                if content is not None:
                    walk(list(content))
            elif el.tag in (W + "customXml", W + "ins"):
                walk(list(el))

    walk(list(body))
    return doc


def _xml_paragraph_text(tc) -> str:
    return " ".join(_xml_paragraph(p)[0].strip() for p in tc.iter(W + "p")).strip()


def read_docx(data: bytes) -> Document:
    try:
        doc = _read_with_python_docx(data)
    except Exception:  # noqa: BLE001 - e.g. .docm/.dotx content types or odd XML: read it directly
        doc = _read_with_xml(data)
        doc.note("This file was read in a simpler way, so some formatting and pictures may be missing.")
    if not doc.blocks:
        raise ConvertError("This Word document is empty.")
    return doc

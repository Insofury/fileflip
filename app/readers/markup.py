"""Plain text, Markdown and HTML -> Document."""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

from ..model import ConvertError, Document, ListItem, PageBreak, Paragraph, Run, Table, normalize_image, strip_runs

BULLET = re.compile(r"^\s*(?:[-*•●▪◦‣]|(\d{1,3})[.)])\s+")


def decode_text(data: bytes) -> str:
    """Bytes -> str: UTF-8 (with or without BOM), UTF-16 with BOM, else Windows-1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# ------------------------------------------------------------------ text
def read_txt(data: bytes) -> Document:
    text = decode_text(data).replace("\r\n", "\n").replace("\r", "\n")
    doc = Document()
    for chunk in re.split(r"\n\s*\n", text):
        if "\f" in chunk:  # form feed = page break in many text exports
            parts = chunk.split("\f")
            for i, part in enumerate(parts):
                _text_chunk(doc, part)
                if i < len(parts) - 1:
                    doc.add(PageBreak())
            continue
        _text_chunk(doc, chunk)
    return doc


def _text_chunk(doc: Document, chunk: str) -> None:
    lines = [line.rstrip() for line in chunk.split("\n") if line.strip()]
    if not lines:
        return
    if all(BULLET.match(line) for line in lines):
        for line in lines:
            m = BULLET.match(line)
            indent = (len(line) - len(line.lstrip())) // 2
            doc.add(ListItem([Run(line[m.end():].strip())], ordered=bool(m.group(1)), level=min(indent, 3)))
        return
    doc.add(Paragraph([Run("\n".join(line.strip() for line in lines))]))


# ------------------------------------------------------------------ markdown
NESTED_ITEM = re.compile(r"^( +)(?=(?:[-*+]|\d+[.)])\s)", re.M)


def read_markdown(data: bytes) -> Document:
    import markdown

    # Python-Markdown wants 4 spaces for a nested list; most people (and GitHub) use 2.
    text = NESTED_ITEM.sub(lambda m: " " * (len(m.group(1)) * 2 if len(m.group(1)) % 4 else len(m.group(1))), decode_text(data))
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    return read_html(html.encode("utf-8"))


# ------------------------------------------------------------------ html
HEADINGS = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h3", "h5": "h3", "h6": "h3"}
BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "figure", "figcaption", "address"}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object", "button", "select", "textarea"}


class _HTMLToDoc(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = Document()
        self.runs: list[Run] = []
        self.style = "normal"
        self.align = "left"
        self.fmt = {"bold": 0, "italic": 0, "underline": 0, "code": 0}
        self.links: list[str | None] = []
        self.lists: list[dict] = []  # stack of {"ordered": bool}
        self.in_li = False
        self.skip = 0
        self.pre = 0
        self.in_title = False
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.header_row = False
        self.skipped_images = 0

    # -------------------------------------------------- helpers
    def flush(self) -> None:
        run_list = self.runs if self.pre else strip_runs(self.runs)
        if self.pre and run_list:  # trim the blank lines around a code block, keep its indentation
            run_list = list(run_list)
            run_list[0] = Run(run_list[0].text.lstrip("\n"), code=True)
            run_list[-1] = Run(run_list[-1].text.rstrip(), code=True)
        self.runs = []
        if not run_list or not "".join(r.text for r in run_list).strip():
            return
        if self.in_li and self.lists:
            top = self.lists[-1]
            self.doc.add(ListItem(run_list, ordered=top["ordered"], level=min(len(self.lists) - 1, 3)))
        else:
            self.doc.add(Paragraph(run_list, style="code" if self.pre else self.style, align=self.align))

    def add_text(self, text: str) -> None:
        if self.cell is not None:
            self.cell.append(text)
            return
        if not self.pre:
            text = re.sub(r"\s+", " ", text)
            if not self.runs or self.runs[-1].text.endswith((" ", "\n")):
                text = text.lstrip(" ")
        if not text:
            return
        link = self.links[-1] if self.links else None
        self.runs.append(
            Run(
                text,
                bold=self.fmt["bold"] > 0 or self.style in ("h1", "h2", "h3", "title"),
                italic=self.fmt["italic"] > 0,
                underline=self.fmt["underline"] > 0,
                code=self.fmt["code"] > 0 or self.pre > 0,
                link=link,
            )
        )

    # -------------------------------------------------- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        elif tag in HEADINGS:
            self.flush()
            self.style = HEADINGS[tag]
        elif tag in BLOCK_TAGS or tag in ("blockquote", "pre", "hr"):
            self.flush()
            if tag == "blockquote":
                self.style = "quote"
            if tag == "pre":
                self.pre += 1
            align = (a.get("align") or "").lower() or _css_align(a.get("style") or "")
            if align in ("center", "right", "justify"):
                self.align = align
        elif tag == "br":
            if self.cell is not None:
                self.cell.append(" ")
            else:
                self.runs.append(Run("\n"))
        elif tag in ("b", "strong"):
            self.fmt["bold"] += 1
        elif tag in ("i", "em", "cite"):
            self.fmt["italic"] += 1
        elif tag in ("u", "ins"):
            self.fmt["underline"] += 1
        elif tag in ("code", "kbd", "samp", "tt"):
            self.fmt["code"] += 1
        elif tag == "a":
            href = a.get("href") or ""
            self.links.append(href if href.startswith(("http://", "https://", "mailto:")) else None)
        elif tag in ("ul", "ol"):
            self.flush()
            self.lists.append({"ordered": tag == "ol"})
        elif tag == "li":
            self.flush()
            self.in_li = True
        elif tag == "table":
            self.flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
            self.header_row = False
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            if tag == "th":
                self.header_row = True
        elif tag == "img":
            self.image(a.get("src") or "", a.get("alt") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
        elif tag in HEADINGS:
            self.flush()
            self.style = "normal"
        elif tag in BLOCK_TAGS or tag in ("blockquote", "pre"):
            self.flush()
            if tag == "blockquote":
                self.style = "normal"
            if tag == "pre":
                self.pre = max(0, self.pre - 1)
            self.align = "left"
        elif tag in ("b", "strong"):
            self.fmt["bold"] = max(0, self.fmt["bold"] - 1)
        elif tag in ("i", "em", "cite"):
            self.fmt["italic"] = max(0, self.fmt["italic"] - 1)
        elif tag in ("u", "ins"):
            self.fmt["underline"] = max(0, self.fmt["underline"] - 1)
        elif tag in ("code", "kbd", "samp", "tt"):
            self.fmt["code"] = max(0, self.fmt["code"] - 1)
        elif tag == "a":
            if self.links:
                self.links.pop()
        elif tag == "li":
            self.flush()
            self.in_li = False
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
            self.in_li = bool(self.lists)
        elif tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.table is not None and self.row is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table:
                width = max(len(r) for r in self.table)
                rows = [r + [""] * (width - len(r)) for r in self.table]
                self.doc.add(Table(rows, header=True))
            self.table = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.in_title:
            self.doc.title = (self.doc.title + data).strip()
            return
        self.add_text(data)

    def image(self, src: str, alt: str) -> None:
        m = re.match(r"data:image/[\w.+-]+;base64,(.*)", src, re.S)
        if not m:
            if src:
                self.skipped_images += 1
            return
        try:
            img = normalize_image(base64.b64decode(m.group(1)), alt)
        except ValueError:
            img = None
        if img is None:
            self.skipped_images += 1
            return
        self.flush()
        self.doc.add(img)


def _css_align(style: str) -> str:
    m = re.search(r"text-align\s*:\s*(\w+)", style, re.I)
    return m.group(1).lower() if m else ""


def read_html(data: bytes) -> Document:
    parser = _HTMLToDoc()
    parser.feed(decode_text(data))
    parser.close()
    parser.flush()
    doc = parser.doc
    if parser.skipped_images:
        doc.note(f"{parser.skipped_images} linked image(s) were left out (only images embedded in the file can be converted).")
    if not doc.blocks:
        raise ConvertError("This file has no text to convert.")
    return doc

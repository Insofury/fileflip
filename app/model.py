"""The document model every conversion goes through.

Readers turn a file (PDF, Word, slides, spreadsheet, text, ...) into a `Document`: a flat
list of blocks. Writers turn a `Document` into a file. With N readers and M writers this
gives N x M conversions without writing each pair by hand:

    report.pdf  --read_pdf-->  Document  --write_docx-->  report.docx
                                         --write_txt--->  report.txt
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Union


class ConvertError(ValueError):
    """A problem with the user's file or request. The message is shown to the user as-is;
    `status` is the HTTP status the API returns."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Run:
    """A piece of text with one formatting. Text may contain "\\n" (line break) and "\\t"."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    code: bool = False
    link: str | None = None

    def same_format(self, other: Run) -> bool:
        return (self.bold, self.italic, self.underline, self.code, self.link) == (
            other.bold, other.italic, other.underline, other.code, other.link,
        )


@dataclass
class Paragraph:
    runs: list[Run]
    style: str = "normal"  # normal | title | h1 | h2 | h3 | quote | code
    align: str = "left"  # left | center | right | justify


@dataclass
class ListItem:
    runs: list[Run]
    ordered: bool = False
    level: int = 0  # 0 = top level


@dataclass
class Table:
    rows: list[list[str]]
    header: bool = True  # treat the first row as column headings
    name: str = ""  # e.g. the spreadsheet tab


@dataclass
class Image:
    data: bytes  # PNG or JPEG
    mime: str  # image/png | image/jpeg
    width: int  # pixels
    height: int
    name: str = ""


@dataclass
class PageBreak:
    pass


Block = Union[Paragraph, ListItem, Table, Image, PageBreak]


@dataclass
class Document:
    blocks: list[Block] = field(default_factory=list)
    title: str = ""
    notes: list[str] = field(default_factory=list)  # things the user should know (shown in the UI)

    def add(self, block: Block) -> None:
        self.blocks.append(block)

    def tables(self) -> list[Table]:
        return [b for b in self.blocks if isinstance(b, Table)]

    def images(self) -> list[Image]:
        return [b for b in self.blocks if isinstance(b, Image)]

    def has_text(self) -> bool:
        return any(
            (isinstance(b, (Paragraph, ListItem)) and text_of(b.runs).strip())
            or (isinstance(b, Table) and any(c.strip() for row in b.rows for c in row))
            for b in self.blocks
        )

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)


# ------------------------------------------------------------------ helpers
def runs(text: str, **fmt) -> list[Run]:
    return [Run(text, **fmt)] if text else []


def text_of(run_list: list[Run]) -> str:
    return "".join(r.text for r in run_list)


def merge_runs(run_list: list[Run]) -> list[Run]:
    """Join neighbouring runs that have the same formatting, and drop empty ones."""
    out: list[Run] = []
    for r in run_list:
        if not r.text:
            continue
        if out and out[-1].same_format(r):
            out[-1] = Run(out[-1].text + r.text, r.bold, r.italic, r.underline, r.code, r.link)
        else:
            out.append(r)
    return out


def strip_runs(run_list: list[Run]) -> list[Run]:
    """Trim whitespace at the start and end of a paragraph."""
    run_list = merge_runs(run_list)
    if run_list:
        first, last = run_list[0], run_list[-1]
        run_list[0] = Run(first.text.lstrip(), first.bold, first.italic, first.underline, first.code, first.link)
        last = run_list[-1]
        run_list[-1] = Run(last.text.rstrip(), last.bold, last.italic, last.underline, last.code, last.link)
    return [r for r in run_list if r.text]




# Characters Word/Excel/XML can't store: C0 controls (except tab and newline), lone
# surrogates and the "non-characters" U+FFFE/U+FFFF.
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
_ZERO_WIDTH = re.compile("[​‌‎‏⁠﻿­]")  # invisible; break PDF fonts


def clean_text(text: str) -> str:
    """Make any text safe for every writer: normalise line breaks, turn vertical tabs
    and form feeds into line breaks, drop invisible and invalid characters."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = _INVALID_XML.sub("", text)
    return _ZERO_WIDTH.sub("", text)


def sanitize(doc: Document) -> Document:
    """Clean every piece of text in a document (called once after reading)."""
    for block in doc.blocks:
        if isinstance(block, (Paragraph, ListItem)):
            block.runs = [
                Run(clean_text(r.text), r.bold, r.italic, r.underline, r.code, (clean_text(r.link) or None) if r.link else None)
                for r in block.runs
            ]
            block.runs = [r for r in block.runs if r.text]
        elif isinstance(block, Table):
            block.rows = [[clean_text(str(c)) for c in row] for row in block.rows]
            width = max((len(r) for r in block.rows), default=0)
            block.rows = [r + [""] * (width - len(r)) for r in block.rows if width]
            block.name = clean_text(block.name)
    doc.blocks = [
        b for b in doc.blocks
        if not (isinstance(b, (Paragraph, ListItem)) and not b.runs) and not (isinstance(b, Table) and not b.rows)
    ]
    doc.title = clean_text(doc.title).replace("\n", " ").strip()
    return doc


MAX_IMAGE_SIDE = 3000  # keep embedded images a sensible size
MAX_SOURCE_PIXELS = 120_000_000  # refuse to decode absurdly large images (memory)


def open_image_frames(data: bytes, all_frames: bool = False) -> list:
    """Decode an image with Pillow, upright (phone photos store their rotation in EXIF).
    Multi-page TIFFs (scans) give one image per page when `all_frames` is set.
    Returns [] if the data isn't an image Pillow can read."""
    from PIL import Image as PILImage
    from PIL import ImageOps, ImageSequence

    try:
        img = PILImage.open(io.BytesIO(data))
        if img.width * img.height > MAX_SOURCE_PIXELS:
            return []
        frames = []
        sequence = ImageSequence.Iterator(img) if all_frames and (img.format or "").upper() == "TIFF" else [img]
        for frame in sequence:
            frame.load()
            frames.append(ImageOps.exif_transpose(frame.copy()) or frame.copy())
        return frames
    except Exception:  # noqa: BLE001 - unreadable or damaged images are reported by the caller
        return []


def _to_8bit(img):
    """16/32-bit greyscale (scientific PNGs, some scans) -> 8-bit, keeping the contrast."""
    if img.mode.startswith("I") or img.mode == "F":
        img = img.convert("I") if img.mode != "F" else img
        lo, hi = img.getextrema()
        scale = 255 / (hi - lo) if hi > lo else 1
        return img.point(lambda v: (v - lo) * scale).convert("L")
    return img


def encode_image(img, name: str = "", original: bytes | None = None, fmt: str = "") -> Image:
    """A decoded Pillow image -> PNG or JPEG bytes ready for Word and PDF."""
    fmt = fmt.upper()
    if (  # already a small, standard PNG/JPEG: keep the original bytes untouched
        original is not None
        and fmt in ("PNG", "JPEG")
        and max(img.size) <= MAX_IMAGE_SIDE
        and img.mode in ("RGB", "RGBA", "L", "P", "LA")
    ):
        return Image(data=original, mime=f"image/{fmt.lower()}", width=img.width, height=img.height, name=name)
    img = _to_8bit(img)
    if max(img.size) > MAX_IMAGE_SIDE:
        img = img.copy()
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    buf = io.BytesIO()
    if has_alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=img.width * img.height <= 2_500_000)
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        mime = "image/jpeg"
    return Image(data=buf.getvalue(), mime=mime, width=img.width, height=img.height, name=name)


def normalize_image(data: bytes, name: str = "") -> Image | None:
    """Any image Pillow can read -> PNG or JPEG bytes ready for Word and PDF.
    Returns None for formats Pillow can't read (e.g. EMF/WMF drawings)."""
    from PIL import Image as PILImage

    frames = open_image_frames(data)
    if not frames:
        return None
    try:
        raw = PILImage.open(io.BytesIO(data))
        fmt = raw.format or ""
        rotated = raw.getexif().get(0x0112, 1) not in (None, 1)  # then re-encode the upright copy
    except Exception:  # noqa: BLE001
        fmt, rotated = "", True
    try:
        return encode_image(frames[0], name, None if rotated else data, fmt)
    except Exception:  # noqa: BLE001 - an image we can't re-encode is skipped with a note
        return None

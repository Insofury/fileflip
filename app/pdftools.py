"""PDF tools with pypdf: merge, split, rotate and compress. Plus images -> one PDF (fpdf2)."""

from __future__ import annotations

import io
import re

from .model import ConvertError

COMPRESS_LEVELS = {  # level -> (JPEG quality, longest image side in px)
    "low": (85, 2400),
    "medium": (70, 1800),
    "high": (50, 1200),
}


def _reader(data: bytes, name: str = "this PDF"):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ConvertError(f"{name} is password-protected. Remove the password first.")
        _ = len(reader.pages)
    except ConvertError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise ConvertError(f"{name} couldn't be read. It may be damaged.") from exc
    return reader


def _bytes(writer) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def parse_ranges(spec: str, count: int) -> list[list[int]]:
    """'1-3, 5, 8-' -> [[0,1,2], [4], [7..count-1]] (0-based). 'all' or '' -> every page."""
    spec = (spec or "").strip().lower()
    if spec in ("", "all"):
        return [list(range(count))]
    groups = []
    for part in re.split(r"[,;]+", spec):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ConvertError(f"'{part}' isn't a page range. Use something like 1-3, 5, 8-.")
        if m.group(3):
            start = end = int(m.group(3))
        else:
            start = int(m.group(1)) if m.group(1) else 1
            end = int(m.group(2)) if m.group(2) else count
        if not (1 <= start <= count and 1 <= end <= count):
            raise ConvertError(f"Page range '{part}' is outside this PDF (it has {count} pages).")
        step = 1 if end >= start else -1
        groups.append(list(range(start - 1, end - 1 + step, step)))
    if not groups:
        raise ConvertError("Enter the pages to use, like 1-3, 5.")
    return groups


def merge(files: list[tuple[str, bytes]]) -> bytes:
    from pypdf import PdfWriter

    if len(files) < 2:
        raise ConvertError("Add at least two PDFs to merge.")
    writer = PdfWriter()
    for name, data in files:
        reader = _reader(data, name)
        for page in reader.pages:
            writer.add_page(page)
    return _bytes(writer)


def split(data: bytes, name: str, mode: str = "each", ranges: str = "") -> list[tuple[str, bytes]]:
    """mode 'each': one PDF per page. mode 'ranges': one PDF per comma-separated range."""
    from pypdf import PdfWriter

    reader = _reader(data, name)
    count = len(reader.pages)
    groups = [[i] for i in range(count)] if mode == "each" else parse_ranges(ranges, count)
    out = []
    for group in groups:
        writer = PdfWriter()
        for i in group:
            writer.add_page(reader.pages[i])
        label = f"page-{group[0] + 1}" if len(group) == 1 else f"pages-{group[0] + 1}-{group[-1] + 1}"
        out.append((label, _bytes(writer)))
    return out


def rotate(data: bytes, name: str, angle: int = 90, pages: str = "all") -> bytes:
    from pypdf import PdfWriter

    if angle not in (90, 180, 270):
        raise ConvertError("Rotate by 90, 180 or 270 degrees.")
    reader = _reader(data, name)
    chosen = {i for group in parse_ranges(pages, len(reader.pages)) for i in group}
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        writer.add_page(page)
        if i in chosen:
            writer.pages[i].rotate(angle)
    return _bytes(writer)


def compress(data: bytes, name: str, level: str = "medium") -> tuple[bytes, list[str]]:
    """Recompress big pictures as JPEG, compress page content and drop duplicate objects."""
    from pypdf import PdfWriter

    quality, max_side = COMPRESS_LEVELS.get(level, COMPRESS_LEVELS["medium"])
    reader = _reader(data, name)
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        try:
            images = list(page.images)
        except Exception:  # noqa: BLE001
            images = []
        for img in images:
            try:
                pil = img.image
                if pil is None or pil.mode not in ("RGB", "L", "CMYK") or max(pil.size) < 300:
                    continue  # keep transparency and small images as they are
                if max(pil.size) > max_side:
                    pil.thumbnail((max_side, max_side))
                img.replace(pil.convert("RGB") if pil.mode == "CMYK" else pil, quality=quality)
            except Exception:  # noqa: BLE001 - leave any picture we can't recompress untouched
                continue
        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001
            pass
    writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    result = _bytes(writer)
    if len(result) >= len(data):
        return data, ["This PDF is already well optimised, so it was kept as it was."]
    saved = round((1 - len(result) / len(data)) * 100)
    return result, [f"{saved}% smaller."]


def images_to_pdf(files: list[tuple[str, bytes]], page_size: str = "A4") -> bytes:
    """Each image on its own page (portrait or landscape to match the picture), centred."""
    from .compat import fpdf
    from .model import normalize_image

    FPDF = fpdf()

    if not files:
        raise ConvertError("Add some images first.")
    pdf = FPDF(format="Letter" if page_size == "Letter" else "A4", unit="mm")
    pdf.set_auto_page_break(False)
    pdf.set_creator("FileFlip")
    margin = 10
    for name, data in files:
        img = normalize_image(data, name)
        if img is None:
            raise ConvertError(f"{name} couldn't be read as an image.")
        pdf.add_page(orientation="L" if img.width > img.height else "P")
        max_w, max_h = pdf.w - 2 * margin, pdf.h - 2 * margin
        scale = min(max_w / img.width, max_h / img.height)
        w, h = img.width * scale, img.height * scale
        pdf.image(io.BytesIO(img.data), x=(pdf.w - w) / 2, y=(pdf.h - h) / 2, w=w, h=h)
    return bytes(pdf.output())

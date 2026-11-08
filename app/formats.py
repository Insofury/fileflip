"""Which files we can read, what each can become, and how to recognise them."""

from __future__ import annotations

import io
import zipfile

from .model import ConvertError

# kind -> human name
KINDS = {
    "pdf": "PDF",
    "docx": "Word",
    "pptx": "PowerPoint",
    "xlsx": "Excel",
    "csv": "CSV",
    "txt": "Text",
    "md": "Markdown",
    "html": "HTML",
    "image": "Image",
}

# output format -> (label, extension, MIME type)
OUTPUTS = {
    "pdf": ("PDF", "pdf", "application/pdf"),
    "docx": ("Word", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xlsx": ("Excel", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "csv": ("CSV", "csv", "text/csv"),
    "txt": ("Text", "txt", "text/plain"),
    "md": ("Markdown", "md", "text/markdown"),
    "html": ("HTML", "html", "text/html"),
    "jpg": ("JPG", "jpg", "image/jpeg"),
    "png": ("PNG", "png", "image/png"),
    "webp": ("WebP", "webp", "image/webp"),
}

# what each kind of file can be converted to (first = most popular)
TARGETS = {
    "pdf": ["docx", "txt", "md", "html"],
    "docx": ["pdf", "txt", "md", "html"],
    "pptx": ["pdf", "docx", "txt", "md"],
    "xlsx": ["csv", "pdf", "docx", "html"],
    "csv": ["xlsx", "pdf", "docx", "html"],
    "txt": ["pdf", "docx", "html"],
    "md": ["pdf", "docx", "html", "txt"],
    "html": ["pdf", "docx", "md", "txt"],
    "image": ["pdf", "docx", "jpg", "png", "webp"],
}

EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".docm": "docx",
    ".dotx": "docx",
    ".dotm": "docx",
    ".pptx": "pptx",
    ".pptm": "pptx",
    ".ppsx": "pptx",
    ".ppsm": "pptx",
    ".potx": "pptx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xltx": "xlsx",
    ".xltm": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "txt",
    ".text": "txt",
    ".log": "txt",
    ".md": "md",
    ".markdown": "md",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".jpg": "image",
    ".jpeg": "image",
    ".jfif": "image",
    ".jpe": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tif": "image",
    ".tiff": "image",
}

LEGACY = {
    ".doc": "Old Word .doc files aren't supported. Open it in Word or Google Docs and save it as .docx first.",
    ".ppt": "Old PowerPoint .ppt files aren't supported. Save it as .pptx first.",
    ".xls": "Old Excel .xls files aren't supported. Save it as .xlsx first.",
    ".odt": "OpenDocument (.odt) files aren't supported yet. Save it as .docx first.",
    ".heic": "HEIC photos aren't supported. Export them as JPG first (most phones can do this).",
}

# Python packages each step needs. The browser version installs them on demand.
READER_PACKAGES = {
    "pdf": ["pdfminer.six", "pypdf"],
    "docx": ["python-docx"],
    "pptx": ["python-pptx"],
    "xlsx": ["openpyxl"],
    "md": ["markdown"],
}
WRITER_PACKAGES = {
    "pdf": ["fpdf2"],
    "docx": ["python-docx"],
    "xlsx": ["openpyxl"],
}
TOOL_PACKAGES = {"merge": ["pypdf"], "split": ["pypdf"], "rotate": ["pypdf"], "compress": ["pypdf"], "images-to-pdf": ["fpdf2"]}


def extension(filename: str) -> str:
    name = (filename or "").lower().rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _zip_kind(data: bytes) -> str | None:
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return None


def detect(filename: str, data: bytes) -> str:
    """Work out what kind of file this is, trusting the content over the file name."""
    ext = extension(filename)
    if ext in LEGACY:
        raise ConvertError(LEGACY[ext])
    if not data:
        raise ConvertError("The file is empty.")
    if data[:5] == b"%PDF-" or (ext == ".pdf" and b"%PDF-" in data[:1024]):
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        kind = _zip_kind(data)
        if kind:
            return kind
        raise ConvertError("This looks like a ZIP file, not a document FileFlip can convert.")
    kind = EXTENSIONS.get(ext)
    if kind == "image" or (kind is None and _looks_like_image(data)):
        if not _looks_like_image(data):
            raise ConvertError("This image file couldn't be read. It may be damaged.")
        return "image"
    if kind in ("csv", "txt", "md", "html"):
        return kind
    if kind in ("pdf", "docx", "pptx", "xlsx"):
        raise ConvertError(f"This file doesn't look like a real {KINDS[kind]} file. It may be damaged.")
    raise ConvertError(
        "FileFlip can't convert this type of file. Try PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), "
        "CSV, text, Markdown, HTML or an image."
    )


def _looks_like_image(data: bytes) -> bool:
    signatures = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")
    return data.startswith(signatures) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def packages_for(kind: str, target: str) -> list[str]:
    needed = READER_PACKAGES.get(kind, []) + WRITER_PACKAGES.get(target, [])
    return sorted(set(needed))


TOOLS = ("merge", "split", "rotate", "compress", "images-to-pdf")


def formats_info(max_upload_mb: int, max_files: int) -> dict:
    """Everything the UI needs to know about formats (served by the API, and baked into
    the GitHub Pages build)."""
    from . import __version__

    return {
        "version": __version__,
        "kinds": KINDS,
        "extensions": EXTENSIONS,
        "legacy": LEGACY,
        "targets": TARGETS,
        "outputs": {k: {"label": v[0], "ext": v[1]} for k, v in OUTPUTS.items()},
        "tools": list(TOOLS),
        "max_upload_mb": max_upload_mb,
        "max_files": max_files,
    }

"""The conversion use case shared by the API and the browser version.

    convert(data, "notes.pdf", "docx")       -> Output("notes.docx", ...)
    run_tool("merge", [(name, data), ...])   -> Output("merged.pdf", ...)
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from . import pdftools
from .formats import KINDS, OUTPUTS, TARGETS, TOOLS, detect
from .model import ConvertError
from .readers import read
from .writers import write

UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


@dataclass
class Output:
    filename: str
    data: bytes
    mime: str
    notes: list[str] = field(default_factory=list)

    def info(self) -> dict:
        return {"filename": self.filename, "mime": self.mime, "bytes": len(self.data), "notes": self.notes}


def stem(filename: str) -> str:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = name.rsplit(".", 1)[0] if "." in name.strip(".") else name
    return UNSAFE.sub("_", base).strip(" ._")[:80] or "file"


def _zip(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()
        for name, data in files:
            unique, n = name, 2
            while unique.lower() in seen:
                base, dot, ext = name.rpartition(".")
                unique = f"{base} ({n}).{ext}" if dot else f"{name} ({n})"
                n += 1
            seen.add(unique.lower())
            zf.writestr(unique, data)
    return buf.getvalue()


def _bundle(files: list[tuple[str, bytes]], zip_name: str, mime: str, notes: list[str]) -> Output:
    if len(files) == 1:
        return Output(files[0][0], files[0][1], mime, notes)
    return Output(zip_name, _zip(files), "application/zip", notes)


def targets(filename: str, data: bytes) -> dict:
    kind = detect(filename, data)
    return {"kind": kind, "label": KINDS[kind], "targets": TARGETS[kind]}


def convert(data: bytes, filename: str, target: str, page_size: str = "A4") -> Output:
    kind = detect(filename, data)
    target = target.lower().replace("jpeg", "jpg")
    if target not in TARGETS[kind]:
        label = OUTPUTS.get(target, (target.upper(),))[0]
        raise ConvertError(f"{KINDS[kind]} files can't be converted to {label}.")
    _, ext, mime = OUTPUTS[target]
    name = stem(filename)

    if kind == "image" and target in ("jpg", "png", "webp"):
        return Output(f"{name}.{ext}", _convert_image(data, target), mime)

    doc = read(kind, data, filename)
    files = [(f"{name}{suffix}.{ext}", blob) for suffix, blob in write(target, doc, page_size)]
    return _bundle(files, f"{name}-{ext}.zip", mime, doc.notes)


def _convert_image(data: bytes, target: str) -> bytes:
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img) or img
    except Exception as exc:  # noqa: BLE001
        raise ConvertError("This image couldn't be read. It may be damaged.") from exc
    buf = io.BytesIO()
    if target == "jpg":
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            img = background
        img.convert("RGB").save(buf, "JPEG", quality=90, optimize=True, progressive=True)
    elif target == "png":
        img.save(buf, "PNG", optimize=img.width * img.height <= 2_500_000)
    else:
        img.save(buf, "WEBP", quality=85, method=2)
    return buf.getvalue()


def run_tool(tool: str, files: list[tuple[str, bytes]], options: dict | None = None) -> Output:
    options = options or {}
    if tool not in TOOLS:
        raise ConvertError(f"Unknown tool '{tool}'.")
    if not files:
        raise ConvertError("Add a file first.")

    if tool == "images-to-pdf":
        for name, data in files:
            if detect(name, data) != "image":
                raise ConvertError(f"{name} isn't an image.")
        first = stem(files[0][0]) if len(files) == 1 else "images"
        return Output(f"{first}.pdf", pdftools.images_to_pdf(files, options.get("page_size", "A4")), "application/pdf")

    for name, data in files:
        if detect(name, data) != "pdf":
            raise ConvertError(f"{name} isn't a PDF.")
    pdf_mime = "application/pdf"

    if tool == "merge":
        return Output("merged.pdf", pdftools.merge(files), pdf_mime, [f"Merged {len(files)} PDFs."])
    if tool == "split":
        if len(files) != 1:
            raise ConvertError("Split one PDF at a time.")
        name, data = files[0]
        parts = pdftools.split(data, name, options.get("mode", "each"), options.get("ranges", ""))
        out = [(f"{stem(name)}-{label}.pdf", blob) for label, blob in parts]
        return _bundle(out, f"{stem(name)}-split.zip", pdf_mime, [f"Split into {len(out)} file(s)."])
    if tool == "rotate":
        angle = int(options.get("angle", 90) or 90)
        out = [(f"{stem(n)}-rotated.pdf", pdftools.rotate(d, n, angle, options.get("pages", "all"))) for n, d in files]
        return _bundle(out, "rotated.zip", pdf_mime, [])
    # compress
    out, notes = [], []
    for name, data in files:
        blob, file_notes = pdftools.compress(data, name, options.get("level", "medium"))
        out.append((f"{stem(name)}-compressed.pdf", blob))
        notes += [f"{name}: {n}" if len(files) > 1 else n for n in file_notes]
    return _bundle(out, "compressed.zip", pdf_mime, notes)

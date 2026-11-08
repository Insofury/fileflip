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
from .model import ConvertError, sanitize
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

    label = KINDS[kind]
    try:
        doc = sanitize(read(kind, data, filename))
    except ConvertError:
        raise
    except MemoryError as exc:
        raise ConvertError("This file is too big to convert here. Try a smaller one.", status=413) from exc
    except Exception as exc:  # noqa: BLE001 - never show a raw crash: explain and keep the cause for debugging
        raise ConvertError(
            f"FileFlip couldn't read this {label} file. It may be damaged or use a feature it doesn't support yet "
            f"({type(exc).__name__}).",
            status=422,
        ) from exc
    if not doc.blocks:
        raise ConvertError(f"There's nothing in this {label} file to convert.", status=422)
    try:
        files = [(f"{name}{suffix}.{ext}", blob) for suffix, blob in write(target, doc, page_size)]
    except ConvertError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConvertError(
            f"FileFlip read this file but couldn't create the {OUTPUTS[target][0]} file ({type(exc).__name__}). "
            "Try another format.",
            status=500,
        ) from exc
    return _bundle(files, f"{name}-{ext}.zip", mime, doc.notes)


def _convert_image(data: bytes, target: str) -> bytes:
    """Image -> JPG / PNG / WebP, upright, from any colour mode (CMYK, 16-bit, palette...)."""
    from PIL import Image

    from .model import _to_8bit, open_image_frames

    frames = open_image_frames(data)
    if not frames:
        raise ConvertError("This image couldn't be read. It may be damaged, or too large.")
    img = _to_8bit(frames[0])
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if has_alpha else "RGB")
    buf = io.BytesIO()
    try:
        if target == "jpg":
            if has_alpha:
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            img.save(buf, "JPEG", quality=90, optimize=True, progressive=True)
        elif target == "png":
            img.save(buf, "PNG", optimize=img.width * img.height <= 2_500_000)
        else:
            img.save(buf, "WEBP", quality=85, method=2)
    except Exception as exc:  # noqa: BLE001
        raise ConvertError(f"This image couldn't be saved as {OUTPUTS[target][0]} ({type(exc).__name__}).") from exc
    return buf.getvalue()


def run_tool(tool: str, files: list[tuple[str, bytes]], options: dict | None = None) -> Output:
    """Run a PDF tool. Any unexpected problem becomes a clear ConvertError."""
    try:
        return _run_tool(tool, files, options)
    except ConvertError:
        raise
    except MemoryError as exc:
        raise ConvertError("These files are too big to process here. Try smaller ones.", status=413) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConvertError(f"FileFlip couldn't finish this ({type(exc).__name__}). One of the files may be damaged.", status=422) from exc


def _run_tool(tool: str, files: list[tuple[str, bytes]], options: dict | None = None) -> Output:
    options = options if isinstance(options, dict) else {}
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
        try:
            angle = int(float(options.get("angle", 90) or 90))
        except (TypeError, ValueError) as exc:
            raise ConvertError("Rotate by 90, 180 or 270 degrees.") from exc
        out = [(f"{stem(n)}-rotated.pdf", pdftools.rotate(d, n, angle, options.get("pages", "all"))) for n, d in files]
        return _bundle(out, "rotated.zip", pdf_mime, [])
    # compress
    out, notes = [], []
    for name, data in files:
        blob, file_notes = pdftools.compress(data, name, options.get("level", "medium"))
        out.append((f"{stem(name)}-compressed.pdf", blob))
        notes += [f"{name}: {n}" if len(files) > 1 else n for n in file_notes]
    return _bundle(out, "compressed.zip", pdf_mime, notes)

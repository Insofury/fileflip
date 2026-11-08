"""Readers: file bytes -> Document. Heavy libraries are imported inside each function so
the browser version only downloads what a conversion actually needs."""

from __future__ import annotations

from ..model import Document


def read(kind: str, data: bytes, filename: str = "") -> Document:
    if kind == "pdf":
        from .pdf import read_pdf

        return read_pdf(data)
    if kind == "docx":
        from .word import read_docx

        return read_docx(data)
    if kind == "pptx":
        from .slides import read_pptx

        return read_pptx(data)
    if kind == "xlsx":
        from .sheets import read_xlsx

        return read_xlsx(data)
    if kind == "csv":
        from .sheets import read_csv

        return read_csv(data, filename)
    if kind in ("txt", "md", "html"):
        from . import markup

        return {"txt": markup.read_txt, "md": markup.read_markdown, "html": markup.read_html}[kind](data)
    if kind == "image":
        from .images import read_image

        return read_image(data, filename)
    raise ValueError(f"no reader for {kind!r}")

"""Images -> Document (a single picture)."""

from __future__ import annotations

from ..model import ConvertError, Document, normalize_image


def read_image(data: bytes, filename: str = "") -> Document:
    img = normalize_image(data, filename)
    if img is None:
        raise ConvertError("This image couldn't be read. It may be damaged.")
    return Document(blocks=[img])

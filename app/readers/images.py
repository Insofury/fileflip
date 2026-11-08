"""Images -> Document (a single picture)."""

from __future__ import annotations

from ..model import ConvertError, Document, PageBreak, encode_image, open_image_frames


def read_image(data: bytes, filename: str = "") -> Document:
    """One picture - or one per page for multi-page TIFF scans - upright and ready to use."""
    frames = open_image_frames(data, all_frames=True)
    if not frames:
        raise ConvertError("This image couldn't be read. It may be damaged, or too large.")
    doc = Document()
    for i, frame in enumerate(frames):
        if i:
            doc.add(PageBreak())
        doc.add(encode_image(frame, filename))
    return doc

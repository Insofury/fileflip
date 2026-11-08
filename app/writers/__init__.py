"""Writers: Document -> file bytes."""

from __future__ import annotations

from ..model import Document


def write(target: str, doc: Document, page_size: str = "A4") -> list[tuple[str, bytes]]:
    """Returns [(file name suffix, data)]. Most formats give one file; CSV gives one per table."""
    if target == "pdf":
        from .pdf import write_pdf

        return [("", write_pdf(doc, page_size))]
    if target == "docx":
        from .word import write_docx

        return [("", write_docx(doc, page_size))]
    if target == "xlsx":
        from .sheets import write_xlsx

        return [("", write_xlsx(doc))]
    if target == "csv":
        from .sheets import write_csv

        return write_csv(doc)
    from . import markup

    return [("", {"txt": markup.write_txt, "md": markup.write_markdown, "html": markup.write_html}[target](doc))]

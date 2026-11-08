"""Excel (.xlsx) and CSV -> Document (one table per sheet)."""

from __future__ import annotations

import csv
import datetime as dt
import io

from ..model import ConvertError, Document, Paragraph, Table, runs
from .markup import decode_text

MAX_ROWS = 20_000
MAX_COLS = 200


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.10g}"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M") if (value.hour, value.minute) != (0, 0) else value.strftime("%Y-%m-%d")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    return str(value).strip()


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """Drop empty trailing rows and columns, and pad rows to the same width."""
    rows = [r for r in rows]
    while rows and not any(rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows), default=0)
    return [(r + [""] * width)[:width] for r in rows]


def read_xlsx(data: bytes) -> Document:
    """Read-only mode is fast and light, but trusts the size the file says it has - some
    files written by other programs get that wrong. If it finds nothing (or fails), read
    the workbook normally."""
    try:
        doc = _read_xlsx(data, read_only=True)
        if doc.tables():
            return doc
    except Exception:  # noqa: BLE001
        pass
    try:
        doc = _read_xlsx(data, read_only=False)
    except ConvertError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConvertError("This Excel file couldn't be opened. It may be damaged or password-protected.") from exc
    if not doc.tables():
        raise ConvertError("This spreadsheet is empty.")
    return doc


def _read_xlsx(data: bytes, read_only: bool) -> Document:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=read_only, data_only=True)
    doc = Document(title=(wb.properties.title or "").strip() if wb.properties else "")
    sheets = wb.worksheets
    truncated = False
    for ws in sheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                truncated = True
                break
            rows.append([_cell(v) for v in row[:MAX_COLS]])
        rows = _trim(rows)
        if not rows:
            continue
        if len(sheets) > 1:
            doc.add(Paragraph(runs(ws.title), style="h2"))
        doc.add(Table(rows, header=True, name=ws.title))
    wb.close()
    if truncated:
        doc.note(f"Only the first {MAX_ROWS:,} rows of each sheet were converted.")
    return doc


csv.field_size_limit(10_000_000)  # allow very long cells (the default is 128 KB)


def read_csv(data: bytes, filename: str = "") -> Document:
    text = decode_text(data).replace("\x00", "")
    sample = text[:20_000]
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    rows, truncated = [], False
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        rows.append([c.strip() for c in row[:MAX_COLS]])
    rows = _trim(rows)
    if not rows:
        raise ConvertError("This CSV file is empty.")
    doc = Document(blocks=[Table(rows, header=True, name="Sheet1")])
    if truncated:
        doc.note(f"Only the first {MAX_ROWS:,} rows were converted.")
    return doc

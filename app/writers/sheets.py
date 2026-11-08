"""Document tables -> Excel (.xlsx) or CSV."""

from __future__ import annotations

import csv
import io
import re

from ..model import ConvertError, Document

NUMBER = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _value(text: str):
    """Turn "1,234.5" / "42" into numbers so Excel can add them up; keep everything else
    (including numbers with leading zeros, like phone numbers or IDs) as text."""
    t = text.strip()
    if NUMBER.match(t) and not (len(t) > 1 and t.lstrip("-").startswith("0") and not t.lstrip("-").startswith("0.")):
        plain = t.replace(",", "")
        try:
            return int(plain) if "." not in plain else float(plain)
        except ValueError:
            return text
    return text


def _tables(doc: Document):
    tables = doc.tables()
    if not tables:
        raise ConvertError("There are no tables in this file to put in a spreadsheet.")
    return tables


def write_xlsx(doc: Document) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for i, table in enumerate(_tables(doc), start=1):
        name = re.sub(r"[\[\]:*?/\\]", "", table.name or f"Table {i}")[:31] or f"Table {i}"
        base, n = name, 2
        while name.lower() in used:
            name = f"{base[:28]} {n}"
            n += 1
        used.add(name.lower())
        ws = wb.create_sheet(name)
        for row in table.rows:
            ws.append([_value(c) for c in row])
        if table.header and table.rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E7F0ED")
            ws.freeze_panes = "A2"
        widths: dict[int, int] = {}
        for row in table.rows[:500]:
            for j, value in enumerate(row, start=1):
                widths[j] = max(widths.get(j, 0), min(len(str(value)), 60))
        for j, w in widths.items():
            ws.column_dimensions[get_column_letter(j)].width = max(8, w + 2)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_csv(doc: Document) -> list[tuple[str, bytes]]:
    """One CSV per table. UTF-8 with a BOM so Excel opens accented text correctly."""
    tables = _tables(doc)
    out = []
    for i, table in enumerate(tables, start=1):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows(table.rows)
        suffix = "" if len(tables) == 1 else "-" + (re.sub(r"[^\w-]+", "-", table.name).strip("-") or f"table-{i}")
        out.append((suffix, ("﻿" + buf.getvalue()).encode("utf-8")))
    return out

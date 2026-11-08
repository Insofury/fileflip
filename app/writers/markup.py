"""Document -> plain text, Markdown or a standalone HTML page."""

from __future__ import annotations

import base64
import html

from ..model import Document, Image, ListItem, PageBreak, Paragraph, Run, Table, text_of

# ------------------------------------------------------------------ text
def write_txt(doc: Document) -> bytes:
    """Paragraphs separated by blank lines; list items kept together; tables tab-separated."""
    chunks: list[str] = []
    skipped = 0
    numbers: dict[int, int] = {}
    prev = None
    for block in doc.blocks:
        if isinstance(block, ListItem):
            if not isinstance(prev, ListItem):
                numbers = {}
            # deeper levels restart; a bullet item ends the numbering at its level
            numbers = {lvl: n for lvl, n in numbers.items() if lvl < block.level or (lvl == block.level and block.ordered)}
            if block.ordered:
                numbers[block.level] = numbers.get(block.level, 0) + 1
            marker = f"{numbers[block.level]}." if block.ordered else "-"
            line = f"{'  ' * block.level}{marker} {text_of(block.runs)}"
            if isinstance(prev, ListItem):
                chunks[-1] += "\n" + line
            else:
                chunks.append(line)
        elif isinstance(block, Paragraph):
            text = text_of(block.runs)
            chunks.append(text.upper() if block.style in ("title", "h1") else text)
        elif isinstance(block, Table):
            chunks.append("\n".join("\t".join(row) for row in block.rows))
        elif isinstance(block, Image):
            skipped += 1
        prev = block
    if skipped:
        doc.note(f"{skipped} picture(s) can't be included in a text file.")
    return ("\n\n".join(c for c in chunks if c.strip()).strip() + "\n").encode("utf-8")


# ------------------------------------------------------------------ markdown
def _md_escape(text: str) -> str:
    for ch in ("\\", "*", "_", "`", "[", "]"):
        text = text.replace(ch, "\\" + ch)
    return text


def _md_runs(runs: list[Run]) -> str:
    parts = []
    for r in runs:
        text = r.text
        if not text.strip():
            parts.append(text)
            continue
        lead, core, trail = text[: len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()):]
        core = f"`{core}`" if r.code else _md_escape(core)
        if r.bold and r.italic:
            core = f"***{core}***"
        elif r.bold:
            core = f"**{core}**"
        elif r.italic:
            core = f"*{core}*"
        if r.link:
            core = f"[{core}]({r.link})"
        parts.append(lead + core + trail)
    return "".join(parts).replace("\n", "  \n")


def write_markdown(doc: Document) -> bytes:
    out: list[str] = []
    skipped = 0
    prev = None
    for block in doc.blocks:
        if isinstance(block, Paragraph):
            text = _md_runs(block.runs)
            prefix = {"title": "# ", "h1": "# ", "h2": "## ", "h3": "### ", "quote": "> "}.get(block.style, "")
            if block.style == "code":
                out.append(f"```\n{text_of(block.runs)}\n```")
            else:
                out.append(prefix + (text_of(block.runs) if prefix.startswith("#") else text))
        elif isinstance(block, ListItem):
            marker = "1." if block.ordered else "-"
            line = f"{'   ' * block.level}{marker} {_md_runs(block.runs)}"
            if isinstance(prev, ListItem):
                out[-1] += "\n" + line
            else:
                out.append(line)
        elif isinstance(block, Table):
            rows = block.rows
            width = max(len(r) for r in rows)
            cell = lambda c: c.replace("|", "\\|").replace("\n", " ")  # noqa: E731
            header = rows[0] if block.header else [""] * width
            body = rows[1:] if block.header else rows
            lines = ["| " + " | ".join(cell(c) for c in header) + " |", "|" + "---|" * width]
            lines += ["| " + " | ".join(cell(c) for c in r + [""] * (width - len(r))) + " |" for r in body]
            out.append("\n".join(lines))
        elif isinstance(block, PageBreak):
            out.append("---")
        elif isinstance(block, Image):
            skipped += 1
        prev = block
    if skipped:
        doc.note(f"{skipped} picture(s) aren't included in the Markdown file.")
    return ("\n\n".join(out).strip() + "\n").encode("utf-8")


# ------------------------------------------------------------------ html
CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:46rem;margin:2.5rem auto;padding:0 1.25rem;line-height:1.6;color:#21292e;background:#fbfbf9}
h1,h2,h3{line-height:1.25;color:#1d3f3a}h1{font-size:1.9rem}h2{font-size:1.45rem}h3{font-size:1.15rem}
blockquote{margin:1rem 0;padding:.25rem 1rem;border-left:3px solid #2f6f66;color:#4a565a}
pre{background:#f0f3f1;padding:.75rem 1rem;border-radius:8px;overflow:auto}code{font-family:ui-monospace,Consolas,monospace}
table{border-collapse:collapse;margin:1rem 0;width:100%}th,td{border:1px solid #d6dcd8;padding:.4rem .6rem;text-align:left;vertical-align:top}th{background:#e7f0ed}
img{max-width:100%;height:auto}hr{border:0;border-top:1px dashed #cfd6d2;margin:2rem 0}a{color:#1f5fa8}
"""


def _html_runs(runs: list[Run]) -> str:
    parts = []
    for r in runs:
        text = html.escape(r.text).replace("\n", "<br>")
        if r.code:
            text = f"<code>{text}</code>"
        if r.bold:
            text = f"<strong>{text}</strong>"
        if r.italic:
            text = f"<em>{text}</em>"
        if r.underline and not r.link:
            text = f"<u>{text}</u>"
        if r.link:
            text = f'<a href="{html.escape(r.link, quote=True)}">{text}</a>'
        parts.append(text)
    return "".join(parts)


def write_html(doc: Document) -> bytes:
    body: list[str] = []
    open_lists: list[str] = []

    def close_lists(to_level: int = -1) -> None:
        while len(open_lists) > to_level + 1:
            body.append(f"</{open_lists.pop()}>")

    for block in doc.blocks:
        if not isinstance(block, ListItem):
            close_lists()
        if isinstance(block, Paragraph):
            tag = {"title": "h1", "h1": "h1", "h2": "h2", "h3": "h3", "quote": "blockquote"}.get(block.style, "p")
            if block.style == "code":
                body.append(f"<pre><code>{html.escape(text_of(block.runs))}</code></pre>")
                continue
            style = f' style="text-align:{block.align}"' if block.align != "left" else ""
            inner = html.escape(text_of(block.runs)) if tag.startswith("h") else _html_runs(block.runs)
            body.append(f"<{tag}{style}>{inner}</{tag}>")
        elif isinstance(block, ListItem):
            tag = "ol" if block.ordered else "ul"
            close_lists(block.level)
            while len(open_lists) <= block.level:
                open_lists.append(tag)
                body.append(f"<{tag}>")
            body.append(f"<li>{_html_runs(block.runs)}</li>")
        elif isinstance(block, Table):
            rows = block.rows
            lines = ["<table>"]
            for i, row in enumerate(rows):
                cell = "th" if i == 0 and block.header else "td"
                lines.append("<tr>" + "".join(f"<{cell}>{html.escape(c)}</{cell}>" for c in row) + "</tr>")
            lines.append("</table>")
            body.append("\n".join(lines))
        elif isinstance(block, Image):
            data = base64.b64encode(block.data).decode()
            body.append(f'<p><img src="data:{block.mime};base64,{data}" alt="{html.escape(block.name)}"></p>')
        elif isinstance(block, PageBreak):
            body.append("<hr>")
    close_lists()
    title = html.escape(doc.title or "Converted with FileFlip")
    page = (
        f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{title}</title>\n'
        f"<style>{CSS.strip()}</style>\n</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )
    return page.encode("utf-8")

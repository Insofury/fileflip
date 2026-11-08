import pytest

from app.formats import detect
from app.model import ConvertError, Image, ListItem, PageBreak, Paragraph, Table, text_of
from app.readers import read
from tests import samples


def paragraphs(doc, style=None):
    return [text_of(b.runs) for b in doc.blocks if isinstance(b, Paragraph) and (style is None or b.style == style)]


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("notes.docx", samples.docx_bytes(), "docx"),
        ("renamed.pdf", samples.docx_bytes(), "docx"),  # content wins over the file name
        ("deck.pptx", samples.pptx_bytes(), "pptx"),
        ("marks.xlsx", samples.xlsx_bytes(), "xlsx"),
        ("a.csv", b"a,b\n1,2\n", "csv"),
        ("a.md", b"# hi", "md"),
        ("a.htm", b"<p>hi</p>", "html"),
        ("a.txt", b"hello", "txt"),
        ("photo.png", samples.png(), "image"),
        ("no-extension", samples.png(), "image"),
    ],
)
def test_detect(name, data, kind):
    assert detect(name, data) == kind


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("old.doc", b"\xd0\xcf\x11\xe0", "save it as .docx"),
        ("empty.pdf", b"", "empty"),
        ("fake.pdf", b"not really a pdf", "doesn't look like a real PDF"),
        ("archive.zip", b"PK\x03\x04junk", "ZIP file"),
        ("prog.exe", b"MZ\x90\x00", "can't convert this type"),
        ("photo.jpg", b"this is text", "couldn't be read"),
    ],
)
def test_detect_rejects(name, data, message):
    with pytest.raises(ConvertError, match=message):
        detect(name, data)


def test_damaged_image_is_reported():
    from app.convert import convert

    with pytest.raises(ConvertError, match="couldn't be read"):
        convert(b"\x89PNG broken", "broken.png", "pdf")


# ------------------------------------------------------------------ word
def test_read_docx_keeps_structure():
    doc = read("docx", samples.docx_bytes())
    assert doc.title == "Biology notes"
    assert paragraphs(doc, "title") == ["Photosynthesis"] and paragraphs(doc, "h1") == ["Overview", "Equation"]
    body = next(b for b in doc.blocks if isinstance(b, Paragraph) and b.style == "normal")
    assert [(r.text, r.bold, r.italic) for r in body.runs][:4] == [
        ("Plants turn ", False, False), ("light energy", True, False), (" into ", False, False), ("chemical energy", False, True),
    ]
    items = [b for b in doc.blocks if isinstance(b, ListItem)]
    assert [(text_of(i.runs), i.ordered, i.level) for i in items] == [
        ("Chlorophyll absorbs light", False, 0), ("Oxygen is released", False, 1), ("Collect leaves", True, 0),
    ]
    assert doc.tables()[0].rows == [["Stage", "Output"], ["Calvin cycle", "Glucose"]]
    assert len(doc.images()) == 1 and any(isinstance(b, PageBreak) for b in doc.blocks)


def test_read_broken_docx():
    with pytest.raises(ConvertError, match="couldn't be opened"):
        read("docx", b"PK\x03\x04 not a real zip")


# ------------------------------------------------------------------ slides
def test_read_pptx_as_notes():
    doc = read("pptx", samples.pptx_bytes())
    assert paragraphs(doc, "h2") == ["Slide 1: Cell Biology", "Slide 2: Organelles"]
    assert "Unit 3 revision" in paragraphs(doc, "normal")  # subtitle is text, not a bullet
    items = [(text_of(b.runs), b.level) for b in doc.blocks if isinstance(b, ListItem)]
    assert items == [("Nucleus: stores DNA", 0), ("ATP", 1)]
    assert any("powerhouse" in t for t in paragraphs(doc, "quote"))
    assert len(doc.images()) == 1 and sum(isinstance(b, PageBreak) for b in doc.blocks) == 1


# ------------------------------------------------------------------ spreadsheets
def test_read_xlsx_and_csv():
    doc = read("xlsx", samples.xlsx_bytes())
    tables = doc.tables()
    assert [t.name for t in tables] == ["Marks", "Attendance"]  # the empty sheet is skipped
    assert tables[0].rows == [["Name", "Maths", "Roll no"], ["Asha", "91.5", "007"], ["Ravi", "78", "012"]]

    doc = read("csv", "name;score\r\nZoë;9\r\n".encode("utf-8-sig"), "a.csv")
    assert doc.tables()[0].rows == [["name", "score"], ["Zoë", "9"]]  # delimiter sniffed, BOM removed
    with pytest.raises(ConvertError, match="empty"):
        read("csv", b"\n\n", "a.csv")


# ------------------------------------------------------------------ text, markdown, html
def test_read_markdown():
    doc = read("md", samples.MARKDOWN)
    assert paragraphs(doc, "h1") == ["Study plan"]
    items = [(text_of(b.runs), b.ordered, b.level) for b in doc.blocks if isinstance(b, ListItem)]
    assert ("Newton's laws", False, 1) in items and ("First", True, 0) in items
    assert doc.tables()[0].rows == [["Day", "Topic"], ["Mon", "Atoms"]]
    assert paragraphs(doc, "quote") == ["Stay calm."] and paragraphs(doc, "code") == ["print('hi')"]


def test_read_html():
    doc = read("html", samples.HTML)
    assert doc.title == "Lab report"
    para = next(b for b in doc.blocks if isinstance(b, Paragraph) and b.style == "normal")
    assert para.align == "center"
    assert [r.link for r in para.runs if r.link] == ["https://example.com"]
    items = [(text_of(b.runs), b.level, b.ordered) for b in doc.blocks if isinstance(b, ListItem)]
    assert items == [("One", 0, False), ("Nested", 1, False), ("Two", 0, False), ("First", 0, True)]
    assert "alert" not in str(doc.blocks)  # scripts are ignored
    assert any("linked image" in n for n in doc.notes)


def test_read_text():
    doc = read("txt", "Title line\n\nPara one\nline two\n\n- a\n- b\n\fNext page".encode("cp1252"))
    assert isinstance(doc.blocks[0], Paragraph) and text_of(doc.blocks[1].runs) == "Para one\nline two"
    assert [text_of(b.runs) for b in doc.blocks if isinstance(b, ListItem)] == ["a", "b"]
    assert any(isinstance(b, PageBreak) for b in doc.blocks)


# ------------------------------------------------------------------ pdf
def test_read_pdf_rebuilds_structure():
    doc = read("pdf", samples.pdf_bytes())
    assert paragraphs(doc, "h1")[:2] == ["Photosynthesis", "Overview"] or "Overview" in paragraphs(doc, "h1")
    body = next(b for b in doc.blocks if isinstance(b, Paragraph) and "Plants turn" in text_of(b.runs))
    assert any(r.bold and r.text.strip() == "light energy" for r in body.runs)
    assert any(r.italic and "chemical energy" in r.text for r in body.runs)
    items = [(text_of(b.runs), b.level, b.ordered) for b in doc.blocks if isinstance(b, ListItem)]
    assert items == [("Chlorophyll absorbs light", 0, False), ("Oxygen is released", 1, False), ("Collect leaves", 0, True)]
    assert doc.tables()[0].rows == [["Stage", "Output"], ["Calvin cycle", "Glucose"]]
    assert len(doc.images()) == 1
    assert not any(text_of(b.runs).strip().isdigit() for b in doc.blocks if isinstance(b, Paragraph))  # page numbers dropped


def test_read_scanned_pdf_keeps_pictures():
    from app.pdftools import images_to_pdf

    doc = read("pdf", images_to_pdf([("scan.png", samples.png(800, 1100))]))
    assert isinstance(doc.blocks[0], Image) and any("scan" in n for n in doc.notes)


def test_read_broken_pdf():
    with pytest.raises(ConvertError, match="couldn't be read"):
        read("pdf", b"%PDF-1.4\nthis is not a real pdf")


def test_images():
    doc = read("image", samples.png(mode="RGBA"), "logo.png")
    assert isinstance(doc.blocks[0], Image) and doc.blocks[0].mime == "image/png"
    assert isinstance(Table([["a"]]), Table)

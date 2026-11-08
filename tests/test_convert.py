import io
import json
import zipfile

import pytest

from app.convert import convert, run_tool, stem
from app.formats import TARGETS, detect
from app.model import ConvertError
from tests import samples

SAMPLES = {
    "notes.docx": samples.docx_bytes,
    "deck.pptx": samples.pptx_bytes,
    "marks.xlsx": samples.xlsx_bytes,
    "marks.csv": lambda: b"Name,Score\nAsha,91\n",
    "plan.md": lambda: samples.MARKDOWN,
    "page.html": lambda: samples.HTML,
    "essay.txt": lambda: b"My essay\n\nFirst paragraph.\n",
    "photo.png": samples.png,
    "notes.pdf": samples.pdf_bytes,
}
MAGIC = {
    "pdf": b"%PDF-",
    "docx": b"PK\x03\x04",
    "xlsx": b"PK\x03\x04",
    "jpg": b"\xff\xd8",
    "png": b"\x89PNG",
    "webp": b"RIFF",
}
CASES = [(name, target) for name, make in SAMPLES.items() for target in TARGETS[detect(name, make())]]


@pytest.mark.parametrize(("name", "target"), CASES, ids=[f"{n}->{t}" for n, t in CASES])
def test_every_conversion(name, target):
    out = convert(SAMPLES[name](), name, target)
    assert out.data
    if out.filename.endswith(".zip"):
        assert zipfile.ZipFile(io.BytesIO(out.data)).namelist()
        return
    assert out.filename == f"{stem(name)}.{'jpg' if target == 'jpg' else target}"
    if target in MAGIC:
        assert out.data.startswith(MAGIC[target])
    else:
        out.data.decode("utf-8")  # text formats are valid UTF-8


def test_round_trips_keep_content():
    import docx

    pdf = convert(samples.docx_bytes(), "notes.docx", "pdf").data
    back = docx.Document(io.BytesIO(convert(pdf, "notes.pdf", "docx").data))
    text = "\n".join(p.text for p in back.paragraphs)
    assert "Photosynthesis" in text and "Chlorophyll absorbs light" in text and "Café and €" in text
    assert back.tables and back.tables[0].cell(1, 1).text == "Glucose"
    assert back.inline_shapes  # the picture survived

    md = convert(samples.MARKDOWN, "plan.md", "html").data.decode()
    assert "<h1>Study plan</h1>" in md and "<strong>chemistry</strong>" in md and "<table>" in md


def test_xlsx_to_csv_and_back():
    out = convert(samples.xlsx_bytes(), "marks.xlsx", "csv")
    assert out.filename == "marks-csv.zip"  # two sheets -> two CSVs
    names = zipfile.ZipFile(io.BytesIO(out.data)).namelist()
    assert names == ["marks-Marks.csv", "marks-Attendance.csv"]
    csv_data = zipfile.ZipFile(io.BytesIO(out.data)).read("marks-Marks.csv")
    assert csv_data.startswith("\ufeff".encode()) and b"Asha,91.5,007" in csv_data

    import openpyxl

    back = openpyxl.load_workbook(io.BytesIO(convert(csv_data, "marks.csv", "xlsx").data)).active
    assert back["B2"].value == 91.5 and back["C2"].value == "007"  # numbers are numbers, IDs stay text
    assert back["A1"].font.bold


def test_text_outputs():
    txt = convert(samples.docx_bytes(), "notes.docx", "txt")
    assert "- Chlorophyll absorbs light\n  - Oxygen is released\n1. Collect leaves" in txt.data.decode()
    assert any("picture" in n for n in txt.notes)
    md = convert(samples.docx_bytes(), "notes.docx", "md").data.decode()
    assert "**light energy**" in md and "| Stage | Output |" in md


def test_pdf_notes_missing_characters():
    out = convert("Hello 你好 नमस्ते".encode(), "hi.txt", "pdf")
    assert out.data.startswith(b"%PDF-") and any("aren't in the PDF font" in n for n in out.notes)


def test_image_conversions():
    rgba = samples.png(mode="RGBA")
    assert convert(rgba, "logo.png", "jpg").data.startswith(b"\xff\xd8")  # transparency flattened
    assert convert(rgba, "logo.png", "webp").data[8:12] == b"WEBP"


def test_unsupported_target():
    with pytest.raises(ConvertError, match="Excel files can't be converted to JPG"):
        convert(samples.xlsx_bytes(), "marks.xlsx", "jpg")


def test_file_names():
    assert stem("C:\\Users\\me\\My Notes.final.docx") == "My Notes.final"
    assert stem('bad:"name?.pdf') == "bad_name"
    assert stem("") == "file"


# ------------------------------------------------------------------ tools
def test_merge_split_rotate_compress():
    from pypdf import PdfReader

    pdf = samples.pdf_bytes()
    pages = len(PdfReader(io.BytesIO(pdf)).pages)
    merged = run_tool("merge", [("a.pdf", pdf), ("b.pdf", pdf)])
    assert merged.filename == "merged.pdf" and len(PdfReader(io.BytesIO(merged.data)).pages) == pages * 2

    split = run_tool("split", [("a.pdf", pdf)], {"mode": "each"})
    assert split.filename == "a-split.zip"
    assert zipfile.ZipFile(io.BytesIO(split.data)).namelist() == [f"a-page-{i}.pdf" for i in range(1, pages + 1)]
    one = run_tool("split", [("a.pdf", pdf)], {"mode": "ranges", "ranges": "2"})
    assert one.filename == "a-page-2.pdf"

    rotated = run_tool("rotate", [("a.pdf", pdf)], {"angle": 90, "pages": "1"})
    reader = PdfReader(io.BytesIO(rotated.data))
    assert reader.pages[0].rotation == 90 and reader.pages[1].rotation == 0

    compressed = run_tool("compress", [("a.pdf", pdf)], {"level": "high"})
    assert compressed.data.startswith(b"%PDF-") and len(compressed.data) <= len(pdf)


def test_images_to_pdf():
    from pypdf import PdfReader

    out = run_tool("images-to-pdf", [("a.png", samples.png(600, 300)), ("b.png", samples.png(300, 600))])
    reader = PdfReader(io.BytesIO(out.data))
    assert len(reader.pages) == 2
    assert reader.pages[0].mediabox.width > reader.pages[0].mediabox.height  # landscape for a wide picture


@pytest.mark.parametrize(
    ("tool", "files", "options", "message"),
    [
        ("merge", [("a.pdf", b"%PDF-1.4")], {}, "at least two"),
        ("split", [("a.png", samples.png())], {}, "isn't a PDF"),
        ("images-to-pdf", [("a.txt", b"hi")], {}, "isn't an image"),
        ("split", [("a.pdf", None)], {"mode": "ranges", "ranges": "9-12"}, "outside this PDF"),
        ("split", [("a.pdf", None)], {"mode": "ranges", "ranges": "abc"}, "isn't a page range"),
        ("rotate", [("a.pdf", None)], {"angle": 45}, "90, 180 or 270"),
        ("explode", [("a.pdf", b"x")], {}, "Unknown tool"),
    ],
)
def test_tool_errors(tool, files, options, message):
    pdf = samples.pdf_bytes()
    files = [(n, pdf if d is None else d) for n, d in files]
    with pytest.raises(ConvertError, match=message):
        run_tool(tool, files, options)


def test_info_is_json_safe():
    out = convert(b"hello", "a.txt", "html")
    assert json.loads(json.dumps(out.info()))["filename"] == "a.html"

"""Real-world files that used to trip converters up, and a 'never crash' check."""

import io
import zipfile

import pytest
from PIL import Image as PILImage

from app.convert import convert, run_tool
from app.formats import TARGETS, detect
from app.model import ConvertError, ListItem, Paragraph, Table, text_of
from app.readers import read
from app.readers import pdf as pdf_reader
from tests import samples

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def texts(doc):
    return [text_of(b.runs) for b in doc.blocks if isinstance(b, (Paragraph, ListItem))]


# ------------------------------------------------------------------ PDF: Canva-style files
def wrap_pages_in_forms(pdf: bytes) -> bytes:
    """Put each page's content inside a form XObject, like Canva and many design tools do."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

    reader, writer = PdfReader(io.BytesIO(pdf)), PdfWriter()
    for page in reader.pages:
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        new = writer.add_blank_page(width=w, height=h)
        form = DecodedStreamObject()
        form.set_data(page.get_contents().get_data())
        form.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(w), FloatObject(h)]),
            NameObject("/Resources"): page["/Resources"].get_object().clone(writer),
        })
        content = DecodedStreamObject()
        content.set_data(b"q /X1 Do Q")
        new[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/X1"): writer._add_object(form)})})
        new[NameObject("/Contents")] = writer._add_object(content)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_with_text_inside_figures():
    """The bug reported with a Canva CV: all text was inside a form XObject."""
    wrapped = wrap_pages_in_forms(samples.pdf_bytes())
    doc = read("pdf", wrapped)
    assert "Overview" in texts(doc)
    assert [text_of(b.runs) for b in doc.blocks if isinstance(b, ListItem)][:2] == ["Chlorophyll absorbs light", "Oxygen is released"]
    assert doc.tables() and doc.tables()[0].rows[1] == ["Calvin cycle", "Glucose"]
    assert convert(wrapped, "cv.pdf", "docx").data.startswith(b"PK")


def designed_pdf() -> bytes:
    """A CV-style page: letter-spaced name, bullets drawn as small dots, a superscript
    ordinal, and two bullet points sharing one text box."""
    from app.compat import fpdf

    pdf = fpdf()(unit="pt", format="A4")
    pdf.add_page()
    pdf.set_font("helvetica", "B", 24)
    pdf.set_xy(60, 60)
    pdf.cell(text="J A N E   D O E")
    pdf.set_font("helvetica", "", 10)
    y = 120
    for line in ("Built a timetable app used by 300 students.", "Led the coding club for two years."):
        pdf.ellipse(62, y + 3.5, 3, 3, style="F")  # the bullet is a drawn dot, not a character
        pdf.set_xy(72, y)
        pdf.cell(text=line)
        y += 14
    pdf.set_xy(72, y)
    pdf.cell(text="Finished the 10")
    x = pdf.get_x()
    pdf.set_font("helvetica", "", 6)
    pdf.set_xy(x, y - 2.5)
    pdf.cell(text="th")
    pdf.set_font("helvetica", "", 10)
    pdf.set_xy(x + 8, y)
    pdf.cell(text="grade with honours.")
    return bytes(pdf.output())


def test_drawn_bullets_letter_spacing_and_superscripts():
    doc = read("pdf", designed_pdf())
    all_text = " ".join(texts(doc))
    assert "JANE DOE" in all_text and "J A N E" not in all_text
    items = [text_of(b.runs) for b in doc.blocks if isinstance(b, ListItem)]
    assert items[:2] == ["Built a timetable app used by 300 students.", "Led the coding club for two years."]
    assert "10th" in all_text


def test_pdf_falls_back_to_pypdf(monkeypatch):
    def broken(_data):
        raise RuntimeError("pdfminer choked")

    monkeypatch.setattr(pdf_reader, "_read_with_pdfminer", broken)
    doc = read("pdf", samples.pdf_bytes())
    assert any("Chlorophyll" in t for t in texts(doc)) and any("simpler method" in n for n in doc.notes)


def test_pdf_without_text_explains_why():
    from app.compat import fpdf

    pdf = fpdf()()
    pdf.add_page()
    pdf.rect(20, 20, 100, 50, style="F")  # only shapes (like text saved as outlines)
    with pytest.raises(ConvertError, match="OCR") as info:
        convert(bytes(pdf.output()), "outlines.pdf", "docx")
    assert info.value.status == 422


# ------------------------------------------------------------------ Word: hidden places text lives
def docx_with_hidden_text() -> bytes:
    import docx
    from docx.oxml import parse_xml

    d = docx.Document()
    d.add_paragraph("Before")
    body = d.element.body
    body.insert(1, parse_xml(f'<w:sdt {W_NS}><w:sdtContent><w:p><w:r><w:t>Inside a content control</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
    p = d.add_paragraph("Equation: ")
    p._p.append(parse_xml('<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x=2</m:t></m:r></m:oMath>'))
    p._p.append(parse_xml(f'<w:ins {W_NS} w:id="1" w:author="a"><w:r><w:t> (added)</w:t></w:r></w:ins>'))
    p._p.append(parse_xml(f'<w:del {W_NS} w:id="2" w:author="a"><w:r><w:delText>removed</w:delText></w:r></w:del>'))
    box = (
        f'<w:r {W_NS} xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:v="urn:schemas-microsoft-com:vml"><mc:AlternateContent>'
        "<mc:Choice Requires=\"wps\"><w:drawing><w:txbxContent><w:p><w:r><w:t>Sidebar text</w:t></w:r></w:p></w:txbxContent></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><v:textbox><w:txbxContent><w:p><w:r><w:t>Sidebar text</w:t></w:r></w:p></w:txbxContent></v:textbox></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )
    d.add_paragraph("Profile")._p.append(parse_xml(box))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_content_controls_text_boxes_equations_and_tracked_changes():
    found = texts(read("docx", docx_with_hidden_text()))
    joined = " | ".join(found)
    assert "Inside a content control" in joined
    assert "Equation: x=2 (added)" in joined and "removed" not in joined
    assert found.count("Sidebar text") == 1  # the old-Word fallback copy is ignored


def with_content_type(data: bytes, old: str, new: str) -> bytes:
    src, buf = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                content = content.replace(old.encode(), new.encode())
            out.writestr(item, content)
    return buf.getvalue()


def test_macro_enabled_word_and_powerpoint_still_convert():
    docm = with_content_type(samples.docx_bytes(), "wordprocessingml.document.main+xml", "ms-word.document.macroEnabled.main+xml")
    doc = read("docx", docm)
    assert "Overview" in texts(doc) and any("simpler way" in n for n in doc.notes)
    assert convert(docm, "notes.docm", "pdf").data.startswith(b"%PDF")

    pptm = with_content_type(samples.pptx_bytes(), "presentationml.presentation.main+xml", "ms-powerpoint.presentation.macroEnabled.main+xml")
    slides = texts(read("pptx", pptm))
    assert "Slide 2: Organelles" in slides and "Nucleus: stores DNA" in slides


# ------------------------------------------------------------------ images
def jpeg(size, exif_orientation=None, mode="RGB") -> bytes:
    img = PILImage.new(mode, size, (0, 0, 0, 0) if mode == "CMYK" else 0)
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = PILImage.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif.tobytes()
    img.save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def test_phone_photos_are_turned_upright():
    from pypdf import PdfReader

    photo = jpeg((400, 200), exif_orientation=6)  # stored sideways, meant to be portrait
    page = PdfReader(io.BytesIO(convert(photo, "photo.jpg", "pdf").data)).pages[0]
    assert page.mediabox.height > page.mediabox.width
    out = PILImage.open(io.BytesIO(convert(photo, "photo.jpg", "png").data))
    assert out.size == (200, 400)


def test_multipage_tiff_and_unusual_colour_modes():
    from pypdf import PdfReader

    frames = [PILImage.new("RGB", (300, 200), c) for c in ("red", "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    tiff = buf.getvalue()
    assert len(PdfReader(io.BytesIO(run_tool("images-to-pdf", [("scan.tif", tiff)]).data)).pages) == 2
    assert len(PdfReader(io.BytesIO(convert(tiff, "scan.tif", "pdf").data)).pages) == 2

    cmyk = jpeg((60, 40), mode="CMYK")
    assert convert(cmyk, "print.jpg", "png").data.startswith(b"\x89PNG")
    assert convert(cmyk, "print.jpg", "webp").data[8:12] == b"WEBP"
    deep = PILImage.new("I;16", (50, 50), 40000)
    buf = io.BytesIO()
    deep.save(buf, "PNG")
    assert convert(buf.getvalue(), "scan16.png", "jpg").data.startswith(b"\xff\xd8")


# ------------------------------------------------------------------ odd text and big tables
def test_control_characters_never_break_word_or_excel():
    text = "Title\x0bsecond line\x00 with \x1f junk​ and ￾ more"
    assert convert(text.encode(), "notes.txt", "docx").data.startswith(b"PK")
    csv_data = f"name,note\nA,{text}\n".encode()
    assert convert(csv_data, "data.csv", "xlsx").data.startswith(b"PK")


def test_formula_like_text_stays_text_in_excel():
    import openpyxl

    out = convert(b"name,value\nA,=1+1\nB,@SUM(A1)\n", "data.csv", "xlsx")
    ws = openpyxl.load_workbook(io.BytesIO(out.data)).active
    assert ws["B2"].value == "=1+1" and ws["B2"].data_type == "s"


def test_wide_and_huge_tables_fit_in_a_pdf():
    wide = "\n".join(",".join(f"c{r}{c}" for c in range(20)) for r in range(5)).encode()
    assert convert(wide, "wide.csv", "pdf").data.startswith(b"%PDF")
    very_wide = "\n".join(",".join(f"v{c}" for c in range(45)) for _r in range(3)).encode()
    out = convert(very_wide, "very-wide.csv", "pdf")
    assert out.data.startswith(b"%PDF") and any("lines of text" in n for n in out.notes)
    huge = ("a,b\n" + "x," + "word " * 3000 + "\n").encode()
    assert convert(huge, "huge.csv", "pdf").data.startswith(b"%PDF")


def test_big_spreadsheet_to_word_is_fast():
    import time

    rows = "\n".join(f"{i},name {i},{i * 3}" for i in range(3000))
    start = time.time()
    out = convert(("id,name,score\n" + rows).encode(), "big.csv", "docx")
    assert out.data.startswith(b"PK") and time.time() - start < 60


# ------------------------------------------------------------------ never crash
def corrupted(data: bytes) -> list[bytes]:
    middle = len(data) // 2
    return [
        data[: max(1, len(data) // 2)],  # cut off
        data[:middle] + bytes(200) + data[middle + 200 :],  # a block of zeros
        data[:16] + bytes(b ^ 0x5A for b in data[16:64]) + data[64:],  # scrambled header area
    ]


SOURCES = {
    "notes.docx": samples.docx_bytes,
    "deck.pptx": samples.pptx_bytes,
    "marks.xlsx": samples.xlsx_bytes,
    "notes.pdf": samples.pdf_bytes,
    "photo.png": samples.png,
    "plan.md": lambda: samples.MARKDOWN,
    "page.html": lambda: samples.HTML,
}


@pytest.mark.parametrize("name", list(SOURCES))
def test_damaged_files_never_crash(name):
    """Whatever the damage, the user gets either a result or a clear ConvertError."""
    original = SOURCES[name]()
    for i, data in enumerate(corrupted(original)):
        try:
            kind = detect(name, data)
        except ConvertError:
            continue
        for target in TARGETS[kind]:
            try:
                out = convert(data, name, target)
                assert out.data
            except ConvertError as exc:
                assert str(exc)  # a readable message
            except Exception as exc:  # pragma: no cover - this is what the test is guarding
                pytest.fail(f"{name} (damage {i}) -> {target} raised {type(exc).__name__}: {exc}")


def test_damaged_pdfs_in_tools_never_crash():
    pdf = samples.pdf_bytes()
    for data in corrupted(pdf):
        for tool, files, options in (
            ("merge", [("a.pdf", data), ("b.pdf", pdf)], {}),
            ("split", [("a.pdf", data)], {"mode": "each"}),
            ("rotate", [("a.pdf", data)], {"angle": "90"}),
            ("compress", [("a.pdf", data)], {"level": "high"}),
        ):
            try:
                run_tool(tool, files, options)
            except ConvertError:
                pass
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"{tool} raised {type(exc).__name__}: {exc}")
    with pytest.raises(ConvertError, match="90, 180 or 270"):
        run_tool("rotate", [("a.pdf", pdf)], {"angle": "sideways"})


def test_tables_stay_tables():
    assert isinstance(read("csv", b"a,b\n1,2\n", "x.csv").blocks[0], Table)

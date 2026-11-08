import json
import re

from fastapi.testclient import TestClient

from app.browser import convert_file, inspect, run_tool_files
from app.config import Settings
from app.main import content_disposition, create_app
from scripts.build_pages import BROWSER_PACKAGES, SERVER_ONLY, browser_modules, build
from tests import samples


def client(**settings) -> TestClient:
    return TestClient(create_app(Settings(**settings)))


def test_health_and_formats():
    c = client()
    assert c.get("/api/health").json()["status"] == "ok"
    formats = c.get("/api/formats").json()
    assert formats["mode"] == "server" and formats["targets"]["pdf"][0] == "docx"
    assert formats["extensions"][".pptx"] == "pptx" and ".doc" in formats["legacy"]


def test_convert_endpoint():
    res = client().post(
        "/api/convert",
        files={"file": ("Résumé.docx", samples.docx_bytes())},
        data={"target": "pdf", "page_size": "Letter"},
    )
    assert res.status_code == 200 and res.content.startswith(b"%PDF-")
    info = json.loads(res.headers["x-fileflip-info"])
    assert info["filename"] == "Résumé.pdf" and info["bytes"] == len(res.content)
    assert "filename*=UTF-8''R%C3%A9sum%C3%A9.pdf" in res.headers["content-disposition"]


def test_convert_errors():
    c = client()
    bad = c.post("/api/convert", files={"file": ("a.doc", b"\xd0\xcf")}, data={"target": "pdf"})
    assert bad.status_code == 400 and ".docx" in bad.json()["detail"]
    wrong = c.post("/api/convert", files={"file": ("a.png", samples.png())}, data={"target": "xlsx"})
    assert wrong.status_code == 400
    big = client(max_upload_bytes=100).post("/api/convert", files={"file": ("a.txt", b"x" * 500)}, data={"target": "pdf"})
    assert big.status_code == 413


def test_tools_endpoint():
    c = client()
    pdf = samples.pdf_bytes()
    res = c.post("/api/tools/merge", files=[("files", ("a.pdf", pdf)), ("files", ("b.pdf", pdf))])
    assert res.status_code == 200 and json.loads(res.headers["x-fileflip-info"])["filename"] == "merged.pdf"
    res = c.post("/api/tools/split", files=[("files", ("a.pdf", pdf))], data={"options": '{"mode": "each"}'})
    assert res.headers["content-type"] == "application/zip"
    assert c.post("/api/tools/nope", files=[("files", ("a.pdf", pdf))]).status_code == 404
    assert c.post("/api/tools/split", files=[("files", ("a.pdf", pdf))], data={"options": "[1]"}).status_code == 400
    many = client(max_files=1).post("/api/tools/merge", files=[("files", ("a.pdf", pdf)), ("files", ("b.pdf", pdf))])
    assert many.status_code == 400


def test_rate_limit_and_site():
    c = client(requests_per_minute=1)
    assert c.post("/api/convert", files={"file": ("a.txt", b"hi")}, data={"target": "html"}).status_code == 200
    limited = c.post("/api/convert", files={"file": ("a.txt", b"hi")}, data={"target": "html"})
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    page = c.get("/")
    assert page.status_code == 200 and "FileFlip" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert c.get("/browser-mode.json").status_code == 404  # so the UI uses the server engine


def test_content_disposition():
    assert content_disposition('a "b".pdf').startswith('attachment; filename="a _b_.pdf"')


# ------------------------------------------------------------------ browser (Pyodide) entry point
def test_browser_entry_points(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    src.write_bytes(samples.docx_bytes())
    info = json.loads(inspect(str(src), "notes.docx"))
    assert info["kind"] == "docx" and info["packages"]["pdf"] == ["fpdf2", "python-docx"]
    result = json.loads(convert_file(str(src), "notes.docx", "md", "A4", str(out)))
    assert result["ok"] and result["result"]["filename"] == "notes.md" and out.read_text(encoding="utf-8").startswith("#")
    assert not src.exists()  # input copy removed

    src.write_bytes(b"\xd0\xcf")
    assert json.loads(inspect(str(src), "old.doc"))["ok"] is False

    paths = []
    for i in range(2):
        p = tmp_path / f"p{i}"
        p.write_bytes(samples.png())
        paths.append(str(p))
    result = json.loads(run_tool_files("images-to-pdf", json.dumps(paths), json.dumps(["a.png", "b.png"]), "{}", str(out)))
    assert result["ok"] and out.read_bytes().startswith(b"%PDF-")


def test_pages_build(tmp_path):
    site = build(tmp_path / "_site")
    config = json.loads((site / "browser-mode.json").read_text())
    assert config["mode"] == "browser" and config["formats"]["targets"]["docx"][0] == "pdf"
    assert set(config["packages"]) == set(BROWSER_PACKAGES)
    for path in config["modules"]:
        assert (site / "py" / path).is_file(), path
    for font in config["fonts"]:
        assert (site / "py" / "app" / "fonts" / font).is_file()
    assert not any(p.endswith(tuple(f"app/{m}" for m in SERVER_ONLY)) for p in config["modules"])
    assert 'src="/static' not in (site / "index.html").read_text()  # relative paths for /fileflip/


def test_browser_modules_avoid_server_libraries():
    """Code shipped to the browser must not import FastAPI or other server-only packages."""
    allowed = {
        "__future__", "base64", "csv", "dataclasses", "datetime", "functools", "html", "http", "io", "json", "os",
        "pathlib", "re", "typing", "urllib", "zipfile", "collections",
        "PIL", "docx", "pptx", "openpyxl", "pdfminer", "pypdf", "fpdf", "fontTools", "markdown",
    }
    for rel in browser_modules():
        source = open(rel, encoding="utf-8").read()
        imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.MULTILINE)
        third_party = {i.split(".")[0] for i in imports if not i.startswith(".")} - allowed
        assert not third_party, f"{rel} imports {third_party}"

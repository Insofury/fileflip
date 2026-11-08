"""Build the static GitHub Pages version of FileFlip into ./_site

GitHub Pages can only host static files, so this build ships the front-end, the Python
package (minus the web-server modules) and the fonts. In the browser, a Web Worker loads
Pyodide (Python compiled to WebAssembly) and runs the exact same conversion code the
server uses; the libraries each conversion needs are installed on first use.

    python scripts/build_pages.py            # writes ./_site
    python -m http.server -d _site 8070      # preview at http://localhost:8070
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PYODIDE_VERSION = "0.26.4"  # Python 3.12 + Pillow, lxml, cryptography and fontTools
# Pure-Python wheels installed with micropip, pinned to versions tested in Pyodide 0.26.4
BROWSER_PACKAGES = {
    "pypdf": "pypdf==7.0.0",
    "pdfminer.six": "pdfminer.six==20260107",
    "python-docx": "python-docx==1.2.0",
    "python-pptx": "python-pptx==1.0.2",
    "openpyxl": "openpyxl==3.1.5",
    "fpdf2": "fpdf2==2.9.0",
    "markdown": "markdown==3.11.3",
}
SERVER_ONLY = {"main.py", "config.py", "security.py"}  # FastAPI parts, not needed in the browser


def browser_modules() -> list[str]:
    """Every .py file of the app package except the web-server ones, as 'app/...' paths."""
    files = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if path.name in SERVER_ONLY and path.parent == ROOT / "app":
            continue
        if "__pycache__" in rel:
            continue
        files.append(rel)
    return files


def build(out: Path = ROOT / "_site") -> Path:
    from app import __version__
    from app.fonts import FILES as FONT_FILES
    from app.formats import READER_PACKAGES, TOOL_PACKAGES, WRITER_PACKAGES, formats_info

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "static", out / "static")
    shutil.copy2(ROOT / "static" / "index.html", out / "index.html")
    shutil.copy2(ROOT / "static" / "favicon.svg", out / "favicon.ico")

    modules = browser_modules()
    for rel in modules:
        dest = out / "py" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    fonts = sorted(set(FONT_FILES.values()))
    for name in fonts + ["LICENSE-DejaVu.txt"]:
        shutil.copy2(ROOT / "app" / "fonts" / name, out / "py" / "app" / "fonts" / name)

    formats = formats_info(max_upload_mb=25, max_files=20)
    config = {
        "mode": "browser",
        "version": __version__,
        "pyodide_base": f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/",
        "packages": BROWSER_PACKAGES,
        "reader_packages": READER_PACKAGES,
        "writer_packages": WRITER_PACKAGES,
        "tool_packages": TOOL_PACKAGES,
        "modules": modules,
        "fonts": fonts,
        "formats": formats,
    }
    (out / "browser-mode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")  # serve files as-is (no Jekyll processing)
    return out


if __name__ == "__main__":
    site = build()
    files = sorted(p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file())
    print(f"Built {len(files)} files into {site}")

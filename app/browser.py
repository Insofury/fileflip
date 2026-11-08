"""Entry point for the GitHub Pages build: JavaScript calls these functions via Pyodide.

Files move through Pyodide's in-memory file system, so nothing leaves the browser.
Every function returns JSON: {"ok": true, ...} or {"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
import os

from .convert import convert, run_tool
from .formats import KINDS, TARGETS, TOOL_PACKAGES, detect, packages_for
from .model import ConvertError


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _error(exc: BaseException) -> str:
    if isinstance(exc, ConvertError):
        message = str(exc)
    elif isinstance(exc, MemoryError):
        message = "This file is too big to convert in the browser. Try a smaller one."
    else:
        message = f"Something went wrong while converting this file ({type(exc).__name__}: {exc})."[:300]
    return json.dumps({"ok": False, "error": message})


def inspect(path: str, filename: str) -> str:
    """What kind of file is this, what can it become, and which packages does each need."""
    try:
        kind = detect(filename, _read(path))
        return json.dumps(
            {
                "ok": True,
                "kind": kind,
                "label": KINDS[kind],
                "targets": TARGETS[kind],
                "packages": {t: packages_for(kind, t) for t in TARGETS[kind]},
            }
        )
    except Exception as exc:  # noqa: BLE001 - every error goes back to the UI as a message
        return _error(exc)


def tool_packages(tool: str) -> str:
    return json.dumps(TOOL_PACKAGES.get(tool, []))


def convert_file(path: str, filename: str, target: str, page_size: str, out_path: str) -> str:
    try:
        output = convert(_read(path), filename, target, page_size)
        with open(out_path, "wb") as fh:
            fh.write(output.data)
        return json.dumps({"ok": True, "result": output.info()})
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    finally:
        if os.path.exists(path):
            os.remove(path)


def run_tool_files(tool: str, paths_json: str, names_json: str, options_json: str, out_path: str) -> str:
    paths, names = json.loads(paths_json), json.loads(names_json)
    try:
        files = [(name, _read(path)) for path, name in zip(paths, names)]
        output = run_tool(tool, files, json.loads(options_json or "{}"))
        with open(out_path, "wb") as fh:
            fh.write(output.data)
        return json.dumps({"ok": True, "result": output.info()})
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    finally:
        for path in paths:
            if os.path.exists(path):
                os.remove(path)

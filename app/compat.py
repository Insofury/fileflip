"""Small shims so the same code runs on a server and in the browser (Pyodide)."""

from __future__ import annotations


def fpdf():
    """Import fpdf2's FPDF class.

    In Pyodide there is no `ssl` module, so `urllib.request.HTTPSHandler` and
    `http.client.HTTPSConnection` don't exist. fpdf2 subclasses both at import time (for
    fetching images from URLs - which we never do), so placeholder classes let the import
    succeed."""
    import http.client
    import urllib.request

    if not hasattr(http.client, "HTTPSConnection"):  # pragma: no cover - browser only

        class HTTPSConnection(http.client.HTTPConnection):
            pass

        http.client.HTTPSConnection = HTTPSConnection
    if not hasattr(urllib.request, "HTTPSHandler"):  # pragma: no cover - browser only

        class HTTPSHandler(urllib.request.BaseHandler):
            pass

        urllib.request.HTTPSHandler = HTTPSHandler
    from fpdf import FPDF

    return FPDF

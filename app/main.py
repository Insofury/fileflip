"""FastAPI application: the conversion API and the web app.

GET  /                    the web app
GET  /api/health          health check
GET  /api/formats         what converts to what, tools and limits
POST /api/convert         one file + target format -> the converted file
POST /api/tools/{tool}    merge | split | rotate | compress | images-to-pdf
GET  /api/docs            interactive API docs
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from functools import partial
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import STATIC_DIR, Settings
from .convert import Output, convert, run_tool
from .formats import TOOLS, formats_info
from .model import ConvertError
from .security import BodySizeLimitMiddleware, RateLimiter, SecurityHeadersMiddleware, client_ip

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fileflip")


def content_disposition(filename: str) -> str:
    """attachment header that works for non-English names (RFC 6266 / 5987)."""
    fallback = filename.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    limiter = RateLimiter(settings.requests_per_minute)
    slots = asyncio.Semaphore(settings.concurrency)

    app = FastAPI(
        title="FileFlip API",
        version=__version__,
        description="Convert PDF, Word, PowerPoint, Excel, CSV, Markdown, HTML, text and images.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_upload_bytes * settings.max_files + 1024 * 1024,
        message=f"The upload is too large. Each file can be up to {settings.max_upload_mb} MB.",
    )
    app.add_middleware(SecurityHeadersMiddleware)
    logger.info("FileFlip v%s starting", __version__)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """Last line of defence: log the details, show the user a calm message."""
        logger.exception("Unexpected error on %s", request.url.path)
        return JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)

    def limit(request: Request) -> None:
        allowed, retry_after = limiter.hit(client_ip(request, settings.trust_proxy))
        if not allowed:
            wait = max(1, math.ceil(retry_after))
            raise HTTPException(429, f"Too many requests - please wait {wait}s.", headers={"Retry-After": str(wait)})

    async def read_upload(upload: UploadFile) -> tuple[str, bytes]:
        data = await upload.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"{upload.filename or 'A file'} is over the {settings.max_upload_mb} MB limit.")
        return upload.filename or "file", data

    async def run(job) -> Output:
        try:
            async with slots:  # conversions are CPU-heavy: bound how many run at once
                return await run_in_threadpool(job)
        except ConvertError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    def respond(output: Output) -> Response:
        return Response(
            content=output.data,
            media_type=output.mime,
            headers={
                "Content-Disposition": content_disposition(output.filename),
                "X-FileFlip-Info": json.dumps(output.info(), ensure_ascii=True),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/formats", tags=["meta"])
    async def formats() -> dict:
        return {**formats_info(settings.max_upload_mb, settings.max_files), "mode": "server"}

    @app.post("/api/convert", tags=["convert"], response_class=Response)
    async def convert_route(
        request: Request,
        file: UploadFile = File(...),
        target: str = Form(..., description="pdf, docx, xlsx, csv, txt, md, html, jpg, png or webp"),
        page_size: str = Form("A4", description="A4 or Letter (for PDF and Word output)"),
    ) -> Response:
        """Convert one file. Details (name, size, notes) are in the `X-FileFlip-Info` header."""
        limit(request)
        name, data = await read_upload(file)
        size = "Letter" if page_size.lower() == "letter" else "A4"
        return respond(await run(partial(convert, data, name, target, size)))

    @app.post("/api/tools/{tool}", tags=["pdf tools"], response_class=Response)
    async def tool_route(
        tool: str,
        request: Request,
        files: list[UploadFile] = File(...),
        options: str = Form("{}", description='JSON, e.g. {"mode": "ranges", "ranges": "1-3, 5"}'),
    ) -> Response:
        """merge (2+ PDFs), split, rotate, compress (PDFs) and images-to-pdf (images)."""
        limit(request)
        if tool not in TOOLS:
            raise HTTPException(404, f"Unknown tool '{tool}'.")
        if len(files) > settings.max_files:
            raise HTTPException(400, f"Use at most {settings.max_files} files at a time.")
        try:
            opts = json.loads(options or "{}")
            if not isinstance(opts, dict):
                raise ValueError
        except ValueError as exc:
            raise HTTPException(400, "The options must be a JSON object.") from exc
        uploads = [await read_upload(f) for f in files]
        return respond(await run(partial(run_tool, tool, uploads, opts)))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app


app = create_app()

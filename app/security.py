"""Security helpers: per-IP rate limiting, client IP detection, upload size limit and headers."""

from __future__ import annotations

import json
import time
from collections import deque

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class RateLimiter:
    """Sliding-window limiter: at most `limit` hits per `window` seconds per key."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False, hits[0] + self.window - now
        hits.append(now)
        if len(self._hits) > 10_000:  # keep memory bounded
            for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
                del self._hits[k]
        return True, 0.0


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class BodySizeLimitMiddleware:
    """Rejects requests whose Content-Length exceeds `max_bytes` with HTTP 413,
    before the upload is read."""

    def __init__(self, app: ASGIApp, max_bytes: int, message: str = "The upload is too large."):
        self.app, self.max_bytes = app, max_bytes
        self.body = json.dumps({"detail": message}).encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            length = dict(scope.get("headers", [])).get(b"content-length")
            if length and length.isdigit() and int(length) > self.max_bytes:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(self.body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": self.body})
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds CSP and other security headers (pure ASGI, so it never buffers responses)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        add_csp = not scope.get("path", "").startswith(("/api/docs", "/api/openapi.json"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers.setdefault(name, value)
                if add_csp:
                    headers.setdefault("Content-Security-Policy", CSP)
            await send(message)

        await self.app(scope, receive, send_wrapper)

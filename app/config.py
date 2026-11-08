"""Settings read from environment variables (or a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader - real environment variables always take priority."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    max_upload_bytes: int = 25 * 1024 * 1024  # per file
    max_files: int = 20  # per request (merge / images to PDF)
    requests_per_minute: int = 60  # per IP
    concurrency: int = 4  # conversions running at once
    trust_proxy: bool = False

    @property
    def max_upload_mb(self) -> int:
        return self.max_upload_bytes // (1024 * 1024)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            max_upload_bytes=max(1, _int("MAX_UPLOAD_MB", 25)) * 1024 * 1024,
            max_files=max(1, _int("MAX_FILES", 20)),
            requests_per_minute=_int("REQUESTS_PER_MINUTE", 60),
            concurrency=max(1, _int("CONCURRENCY", 4)),
            trust_proxy=_bool("TRUST_PROXY", False),
        )

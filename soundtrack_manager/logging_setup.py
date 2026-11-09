"""Logging with secret redaction and a rotating file under the data dir."""

from __future__ import annotations

import logging
import re
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    (re.compile(r"(?i)(cookie|set-cookie|authorization|x-admin-token)(\s*[:=]\s*)([^\r\n]+)"), r"\1\2***"),
    (re.compile(r"(?i)(password|passwd|_xftoken|cf_clearance|xf_session|xf_user)(\s*[=:]\s*)([^\s&;,]+)"), r"\1\2***"),
]


class RedactingFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.secrets: set[str] = set()

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.secrets.add(value)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for pat, repl in _PATTERNS:
            text = pat.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = self.redact(msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


REDACTOR = RedactingFilter()


def configure(level: str, log_path: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(REDACTOR)
    root.addHandler(console)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(REDACTOR)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def tail(path: Path, lines: int = 200) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return [REDACTOR.redact(ln.rstrip("\n")) for ln in deque(f, maxlen=lines)]
    except FileNotFoundError:
        return []

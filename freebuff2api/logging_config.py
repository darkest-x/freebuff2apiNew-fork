from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from itertools import count
from typing import Any

from .config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

RESET = "\033[0m"
COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[35m",
}

# 明文日志开关（FREEBUFF_LOG_PLAINTEXT=true 时置为 True）。
# 个人自用调试场景：把入站/出站请求头（含 Authorization）与请求/响应体
# 完整写入日志（管理面板运行日志页 + stdout），替代抓包。
# 默认 False：authorization/cookie 照常打码，body 照常截断。
_plaintext = False


@dataclass(frozen=True)
class BufferedLogRecord:
    id: int
    time: str
    level: str
    logger: str
    message: str
    detail: str = ""


class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.capacity = max(capacity, 100)
        self._records: deque[BufferedLogRecord] = deque(maxlen=self.capacity)
        self._counter = count(1)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail = ""
            if record.exc_info:
                detail = self.formatException(record.exc_info)
            item = BufferedLogRecord(
                id=next(self._counter),
                time=self.formatTime(record),
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
                detail=detail,
            )
            with self._lock:
                self._records.append(item)
        except Exception:
            self.handleError(record)

    def formatTime(self, record: logging.LogRecord) -> str:
        formatter = logging.Formatter(datefmt=DATE_FORMAT)
        return formatter.formatTime(record, DATE_FORMAT)

    def records(
        self,
        *,
        since_id: int = 0,
        limit: int = 200,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), self.capacity)
        selected_level = level.upper() if level else None
        with self._lock:
            items = list(self._records)
        if since_id > 0:
            items = [item for item in items if item.id > since_id]
        if selected_level:
            items = [item for item in items if item.level == selected_level]
        return [item.__dict__ for item in items[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_memory_handler: InMemoryLogHandler | None = None


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = COLORS.get(record.levelno)
        if not color:
            return message
        return f"{color}{message}{RESET}"


def configure_logging(settings: Settings) -> None:
    global _memory_handler, _plaintext
    _plaintext = bool(getattr(settings, "log_plaintext", False))

    handler = logging.StreamHandler(sys.stdout)
    formatter_cls = ColorFormatter if settings.log_color else logging.Formatter
    handler.setFormatter(formatter_cls(LOG_FORMAT, datefmt=DATE_FORMAT))

    _memory_handler = InMemoryLogHandler(settings.admin_log_lines)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(_memory_handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    logging.getLogger("httpx").setLevel(logging.DEBUG if settings.debug else logging.WARNING)
    logging.getLogger("freebuff2api").debug(
        "logging configured debug=%s level=%s body_chars=%s plaintext=%s color=%s",
        settings.debug,
        settings.log_level,
        settings.log_body_chars,
        _plaintext,
        settings.log_color,
    )


def get_buffered_logs(
    *,
    since_id: int = 0,
    limit: int = 200,
    level: str | None = None,
) -> list[dict[str, Any]]:
    if _memory_handler is None:
        return []
    return _memory_handler.records(since_id=since_id, limit=limit, level=level)


def clear_buffered_logs() -> None:
    if _memory_handler is not None:
        _memory_handler.clear()


def render_debug(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)

    # 明文模式：完整输出，不截断。
    if _plaintext:
        return text
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    # 明文模式：返回原始 headers（含 Authorization / Cookie）。
    if _plaintext:
        return dict(headers)
    redacted = {}
    for key, value in headers.items():
        if key.lower() in {"authorization", "cookie", "set-cookie"}:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted

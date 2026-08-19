from __future__ import annotations

import json
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import count
from typing import Any

from .config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
BEIJING_TZ = timezone(timedelta(hours=8))


def _format_time_beijing(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, BEIJING_TZ).strftime(DATE_FORMAT)

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


# [FP-8 确定] 2026-08-19：`logging.Handler` 并没有 `formatException` 方法
# （那是 `logging.Formatter` 的方法）。旧代码在 emit() 里直接调 self.formatException(...)，
# 只要有任何一条带 exc_info 的日志就抛 AttributeError，被 handleError 吞掉 ——
# 结果是「记录异常」这条路径本身坏了，真实的上游错误反而看不到。
# log.txt 实录：AttributeError: 'InMemoryLogHandler' object has no attribute 'formatException'
_EXC_FORMATTER = logging.Formatter()


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
                detail = _EXC_FORMATTER.formatException(record.exc_info)
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
        return _format_time_beijing(record)

    def records(
        self,
        *,
        since_id: int = 0,
        limit: int = 200,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        selected_level = level.upper() if level else None
        with self._lock:
            items = list(self._records)
        if since_id > 0:
            items = [item for item in items if item.id > since_id]
        if selected_level:
            items = [item for item in items if item.level == selected_level]
        # limit=0 means all retained records; positive values retain the old
        # count-based behavior for the log table.
        if limit > 0:
            items = items[-min(limit, self.capacity):]
        return [item.__dict__ for item in items]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_memory_handler: InMemoryLogHandler | None = None


class ColorFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return _format_time_beijing(record)

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
    if settings.log_color:
        formatter_cls = ColorFormatter
    else:
        class PlainBeijingFormatter(logging.Formatter):
            def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
                return _format_time_beijing(record)
        formatter_cls = PlainBeijingFormatter
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


MAX_LOG_EXPORT_BYTES = 30 * 1024 * 1024


def get_buffered_logs(
    *,
    since_id: int = 0,
    limit: int = 200,
    level: str | None = None,
    since_min: int | None = None,
    max_bytes: int | None = None,
) -> list[dict[str, Any]]:
    if _memory_handler is None:
        return []

    # Export mode: no message-count cap; only apply the explicit byte cap.
    export_limit = max_bytes is not None or since_min is not None or limit == 0
    items = _memory_handler.records(
        since_id=since_id,
        limit=0 if export_limit else limit,
        level=level,
    )

    if since_min is not None:
        since_min = max(0, since_min)
        now = datetime.now(BEIJING_TZ).replace(tzinfo=None)
        cutoff = now - timedelta(minutes=since_min)
        filtered: list[dict[str, Any]] = []
        for item in items:
            try:
                item_time = datetime.strptime(item.get("time", ""), DATE_FORMAT)
            except (TypeError, ValueError):
                continue
            if item_time >= cutoff:
                filtered.append(item)
        items = filtered

    if max_bytes is None:
        return items

    byte_limit = min(max(1, max_bytes), MAX_LOG_EXPORT_BYTES)
    # Prefer the newest records when the 30MB cap is reached, then restore
    # chronological order for copying/reading.
    selected: list[dict[str, Any]] = []
    total = 0
    for item in reversed(items):
        encoded = (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if selected and total + len(encoded) > byte_limit:
            break
        if not selected and len(encoded) > byte_limit:
            # Keep at least one record even if an individual log is oversized.
            selected.append(item)
            break
        selected.append(item)
        total += len(encoded)
    selected.reverse()
    return selected


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

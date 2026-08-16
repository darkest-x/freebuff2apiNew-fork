from __future__ import annotations

import json
import logging
import urllib.request
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger("freebuff2api.config")

HAR_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_ADMIN_KEY = "sk-admin"

# 默认兜底指纹必须是“干净的”：绝不回退到亚洲/中国时区。
# 服务端会把设备时区/locale 用于地区访问层判定（limited access tier），
# Asia/Shanghai + zh-CN 会被归入受限层，导致 pro/luna 等 premium 模型被拒。
GEO_FALLBACK: dict[str, str] = {
    "timezone": "America/Los_Angeles",
    "locale": "en-US",
    "country": "United States",
    "countryCode": "US",
}

_last_geo: dict[str, str] = {}


def _locale_for_country(country_code: str) -> str:
    """Map an ISO country code to a plausible locale (never zh-CN by default)."""
    code = (country_code or "").upper()
    if code == "GB":
        return "en-GB"
    if code == "CA":
        return "en-CA"
    if code == "AU":
        return "en-AU"
    if code in {"DE", "AT", "CH"}:
        return "de-DE"
    if code == "FR":
        return "fr-FR"
    if code == "ES":
        return "es-ES"
    if code == "IT":
        return "it-IT"
    if code == "NL":
        return "nl-NL"
    if code == "JP":
        return "ja-JP"
    if code == "KR":
        return "ko-KR"
    return "en-US"


def detect_geo(timeout: float = 4.0) -> dict[str, str]:
    """Detect server public-IP geo (timezone/locale/country) via free IP APIs.

    Probe order: ipinfo.io first (observed most reliable on Railway), then
    ipwho.is, then ipapi.co (free tier is often 429 rate-limited).
    Never raises; falls back to GEO_FALLBACK when all probes fail.
    """
    global _last_geo
    for url in (
        "https://ipinfo.io/json",
        "https://ipwho.is/",
        "https://ipapi.co/json/",
    ):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "freebuff2api/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if url.startswith("http://ip-api.com"):
                if data.get("status") != "success":
                    continue
                timezone = data.get("timezone") or GEO_FALLBACK["timezone"]
                cc = data.get("countryCode") or "US"
                country = data.get("country") or GEO_FALLBACK["country"]
            else:
                timezone = data.get("timezone") or GEO_FALLBACK["timezone"]
                cc = data.get("country_code") or data.get("country") or "US"
                country = data.get("country_name") or data.get("country") or GEO_FALLBACK["country"]
            geo = {
                "timezone": timezone,
                "locale": _locale_for_country(cc),
                "country": country,
                "countryCode": cc,
            }
            _last_geo = dict(geo)
            logger.info(
                "geo detected url=%s timezone=%s locale=%s country=%s",
                url,
                geo["timezone"],
                geo["locale"],
                geo["country"],
            )
            return geo
        except Exception as exc:
            logger.debug("geo detect failed url=%s error=%s", url, exc)
            continue
    _last_geo = dict(GEO_FALLBACK)
    logger.warning("geo detect failed for all sources; using fallback %s", GEO_FALLBACK)
    return dict(GEO_FALLBACK)


def last_geo_info() -> dict[str, str]:
    """Return the last detection result (empty dict if never detected)."""
    return dict(_last_geo) if _last_geo else {}


def refresh_geo(settings: Settings) -> dict[str, str]:
    """Re-detect geo and apply to a live Settings instance (admin UI refresh)."""
    geo = detect_geo()
    # Settings is frozen; object.__setattr__ is the intended escape hatch for
    # runtime-updated admin fields (same pattern as proxy/token settings).
    object.__setattr__(settings, "timezone", geo["timezone"])
    object.__setattr__(settings, "locale", geo["locale"])
    try:
        write_env_values(
            {
                "FREEBUFF_TIMEZONE": geo["timezone"],
                "FREEBUFF_LOCALE": geo["locale"],
            }
        )
    except Exception as exc:
        logger.warning("could not persist geo to .env: %s", exc)
    return geo


@dataclass(frozen=True)
class Settings:
    codebuff_token: str | None
    local_api_key: str | None
    admin_key: str | None = None
    codebuff_base_url: str = "https://www.codebuff.com"
    zeroclick_base_url: str = "https://zeroclick.dev"
    session_id: str = ""
    client_id: str = ""
    ad_providers: tuple[str, ...] = ("gravity", "carbon")
    request_timeout: float = 60.0
    debug: bool = False
    log_level: str = "INFO"
    log_body_chars: int = 2000
    log_plaintext: bool = False
    log_color: bool = True
    admin_log_lines: int = 1000
    host: str = "0.0.0.0"
    port: int = 8000
    proxy_enabled: bool = False
    proxy_type: str = "socks5"
    proxy_host: str = ""
    proxy_port: int = 1080
    proxy_username: str | None = None
    proxy_password: str | None = None
    timezone: str = "America/Los_Angeles"
    locale: str = "en-US"
    os_name: str = "windows"
    system_prompt_override: str | None = None
    api_keys_json: str | None = None
    max_request_records: int = 5000
    max_concurrency_per_account: int = 1

    @property
    def codebuff_api_url(self) -> str:
        return self.codebuff_base_url.strip().rstrip("/")

    @property
    def zeroclick_api_url(self) -> str:
        return self.zeroclick_base_url.rstrip("/")

    @property
    def upstream_proxy_url(self) -> str | None:
        if not self.proxy_enabled:
            return None
        if not self.proxy_host:
            return None
        auth = ""
        if self.proxy_username:
            auth = f"{self.proxy_username}:{self.proxy_password or ''}@"
        return f"{self.proxy_type}://{auth}{self.proxy_host}:{self.proxy_port}"

    @property
    def codebuff_tokens(self) -> tuple[str, ...]:
        if not self.codebuff_token:
            return ()
        values = [item.strip() for item in self.codebuff_token.split(",")]
        return tuple(item for item in values if item)


def _csv(name: str, default: str) -> tuple[str, ...]:
    values = [item.strip() for item in os.getenv(name, default).split(",")]
    return tuple(item for item in values if item)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _api_base_url() -> str:
    return (
        os.getenv("FREEBUFF_API_BASE_URL")
        or os.getenv("CODEBUFF_BASE_URL")
        or "https://www.codebuff.com"
    )


def load_settings() -> Settings:
    debug = _bool("FREEBUFF_DEBUG", False)
    log_level = "DEBUG" if debug else os.getenv("FREEBUFF_LOG_LEVEL", "INFO")
    color_default = os.getenv("NO_COLOR") is None

    # P0-A: 时区/地区指纹自动识别。
    # Railway 等容器平台每次部署出口 IP 可能不同（欧/美西/美东），
    # 启动时检测一次并写入内存；用户手动设置的环境变量永远优先。
    timezone = os.getenv("FREEBUFF_TIMEZONE")
    locale = os.getenv("FREEBUFF_LOCALE")
    if not timezone or not locale:
        geo = detect_geo()
        if not timezone:
            timezone = geo["timezone"]
        if not locale:
            locale = geo["locale"]

    return Settings(
        codebuff_token=os.getenv("FREEBUFF_TOKEN") or os.getenv("CODEBUFF_TOKEN"),
        local_api_key=os.getenv("FREEBUFF_API_KEY") or os.getenv("OPENAI_API_KEY"),
        admin_key=os.getenv("FREEBUFF_ADMIN_KEY") or DEFAULT_ADMIN_KEY,
        codebuff_base_url=_api_base_url(),
        zeroclick_base_url=os.getenv("ZEROCLICK_BASE_URL", "https://zeroclick.dev"),
        session_id=os.getenv("FREEBUFF_SESSION_ID", str(uuid.uuid4())),
        # 官方 SDK 要求 client_id = clientSessionId（会话级稳定标识）。
        # 使用完整 UUID 而不是随机 11 位 hex，贴近官方格式，降低指纹风险。
        client_id=os.getenv("FREEBUFF_CLIENT_ID", str(uuid.uuid4())),
        ad_providers=_csv("FREEBUFF_AD_PROVIDERS", "gravity,carbon"),
        request_timeout=float(os.getenv("FREEBUFF_TIMEOUT", "60")),
        debug=debug,
        log_level=log_level,
        log_body_chars=_int("FREEBUFF_LOG_BODY_CHARS", 0 if debug else 2000),
        log_plaintext=_bool("FREEBUFF_LOG_PLAINTEXT", False),
        log_color=_bool("FREEBUFF_LOG_COLOR", color_default),
        admin_log_lines=_int("FREEBUFF_ADMIN_LOG_LINES", 1000),
        host=os.getenv("FREEBUFF_HOST", "0.0.0.0"),
        port=_int("FREEBUFF_PORT", 8000),
        proxy_enabled=_bool("FREEBUFF_PROXY_ENABLED", False),
        proxy_type=os.getenv("FREEBUFF_PROXY_TYPE", "socks5"),
        proxy_host=os.getenv("FREEBUFF_PROXY_HOST", ""),
        proxy_port=_int("FREEBUFF_PROXY_PORT", 1080),
        proxy_username=os.getenv("FREEBUFF_PROXY_USERNAME"),
        proxy_password=os.getenv("FREEBUFF_PROXY_PASSWORD"),
        timezone=timezone,
        locale=locale,
        os_name=os.getenv("FREEBUFF_OS", "windows"),
        system_prompt_override=os.getenv("FREEBUFF_SYSTEM_PROMPT_OVERRIDE"),
        api_keys_json=os.getenv("FREEBUFF_API_KEYS"),
        max_request_records=_int("FREEBUFF_MAX_REQUEST_RECORDS", 5000),
        max_concurrency_per_account=_int("FREEBUFF_ACCOUNT_CONCURRENCY", 1),
    )


def project_env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def write_env_values(values: dict[str, str | None], env_path: Path | None = None) -> None:
    path = env_path or project_env_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        name = line.split("=", 1)[0].strip()
        if name in pending:
            value = pending.pop(name)
            if value is not None:
                output.append(f"{name}={value}")
            continue
        output.append(line)

    for name, value in pending.items():
        if value is not None:
            output.append(f"{name}={value}")

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

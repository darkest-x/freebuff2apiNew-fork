from __future__ import annotations

import asyncio
import datetime
import hmac
import json
import logging
import os
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .codebuff import CodebuffAccountPool, CodebuffClient, CodebuffError
from .config import (
    DEFAULT_ADMIN_KEY,
    Settings,
    last_geo_info,
    project_env_path,
    refresh_geo,
    write_env_values,
)
from .logging_config import clear_buffered_logs, get_buffered_logs
from .models import ALL_MODELS, DEFAULT_MODEL, get_model_registry, models_response
from .usage import ApiKeyRecord
from .usage_store import ApiKeyStore, RequestStore


COOKIE_NAME = "freebuff_admin_session"
COOKIE_MAX_AGE = 60 * 60 * 12
NO_STORE_HEADERS = {"Cache-Control": "no-store"}

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _admin_secret(settings: Settings) -> str | None:
    return settings.admin_key or settings.local_api_key


def _expected_login_key(settings: Settings) -> str | None:
    return settings.admin_key or settings.local_api_key


def _sign(secret: str, issued_at: str) -> str:
    return hmac.new(secret.encode("utf-8"), issued_at.encode("utf-8"), sha256).hexdigest()


def _cookie_value(secret: str) -> str:
    issued_at = str(int(time.time()))
    return f"{issued_at}.{_sign(secret, issued_at)}"


def _check_admin_auth(request: Request) -> None:
    if _is_admin_authenticated(request):
        return
    raise HTTPException(status_code=401, detail="Admin login required")


def _is_admin_authenticated(request: Request) -> bool:
    settings = _settings(request)
    secret = _admin_secret(settings)
    if not secret:
        return False
    raw = request.cookies.get(COOKIE_NAME) or ""
    try:
        issued_at, signature = raw.split(".", 1)
        issued_ts = int(issued_at)
    except ValueError:
        return False
    if int(time.time()) - issued_ts > COOKIE_MAX_AGE:
        return False
    return hmac.compare_digest(signature, _sign(secret, issued_at))


def _is_vercel() -> bool:
    return os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))


def _mask(value: str | None, *, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


def _token_rows(settings: Settings) -> list[dict[str, Any]]:
    rows = []
    for index, token in enumerate(settings.codebuff_tokens, start=1):
        rows.append(
            {
                "index": index,
                "masked": _mask(token),
                "prefix": token[:8],
                "length": len(token),
            }
        )
    return rows


def _rotation_payload(request: Request) -> dict[str, Any]:
    """Account pool health + rotation status for the admin panel."""
    accounts: CodebuffAccountPool = request.app.state.accounts
    rotation = accounts.rotation
    return {
        "current_index": rotation.current_index + 1 if rotation.account_count else 0,
        "total_rotations": rotation.total_rotations,
        "last_429_time": rotation.last_429_time,
        "last_429_account": (
            rotation.last_429_account + 1
            if rotation.last_429_account is not None
            else None
        ),
        "last_429_info": rotation.last_429_info,
        "all_blocked": rotation.all_blocked,
        "available_count": rotation.available_count,
        "accounts": accounts.account_statuses(),
    }


def _config_payload(settings: Settings, request: Request | None = None) -> dict[str, Any]:
    using_default_admin_key = settings.admin_key == DEFAULT_ADMIN_KEY
    payload: dict[str, Any] = {
        "environment": "vercel" if _is_vercel() else "local",
        "token_count": len(settings.codebuff_tokens),
        "tokens": _token_rows(settings),
        "api_key_configured": bool(settings.local_api_key),
        "api_key_masked": _mask(settings.local_api_key),
        "admin_key_configured": bool(settings.admin_key),
        "admin_key_masked": _mask(settings.admin_key),
        "using_default_admin_key": using_default_admin_key,
        "setup_complete": bool(settings.local_api_key and settings.codebuff_tokens and not using_default_admin_key),
        "debug": settings.debug,
        "log_level": settings.log_level,
        "proxy_enabled": settings.proxy_enabled,
        "proxy_type": settings.proxy_type,
        "proxy_host": settings.proxy_host,
        "proxy_port": settings.proxy_port,
        "proxy_username": settings.proxy_username or "",
        "proxy_has_auth": bool(settings.proxy_username),
        "base_url": settings.codebuff_api_url,
        "port": settings.port,
        "timezone": settings.timezone,
        "locale": settings.locale,
        "os_name": settings.os_name,
    }
    if request is not None:
        payload["accounts"] = request.app.state.accounts.account_statuses()
        payload["rotation"] = _rotation_payload(request)
    else:
        payload["accounts"] = []
        payload["rotation"] = None
    return payload


def _api_ok(data: dict[str, Any] | list[Any] | None = None, msg: str = "ok") -> dict[str, Any]:
    return {"code": 0, "msg": msg, "data": data or {}}


def _apply_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


async def _replace_accounts(request: Request, settings: Settings) -> None:
    old_accounts = request.app.state.accounts
    new_accounts = CodebuffAccountPool(settings)
    request.app.state.settings = settings
    request.app.state.accounts = new_accounts
    request.app.state.rotation = new_accounts.rotation
    request.app.state.codebuff = new_accounts.default_client
    request.app.state.sessions = new_accounts.default_sessions
    # Do NOT close the old pool immediately: in-flight streaming requests still
    # hold the old httpx clients. Close it in the background once those finish,
    # so updating tokens mid-request never interrupts active calls.
    asyncio.create_task(_close_pool_when_idle(old_accounts))


async def _close_pool_when_idle(pool: CodebuffAccountPool) -> None:
    """Wait for all in-flight requests on the pool to finish, then close it."""
    try:
        while pool.active_request_count > 0:
            await asyncio.sleep(0.5)
        await pool.aclose()
        logger.info("old account pool closed after all requests finished")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("failed to close old account pool")


def _tokens(settings: Settings) -> list[str]:
    return list(settings.codebuff_tokens)


async def _save_token_list(request: Request, tokens: list[str]) -> dict[str, Any]:
    clean_tokens = [item.strip() for item in tokens if item and item.strip()]
    token_value = ",".join(clean_tokens)
    old_settings = _settings(request)
    new_settings = replace(old_settings, codebuff_token=token_value or None)
    if not _is_vercel():
        write_env_values({"FREEBUFF_TOKEN": token_value or None})
    _apply_env({"FREEBUFF_TOKEN": token_value or None})
    await _replace_accounts(request, new_settings)
    return {
        **_config_payload(new_settings, request),
        "persisted": not _is_vercel(),
        "env": f"FREEBUFF_TOKEN={token_value}",
    }


@router.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=307)


@router.post("/admin/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    key = str(body.get("key") or "")
    expected = _expected_login_key(_settings(request))
    if not expected:
        raise HTTPException(status_code=503, detail="Set FREEBUFF_ADMIN_KEY first")
    if not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid admin key")
    response = JSONResponse(_api_ok(_config_payload(_settings(request), request)))
    response.set_cookie(
        COOKIE_NAME,
        _cookie_value(_admin_secret(_settings(request)) or expected),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


def _region_from_payload(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    if source == "ipapi.co":
        return {
            "ip": payload.get("ip"),
            "country": payload.get("country_name") or payload.get("country"),
            "region": payload.get("region"),
            "city": payload.get("city"),
            "timezone": payload.get("timezone"),
            "org": payload.get("org"),
        }
    if source == "ipinfo.io":
        return {
            "ip": payload.get("ip"),
            "country": payload.get("country"),
            "region": payload.get("region"),
            "city": payload.get("city"),
            "timezone": payload.get("timezone"),
            "org": payload.get("org"),
        }
    return {
        "ip": payload.get("query"),
        "country": payload.get("country"),
        "region": payload.get("regionName") or payload.get("region"),
        "city": payload.get("city"),
        "timezone": payload.get("timezone"),
        "org": payload.get("isp") or payload.get("org"),
    }


async def _probe_region(settings: Settings) -> dict[str, Any]:
    probes = [
        ("ipinfo.io", "https://ipinfo.io/json"),
        ("ipapi.co", "https://ipapi.co/json/"),
        ("ip-api.com", "http://ip-api.com/json/?fields=status,message,query,country,regionName,city,timezone,isp,org"),
    ]
    errors: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0),
        follow_redirects=True,
        proxy=settings.upstream_proxy_url,
        trust_env=False,
    ) as client:
        for source, url in probes:
            started = time.perf_counter()
            try:
                response = await client.get(url, headers={"Accept": "application/json"})
                latency_ms = round((time.perf_counter() - started) * 1000)
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "fail":
                    raise ValueError(payload.get("message") or "region probe failed")
                return {
                    "ok": True,
                    "source": source,
                    "latency_ms": latency_ms,
                    **_region_from_payload(source, payload),
                }
            except Exception as error:
                errors.append({"source": source, "error": str(error)})
    return {"ok": False, "source": "unknown", "errors": errors}


async def _probe_url(client: httpx.AsyncClient, name: str, url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.get(url, headers={"Accept": "*/*"})
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {
            "name": name,
            "ok": response.status_code < 500,
            "status": response.status_code,
            "latency_ms": latency_ms,
        }
    except Exception as error:
        return {"name": name, "ok": False, "error": str(error)}


@router.post("/admin/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse(_api_ok())
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/admin/api/session")
async def session_status(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    return _api_ok(
        {
            "authenticated": _is_admin_authenticated(request),
            "admin_key_configured": bool(settings.admin_key),
            "api_key_configured": bool(settings.local_api_key),
            "using_default_admin_key": settings.admin_key == DEFAULT_ADMIN_KEY,
        }
    )


@router.get("/admin/api/overview")
async def overview(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    settings = _settings(request)
    accounts: CodebuffAccountPool = request.app.state.accounts
    model_ids = [model.id for model in ALL_MODELS]
    model_availability = (
        accounts.rotation.model_availability(model_ids)
        if accounts.rotation is not None
        else []
    )
    registry = get_model_registry()
    return _api_ok(
        {
            "status": "ok",
            "environment": "vercel" if _is_vercel() else "local",
            "account_count": accounts.account_count,
            "model_count": len(models_response()["data"]),
            "base_url": settings.codebuff_api_url,
            "debug": settings.debug,
            "log_level": settings.log_level,
            "model_availability": model_availability,
            "model_registry": registry.status() if registry else {"loaded": False},
        }
    )


@router.get("/admin/api/config")
async def config(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(_config_payload(_settings(request), request))


@router.get("/admin/api/env")
async def env_content(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    env_path = project_env_path()
    vercel = _is_vercel()
    content = ""
    exists = env_path.exists()
    if exists and not vercel:
        content = env_path.read_text(encoding="utf-8")
    return _api_ok(
        {
            "environment": "vercel" if vercel else "local",
            "path": str(env_path),
            "exists": exists,
            "content": content,
            "editable": not vercel,
            "message": (
                "Vercel 部署环境不能通过运行中的服务持久修改 .env；请到 Vercel 项目 Settings -> Environment Variables 修改变量，然后重新部署。"
                if vercel
                else "本地服务读取项目根目录 .env。管理面板里的 Token/API Key 保存会写回这个文件。"
            ),
        }
    )


@router.get("/admin/api/models")
async def admin_models(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(models_response())


@router.get("/admin/api/logs")
async def logs(
    request: Request,
    since_id: int = 0,
    limit: int = 200,
    level: str | None = None,
) -> dict[str, Any]:
    _check_admin_auth(request)
    return _api_ok(
        {
            "items": get_buffered_logs(since_id=since_id, limit=limit, level=level),
            "limit": limit,
        }
    )


@router.delete("/admin/api/logs")
async def clear_logs(request: Request) -> dict[str, Any]:
    """Clear the in-memory buffered logs (admin UI 清除日志按钮)."""
    _check_admin_auth(request)
    clear_buffered_logs()
    return _api_ok({}, "logs cleared")


@router.get("/admin/api/network")
async def network(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    settings = _settings(request)
    region = await _probe_region(settings)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(4.0),
        follow_redirects=True,
        proxy=settings.upstream_proxy_url,
        trust_env=False,
    ) as client:
        connectivity = [
            await _probe_url(client, "codebuff", f"{settings.codebuff_api_url}/api/healthz"),
            await _probe_url(client, "freebuff", "https://freebuff.com"),
        ]
    return _api_ok(
        {
            "region": region,
            "connectivity": connectivity,
            "proxy_enabled": settings.proxy_enabled,
            "proxy_display": f"{settings.proxy_type}://{settings.proxy_host}:{settings.proxy_port}" if settings.proxy_host else "",
        }
    )


# ── Geo / 设备指纹 ─────────────────────────────────────────────────────


@router.get("/admin/api/geo")
async def geo_status(request: Request) -> dict[str, Any]:
    """Current device fingerprint (timezone/locale) + last detection result."""
    _check_admin_auth(request)
    settings = _settings(request)
    detected = last_geo_info()
    return _api_ok(
        {
            "timezone": settings.timezone,
            "locale": settings.locale,
            "os_name": settings.os_name,
            "detected": detected or {"timezone": settings.timezone, "locale": settings.locale},
        }
    )


@router.post("/admin/api/geo/refresh")
async def geo_refresh(request: Request) -> dict[str, Any]:
    """Re-detect server IP geo and apply timezone/locale to the live settings."""
    _check_admin_auth(request)
    settings = _settings(request)
    try:
        geo = await asyncio.to_thread(refresh_geo, settings)
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
    return _api_ok(
        {
            "timezone": settings.timezone,
            "locale": settings.locale,
            "detected": geo,
        },
        "geo refreshed",
    )


# ── 动态模型注册表 ─────────────────────────────────────────────────────


@router.get("/admin/api/model-registry")
async def model_registry_status(request: Request) -> dict[str, Any]:
    """Dynamic model registry status (official source mirror)."""
    _check_admin_auth(request)
    registry = get_model_registry()
    return _api_ok(
        registry.status() if registry else {"loaded": False},
    )


@router.post("/admin/api/model-registry/refresh")
async def model_registry_refresh(request: Request) -> dict[str, Any]:
    """Force a synchronous refresh of the dynamic model registry."""
    _check_admin_auth(request)
    registry = get_model_registry()
    if registry is None:
        raise HTTPException(status_code=503, detail="model registry not initialized")
    try:
        table = await asyncio.to_thread(registry.refresh_sync)
    except Exception as error:
        return _api_ok(
            {
                "ok": False,
                "error": str(error),
                "loaded": registry.table is not None,
                "model_count": len(registry.table.models) if registry.table else 0,
            },
            "model registry refresh failed; hardcoded fallback active",
        )
    return _api_ok(
        {
            "ok": True,
            "model_count": len(table.models),
            "premium_count": len(table.premium_ids),
            "glm_count": len(table.glm_ids),
            "fetched_at": table.fetched_at,
        },
        "model registry refreshed",
    )


@router.put("/admin/api/freebuff-tokens")
async def save_tokens(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    tokens = [str(item).strip() for item in body.get("tokens") or []]
    return _api_ok(await _save_token_list(request, tokens), "tokens saved")


@router.post("/admin/api/freebuff-tokens/verify")
async def verify_token(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    settings = replace(_settings(request), codebuff_token=token)
    client = CodebuffClient(settings)
    try:
        data = await client.get_session()
        return _api_ok({"ok": True, "info": "Token verified", "upstream": data})
    except CodebuffError as error:
        return _api_ok({"ok": False, "info": str(error)})
    finally:
        await client.aclose()


@router.get("/admin/api/freebuff-tokens/{index}")
async def get_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    token = tokens[index - 1]
    return _api_ok({"index": index, "token": token, "masked": _mask(token)})


@router.post("/admin/api/freebuff-tokens")
async def add_token(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    tokens = _tokens(_settings(request))
    tokens.append(token)
    return _api_ok(await _save_token_list(request, tokens), "token added")


@router.put("/admin/api/freebuff-tokens/{index}")
async def update_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    tokens[index - 1] = token
    return _api_ok(await _save_token_list(request, tokens), "token updated")


@router.delete("/admin/api/freebuff-tokens/{index}")
async def delete_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    tokens = _tokens(_settings(request))
    if index < 1 or index > len(tokens):
        raise HTTPException(status_code=404, detail="Token not found")
    tokens.pop(index - 1)
    return _api_ok(await _save_token_list(request, tokens), "token deleted")


@router.post("/admin/api/tokens/rotate")
async def rotate_tokens(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    accounts: CodebuffAccountPool = request.app.state.accounts
    index = accounts.manual_rotate()
    return _api_ok(
        _rotation_payload(request),
        f"rotated to account {index + 1}",
    )


@router.post("/admin/api/tokens/activate/{index}")
async def activate_token(request: Request, index: int) -> dict[str, Any]:
    _check_admin_auth(request)
    accounts: CodebuffAccountPool = request.app.state.accounts
    try:
        accounts.set_active(index)
    except IndexError:
        raise HTTPException(status_code=404, detail="Account not found")
    return _api_ok(_rotation_payload(request), f"active account {index}")


@router.post("/admin/api/tokens/validate")
async def validate_tokens(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    accounts: CodebuffAccountPool = request.app.state.accounts
    await accounts.validate_accounts()
    return _api_ok(_rotation_payload(request), "validation done")


@router.put("/admin/api/api-key")
async def save_api_key(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    api_key = str(body.get("api_key") or "").strip()
    if len(api_key) < 8:
        raise HTTPException(status_code=400, detail="API key must be at least 8 characters")
    new_settings = replace(_settings(request), local_api_key=api_key)
    if not _is_vercel():
        write_env_values({"FREEBUFF_API_KEY": api_key})
    _apply_env({"FREEBUFF_API_KEY": api_key})
    request.app.state.settings = new_settings
    return _api_ok(
        {**_config_payload(new_settings, request), "persisted": not _is_vercel()},
        "api key saved",
    )


@router.put("/admin/api/security")
async def save_security(request: Request) -> JSONResponse:
    _check_admin_auth(request)
    body = await request.json()
    admin_key = str(body.get("admin_key") or "").strip()
    if len(admin_key) < 8:
        raise HTTPException(status_code=400, detail="Admin key must be at least 8 characters")
    old_settings = _settings(request)
    new_settings = replace(
        old_settings,
        admin_key=admin_key,
    )
    values = {"FREEBUFF_ADMIN_KEY": admin_key}
    if not _is_vercel():
        write_env_values(values)
    _apply_env(values)
    request.app.state.settings = new_settings
    response = JSONResponse(
        _api_ok(
            {
                **_config_payload(new_settings, request),
                "persisted": not _is_vercel(),
            },
            "security saved",
        )
    )
    response.delete_cookie(COOKIE_NAME)
    return response


# ── Proxy Configuration ──────────────────────────────────────────────

@router.put("/admin/api/proxy")
async def save_proxy(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    proxy_enabled = bool(body.get("proxy_enabled", False))
    proxy_type = str(body.get("proxy_type") or "socks5").strip() or "socks5"
    proxy_host = str(body.get("proxy_host") or "").strip()
    proxy_port = int(body.get("proxy_port") or 1080)
    proxy_username = str(body.get("proxy_username") or "").strip() or None
    proxy_password = str(body.get("proxy_password") or "").strip() or None

    if proxy_enabled and not proxy_host:
        raise HTTPException(status_code=400, detail="proxy_host is required when enabled")

    old_settings = _settings(request)
    new_settings = replace(
        old_settings,
        proxy_enabled=proxy_enabled,
        proxy_type=proxy_type,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
    )
    if not _is_vercel():
        write_env_values({
            "FREEBUFF_PROXY_ENABLED": "true" if proxy_enabled else "false",
            "FREEBUFF_PROXY_TYPE": proxy_type,
            "FREEBUFF_PROXY_HOST": proxy_host,
            "FREEBUFF_PROXY_PORT": str(proxy_port),
            "FREEBUFF_PROXY_USERNAME": proxy_username or "",
            "FREEBUFF_PROXY_PASSWORD": proxy_password or "",
        })
    _apply_env({
        "FREEBUFF_PROXY_ENABLED": "true" if proxy_enabled else "false",
        "FREEBUFF_PROXY_TYPE": proxy_type,
        "FREEBUFF_PROXY_HOST": proxy_host,
        "FREEBUFF_PROXY_PORT": str(proxy_port),
        "FREEBUFF_PROXY_USERNAME": proxy_username or "",
        "FREEBUFF_PROXY_PASSWORD": proxy_password or "",
    })
    request.app.state.settings = new_settings
    return _api_ok(
        {**_config_payload(new_settings, request), "persisted": not _is_vercel()},
        "proxy config saved",
    )


@router.post("/admin/api/proxy/test")
async def test_proxy(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    proxy_type = str(body.get("proxy_type") or "socks5").strip() or "socks5"
    proxy_host = str(body.get("proxy_host") or "").strip()
    proxy_port = int(body.get("proxy_port") or 1080)
    proxy_username = str(body.get("proxy_username") or "").strip() or ""
    proxy_password = str(body.get("proxy_password") or "").strip() or ""

    if not proxy_host:
        raise HTTPException(status_code=400, detail="proxy_host is required")

    auth = f"{proxy_username}:{proxy_password}@" if proxy_username else ""
    proxy_url = f"{proxy_type}://{auth}{proxy_host}:{proxy_port}"

    import httpx
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
        proxy=proxy_url,
        trust_env=False,
    ) as client:
        try:
            r = await client.get("https://ipinfo.io/json")
            data = r.json()
            return _api_ok({
                "ok": True,
                "ip": data.get("ip"),
                "country": data.get("country"),
                "city": data.get("city"),
                "org": data.get("org"),
                "latency_ms": round(r.elapsed.total_seconds() * 1000),
            })
        except Exception as e:
            return _api_ok({"ok": False, "error": str(e)})


@router.post("/admin/api/chat-test")
async def chat_test(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    model = str(body.get("model") or DEFAULT_MODEL.id).strip() or DEFAULT_MODEL.id
    prompt = str(body.get("prompt") or "ping").strip() or "ping"
    from .app import _collect_completion, _start_freebuff_run_chain
    from .openai_compat import build_upstream_payload, normalize_chat_messages
    from .models import resolve_model

    model_config = resolve_model(model)
    messages = normalize_chat_messages([{"role": "user", "content": prompt}])
    lease = await request.app.state.accounts.acquire_session(model_config.session_id, messages)
    try:
        # 与 chat 主链路一致：不再调用 validate_agents()（旧 CLI 管理请求，缩小暴露面）。
        await lease.client.request_ad_chain(messages=messages)
        run = await _start_freebuff_run_chain(lease.client, model_config)
        payload = build_upstream_payload(
            {"model": model_config.id, "messages": messages, "stream": False},
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=_settings(request).client_id,
            trace_session_id="admin-test",
            upstream_model_id=model_config.upstream_id,
        )
        response = await _collect_completion(request, payload, run, model_config.id, client=lease.client)
        return _api_ok({"ok": True, "response": response})
    except Exception as error:
        return _api_ok({"ok": False, "info": str(error)})
    finally:
        await lease.aclose()


# ── Request Records ────────────────────────────────────────────────────


@router.get("/admin/api/requests")
async def list_requests(
    request: Request,
    since_id: int = 0,
    limit: int = 200,
    model: str | None = None,
    status: str | None = None,
    api_key_name: str | None = None,
) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    items = store.list(
        since_id=since_id, limit=limit, model=model,
        status=status, api_key_name=api_key_name,
    )
    return _api_ok({"items": items, "limit": limit})


@router.get("/admin/api/requests/stats")
async def request_stats(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    return _api_ok(store.stats())


@router.delete("/admin/api/requests")
async def clear_requests(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store: RequestStore = request.app.state.request_store
    store.clear()
    return _api_ok({}, "request records cleared")


# ── API Key Management ────────────────────────────────────────────────


def _api_key_store(request: Request) -> ApiKeyStore:
    return request.app.state.api_key_store


def _persist_api_keys(request: Request) -> tuple[bool, str]:
    store = _api_key_store(request)
    env_json = store.to_env_json()
    if not _is_vercel():
        write_env_values({"FREEBUFF_API_KEYS": env_json})
    _apply_env({"FREEBUFF_API_KEYS": env_json})
    return not _is_vercel(), env_json


@router.get("/admin/api/api-keys")
async def list_api_keys(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    return _api_ok({"items": store.list_all(), "count": store.total_count, "active_count": store.count})


@router.post("/admin/api/api-keys")
async def create_api_key(request: Request) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    key = str(body.get("key") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(key) < 8:
        raise HTTPException(status_code=400, detail="key must be at least 8 characters")
    store = _api_key_store(request)
    if store.get(name):
        raise HTTPException(status_code=409, detail=f"API key '{name}' already exists")
    allowed = body.get("allowed_models", ["*"])
    rec = ApiKeyRecord(
        name=name, key=key,
        allowed_models=allowed if isinstance(allowed, list) else ["*"],
        enabled=True,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    store.add(rec)
    persisted, _ = _persist_api_keys(request)
    return _api_ok(rec.to_dict(mask=True), f"created{' and persisted' if persisted else ''}")


@router.put("/admin/api/api-keys/{name}")
async def update_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    body = await request.json()
    store = _api_key_store(request)
    fields: dict[str, Any] = {}
    if "key" in body:
        k = str(body["key"]).strip()
        if k and len(k) < 8:
            raise HTTPException(status_code=400, detail="key must be at least 8 characters")
        fields["key"] = k
    if "allowed_models" in body:
        am = body["allowed_models"]
        fields["allowed_models"] = am if isinstance(am, list) else ["*"]
    if "enabled" in body:
        fields["enabled"] = bool(body["enabled"])
    if not store.update(name, **fields):
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    persisted, _ = _persist_api_keys(request)
    updated = store.get(name)
    return _api_ok(updated.to_dict(mask=True) if updated else {}, f"updated{' and persisted' if persisted else ''}")


@router.delete("/admin/api/api-keys/{name}")
async def delete_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    if not store.delete(name):
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    persisted, _ = _persist_api_keys(request)
    return _api_ok({}, f"deleted{' and persisted' if persisted else ''}")


@router.put("/admin/api/api-keys/{name}/toggle")
async def toggle_api_key(request: Request, name: str) -> dict[str, Any]:
    _check_admin_auth(request)
    store = _api_key_store(request)
    rec = store.get(name)
    if not rec:
        raise HTTPException(status_code=404, detail=f"API key '{name}' not found")
    store.update(name, enabled=not rec.enabled)
    persisted, _ = _persist_api_keys(request)
    updated = store.get(name)
    return _api_ok(updated.to_dict(mask=True) if updated else {}, "toggled")


# ── SPA Fallback (must be LAST) ──────────────────────────────────────

_REACT_DIST = Path(__file__).parent / "admin_static"


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse)
@router.get("/admin/{path:path}", response_class=HTMLResponse)
async def admin_page(path: str = "") -> HTMLResponse:
    # Don't serve SPA for API routes
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    # Serve static files if they exist (favicon, assets, etc.)
    if path:
        static_file = (_REACT_DIST / path).resolve()
        if not static_file.is_relative_to(_REACT_DIST.resolve()):
            raise HTTPException(status_code=404, detail="Not found")
        if static_file.is_file():
            from fastapi.responses import FileResponse
            return FileResponse(str(static_file))
    # SPA fallback: serve index.html
    index = _REACT_DIST / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"), headers=NO_STORE_HEADERS)
    html_path = Path(__file__).parent / "admin_static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"), headers=NO_STORE_HEADERS)

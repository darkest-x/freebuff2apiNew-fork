from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import datetime
import json
import logging
import time
from typing import Any, AsyncIterator
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from .admin import router as admin_router
from .codebuff import (
    CodebuffAccountLease,
    CodebuffAccountPool,
    CodebuffClient,
    CodebuffError,
    FreebuffRun,
    SessionManager,
    utc_now_iso,
)
from .config import Settings, load_settings
from .logging_config import configure_logging, redact_headers, render_debug
from .notices import describe_error, notice_for_error, truncate_detail
from .token_rotation import parse_429_info
from .openai_compat import (
    CompletionAccumulator,
    build_upstream_payload,
    normalize_chat_messages,
    sanitize_stream_chunk,
)
from .anthropic_compat import (
    AnthropicCompletionAccumulator,
    AnthropicStreamState,
    anthropic_error_payload,
    anthropic_sse_encode,
    anthropic_sse_ping,
    build_anthropic_upstream_payload,
)
from .models import (
    CONTEXT_PRUNER_AGENT_ID,
    FreebuffModel,
    model_response,
    models_response,
    resolve_model,
)
from .sse import decode_sse_data, encode_sse
from .usage import RequestRecord
from .usage_store import RequestStore, ApiKeyStore, create_stores


logger = logging.getLogger("freebuff2api.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    configure_logging(settings)
    accounts = CodebuffAccountPool(settings)
    request_store, api_key_store = create_stores(settings.max_request_records)
    api_key_store.load_from_settings(settings.api_keys_json, settings.local_api_key)
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.rotation = accounts.rotation
    app.state.codebuff = accounts.default_client
    app.state.sessions = accounts.default_sessions
    app.state.request_store = request_store
    app.state.api_key_store = api_key_store
    validation_task = asyncio.create_task(accounts.validate_accounts())
    logger.info("configured freebuff accounts count=%s api_keys=%s", accounts.account_count, api_key_store.total_count)
    try:
        yield
    finally:
        validation_task.cancel()
        await accounts.aclose()


app = FastAPI(title="freebuff2api", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)


@app.exception_handler(ClientDisconnect)
async def _handle_client_disconnect(request: Request, exc: ClientDisconnect) -> JSONResponse:
    """客户端在请求体读完前断开（用户停止/网络中断）→ 静默结束，不打 ERROR 堆栈。

    这是常态而非故障：request.json() 读到一半客户端断开会抛 ClientDisconnect，
    若不加 handler 会在 uvicorn 层打出整段 traceback 干扰监控。客户端已断开，
    响应实际送不出去，这里返回 499（Nginx 语义）仅作记录。
    """
    logger.info(
        "client disconnected before request body complete path=%s",
        request.url.path,
    )
    return JSONResponse(status_code=499, content={"error": {"message": "client closed connection", "type": "client_disconnect"}})


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(request: Request) -> CodebuffClient:
    return request.app.state.codebuff


def _sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def _accounts(request: Request) -> CodebuffAccountPool:
    return request.app.state.accounts


def _check_local_auth(request: Request, *, require_configured: bool = False):
    store: ApiKeyStore = request.app.state.api_key_store
    if store.total_count == 0:
        if require_configured:
            raise HTTPException(
                status_code=503,
                detail="Set FREEBUFF_API_KEY in the admin panel before using /v1 APIs",
            )
        return None
    key = store.authenticate(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
    )
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


def _check_freebuff_token(request: Request) -> None:
    if not _settings(request).codebuff_tokens:
        raise HTTPException(
            status_code=503,
            detail="Set FREEBUFF_TOKEN in the admin panel before using chat completions",
        )


def _check_anthropic_auth(request: Request, *, require_configured: bool = False):
    store: ApiKeyStore = request.app.state.api_key_store
    if store.total_count == 0:
        if require_configured:
            raise HTTPException(
                status_code=503,
                detail="Set FREEBUFF_API_KEY in the admin panel before using /v1 APIs",
            )
        return None
    key = store.authenticate(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
    )
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


def _friendly_upstream_message(error: Exception) -> str:
    """把错误包一层客户端可读的中文解释，并保留原始英文详情。"""
    original = str(error)
    return f"{describe_error(error)}（原始信息：{truncate_detail(original)}）"


def _wrapped_error(error: Exception) -> CodebuffError | Exception:
    if isinstance(error, CodebuffError):
        return CodebuffError(_friendly_upstream_message(error), error.status_code)
    return error


def _openai_notice_response(model: str, text: str) -> dict[str, Any]:
    """构造正常的 OpenAI chat.completion 响应，内容为中文中转提示。"""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _openai_notice_stream(model: str, text: str):
    """生成与 OpenAI SSE 兼容的中文提示流（[DONE] 由调用方补发）。"""
    message_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    base = {
        "id": message_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    yield encode_sse(
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
    )
    yield encode_sse(
        {
            **base,
            "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}
            ],
        }
    )
    yield encode_sse(
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )


def _anthropic_notice_response(model: str, text: str) -> dict[str, Any]:
    """构造正常的 Anthropic Messages 响应，内容为中文中转提示。"""
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, CodebuffError):
        friendly = _wrapped_error(error)
        assert isinstance(friendly, CodebuffError)
        headers: dict[str, str] = {}
        if friendly.status_code == 429:
            retry_ms = parse_429_info(str(error)).get("retry_after_ms", 0)
            if retry_ms:
                headers["Retry-After"] = str(max(1, round(retry_ms / 1000)))
        return JSONResponse(
            status_code=friendly.status_code,
            content={
                "error": {
                    "message": str(friendly),
                    "upstream_message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            },
            headers=headers or None,
        )
    raise error


def _handle_upstream_error(request: Request, account_index: int | None, error: Exception, model: str = "") -> None:
    """Feed an upstream failure into the account rotation/health tracker."""
    if not isinstance(error, CodebuffError) or account_index is None:
        return
    accounts: CodebuffAccountPool = request.app.state.accounts
    # 428 waiting_room_required：缓存 session 已失效（僵尸实例，上游 chat gate 不识别）。
    # 清除该账号/模型的 session 缓存让下次请求重建，且不记入 failure/rotation，
    # 避免账号被误判失效（对齐 Worker 1.7.0 stale-session 处理）。
    if error.status_code == 428:
        try:
            account = accounts._accounts[account_index]
            account.sessions.discard_session(model)
        except Exception:
            pass
        logger.warning(
            "session stale (428 waiting_room_required) account=%s model=%s; cache cleared",
            account_index + 1,
            model,
        )
        return
    accounts.handle_error(account_index, str(error), error.status_code, model)


async def _recreate_session_and_run_for_retry(
    account_lease: CodebuffAccountLease,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """空流恢复（对齐 pingmike2/freebuff2api-wokers v1.8.5 empty-stream recovery）：

    上游 200 但返回空流（首 chunk 即 EOF，常见于免费通道长对话/额度脏状态）时，
    删除上游 session → 重建同模型 session → 重新 START run → 重建 payload 重试一次。
    只重建同模型，绝不改成别的模型（v1.8.5 明确语义）。失败返回 None 交由上层报错。
    """
    try:
        account = account_lease._pool._accounts[account_lease._account_index]
        client = account.client
        sessions = account.sessions
        model = payload.get("model") or ""
        await client.delete_session(account_lease.session.instance_id)
        sessions.discard_session(model)
        # 注意：此处调用方仍持有该 bucket 的会话锁，必须走锁内私有方法，
        # 不能调用 ensure_session（会再次获取同一把锁导致死锁）。
        new_session = await sessions._create_session_locked(model)
        new_run = await _start_freebuff_run_chain(client, model)
        client_id = (payload.get("codebuff_metadata") or {}).get("client_id") or ""
        return build_upstream_payload(
            payload,
            session=new_session,
            run_id=new_run.payload_run_id,
            client_id=client_id,
            trace_session_id=str(uuid.uuid4()),
        )
    except Exception as error:
        logger.warning(
            "empty stream retry failed model=%s: %s",
            payload.get("model"),
            error,
        )
        return None


def _record_request(
    request: Request,
    api_key,
    model: str,
    duration_ms: int,
    status: str,
    *,
    error: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    store: RequestStore = request.app.state.request_store
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = RequestRecord(
        id=0,
        timestamp=ts,
        api_key_name=api_key.name if api_key else "anonymous",
        api_key_prefix=api_key.prefix if api_key else "---",
        model=model,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        status=status,
        error=error,
        client_ip=request.client.host if request.client else None,
    )
    store.add(record)


@app.get("/api/keep-warm")
async def keep_warm() -> dict[str, Any]:
    return {"status": "ok", "warm": True}

@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    _check_local_auth(request)
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    _check_local_auth(request, require_configured=True)
    response = models_response()
    settings = _settings(request)
    for item in response["data"]:
        item["max_request_bytes"] = settings.max_request_body_bytes
    return response


@app.get("/v1/models/{model_id:path}")
async def get_model(request: Request, model_id: str) -> dict[str, Any]:
    _check_local_auth(request, require_configured=True)
    result = model_response(model_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")
    settings = _settings(request)
    result["max_request_bytes"] = settings.max_request_body_bytes
    return result


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    api_key = _check_local_auth(request, require_configured=True)
    _check_freebuff_token(request)
    settings = _settings(request)
    raw_body = await request.body()
    if settings.max_request_body_bytes > 0 and len(raw_body) > settings.max_request_body_bytes:
        logger.warning(
            "[client] chat request rejected 413 body_too_large size=%s limit=%s ip=%s",
            len(raw_body),
            settings.max_request_body_bytes,
            request.client.host if request.client else None,
        )
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "message": (
                        f"Request body too large / 请求体过大：当前 {len(raw_body)} bytes，"
                        f"超过限制 {settings.max_request_body_bytes} bytes。请减小上下文/附件大小，"
                        "或在客户端启用上下文压缩。"
                    ),
                    "type": "invalid_request_error",
                    "code": "request_body_too_large",
                }
            },
        )
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "Invalid JSON body.",
                    "type": "invalid_request_error",
                    "code": "invalid_json",
                }
            },
        )
    try:
        model_config = resolve_model(body.get("model"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    model = model_config.id
    if api_key and not api_key.allows_model(model):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": f"API key '{api_key.name}' not allowed to use model '{model}'",
                    "type": "invalid_request_error",
                    "code": "model_not_allowed",
                }
            },
        )
    logger.info(
        "[client] chat completion request model=%s stream=%s messages=%s",
        model,
        body.get("stream") is True,
        len(body.get("messages") or []),
    )
    if settings.debug:
        logger.debug(
            "[inbound] chat completion request headers=%s",
            redact_headers(dict(request.headers)),
        )
        logger.debug(
            "[inbound] chat completion request body=%s",
            render_debug(body, settings.log_body_chars),
        )

    messages = normalize_chat_messages(body.get("messages"))
    lease: CodebuffAccountLease | None = None
    try:
        lease = await _accounts(request).acquire_session(
            model_config.session_id,
            messages=messages,
        )
        client = lease.client
        await client.request_ad_chain(messages=messages)
        # 不再调用 validate_agents()：/api/agents/validate 是旧 CLI 的额外管理请求，
        # Worker 1.7.0 桌面版协议不发送，去掉以缩小暴露面。
        run = await _start_freebuff_run_chain(client, model_config)
        trace_session_id = str(uuid.uuid4())
        payload = build_upstream_payload(
            {**body, "messages": messages},
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=settings.client_id,
            trace_session_id=trace_session_id,
            upstream_model_id=model_config.upstream_id,
            system_prompt=settings.system_prompt_override,
            max_tools=settings.max_tools_per_request,
            llm_step_number=_next_llm_step_number(run.payload_run_id),
            max_messages=settings.max_messages_per_request,
        )
        if settings.debug:
            logger.debug(
                "[outbound] prepared upstream chat trace=%s run=%s payload=%s",
                trace_session_id,
                run,
                render_debug(payload, settings.log_body_chars),
            )
    except CodebuffError as error:
        if lease is not None:
            _handle_upstream_error(request, lease._account_index, error, model)
            await lease.aclose()
        logger.warning(
            "failed to prepare chat completion: %s",
            error,
            exc_info=settings.debug,
        )
        notice = notice_for_error(error, model)
        if notice is not None:
            _record_request(request, api_key, model, 0, "notice", error=str(error))
            return JSONResponse(_openai_notice_response(model, notice))
        return _error_response(error)
    except Exception as error:
        if lease is not None:
            await lease.aclose()
        logger.exception("failed to prepare chat completion")
        return _error_response(error)

    if body.get("stream") is True:
        return StreamingResponse(
            _stream_openai_chunks(request, payload, run, api_key=api_key, account_lease=lease),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    started = time.time()
    try:
        response = await _collect_completion(
            request,
            payload,
            run,
            model,
            client=lease.client,
            account_lease=lease,
        )
        duration_ms = int((time.time() - started) * 1000)
        usage = response.get("usage") or {}
        _record_request(request, api_key, model, duration_ms, "success",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0))
        return JSONResponse(response)
    except Exception as error:
        duration_ms = int((time.time() - started) * 1000)
        if isinstance(error, CodebuffError):
            _handle_upstream_error(request, lease._account_index, error, model)
            notice = notice_for_error(error, model)
            if notice is not None:
                _record_request(request, api_key, model, duration_ms, "notice", error=str(error))
                return JSONResponse(_openai_notice_response(model, notice))
        else:
            _handle_upstream_error(request, lease._account_index, error, model)
        _record_request(request, api_key, model, duration_ms, "error", error=str(error))
        return _error_response(error)
    finally:
        await lease.aclose()


async def _run_heartbeat_loop(
    client: CodebuffClient,
    instance_id: str,
    active: asyncio.Event,
) -> None:
    """后台心跳：立即一拍，随后每 45 秒一次，直到 active 被 clear。

    [FP-3 确定] 2026-08-19：旧实现是 `await asyncio.sleep(45)` 在前，
    而我们单次流式响应通常只有 10 余秒，于是心跳任务**每次都在睡眠中被取消**——
    log.txt 全文心跳计数为 0，一次都没真正发出去。上游看到的是
    「创建并持有 session + 高频 chat + 零心跳」，与任何真实客户端都不同。

    官方行为（抓包会话 123 实证）：
    - POST /session 成功后 **1.9 秒**就发第一拍（seq 19 → seq 20）
    - 之后连续 `remainingMs` 差值 46689/45963/42595/47446/42264/45048/45019 ms，
      即稳定 45 秒，且**贯穿整个 session 生命周期，包括 run 之间的空闲间隙**

    遗留差距：本函数仍绑在流式响应期间，两次请求之间的空闲期没有心跳。
    要完全对齐需把心跳挂到 SessionManager 的 session 生命周期上，
    属下一轮改动（见 Docs/VERIFY-2026-08-19.md 的 A4 条目）。
    """
    if not instance_id:
        return
    try:
        # 立即一拍：对齐官方「建 session 后马上心跳」，也保证短流式请求至少发一次。
        await client.heartbeat(instance_id)
        while active.is_set():
            await asyncio.sleep(45)
            if active.is_set():
                await client.heartbeat(instance_id)
    except asyncio.CancelledError:
        pass


async def _stream_openai_chunks(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    *,
    api_key = None,
    account_lease: CodebuffAccountLease | None = None,
    client: CodebuffClient | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    message_id: str | None = None
    client = client or (account_lease.client if account_lease else _client(request))
    settings = _settings(request)
    # 启动心跳保活
    heartbeat_active = asyncio.Event()
    heartbeat_active.set()
    instance_id = (payload.get("codebuff_metadata") or {}).get("freebuff_instance_id", "")
    heartbeat_task = asyncio.create_task(_run_heartbeat_loop(client, instance_id, heartbeat_active))
    done_sent = False
    recorded = False
    chunk_yielded = False
    finish_reason_sent = False
    chunk_log_count = 0
    retried = False
    try:
        while True:
            try:
                async for line in client.chat_events(payload):
                    data = decode_sse_data(line)
                    if data is None:
                        continue
                    if (
                        not chunk_yielded
                        and time.monotonic() - started > settings.empty_stream_timeout
                    ):
                        raise CodebuffError(
                            "Codebuff chat returned empty stream (no content within timeout)",
                            502,
                        )
                    if data == "[DONE]":
                        if settings.debug:
                            logger.debug(
                                "chat stream done run_id=%s message_id=%s",
                                run.run_id,
                                message_id,
                            )
                        if not finish_reason_sent:
                            yield encode_sse(
                                {
                                    "id": message_id or f"chatcmpl-{uuid.uuid4().hex}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": payload.get("model", ""),
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": (
                                                {"role": "assistant"}
                                                if not chunk_yielded
                                                else {}
                                            ),
                                            "finish_reason": "stop",
                                        }
                                    ],
                                }
                            )
                            finish_reason_sent = True
                        yield encode_sse("[DONE]")
                        done_sent = True
                        break

                    message_id = data.get("id") or message_id
                    chunk = sanitize_stream_chunk(
                        data,
                        fold_reasoning_in_content=settings.reasoning_in_content,
                    )
                    if chunk is not None:
                        for choice in chunk.get("choices") or []:
                            if choice.get("finish_reason") is not None:
                                finish_reason_sent = True
                        chunk_bytes = encode_sse(chunk)
                        chunk_log_count += 1
                        if settings.debug and (
                            settings.log_stream_chunks or chunk_log_count <= 10
                        ):
                            logger.debug(
                                "chat stream downstream chunk #%s bytes=%s data=%s",
                                chunk_log_count,
                                len(chunk_bytes),
                                render_debug(chunk, settings.log_body_chars),
                            )
                        yield chunk_bytes
                        chunk_yielded = True
                    elif settings.debug:
                        logger.debug(
                            "chat stream ignored data=%s",
                            render_debug(data, settings.log_body_chars),
                        )
                break
            except CodebuffError as error:
                # 空流恢复（对齐 v1.8.5）：上游 200 但首 chunk 即 EOF → 删上游 session、
                # 重建同模型 session + 重新 START run 后重试一次。仅在尚未发出任何内容
                # chunk 时重试（已发内容无法回滚）。绝不改成别的模型。
                if (
                    not chunk_yielded
                    and not retried
                    and account_lease is not None
                    and (
                        "empty stream" in str(error)
                        or "session expired" in str(error)
                        or "session ended" in str(error)
                        or error.status_code == 410
                        or error.status_code == 428
                    )
                ):
                    retried = True
                    new_payload = await _recreate_session_and_run_for_retry(
                        account_lease, payload
                    )
                    if new_payload is not None:
                        payload = new_payload
                        logger.warning(
                            "session expired or empty stream detected; recreated session and retrying model=%s",
                            payload.get("model"),
                        )
                        continue
                raise
    except CodebuffError as error:
        if account_lease is not None:
            _handle_upstream_error(request, account_lease._account_index, error, payload.get("model", ""))
        logger.warning(
            "chat stream failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=settings.debug,
        )
        stream_model = payload.get("model", "")
        notice = notice_for_error(error, stream_model)
        if notice is not None:
            if api_key:
                duration_ms = int((time.time() - started) * 1000)
                _record_request(request, api_key, stream_model, duration_ms, "notice", error=str(error))
            recorded = True
            for chunk in _openai_notice_stream(stream_model, notice):
                yield chunk
            yield encode_sse("[DONE]")
            done_sent = True
        else:
            if api_key:
                duration_ms = int((time.time() - started) * 1000)
                _record_request(request, api_key, stream_model, duration_ms, "error", error=str(error))
            recorded = True
            yield encode_sse(
                {
                    "error": {
                        "message": _friendly_upstream_message(error),
                        "upstream_message": str(error),
                        "type": "upstream_error",
                        "code": "codebuff_error",
                    }
                }
            )
            yield encode_sse("[DONE]")
            done_sent = True
    except Exception as error:
        # 兜底：任何未预期异常（上游断流/解析异常等）也发 error + [DONE]，
        # 避免生成器裸抛导致连接直接关闭（客户端报 "terminated / other side closed"）。
        if account_lease is not None:
            _handle_upstream_error(request, account_lease._account_index, error, payload.get("model", ""))
        logger.exception(
            "chat stream unexpected error run_id=%s",
            run.run_id,
        )
        if api_key:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "error", error=str(error))
        recorded = True
        yield encode_sse(
            {
                "error": {
                    "message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            }
        )
        yield encode_sse("[DONE]")
        done_sent = True
    finally:
        heartbeat_active.clear()
        heartbeat_task.cancel()
        # 上游 EOF 但未发 [DONE]（免费通道长对话常见）→ 补发终止符，
        # 否则客户端等 [DONE] 等到连接关闭报 "terminated"。
        if not done_sent:
            if not finish_reason_sent:
                yield encode_sse(
                    {
                        "id": message_id or f"chatcmpl-{uuid.uuid4().hex}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": payload.get("model", ""),
                        "choices": [
                            {
                                "index": 0,
                                "delta": (
                                    {"role": "assistant"}
                                    if not chunk_yielded
                                    else {}
                                ),
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
                finish_reason_sent = True
            yield encode_sse("[DONE]")
        if api_key and not recorded:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "success")
        _schedule_finalize_run(client, run, message_id)
        if account_lease is not None:
            await account_lease.aclose()


async def _collect_completion(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    client: CodebuffClient | None = None,
    account_lease: CodebuffAccountLease | None = None,
) -> dict[str, Any]:
    message_id: str | None = None
    accumulator = CompletionAccumulator(model)
    client = client or _client(request)
    retried = False
    try:
        while True:
            try:
                async for line in client.chat_events(payload):
                    data = decode_sse_data(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break
                    message_id = data.get("id") or message_id
                    accumulator.add(data)
                break
            except CodebuffError as error:
                # 空流恢复（与流式路径一致）：尚未累积任何内容时，重建同模型
                # session + run 重试一次；仍失败则把友好错误抛给上层返回客户端。
                if (
                    not retried
                    and account_lease is not None
                    and (
                        "empty stream" in str(error)
                        or "session expired" in str(error)
                        or "session ended" in str(error)
                        or error.status_code == 410
                        or error.status_code == 428
                    )
                ):
                    retried = True
                    new_payload = await _recreate_session_and_run_for_retry(
                        account_lease, payload
                    )
                    if new_payload is not None:
                        payload = new_payload
                        accumulator = CompletionAccumulator(model)
                        logger.warning(
                            "session expired or empty stream detected; recreated session and retrying model=%s",
                            payload.get("model"),
                        )
                        continue
                raise
        response = accumulator.final_response()
        logger.info(
            "chat completion response run_id=%s message_id=%s content_chars=%s finish_reason=%s",
            run.run_id,
            message_id,
            len(response["choices"][0]["message"].get("content") or ""),
            response["choices"][0].get("finish_reason"),
        )
        if _settings(request).debug:
            logger.debug(
                "chat completion response body=%s",
                render_debug(response, _settings(request).log_body_chars),
            )
        return response
    finally:
        await _finalize_run(request, run, message_id, client=client)


# run_id 缓存（对齐 Worker RUN_CACHE_TTL_MS）：上游 chat 只校验 run_id 存在，
# 可跨请求复用，10 分钟缓存省掉每次请求 2 次 agent-runs 调用。
_RUN_CACHE_TTL_SECONDS = 10 * 60
_run_cache: dict[str, tuple[float, FreebuffRun]] = {}
_run_step_counters: dict[str, int] = {}


def _next_llm_step_number(run_id: str) -> str:
    """每个 run 内部按调用次数递增 llm_step_number（官方桌面端行为）。"""
    count = _run_step_counters.get(run_id, 0) + 1
    _run_step_counters[run_id] = count
    return str(count)


def _cached_run(key: str) -> FreebuffRun | None:
    now = time.monotonic()
    if len(_run_cache) > 200:
        expired = [k for k, (ts, _) in _run_cache.items() if now - ts > _RUN_CACHE_TTL_SECONDS]
        for k in expired:
            _run_cache.pop(k, None)
    hit = _run_cache.get(key)
    if hit is None:
        return None
    ts, cached_run = hit
    if now - ts >= _RUN_CACHE_TTL_SECONDS:
        _run_cache.pop(key, None)
        return None
    if cached_run.chat_run_id:
        return FreebuffRun(
            run_id=cached_run.run_id,
            agent_id=cached_run.agent_id,
            started_at=utc_now_iso(),
            child_run_id=cached_run.child_run_id,
            chat_run_id=cached_run.chat_run_id,
            chat_started_at=utc_now_iso(),
        )
    return FreebuffRun(
        run_id=cached_run.run_id,
        agent_id=cached_run.agent_id,
        started_at=utc_now_iso(),
        child_run_id=cached_run.child_run_id,
    )


def _store_cached_run(key: str, run: FreebuffRun) -> None:
    _run_cache[key] = (time.monotonic(), run)


async def _start_freebuff_run_chain(
    client: CodebuffClient,
    model: FreebuffModel | str,
) -> FreebuffRun:
    if isinstance(model, str):
        model = FreebuffModel(model, model)
    if model.parent_agent_id:
        return await _start_child_chat_run_chain(client, model)

    # 0.0.63 实测：官方桌面端对 context-pruner 使用 UNTRACKED_RUN_ID_PREFIX 假 run id，
    # 并不调用 /api/v1/agent-runs。这里只 START 主 run，省掉一次 10s+ 的上游调用，
    # 大幅降低首包前延迟，避免客户端在 run chain 阶段超时重试。
    agent_id = model.agent_id
    token = getattr(getattr(client, "settings", None), "codebuff_token", "") or ""
    # [B1 待验证] 上报给 /api/v1/agent-runs 的 agentId 换成桌面端 thread agent id。
    # 依据 & 回退方式见 config.py 的 desktop_agent_id 注释与
    # Docs/VERIFY-2026-08-19.md B1。缓存键仍按 model.agent_id 区分，
    # 避免不同模型共用同一个 run（额度桶按模型分，run 不能串）。
    start_agent_id = (
        getattr(getattr(client, "settings", None), "desktop_agent_id", "") or agent_id
    )
    cache_key = f"{token}:{agent_id}"
    cached = _cached_run(cache_key)
    if cached is not None:
        logger.debug("reuse cached freebuff run agent_id=%s", agent_id)
        return cached

    started_at = utc_now_iso()
    run_id = await client.start_run(start_agent_id)
    run = FreebuffRun(
        run_id=run_id,
        # 缓存里记业务 agent_id（用于回填 codebuff_metadata / 日志定位），
        # 不记上报值 —— 两者的用途不同，别混。
        agent_id=agent_id,
        started_at=started_at,
        child_run_id=None,
    )
    _store_cached_run(cache_key, run)
    return run


async def _start_child_chat_run_chain(
    client: CodebuffClient,
    model: FreebuffModel,
) -> FreebuffRun:
    # [B1 待验证 · 已知遗留差异] 这条分支（Gemini file-picker / thinker-with-files）
    # 故意**不套用** desktop_agent_id：它们是真正的 codebuff 子 agent，官方也是
    # 用 ancestorRunIds 挂在父 run 下的。抓包那 12 次 START 之所以全是空
    # ancestorRunIds，是那段会话没有生成子 agent，不能反推「官方从不用子 run」。
    # 这里保持原样以免拖垮当前可用的 Gemini 通道；等真实数据能证明官方桌面端
    # 子 agent 的 START 长什么样，再统一。
    assert model.parent_agent_id is not None

    token = getattr(getattr(client, "settings", None), "codebuff_token", "") or ""
    cache_key = f"{token}:child:{model.agent_id}"
    cached = _cached_run(cache_key)
    if cached is not None:
        logger.debug("reuse cached freebuff child run agent_id=%s", model.agent_id)
        return cached

    started_at = utc_now_iso()
    parent_run_id = await client.start_run(model.parent_agent_id)
    chat_started_at = utc_now_iso()
    chat_run_id = await client.start_run(
        model.agent_id,
        ancestor_run_ids=[parent_run_id],
    )
    run = FreebuffRun(
        run_id=parent_run_id,
        agent_id=model.parent_agent_id,
        started_at=started_at,
        child_run_id=chat_run_id,
        chat_run_id=chat_run_id,
        chat_started_at=chat_started_at,
    )
    _store_cached_run(cache_key, run)
    return run


async def _finalize_run(
    request: Request,
    run: FreebuffRun,
    message_id: str | None,
    *,
    client: CodebuffClient | None = None,
) -> None:
    await _finalize_run_with_client(client or _client(request), run, message_id)


def _schedule_finalize_run(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    task = asyncio.create_task(_finalize_run_with_client(client, run, message_id))

    def _log_background_error(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("background finalize task cancelled run_id=%s", run.run_id)
        except Exception:
            logger.exception("background finalize task failed run_id=%s", run.run_id)

    task.add_done_callback(_log_background_error)


async def _finalize_run_with_client(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    # 精简版（对齐 Worker 1.7.0）：chat 只校验 run_id 存在，finalize 无需再打
    # record_step / finish_run 管理请求。保留函数为向后兼容，函数体为空。
    logger.debug(
        "finalize run skipped (streamlined) run_id=%s message_id=%s",
        run.run_id,
        message_id,
    )


# ── Anthropic Messages API (/v1/messages) ─────────────────────────────


@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    api_key = _check_anthropic_auth(request, require_configured=True)
    _check_freebuff_token(request)
    settings = _settings(request)
    raw_body = await request.body()
    if settings.max_request_body_bytes > 0 and len(raw_body) > settings.max_request_body_bytes:
        logger.warning(
            "[client] anthropic request rejected 413 body_too_large size=%s limit=%s ip=%s",
            len(raw_body),
            settings.max_request_body_bytes,
            request.client.host if request.client else None,
        )
        return JSONResponse(
            status_code=413,
            content=anthropic_error_payload(
                f"Request body too large / 请求体过大：当前 {len(raw_body)} bytes，"
                f"超过限制 {settings.max_request_body_bytes} bytes。请减小上下文/附件大小，"
                "或在客户端启用上下文压缩。",
                error_type="invalid_request_error",
                status_code=413,
            ),
        )
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "Invalid JSON body.",
                error_type="invalid_request_error",
                status_code=400,
            ),
        )

    # Validate required fields — return Anthropic-compatible errors.
    if not isinstance(body.get("messages"), list):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "messages: field required (must be a non-empty list)",
                error_type="invalid_request_error",
            ),
        )
    if not body.get("messages"):
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "messages: must be a non-empty list",
                error_type="invalid_request_error",
            ),
        )
    if body.get("max_tokens") is None:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(
                "max_tokens: field required",
                error_type="invalid_request_error",
            ),
        )

    # Model resolution — preserve original model name for the response.
    requested_model = body.get("model")
    try:
        model_config = resolve_model(requested_model)
    except ValueError as error:
        return JSONResponse(
            status_code=400,
            content=anthropic_error_payload(str(error), error_type="invalid_request_error"),
        )
    model = model_config.id
    if api_key and not api_key.allows_model(model):
        return JSONResponse(
            status_code=403,
            content=anthropic_error_payload(
                f"API key '{api_key.name}' not allowed to use model '{model}'",
                error_type="permission_error",
            ),
        )
    stream = body.get("stream") is True
    logger.info(
        "[client] anthropic messages request model=%s stream=%s messages=%s max_tokens=%s",
        model,
        stream,
        len(body["messages"]),
        body["max_tokens"],
    )
    if settings.debug:
        logger.debug(
            "[inbound] anthropic messages request headers=%s",
            redact_headers(dict(request.headers)),
        )
        logger.debug(
            "[inbound] anthropic messages request body=%s",
            render_debug(body, settings.log_body_chars),
        )

    # Session & run preparation (shared with OpenAI path).
    lease: CodebuffAccountLease | None = None
    try:
        lease = await _accounts(request).acquire_session(
            model_config.session_id,
        )
        client = lease.client
        await client.request_ad_chain()
        # 同 OpenAI 路径：不再调用 validate_agents()，缩小暴露面。
        run = await _start_freebuff_run_chain(client, model_config)
        trace_session_id = str(uuid.uuid4())
        payload = build_anthropic_upstream_payload(
            body,
            session=lease.session,
            run_id=run.payload_run_id,
            client_id=settings.client_id,
            trace_session_id=trace_session_id,
            upstream_model_id=model_config.upstream_id,
            system_prompt=settings.system_prompt_override,
            max_tools=settings.max_tools_per_request,
            llm_step_number=_next_llm_step_number(run.payload_run_id),
            max_messages=settings.max_messages_per_request,
        )
        if settings.debug:
            logger.debug(
                "[outbound] prepared upstream anthropic trace=%s run=%s payload=%s",
                trace_session_id,
                run,
                render_debug(payload, settings.log_body_chars),
            )
    except CodebuffError as error:
        if lease is not None:
            _handle_upstream_error(request, lease._account_index, error, model)
            await lease.aclose()
        logger.warning(
            "failed to prepare anthropic messages: %s",
            error,
            exc_info=settings.debug,
        )
        notice = notice_for_error(error, model)
        if notice is not None:
            return JSONResponse(_anthropic_notice_response(model, notice))
        status_code = getattr(error, "status_code", 502)
        return JSONResponse(
            status_code=status_code,
            content=anthropic_error_payload(
                _friendly_upstream_message(error), status_code=status_code
            ),
        )
    except Exception as error:
        if lease is not None:
            await lease.aclose()
        logger.exception("failed to prepare anthropic messages")
        return JSONResponse(
            status_code=500,
            content=anthropic_error_payload(str(error)),
        )

    if stream:
        return StreamingResponse(
            _stream_anthropic_events(request, payload, run, api_key=api_key, account_lease=lease, requested_model=requested_model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    started = time.time()
    try:
        response = await _collect_anthropic_message(
            request,
            payload,
            run,
            requested_model,
            client=lease.client,
            account_lease=lease,
        )
        duration_ms = int((time.time() - started) * 1000)
        _record_request(request, api_key, model, duration_ms, "success",
            prompt_tokens=response.get("usage", {}).get("input_tokens", 0),
            completion_tokens=response.get("usage", {}).get("output_tokens", 0),
            total_tokens=(response.get("usage", {}).get("input_tokens", 0) + response.get("usage", {}).get("output_tokens", 0)))
        return JSONResponse(response)
    except Exception as error:
        duration_ms = int((time.time() - started) * 1000)
        if isinstance(error, CodebuffError):
            _handle_upstream_error(request, lease._account_index, error, model)
            notice = notice_for_error(error, model)
            if notice is not None:
                _record_request(request, api_key, model, duration_ms, "notice", error=str(error))
                return JSONResponse(_anthropic_notice_response(model, notice))
        else:
            _handle_upstream_error(request, lease._account_index, error, model)
        _record_request(request, api_key, model, duration_ms, "error", error=str(error))
        status_code = getattr(error, "status_code", 500)
        return JSONResponse(
            status_code=status_code,
            content=anthropic_error_payload(
                _friendly_upstream_message(error), status_code=status_code
            ),
        )
    finally:
        await lease.aclose()


async def _stream_anthropic_events(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    *,
    api_key = None,
    account_lease: CodebuffAccountLease | None = None,
    client: CodebuffClient | None = None,
    requested_model: str | None = None,
) -> AsyncIterator[bytes]:
    started = time.time()
    client = client or (account_lease.client if account_lease else _client(request))
    settings = _settings(request)
    # 启动心跳保活
    heartbeat_active = asyncio.Event()
    heartbeat_active.set()
    instance_id = (payload.get("codebuff_metadata") or {}).get("freebuff_instance_id", "")
    heartbeat_task = asyncio.create_task(_run_heartbeat_loop(client, instance_id, heartbeat_active))
    state = AnthropicStreamState(model=requested_model or payload.get("model", ""))
    _ping_active = True
    finalized = False
    recorded = False
    retried = False

    async def _ping_loop() -> None:
        """Send ping every ~15 s to keep the connection alive across proxies."""
        try:
            while _ping_active:
                await asyncio.sleep(15)
                if _ping_active:
                    yield anthropic_sse_ping()
        except asyncio.CancelledError:
            pass

    def _emit_finalize():
        nonlocal finalized
        if finalized:
            return
        finalized = True
        for event_type, event_data in state.finalize_events():
            yield anthropic_sse_encode(event_type, event_data)

    try:
        while True:
            try:
                async for line in client.chat_events(payload):
                    data = decode_sse_data(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        # Emit final events.
                        for sse_line in _emit_finalize():
                            yield sse_line
                        break

                    for event_type, event_data in state.consume_chunk(data):
                        if settings.debug:
                            logger.debug(
                                "anthropic stream event=%s data=%s",
                                event_type,
                                render_debug(event_data, settings.log_body_chars),
                            )
                        yield anthropic_sse_encode(event_type, event_data)
                break
            except CodebuffError as error:
                # 空流恢复（对齐 v1.8.5）：尚未发出任何事件时遇到空流 →
                # 重建同模型 session + run 后重试一次。
                if (
                    finalized is False
                    and not retried
                    and account_lease is not None
                    and (
                        "empty stream" in str(error)
                        or "session expired" in str(error)
                        or "session ended" in str(error)
                        or error.status_code == 410
                        or error.status_code == 428
                    )
                ):
                    retried = True
                    new_payload = await _recreate_session_and_run_for_retry(
                        account_lease, payload
                    )
                    if new_payload is not None:
                        payload = new_payload
                        state = AnthropicStreamState(
                            model=requested_model or payload.get("model", "")
                        )
                        logger.warning(
                            "empty stream detected; recreated session and retrying model=%s",
                            payload.get("model"),
                        )
                        continue
                raise
    except CodebuffError as error:
        if account_lease is not None:
            _handle_upstream_error(request, account_lease._account_index, error, requested_model or payload.get("model", ""))
        logger.warning(
            "anthropic stream failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=settings.debug,
        )
        stream_model = requested_model or payload.get("model", "")
        notice = notice_for_error(error, stream_model)
        if notice is not None:
            if api_key:
                duration_ms = int((time.time() - started) * 1000)
                _record_request(request, api_key, stream_model, duration_ms, "notice", error=str(error))
            recorded = True
            chunk = {
                "id": f"msg_{uuid.uuid4().hex[:24]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": stream_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": notice},
                        "finish_reason": "stop",
                    }
                ],
            }
            for event_type, event_data in state.consume_chunk(chunk):
                yield anthropic_sse_encode(event_type, event_data)
            for sse_line in _emit_finalize():
                yield sse_line
        else:
            if api_key:
                duration_ms = int((time.time() - started) * 1000)
                _record_request(request, api_key, stream_model, duration_ms, "error", error=str(error))
            recorded = True
            error_payload = anthropic_error_payload(_friendly_upstream_message(error))
            yield anthropic_sse_encode("error", error_payload)
            # 错误后也补 finalize（message_stop），保证 Anthropic 客户端能收尾
            for sse_line in _emit_finalize():
                yield sse_line
    except Exception as error:
        if account_lease is not None:
            _handle_upstream_error(request, account_lease._account_index, error, requested_model or payload.get("model", ""))
        logger.exception(
            "anthropic stream unexpected error run_id=%s",
            run.run_id,
        )
        if api_key:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, requested_model or payload.get("model", ""), duration_ms, "error", error=str(error))
        recorded = True
        error_payload = anthropic_error_payload(_friendly_upstream_message(error))
        yield anthropic_sse_encode("error", error_payload)
        for sse_line in _emit_finalize():
            yield sse_line
    finally:
        # 上游 EOF 但未发 [DONE]（免费通道长对话常见）→ 补 finalize（message_stop），
        # 否则 Anthropic 客户端悬挂/报 "terminated / other side closed"。
        for sse_line in _emit_finalize():
            yield sse_line
        if api_key and not recorded:
            duration_ms = int((time.time() - started) * 1000)
            _record_request(request, api_key, payload.get("model", ""), duration_ms, "success")
        heartbeat_active.clear()
        heartbeat_task.cancel()
        _ping_active = False
        _schedule_finalize_run(client, run, None)
        if account_lease is not None:
            await account_lease.aclose()


async def _collect_anthropic_message(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    client: CodebuffClient | None = None,
    account_lease: CodebuffAccountLease | None = None,
) -> dict[str, Any]:
    accumulator = AnthropicCompletionAccumulator(model)
    client = client or _client(request)
    retried = False
    try:
        while True:
            try:
                async for line in client.chat_events(payload):
                    data = decode_sse_data(line)
                    if data is None:
                        continue
                    if data == "[DONE]":
                        break
                    accumulator.add(data)
                break
            except CodebuffError as error:
                if (
                    not retried
                    and account_lease is not None
                    and (
                        "empty stream" in str(error)
                        or "session expired" in str(error)
                        or "session ended" in str(error)
                        or error.status_code == 410
                        or error.status_code == 428
                    )
                ):
                    retried = True
                    new_payload = await _recreate_session_and_run_for_retry(
                        account_lease, payload
                    )
                    if new_payload is not None:
                        payload = new_payload
                        accumulator = AnthropicCompletionAccumulator(model)
                        logger.warning(
                            "session expired or empty stream detected; recreated session and retrying model=%s",
                            payload.get("model"),
                        )
                        continue
                raise
        response = accumulator.final_response()
        content_blocks = len(response.get("content") or [])
        stop_reason = response.get("stop_reason")
        logger.info(
            "anthropic message response run_id=%s id=%s content_blocks=%s stop_reason=%s",
            run.run_id,
            response.get("id"),
            content_blocks,
            stop_reason,
        )
        if _settings(request).debug:
            logger.debug(
                "anthropic message response body=%s",
                render_debug(response, _settings(request).log_body_chars),
            )
        return response
    finally:
        await _finalize_run(request, run, None, client=client)

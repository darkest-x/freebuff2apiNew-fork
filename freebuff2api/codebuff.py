from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from .config import HAR_BROWSER_USER_AGENT, Settings, project_env_path
from .logging_config import redact_headers, render_debug
from .models import agent_validation_payload, is_premium_quota_exhausted, session_bucket_for_model
from .token_rotation import (
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STATUS_CHECKING,
    STATUS_INVALID,
    RotationState,
    is_account_banned,
    is_capacity_deferred,
    is_country_blocked,
    is_insufficient_quota,
    is_ip_capped,
    is_model_unavailable,
    is_policy_violation_error,
    is_provider_usage_error,
    is_rate_limit_error,
    is_spend_limited,
    next_beijing_1500_epoch,
    parse_429_info,
)


logger = logging.getLogger("freebuff2api.codebuff")

CODEBUFF_ACCEPT_ENCODING = "gzip, deflate"  # 官方 UA 虽带 br/zstd，但 Python httpx 默认无法解码 br/zstd，保持 gzip/deflate
# 桌面版协议伪装（2026-08-10，对齐 pingmike2/freebuff2api-wokers 1.7.0 / issue #13）：
# 旧版 CLI 指纹（Freebuff-CLI/0.0.105、旧 ai-sdk 版本号）已被上游
# detectForeignFreebuffClient / foreign-client-signals.ts 标记，命中后强制降级到
# 免费层模型（表现为空响应/429），并做账号级统计（终态封禁）。
#
# [FP-2 确定] 2026-08-19 修正一个反效果决策：
#   旧代码「JSON 请求不手动设置 User-Agent（httpx 默认）」，注释写着"消除 CLI 运行时特征"，
#   实际效果完全相反 —— httpx 在没有显式 UA 时会自动补 `python-httpx/0.x.x`，
#   于是 chat 伪装成 bun、session/agent-runs 自报 Python，同一个 token 两种身份，
#   是一击必杀的指纹。抓包（会话 123 seq 19/20）实证官方 JSON 请求 UA 就是 `Bun/1.3.14`，
#   与 chat 的 ai-sdk UA 是两个不同的值。
CODEBUFF_JSON_USER_AGENT = "Bun/1.3.14"
CHAT_COMPLETIONS_USER_AGENT = "ai-sdk/openai-compatible/0.0.0-test/codebuff ai-sdk/provider-utils/3.0.25 runtime/bun/1.3.14"

# 广告/streak 链节流（对齐 worker.js runNormalClientBehavior：每账号 30 分钟一次）。
AD_CHAIN_THROTTLE_SECONDS = 30 * 60
_ad_chain_last_at: dict[str, float] = {}


def _ad_chain_due(token: str) -> bool:
    """返回 True 表示本次请求需要执行广告链，否则跳过（30 分钟节流）。

    module-level dict 足够：token 数量少，异步并发时最坏情况是多执行一次广告链，
    与官方客户端的节流语义一致且无害。
    """
    now = time.monotonic()
    last = _ad_chain_last_at.get(token)
    if last is not None and now - last < AD_CHAIN_THROTTLE_SECONDS:
        return False
    _ad_chain_last_at[token] = now
    return True


class CodebuffError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FreebuffSession:
    instance_id: str
    model: str
    expires_at: str | None = None
    remaining_ms: int | None = None

    @property
    def is_fresh(self) -> bool:
        return self.remaining_ms is None or self.remaining_ms > 60_000


@dataclass
class FreebuffRun:
    run_id: str
    agent_id: str
    started_at: str
    child_run_id: str | None = None
    chat_run_id: str | None = None
    chat_started_at: str | None = None

    @property
    def payload_run_id(self) -> str:
        return self.chat_run_id or self.run_id


@dataclass
class FreebuffSessionLease:
    session: FreebuffSession
    _lock: asyncio.Lock
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lock.release()


class CodebuffClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # httpx client is created lazily on first use. With a proxy configured,
        # constructing an AsyncClient is slow (~1.4s on Windows); deferring it
        # keeps account-pool startup fast even with many accounts.
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._agents_validated = False
        self._validate_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(self.settings.request_timeout, read=None),
                        follow_redirects=True,
                        proxy=self.settings.upstream_proxy_url,
                        trust_env=False,
                        verify=self.settings.ssl_cert_file or True,
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(
        self,
        *,
        json_body: bool = False,
        user_agent: str | None = None,
        require_auth: bool = True,
        extra: dict[str, str] | None = None,
        header_style: str = "json",
    ) -> dict[str, str]:
        """构造上游请求头，键名与顺序对齐官方桌面端。

        [FP-1 确定] 2026-08-19：官方（bun / ai-sdk）发出的头**全小写**，且顺序固定。
        抓包（Anything Analyzer 会话 123）实证两套顺序：

        - `header_style="json"`（bun fetch 直接调，seq 19 POST /session）::

              authorization, x-freebuff-instance-id, x-freebuff-model,
              x-freebuff-multi-session, connection, user-agent, accept,
              host, accept-encoding, content-length

        - `header_style="chat"`（ai-sdk 构造，seq 150 POST /chat/completions）::

              authorization, content-type, user-agent,
              x-freebuff-acting-user-id, connection, accept, host,
              accept-encoding, content-length

        两套的共同规律：业务头（authorization / content-type / x-freebuff-*）在前，
        bun 自动补的通用头在后；`x-freebuff-*` 之间是**字母序**（bun Headers 的排序行为）。
        差异只在 user-agent 的位置：json 在 connection 之后，chat 在 content-type 之后。

        改动前我们发的是 `Accept, Accept-Encoding, Connection, Host, Authorization,
        x-freebuff-*` —— 顺序几乎完全相反，且大写驼峰与小写混排，是 HTTP/1.1 上
        可直接观测的客户端指纹。
        """
        if require_auth and not self.settings.codebuff_token:
            raise CodebuffError("FREEBUFF_TOKEN or CODEBUFF_TOKEN is required", 500)

        # [FP-2 确定] 默认带官方 JSON UA（Bun/1.3.14）。绝不能留空 —— httpx 会自动
        # 补 `python-httpx/x.y.z`，等于自报 Python 客户端。
        resolved_ua = user_agent or CODEBUFF_JSON_USER_AGENT

        headers: dict[str, str] = {}
        if require_auth:
            headers["authorization"] = f"Bearer {self.settings.codebuff_token}"
        if json_body:
            headers["content-type"] = "application/json"
        if header_style == "chat":
            headers["user-agent"] = resolved_ua
        # 业务头按字母序插入，与 bun Headers 的排序行为一致。
        if extra:
            for key in sorted(extra):
                headers[key.lower()] = extra[key]
        headers["connection"] = "keep-alive"
        if header_style != "chat":
            headers["user-agent"] = resolved_ua
        headers["accept"] = "*/*"
        headers["host"] = _host_header(self.settings.codebuff_api_url)
        headers["accept-encoding"] = CODEBUFF_ACCEPT_ENCODING
        return headers

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.codebuff_api_url}{path}"
        request_headers = headers or self._headers(json_body=body is not None)
        try:
            response = await (await self._ensure_client()).request(
                method,
                url,
                json=body,
                headers=request_headers,
            )
        except httpx.RequestError as error:
            # [A5 确定] 网络层失败时也要留下请求现场。风控直接掐连接的表现就是
            # RemoteProtocolError('Server disconnected without sending a response.')，
            # 旧代码在这里直接 raise，日志里只剩一句异常，看不出我们发了什么头/体。
            if self.settings.debug:
                logger.debug(
                    "[outbound] upstream json request FAILED method=%s url=%s headers=%s body=%s error=%r",
                    method,
                    url,
                    redact_headers(request_headers),
                    render_debug(body, self.settings.log_body_chars),
                    error,
                )
            raise _network_error(method, url, error) from error
        if self.settings.debug:
            # [A5 确定] 记录 httpx **实际发出**的头（response.request.headers），
            # 而不是我们构造的 dict。两者的差集正是 httpx 自动补的头
            # （user-agent / content-length / accept-encoding 实际值），
            # 也正是 python-httpx 指纹泄漏在旧日志里"看不见"的原因。
            logger.debug(
                "[outbound] upstream json request method=%s url=%s actual_headers=%s body=%s",
                method,
                url,
                redact_headers(dict(response.request.headers)),
                render_debug(body, self.settings.log_body_chars),
            )
            # [A5 确定] 响应头此前完全没有记录。其中 cf-ray（含机房代码）、
            # server、x-render-origin-server、rndr-id、content-encoding、
            # retry-after / x-ratelimit-* 是判断"是否被风控拦下"的直接线索。
            logger.debug(
                "[outbound] upstream json response status=%s headers=%s body=%s",
                response.status_code,
                redact_headers(dict(response.headers)),
                render_debug(response.text, self.settings.log_body_chars),
            )
        if response.status_code >= 400:
            raise _upstream_error(response)
        if not response.content:
            return {}
        return response.json()

    async def validate_agents(self) -> None:
        """校验 agent 定义（**当前无调用方，勿再启用**）。

        [C7 确定] 抓包实证：`filter_requests(urlPattern="/api/agents")` → **空**，
        官方桌面端**从不**请求 `/api/agents/validate`。而这个请求的 body 是
        `agent_validation_payload()` —— 相当于主动把我们伪造的整份 agent 注册表
        报给上游，是纯送分的暴露面；返回值在这里也只用于打日志，**没有任何功能作用**。
        调用点已在 `app.py:473`/`app.py:1165`/`admin.py:899` 移除，方法体保留仅为
        兼容旧引用。要恢复的话请先证明官方会发这个请求。
        """
        if self._agents_validated:
            return
        async with self._validate_lock:
            if self._agents_validated:
                return
            try:
                data = await self._json(
                    "POST",
                    "/api/agents/validate",
                    body=agent_validation_payload(),
                    headers=self._headers(json_body=True, require_auth=False),
                )
            except CodebuffError:
                logger.warning(
                    "agent validation failed; continuing with server configs",
                    exc_info=self.settings.debug,
                )
                self._agents_validated = True
                return
            error_count = int(data.get("errorCount") or 0)
            if error_count:
                logger.warning(
                    "agent validation returned errors count=%s body=%s",
                    error_count,
                    render_debug(data, self.settings.log_body_chars),
                )
            else:
                logger.info(
                    "agent validation completed configs=%s",
                    len(data.get("configs") or []),
                )
            self._agents_validated = True

    async def health(self) -> dict[str, Any]:
        return await self._json(
            "GET",
            "/api/healthz",
            headers=self._headers(require_auth=False),
        )

    async def get_session(
        self,
        instance_id: str | None = None,
        *,
        include_rate_limits: bool = False,
    ) -> dict[str, Any]:
        """查询当前 session 状态。

        [FP-4 确定] 2026-08-19：`x-freebuff-include-unused-rate-limits` 改为**默认不带**。

        抓包（会话 123）纠正了一处旧认知：响应是最小 body 还是完整 body，
        由这个头决定，**不是** `status` 字段决定的。
        - 官方心跳（seq 20）不带该头 → 响应只有 status/accessTier/instanceId/model/
          admittedAt/expiresAt/remainingMs 七个字段
        - 带该头才会返回 rateLimitsByModel / desktopSessionCounts / referral / glmPromo

        旧代码无条件带该头，导致我们**每次 chat 前都拉一次完整额度快照**
        （log.txt 实测 ≈每 11 秒一次）；官方客户端整个 session 生命周期只在
        UI 需要刷新额度面板时做 1–2 次。高频轮询额度是典型的脚本特征。

        另外 `POST /session` 的响应**本身就含** rateLimitsByModel + desktopSessionCounts
        （seq 19 实证，且该 POST 请求并没带这个头），所以创建后根本不需要额外 GET 去拉。
        """
        extra = {"x-freebuff-multi-session": "1"}
        if include_rate_limits:
            extra["x-freebuff-include-unused-rate-limits"] = "1"
        if instance_id:
            extra["x-freebuff-instance-id"] = instance_id
        return await self._json(
            "GET",
            "/api/v1/freebuff/session",
            headers=self._headers(extra=extra),
        )

    async def create_session(self, model: str) -> FreebuffSession:
        logger.info("create freebuff session requested model=%s", model)
        # 桌面版签名（对齐 Worker 1.7.0）：客户端预生成 instance-id，服务端据此绑定会话，
        # 避免旧版"服务端分配实例"特征被 detectForeignFreebuffClient 标记。
        instance_id = str(uuid.uuid4())
        headers = self._headers(
            extra={
                "x-freebuff-model": model,
                "x-freebuff-instance-id": instance_id,
                "x-freebuff-multi-session": "1",
            }
        )
        try:
            data = await self._json(
                "POST",
                "/api/v1/freebuff/session",
                headers=headers,
            )
        except CodebuffError as error:
            # 旧的同 bucket 会话仍占着 premium 槽（例如服务重启后本地缓存丢失）。
            # 官方桌面版支持 x-freebuff-takeover-instance-id 抢占，但反代里更安全的
            # 做法是删除旧实例后重试一次。
            if "premium_slot_taken" not in str(error):
                raise
            current_instance_id = _extract_current_instance_id(str(error))
            if not current_instance_id:
                raise
            logger.info(
                "premium slot taken by stale session; deleting old instance_id=%s and retrying model=%s",
                current_instance_id,
                model,
            )
            await self.delete_session(current_instance_id)
            data = await self._json(
                "POST",
                "/api/v1/freebuff/session",
                headers=headers,
            )
        if data.get("status") == "queued":
            return await self._wait_for_active_session(data, model)
        return self._session_from_data(data, model)

    def _session_from_data(
        self,
        data: dict[str, Any],
        model: str,
        instance_id: str | None = None,
    ) -> FreebuffSession:
        status = data.get("status")
        if status == "active":
            resolved_instance_id = data.get("instanceId") or instance_id
            if not resolved_instance_id:
                raise CodebuffError(f"Freebuff session is not active: {data}", 502)
            return FreebuffSession(
                instance_id=resolved_instance_id,
                model=data.get("model") or model,
                expires_at=data.get("expiresAt"),
                remaining_ms=data.get("remainingMs"),
            )
        if status == "banned":
            raise CodebuffError(f"Freebuff account banned: {data}", 403)
        if status == "country_blocked":
            raise CodebuffError(f"Freebuff country_blocked: {data}", 403)
        if status == "rate_limited":
            raise CodebuffError(
                f"Freebuff session rate_limited: 429 {json.dumps(data, ensure_ascii=False)}",
                429,
            )
        if status == "spend_limited":
            raise CodebuffError(
                f"Freebuff session spend_limited: 429 {json.dumps(data, ensure_ascii=False)}",
                429,
            )
        if status == "ip_capped":
            raise CodebuffError(
                f"Freebuff session ip_capped: 429 {json.dumps(data, ensure_ascii=False)}",
                429,
            )
        if status == "model_unavailable":
            raise CodebuffError(
                f"Freebuff session model_unavailable: 429 {json.dumps(data, ensure_ascii=False)}",
                429,
            )
        if status == "free_mode_capacity_deferred":
            raise CodebuffError(
                f"Freebuff session free_mode_capacity_deferred: 429 {json.dumps(data, ensure_ascii=False)}",
                429,
            )
        if status == "premium_slot_taken":
            raise CodebuffError(f"Freebuff premium_slot_taken: {data}", 409)
        if status == "session_limit_reached":
            raise CodebuffError(f"Freebuff session_limit_reached: {data}", 409)
        raise CodebuffError(f"Freebuff session is not active: {data}", 502)

    async def _wait_for_active_session(
        self,
        data: dict[str, Any],
        model: str,
    ) -> FreebuffSession:
        instance_id = data.get("instanceId")
        if not instance_id:
            raise CodebuffError(f"Freebuff queued session id missing: {data}", 502)

        deadline = time.monotonic() + self.settings.request_timeout
        attempts = 0
        while data.get("status") == "queued":
            logger.info(
                "freebuff session queued model=%s instance_id=%s position=%s estimated_wait_ms=%s",
                model,
                instance_id,
                data.get("position"),
                data.get("estimatedWaitMs"),
            )
            if time.monotonic() >= deadline:
                raise CodebuffError(
                    f"Freebuff session did not become active before timeout: {data}",
                    502,
                )
            if attempts:
                await asyncio.sleep(_queue_poll_delay(data.get("estimatedWaitMs")))
            data = await self.get_session(instance_id)
            attempts += 1

        return self._session_from_data(data, model, instance_id=instance_id)

    async def delete_session(self, instance_id: str | None = None) -> None:
        extra = {"x-freebuff-multi-session": "1"}
        if instance_id:
            extra["x-freebuff-instance-id"] = instance_id
        await self._json(
            "DELETE",
            "/api/v1/freebuff/session",
            headers=self._headers(extra=extra),
        )
        logger.info("deleted active freebuff session")

    async def heartbeat(self, instance_id: str) -> None:
        """发送心跳保活（对齐官方桌面端 45 秒心跳）。

        官方 FREEBUFF_SESSION_HEARTBEAT_INTERVAL_MS = 45000。
        不心跳的话服务器可能提前回收 session，导致长任务被中断。
        """
        try:
            response = await (await self._ensure_client()).get(
                f"{self.settings.codebuff_api_url}/api/v1/freebuff/session",
                headers=self._headers(
                    extra={
                        "x-freebuff-multi-session": "1",
                        "x-freebuff-instance-id": instance_id,
                        "x-freebuff-heartbeat": "1",
                    }
                ),
                timeout=httpx.Timeout(10.0),
            )
            # 官方只发请求不读 body，直接 cancel
            await response.aclose()
        except Exception:
            pass

    async def get_streak(self) -> dict[str, Any]:
        data = await self._json(
            "GET",
            "/api/v1/freebuff/streak",
            headers=self._headers(),
        )
        logger.info(
            "freebuff streak streak=%s today_used=%s",
            data.get("streak"),
            data.get("todayUsed"),
        )
        return data

    async def request_ads(
        self,
        provider: str,
        messages: list[dict[str, Any]] | None = None,
        surface: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "provider": provider,
            "messages": _ad_messages(messages),
            "sessionId": self.settings.session_id,
            "device": {
                "os": self.settings.os_name,
                "timezone": self.settings.timezone,
                "locale": self.settings.locale,
            },
            "userAgent": HAR_BROWSER_USER_AGENT,
        }
        if surface:
            body["surface"] = surface
        return await self._json(
            "POST",
            "/api/v1/ads",
            body=body,
            headers=self._headers(json_body=True),
        )

    async def request_ad_chain(
        self,
        messages: list[dict[str, Any]] | None = None,
        *,
        surface: str | None = None,
    ) -> None:
        if not _ad_chain_due(_ad_chain_key(self)):
            logger.info("ad chain throttled (30 min window) token=%s", _token_prefix(_client_token(self)))
            return
        for provider in self.settings.ad_providers:
            try:
                ads_data = await self.request_ads(
                    provider,
                    messages=messages,
                    surface=surface,
                )
                ads = ads_data.get("ads") or []
                ad = ads[0] if ads else None
                logger.info(
                    "ads provider=%s messages=%s count=%s selected=%s",
                    provider,
                    len(messages or []),
                    len(ads),
                    bool(ad),
                )
                if not ad:
                    continue
                await self.report_zeroclick_impressions(
                    list(ad.get("impressionIds") or [])
                )
                await self.report_codebuff_impression(ad.get("impUrl") or "")
                return
            except CodebuffError as error:
                logger.warning(
                    "ads provider=%s failed; continuing without blocking chat: %s",
                    provider,
                    error,
                    exc_info=self.settings.debug,
                )

    async def report_zeroclick_impressions(self, ids: list[str]) -> None:
        if not ids:
            return
        url = f"{self.settings.zeroclick_api_url}/api/v2/impressions"
        try:
            response = await (await self._ensure_client()).post(
                url,
                json={"ids": ids},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                },
            )
        except httpx.RequestError as error:
            raise _network_error("POST", url, error) from error
        if self.settings.debug:
            logger.debug(
                "[outbound] zeroclick impression ids=%s status=%s body=%s",
                ids,
                response.status_code,
                render_debug(response.text, self.settings.log_body_chars),
            )
        if response.status_code >= 400:
            raise CodebuffError(
                f"Zeroclick impression failed: {response.status_code} {response.text[:500]}",
                502,
            )

    async def report_codebuff_impression(self, imp_url: str) -> None:
        if not imp_url:
            return
        await self._json(
            "POST",
            "/api/v1/ads/impression",
            body={"impUrl": imp_url, "mode": "LITE"},
            headers=self._headers(json_body=True),
        )

    async def start_run(
        self,
        agent_id: str,
        ancestor_run_ids: list[str] | None = None,
    ) -> str:
        data = await self._json(
            "POST",
            "/api/v1/agent-runs",
            body={
                "action": "START",
                "agentId": agent_id,
                "ancestorRunIds": ancestor_run_ids or [],
            },
        )
        run_id = data.get("runId")
        if not run_id:
            raise CodebuffError(f"Codebuff run id missing: {data}", 502)
        logger.info(
            "agent run started agent_id=%s run_id=%s ancestors=%s",
            agent_id,
            run_id,
            ancestor_run_ids or [],
        )
        return run_id

    # [FP-7/A7 确定] 已删除 `record_run_step()`（原来打 POST /api/v1/agent-runs/{run_id}/steps）。
    # 抓包实证：`filter_requests(urlPattern="agent-runs/")` → **空**，官方从不调用带
    # 路径参数的 agent-runs 子端点。step 信息是打包进 FINISH body 的 `steps` 数组
    # 一次性提交的（见下方 finish_run）。该方法是老参考项目遗留的错误认知，
    # 当前 finalize 被 skip 所以没触发，但一旦有人开启 finalize 就会打 404 —— 定时炸弹。
    # AGENTS.md 第 7 步的描述同步修正。

    async def finish_run(
        self,
        run_id: str,
        *,
        total_steps: int,
        steps: list[dict[str, Any]] | None = None,
        status: str = "completed",
        error_message: str | None = None,
    ) -> None:
        """上报 run 结束。

        [B3 待验证] body 格式已对齐官方抓包实录：
        ```json
        {"action":"FINISH","runId":"...","status":"completed","totalSteps":9,
         "directCredits":0,"totalCredits":0,
         "steps":[{"id":"...","stepNumber":1,"credits":0,"childRunIds":[],
                   "messageId":"...","status":"completed","startTime":"..."}]}
        ```
        官方 12 个 run 的 totalSteps 分布：6,6,6,9,9,10,12,13,14,14,21,42；
        失败时如实报 `status:"failed"` + `errorMessage`（含 JS 堆栈）。

        **注意**：本方法当前**没有生产调用方**（`app.py::_finalize_run_with_client`
        仍然 skip）。这里只把「万一启用时的 body 格式」改对，是否启用、以及反代
        如何划定 run 边界（我们看不到「用户 turn」）尚未定论 ——
        两种候选方案见 Docs/VERIFY-2026-08-19.md 第五节。
        """
        body: dict[str, Any] = {
            # 键序照官方：action, runId, status, totalSteps, directCredits, totalCredits, steps
            "action": "FINISH",
            "runId": run_id,
            "status": status,
            "totalSteps": total_steps,
            "directCredits": 0,
            "totalCredits": 0,
        }
        if error_message:
            body["errorMessage"] = error_message
        # steps 为 None 时给空数组：官方 body 里这个键始终存在，
        # 但我们绝不伪造 step 记录（假 UUID/假 startTime 本身就是指纹）。
        body["steps"] = steps or []
        await self._json("POST", "/api/v1/agent-runs", body=body)
        logger.info(
            "agent run finished run_id=%s status=%s total_steps=%s steps_reported=%s",
            run_id,
            status,
            total_steps,
            len(body["steps"]),
        )

    async def chat_events(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        url = f"{self.settings.codebuff_api_url}/api/v1/chat/completions"
        # 对齐 Freebuff Desktop 0.0.62：chat 请求头不再额外携带 x-freebuff-instance-id。
        # 官方桌面端只把 freebuff_instance_id 放进 codebuff_metadata body；
        # 旧版额外头是 1.7 时期的逆向结论，最新 orchestrator.js 中 chat headers 不含该头。
        #
        # [FP-1 确定] acting-user-id 走 extra 传入，才能落在官方位置
        # （user-agent 之后、connection 之前）；旧代码构造完再追加，会掉到
        # accept-encoding 后面，顺序错位。
        # [C2 未验证] 该 UUID 的可靠来源尚未确认（session 响应里没有 userId 字段），
        # 默认不发送；填错会导致 409，宁缺勿滥。详见 Docs/VERIFY-2026-08-19.md C2。
        chat_extra: dict[str, str] = {}
        if self.settings.acting_user_id:
            chat_extra["x-freebuff-acting-user-id"] = self.settings.acting_user_id
        request_headers = self._headers(
            json_body=True,
            user_agent=CHAT_COMPLETIONS_USER_AGENT,
            header_style="chat",
            extra=chat_extra or None,
        )
        # [A5 确定] 流式响应的行数/字节数统计。旧实现只打印前 11 行且计数器自增
        # 写在 if 内部（见下方注释），既拿不到完整响应也拿不到真实行数。
        line_count = 0
        total_bytes = 0
        try:
            async with (await self._ensure_client()).stream(
                "POST",
                url,
                json=payload,
                headers=request_headers,
            ) as response:
                if self.settings.debug:
                    # [A5 确定] 同 _json：记录 httpx 实际发出的头。
                    # 其中 content-length 就是本次上游请求体的真实字节数，
                    # 是对比官方基线（step1 约 40–110 KB / step19 约 502 KB）的直接依据，
                    # 不必再去数 JSON。
                    logger.debug(
                        "[outbound] chat stream request url=%s actual_headers=%s payload=%s",
                        url,
                        redact_headers(dict(response.request.headers)),
                        render_debug(payload, self.settings.log_body_chars),
                    )
                    logger.debug(
                        "[outbound] chat stream response status=%s headers=%s",
                        response.status_code,
                        redact_headers(dict(response.headers)),
                    )
                if response.status_code >= 400:
                    text = await response.aread()
                    raise _upstream_error(
                        response,
                        body=text,
                        prefix="Codebuff chat failed",
                    )
                # 预读首行（对齐 pingmike2/freebuff2api-wokers v1.8.5 fetchStreamWithQuotaGuard）：
                # 上游 200 但流为空（首 chunk 即 EOF，常见于免费通道长对话/额度脏状态）
                # 时，抛带 "empty stream" 标记的 CodebuffError，供上层同模型 session
                # 重建后重试一次，避免客户端收到空响应 / "terminated"。
                lines = response.aiter_lines()
                try:
                    first_line = await lines.__anext__()
                except StopAsyncIteration:
                    raise CodebuffError(
                        "Codebuff chat returned empty stream",
                        502,
                    ) from None

                def _trace_line(text: str) -> None:
                    """行级统计 + 可选逐行日志。

                    [A5 确定] 计数器必须在 if **之外**自增。旧代码把
                    `line_count += 1` 写进 `if debug and (log_stream_chunks or
                    line_count <= 10)` 内部，后果有两个：
                      ① 关闭逐行日志时，计数一到 11 就不再满足 `<= 10`，
                         于是第 11 行之后彻底不记 —— 日志里永远只有前 11 行；
                      ② line_count 不等于真实行数，汇总统计无从谈起。
                    现在无论是否 debug 都统计，日志详略只影响「打不打印每一行」。
                    """
                    nonlocal line_count, total_bytes
                    size = len(text.encode("utf-8", errors="replace"))
                    line_count += 1
                    total_bytes += size
                    if not self.settings.debug:
                        return
                    # 默认只逐行打印前 10 行（足够看到 role / 首个 delta /
                    # 工具调用开头）；FREEBUFF_LOG_STREAM_CHUNKS=true 打印全部。
                    if self.settings.log_stream_chunks or line_count <= 10:
                        logger.debug(
                            "chat stream upstream line #%s bytes=%s data=%s",
                            line_count,
                            size,
                            render_debug(text, self.settings.log_body_chars),
                        )

                _trace_line(first_line)
                yield first_line
                async for line in lines:
                    _trace_line(line)
                    yield line
        except httpx.RequestError as error:
            # [A5 确定] 旧代码这里直接抛，日志里一片空白。
            # log.txt 实录的「Server disconnected without sending a response」
            # 走的正是这条分支 —— 风控在 TCP 层拒绝是本项目的核心症状，
            # 却连「当时发了什么头、多大的体、断在第几行」都没留下现场。
            if self.settings.debug:
                logger.debug(
                    "[outbound] chat stream FAILED url=%s headers=%s payload_chars=%s "
                    "lines_before_failure=%s bytes_before_failure=%s error=%r",
                    url,
                    redact_headers(request_headers),
                    len(json.dumps(payload, ensure_ascii=False, default=str)),
                    line_count,
                    total_bytes,
                    error,
                )
            raise _network_error("POST", url, error) from error
        finally:
            # [A5 确定] 无论正常结束、上游报错还是下游提前断开（生成器被关闭），
            # 都留一条汇总：总行数 + 总字节数。用来判断「流是不是被截断了」——
            # 比对 finish_reason 缺失 + 行数偏少即可定位。
            if self.settings.debug:
                logger.debug(
                    "[outbound] chat stream closed url=%s lines=%s bytes=%s",
                    url,
                    line_count,
                    total_bytes,
                )


class SessionManager:
    """每个账号维护两条串行通道：premium 1 + unlimited 1。

    与官方桌面端一致：premium 和 unlimited 会话互相独立、可同时存在；
    同一条通道内的请求串行复用同一个上游 session，不会因为下游多开会话而
    挤掉正在响应的另一条通道。
    """

    def __init__(self, client: CodebuffClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._sessions: dict[str, FreebuffSession] = {}
        self._locks = {
            "premium": asyncio.Lock(),
            "unlimited": asyncio.Lock(),
        }
        self.premium_quota_exhausted_until = 0.0

    @staticmethod
    def _bucket(model: str) -> str:
        return session_bucket_for_model(model)

    def _raise_if_premium_quota_exhausted(self, data: dict[str, Any]) -> None:
        rate_limits = data.get("rateLimitsByModel") if isinstance(data, dict) else None
        if not is_premium_quota_exhausted(rate_limits):
            return
        self.premium_quota_exhausted_until = next_beijing_1500_epoch()
        raise CodebuffError(
            "Freebuff premium daily quota exhausted. Resets at 15:00 Asia/Shanghai.",
            403,
        )

    def _lock_for(self, model: str) -> asyncio.Lock:
        return self._locks[self._bucket(model)]

    async def ensure_session(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> FreebuffSession:
        async with self._lock_for(model):
            return await self._ensure_session_locked(model, messages)

    async def acquire_session(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> FreebuffSessionLease:
        lock = self._lock_for(model)
        await lock.acquire()
        try:
            session = await self._ensure_session_locked(model, messages)
        except Exception:
            lock.release()
            raise
        return FreebuffSessionLease(session=session, _lock=lock)

    async def _ensure_session_locked(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> FreebuffSession:
        cached = self._sessions.get(model)
        if cached and cached.is_fresh:
            try:
                data = await self.client.get_session(cached.instance_id)
                if data.get("status") == "active" and data.get("model") in {
                    None,
                    model,
                }:
                    if self._bucket(model) == "premium":
                        self._raise_if_premium_quota_exhausted(data)
                    cached.remaining_ms = data.get("remainingMs")
                    logger.debug(
                        "reuse freebuff session model=%s instance_id=%s remaining_ms=%s",
                        model,
                        cached.instance_id,
                        cached.remaining_ms,
                    )
                    return cached
                if data.get("status") == "active":
                    logger.info(
                        "cached freebuff session model mismatch cached=%s upstream=%s",
                        model,
                        data.get("model"),
                    )
                    self._sessions.pop(model, None)
            except CodebuffError:
                logger.debug(
                    "cached freebuff session invalid model=%s instance_id=%s",
                    model,
                    cached.instance_id,
                    exc_info=self.settings.debug,
                )
                self._sessions.pop(model, None)

        active_session = await self._delete_locked_session(model)
        if active_session:
            return active_session
        # 同 bucket 只保留一个 session：清理本地缓存中的其他同 bucket 会话。
        # 当前请求持有该 bucket 的通道锁，因此这些会话不可能还在被其他请求使用。
        await self._delete_same_bucket_sessions(model, force=True)
        await self._request_ads_and_streak(surface="waiting_room")
        return await self._create_session_locked(model)

    async def _create_session_locked(self, model: str) -> FreebuffSession:
        """创建并缓存 session。调用方必须已持有该 bucket 的通道锁。"""
        try:
            session = await self.client.create_session(model)
        except CodebuffError as error:
            if "model_locked" not in str(error):
                raise
            logger.info(
                "freebuff session locked during create; delete same-bucket session and retry model=%s",
                model,
            )
            await self._delete_same_bucket_sessions(model, force=True)
            await self._request_ads_and_streak(surface="waiting_room")
            session = await self.client.create_session(model)
        self._sessions[model] = session
        logger.debug(
            "created freebuff session model=%s instance_id=%s remaining_ms=%s",
            model,
            session.instance_id,
            session.remaining_ms,
        )
        return session

    async def _request_ads_and_streak(
        self,
        messages: list[dict[str, Any]] | None = None,
        *,
        surface: str | None = None,
    ) -> None:
        if not _ad_chain_due(_ad_chain_key(self.client)):
            logger.info("session ad chain throttled (30 min window) token=%s", _token_prefix(_client_token(self.client)))
            return
        for provider in self.settings.ad_providers:
            try:
                ads_data = await self.client.request_ads(
                    provider,
                    messages=messages,
                    surface=surface,
                )
                ads = ads_data.get("ads") or []
                ad = ads[0] if ads else None
                logger.info(
                    "ads provider=%s messages=%s count=%s selected=%s",
                    provider,
                    len(messages or []),
                    len(ads),
                    bool(ad),
                )
                if not ad:
                    continue
                await self.client.get_streak()
                await self.client.report_zeroclick_impressions(
                    list(ad.get("impressionIds") or [])
                )
                await self.client.report_codebuff_impression(ad.get("impUrl") or "")
                return
            except CodebuffError as error:
                logger.warning(
                    "ads provider=%s failed; continuing without blocking chat: %s",
                    provider,
                    error,
                    exc_info=self.settings.debug,
                )

    async def _delete_locked_session(
        self,
        requested_model: str,
    ) -> FreebuffSession | None:
        """发现并复用/清理服务端当前活跃 session（仅限同 bucket）。

        桌面端 multi-session 下，premium 与 unlimited 通道互不影响，因此这里
        只处理与 requested_model 相同 bucket 的活跃 session；不同 bucket 的
        session 绝不删除。
        """
        try:
            data = await self.client.get_session()
        except CodebuffError:
            logger.debug(
                "could not inspect active freebuff session before create",
                exc_info=self.settings.debug,
            )
            return None

        if data.get("status") != "active":
            return None

        if self._bucket(requested_model) == "premium":
            self._raise_if_premium_quota_exhausted(data)

        current_model = data.get("model")
        instance_id = data.get("instanceId")
        if current_model == requested_model and instance_id:
            session = FreebuffSession(
                instance_id=instance_id,
                model=current_model,
                expires_at=data.get("expiresAt"),
                remaining_ms=data.get("remainingMs"),
            )
            self._sessions[requested_model] = session
            logger.info(
                "discovered active freebuff session model=%s instance_id=%s remaining_ms=%s",
                requested_model,
                session.instance_id,
                session.remaining_ms,
            )
            return session

        if not current_model or current_model == requested_model:
            return None

        if self._bucket(current_model) != self._bucket(requested_model):
            logger.info(
                "keep other-bucket freebuff session current_model=%s requested_model=%s",
                current_model,
                requested_model,
            )
            return None

        logger.info(
            "switch same-bucket freebuff session current_model=%s requested_model=%s instance_id=%s",
            current_model,
            requested_model,
            instance_id,
        )
        await self.client.delete_session(instance_id)
        self._clear_bucket_sessions(requested_model)
        return None

    async def _delete_same_bucket_sessions(
        self,
        requested_model: str,
        *,
        force: bool = False,
    ) -> None:
        bucket = self._bucket(requested_model)
        stale_models = [
            model
            for model in self._sessions
            if model != requested_model and self._bucket(model) == bucket
        ]
        for old_model in stale_models:
            old_session = self._sessions.pop(old_model, None)
            if old_session is None:
                continue
            if force:
                try:
                    await self.client.delete_session(old_session.instance_id)
                except CodebuffError as error:
                    logger.debug(
                        "delete stale freebuff session model=%s failed: %s",
                        old_model,
                        error,
                    )

    def discard_session(self, model: str) -> None:
        self._sessions.pop(model, None)

    def clear_bucket_sessions(self, model: str) -> None:
        self._clear_bucket_sessions(model)

    def _clear_bucket_sessions(self, model: str) -> None:
        bucket = self._bucket(model)
        for key in [m for m in self._sessions if self._bucket(m) == bucket]:
            self._sessions.pop(key, None)


@dataclass
class CodebuffAccount:
    client: CodebuffClient
    sessions: SessionManager
    busy: bool = False
    active_requests: int = 0
    premium_active_requests: int = 0
    unlimited_active_requests: int = 0
    premium_quota_exhausted_until: float = 0.0

    @property
    def is_busy(self) -> bool:
        return self.active_requests > 0


@dataclass
class CodebuffAccountLease:
    client: CodebuffClient
    session: FreebuffSession
    _session_lease: FreebuffSessionLease
    _pool: CodebuffAccountPool
    _account_index: int
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._session_lease.aclose()
        await self._pool.release(self._account_index, self.session.model)


class CodebuffAccountPool:
    def __init__(self, settings: Settings, rotation: RotationState | None = None) -> None:
        tokens = settings.codebuff_tokens or (None,)
        self._accounts: list[CodebuffAccount] = []
        for token in tokens:
            account_settings = replace(settings, codebuff_token=token)
            client = CodebuffClient(account_settings)
            self._accounts.append(
                CodebuffAccount(
                    client=client,
                    sessions=SessionManager(client, account_settings),
                )
            )
        self._next_index = 0
        self._condition = asyncio.Condition()
        self._rotation = rotation or RotationState(len(self._accounts), project_env_path())
        self._max_concurrency = max(1, getattr(settings, "max_concurrency_per_account", 1))
        self.rotation_mode = getattr(settings, "rotation_mode", "balanced")
        self._premium_index = self._rotation.current_index if self._accounts else 0
        self._premium_banned_until = 0.0
        self._invalid_reasons: dict[int, str] = {}
        self._insufficient_quota_strikes: dict[tuple[int, str], tuple[int, float]] = {}
        self._last_used: dict[int, float] = {}
        self._probe_tasks: set[asyncio.Task] = set()

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    @property
    def active_request_count(self) -> int:
        """Number of in-flight requests across all accounts (for deferred close)."""
        return sum(account.active_requests for account in self._accounts)

    @property
    def default_client(self) -> CodebuffClient:
        return self._accounts[0].client

    @property
    def default_sessions(self) -> SessionManager:
        return self._accounts[0].sessions

    async def aclose(self) -> None:
        for task in list(self._probe_tasks):
            task.cancel()
        if self._probe_tasks:
            await asyncio.gather(*self._probe_tasks, return_exceptions=True)
        await asyncio.gather(
            *(account.client.aclose() for account in self._accounts),
            return_exceptions=True,
        )

    async def acquire_session(
        self,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> CodebuffAccountLease:
        bucket = session_bucket_for_model(model) if model else "premium"
        if time.time() < self._premium_banned_until:
            raise CodebuffError(
                "Freebuff account banned; all models are disabled until the next 15:00 Asia/Shanghai.",
                403,
            )
        last_error: Exception | None = None
        for _ in range(2):
            account_index = await self._reserve_account(model)
            account = self._accounts[account_index]
            logger.info(
                "account reserved index=%s session_model=%s messages=%s",
                account_index + 1,
                model,
                len(messages or []),
            )
            try:
                session_lease = await account.sessions.acquire_session(model, messages)
            except CodebuffError as error:
                await self.release(account_index, model)
                if is_rate_limit_error(str(error)):
                    self.handle_error(account_index, str(error), error.status_code, model)
                    last_error = error
                    continue
                if is_account_banned(str(error)) or is_country_blocked(str(error)):
                    self.handle_error(account_index, str(error), error.status_code, model)
                    raise
                if (
                    is_insufficient_quota(str(error))
                    or is_capacity_deferred(str(error))
                    or is_spend_limited(str(error))
                    or is_ip_capped(str(error))
                    or is_model_unavailable(str(error))
                ):
                    self.handle_error(account_index, str(error), error.status_code, model)
                    raise
                if error.status_code == 403 and "quota exhausted" in str(error):
                    account = self._accounts[account_index]
                    account.premium_quota_exhausted_until = next_beijing_1500_epoch()
                    account.sessions.premium_quota_exhausted_until = account.premium_quota_exhausted_until
                    logger.warning(
                        "account %s premium quota exhausted; will retry next account",
                        account_index + 1,
                    )
                    last_error = error
                    continue
                last_error = error
                self.handle_error(account_index, str(error), error.status_code, model)
                logger.warning(
                    "account session acquire transient failure index=%s session_model=%s: %s",
                    account_index + 1,
                    model,
                    error,
                )
                continue
            except Exception:
                logger.exception(
                    "account session acquire failed index=%s session_model=%s",
                    account_index + 1,
                    model,
                )
                await self.release(account_index, model)
                raise
            # Success: reset the transient-failure counter (optimization ②)
            if self._rotation is not None:
                self._rotation.mark_success(account_index)
            self._insufficient_quota_strikes.pop((account_index, model), None)
            self._last_used[account_index] = time.monotonic()
            logger.info(
                "account session acquired index=%s session_model=%s instance_id=%s remaining_ms=%s",
                account_index + 1,
                session_lease.session.model,
                session_lease.session.instance_id,
                session_lease.session.remaining_ms,
            )
            return CodebuffAccountLease(
                client=account.client,
                session=session_lease.session,
                _session_lease=session_lease,
                _pool=self,
                _account_index=account_index,
            )
        raise last_error if last_error else CodebuffError("no account available")

    async def release(self, account_index: int, model: str = "") -> None:
        async with self._condition:
            account = self._accounts[account_index]
            account.active_requests = max(0, account.active_requests - 1)
            if session_bucket_for_model(model) == "premium":
                account.premium_active_requests = max(0, account.premium_active_requests - 1)
            elif model:
                account.unlimited_active_requests = max(0, account.unlimited_active_requests - 1)
            account.busy = account.active_requests > 0
            self._condition.notify(1)

    async def _reserve_account(self, model: str = "") -> int:
        async with self._condition:
            while True:
                account_index = self._next_available_index(model)
                if account_index is not None:
                    account = self._accounts[account_index]
                    bucket = session_bucket_for_model(model) if model else "premium"
                    account.active_requests += 1
                    if bucket == "premium":
                        account.premium_active_requests += 1
                        if self.rotation_mode in {"balanced", "conservative"}:
                            self._premium_index = account_index
                    elif model:
                        account.unlimited_active_requests += 1
                    account.busy = True
                    self._next_index = (account_index + 1) % len(self._accounts)
                    return account_index
                # No usable account right now. Trigger half-open probes for any
                # account whose cooldown just expired, then wait until the earliest
                # unblock (or a release).
                self._maybe_trigger_half_open_probes()
                wait_secs: float | None = None
                if self._rotation:
                    remaining = [
                        self._rotation.block_remaining(i, model)
                        for i in range(len(self._accounts))
                        if self._rotation.is_blocked(i, model)
                    ]
                    if remaining:
                        wait_secs = min(remaining)
                if wait_secs is None or wait_secs <= 0:
                    wait_secs = 5.0
                if self._all_premium_accounts_unavailable(model):
                    raise CodebuffError(
                        "Freebuff premium daily quota exhausted. Resets at 15:00 Asia/Shanghai.",
                        403,
                    )
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_secs)
                except asyncio.TimeoutError:
                    pass

    def _all_premium_accounts_unavailable(self, model: str = "") -> bool:
        """True 表示所有账号都不是因为 busy 而是因为额度耗尽/限流/失效而不可用。

        用于在 _reserve_account 等待循环中提前返回 403，避免无限等待。
        """
        if session_bucket_for_model(model) != "premium":
            return False
        if not self._accounts:
            return False
        now = time.time()
        for index, account in enumerate(self._accounts):
            if account.premium_quota_exhausted_until > now:
                continue
            if self._rotation and (
                self._rotation.is_blocked(index, model)
                or self._rotation.status_of(index) == STATUS_INVALID
            ):
                continue
            # 这个账号还在 busy，说明还有机会，不能判定全部不可用
            if account.active_requests < self._max_concurrency and account.premium_active_requests < 1:
                return False
        return True

    def _next_available_index(self, model: str = "") -> int | None:
        """Select the next usable account according to the configured rotation mode.

        Modes:
        - ``throughput``: round-robin across all accounts; premium/unlimited can
          both fan out (original behavior, highest throughput, highest risk).
        - ``balanced``: unlimited fan out across all accounts; premium uses only
          one account at a time (sequential failover on normal quota).
        - ``conservative``: unlimited uses only account 1; premium sequential
          failover like balanced. If any account is banned, premium is disabled
          until the next 15:00 Asia/Shanghai.
        """
        account_count = len(self._accounts)
        if account_count == 0:
            return None
        bucket = session_bucket_for_model(model) if model else "premium"

        # Banned premium gate（balanced/conservative 模式）：被 ban 后限制模型全部停用，
        # 直到下一个北京时间 15 点。throughput 模式保持原行为。
        if (
            bucket == "premium"
            and self.rotation_mode in {"balanced", "conservative"}
            and time.time() < self._premium_banned_until
        ):
            return None

        if bucket == "premium" and self.rotation_mode in {"balanced", "conservative"}:
            # 限制模型：全局只允许 1 条 premium 通道，从 premium_index 开始选一个账号。
            premium_active = sum(
                account.premium_active_requests for account in self._accounts
            )
            if premium_active >= 1:
                return None
            start = self._premium_index % account_count
            for offset in range(account_count):
                account_index = (start + offset) % account_count
                account = self._accounts[account_index]
                if account.active_requests >= self._max_concurrency:
                    continue
                if account.premium_active_requests >= 1:
                    continue
                if account.premium_quota_exhausted_until > time.time():
                    continue
                if self._rotation and (
                    self._rotation.is_blocked(account_index, model)
                    or self._rotation.status_of(account_index) == STATUS_INVALID
                ):
                    continue
                return account_index
            return None

        if bucket == "unlimited" and self.rotation_mode == "conservative":
            # 最保守模式：免费模型也只用第一个账号。
            account_index = 0
            account = self._accounts[account_index]
            if account.active_requests >= self._max_concurrency:
                return None
            if account.unlimited_active_requests >= 1:
                return None
            if self._rotation and (
                self._rotation.is_blocked(account_index, model)
                or self._rotation.status_of(account_index) == STATUS_INVALID
            ):
                return None
            return account_index

        # throughput / balanced 的 unlimited：round-robin 扇出。
        start = self._next_index % account_count
        for offset in range(account_count):
            account_index = (start + offset) % account_count
            account = self._accounts[account_index]
            if account.active_requests >= self._max_concurrency:
                continue
            if bucket == "premium" and account.premium_active_requests >= 1:
                continue
            if bucket == "unlimited" and account.unlimited_active_requests >= 1:
                continue
            if self._rotation and (
                self._rotation.is_blocked(account_index, model)
                or self._rotation.status_of(account_index) == STATUS_INVALID
            ):
                continue
            return account_index
        return None

    # ── Half-open probes (optimization ③) ───────

    def _maybe_trigger_half_open_probes(self) -> None:
        """Accounts whose cooldown expired but are still marked blocked get a
        background probe; success → active (notifies waiters), failure → re-block."""
        if self._rotation is None:
            return
        now = time.monotonic()
        for index in range(len(self._accounts)):
            if self._rotation.status_of(index) != STATUS_BLOCKED:
                continue
            if not self._rotation.is_blocked(index):
                # cooldown expired → probe in the background
                self._rotation.mark_checking(index)
                task = asyncio.create_task(self._half_open_probe(index))
                self._probe_tasks.add(task)
                task.add_done_callback(self._probe_tasks.discard)
                logger.info("half-open probe scheduled account=%s", index + 1)

    async def _half_open_probe(self, index: int) -> None:
        """Probe one account after cooldown expiry. True → active + notify;
        False → re-block briefly; inconclusive → active (keep usable)."""
        try:
            result = await self._validate_account_token(index)
            async with self._condition:
                if result is False:
                    # Still failing: short cooldown so it isn't retried immediately
                    self._rotation.block(index, retry_after_ms=30_000)
                    logger.warning(
                        "half-open probe failed account=%s re-blocked 30s", index + 1
                    )
                else:
                    self._rotation.mark_active(index)
                    logger.info(
                        "half-open probe ok account=%s", index + 1
                    )
                self._condition.notify_all()
        except Exception:
            logger.exception("half-open probe error account=%s", index + 1)
            async with self._condition:
                self._rotation.mark_active(index)
                self._condition.notify_all()

    # ── Health / rotation helpers ────────────────

    @property
    def rotation(self) -> RotationState:
        return self._rotation

    def account_statuses(self) -> list[dict[str, Any]]:
        """Per-account status for the admin panel."""
        rows: list[dict[str, Any]] = []
        for index, account in enumerate(self._accounts):
            rows.append(
                {
                    "index": index + 1,
                    "token_prefix": (account.client.settings.codebuff_token or "")[:8],
                    "status": (
                        self._rotation.status_of(index) if self._rotation else STATUS_ACTIVE
                    ),
                    "blocked": self._rotation.is_blocked(index) if self._rotation else False,
                    "block_remaining": (
                        round(self._rotation.block_remaining(index), 1)
                        if self._rotation
                        else 0
                    ),
                    "failure_count": (
                        self._rotation.failure_count_of(index) if self._rotation else 0
                    ),
                    "invalid_reason": self._invalid_reasons.get(index, ""),
                    "is_current": bool(
                        self._rotation and index == self._rotation.current_index
                    ),
                    "last_429": (
                        self._rotation.last_429_info
                        if self._rotation
                        and self._rotation.last_429_account == index
                        else {}
                    ),
                }
            )
        return rows

    def handle_error(self, index: int, message: str, status_code: int = 502, model: str = "") -> None:
        """Record an upstream error against an account.

        分类逻辑（重点是区分账号封禁 / 地区限制 / 额度耗尽 / 上游拥堵）：

        - ``banned``：账号级封禁。标记该账号并全局停用所有模型到下一个
          北京时间 15:00，不再尝试其他账号（避免连带）。
        - ``country_blocked``：出口 IP 地区限制。账号本身没问题，不标记
          账号失效，也不轮换（所有账号共享同一个出口 IP）。
        - ``policy violation``：上游模型提供商策略违规，只封当前模型。
        - ``rate_limited`` / 其他 429：正常每日额度耗尽，轮换到下一个账号。
        - ``insufficient_quota``：上游负载饱和。第一次只提示不轮换；
          同一账号同一模型连续第二次仍返回该错误时，按额度耗尽处理并轮换。
        - ``provider usage`` / ``capacity_deferred``：官方侧问题，不轮换。
        """
        if self._rotation is None:
            return
        bucket = session_bucket_for_model(model) if model else "premium"

        if is_account_banned(message):
            self._rotation.mark_invalid(index)
            self._invalid_reasons[index] = "banned"
            self._premium_banned_until = next_beijing_1500_epoch()
            logger.warning(
                "account %s banned; marked invalid and disabled all models until next 15:00 Asia/Shanghai message=%s",
                index + 1,
                message[:300],
            )
            return

        if is_country_blocked(message):
            logger.warning(
                "account %s country_blocked (egress IP issue); keeping account active and not rotating",
                index + 1,
            )
            return

        if is_spend_limited(message) or is_ip_capped(message) or is_model_unavailable(message):
            logger.warning(
                "account %s temporary upstream limit (spend/ip/model); no rotation model=%s",
                index + 1,
                model,
            )
            return

        if is_provider_usage_error(message):
            logger.warning(
                "account %s provider usage exhausted (not account-specific); retrying",
                index + 1,
            )
            return

        if is_policy_violation_error(message):
            until = max(0.0, next_beijing_1500_epoch() - time.time())
            retry_ms = int(until * 1000)
            self._rotation.block(index, retry_ms, model)
            logger.warning(
                "account %s model %s blocked until next 15:00 due to upstream policy violation",
                index + 1,
                model,
            )
            return

        if status_code == 403:
            self._rotation.mark_invalid(index)
            self._invalid_reasons[index] = "forbidden"
            logger.warning(
                "account %s marked invalid due to unclassified 403 message=%s",
                index + 1,
                message[:300],
            )
            return

        if is_capacity_deferred(message):
            logger.warning(
                "account %s free_mode_capacity_deferred (capacity full); no rotation",
                index + 1,
            )
            return

        if is_insufficient_quota(message):
            key = (index, model)
            now = time.time()
            previous = self._insufficient_quota_strikes.get(key)
            if previous is None or now - previous[1] > 1800:
                strikes = 1
            else:
                strikes = previous[0] + 1
            self._insufficient_quota_strikes[key] = (strikes, now)
            if strikes >= 2:
                self._insufficient_quota_strikes[key] = (0, now)
                _, status = self._rotation.rotate(
                    reason="insufficient_quota_x2",
                    error_message=message,
                    is_429=True,
                    failed_index=index,
                    model=model,
                )
                if self.rotation_mode in {"balanced", "conservative"} and bucket == "premium":
                    self._premium_index = self._rotation.current_index
                logger.warning(
                    "account %s insufficient_quota twice; treating as quota exhausted and rotating to %s status=%s model=%s",
                    index + 1,
                    self._rotation.current_index + 1,
                    status,
                    model,
                )
            else:
                logger.info(
                    "account %s insufficient_quota strike %s/2; not rotating model=%s",
                    index + 1,
                    strikes,
                    model,
                )
            return

        if is_rate_limit_error(message) or status_code == 429:
            _, status = self._rotation.rotate(
                reason="429",
                error_message=message,
                is_429=True,
                failed_index=index,
                model=model,
            )
            # balanced/conservative 模式：premium 指针跟随 rotation（正常额度轮换）
            if self.rotation_mode in {"balanced", "conservative"} and bucket == "premium":
                self._premium_index = self._rotation.current_index
            logger.info(
                "account %s rate limited (normal quota), rotated to %s status=%s model=%s",
                index + 1,
                self._rotation.current_index + 1,
                status,
                model,
            )
        elif status_code in (502, 500):
            self._rotation.record_failure(index)


    def manual_rotate(self) -> int:
        if self._rotation is None:
            return 0
        index, _ = self._rotation.rotate(reason="manual")
        if self._accounts:
            self._next_index = index
            self._premium_index = index
        return index

    def set_active(self, index: int) -> None:
        """index is 1-based (admin UI). Sets the preferred rotation pointer."""
        if self._rotation is None:
            return
        if index < 1 or index > len(self._accounts):
            raise IndexError(f"account index out of range: {index}")
        self._rotation.set_active(index - 1)
        self._next_index = index - 1
        self._premium_index = index - 1

    async def validate_accounts(self) -> None:
        """Startup/background health check. Marks invalid accounts so selection skips them."""
        if self._rotation is None:
            return
        if not any(account.client.settings.codebuff_token for account in self._accounts):
            return
        # Cap concurrency: creating an httpx client with a proxy is slow (~1.4s
        # each on Windows), so validating many accounts at once would flood the
        # event loop and delay the server from binding its socket.
        semaphore = asyncio.Semaphore(5)

        async def _run(index: int):
            async with semaphore:
                return await self._validate_account_token(index)

        results = await asyncio.gather(
            *(_run(index) for index in range(len(self._accounts))),
            return_exceptions=True,
        )
        valid = 0
        inconclusive = 0
        for index, ok in enumerate(results):
            if ok is True:
                self._rotation.mark_active(index)
                self._rotation.reset_failures(index)
                valid += 1
            elif ok is False:
                self._rotation.mark_invalid(index)
                logger.warning(
                    "account validation marked invalid index=%s", index + 1
                )
            else:
                # Could not verify (timeout/network) → keep the account usable
                self._rotation.mark_active(index)
                inconclusive += 1
        logger.info(
            "account validation done valid=%s inconclusive=%s total=%s",
            valid,
            inconclusive,
            len(self._accounts),
        )

    async def _validate_account_token(self, index: int) -> bool | None:
        self._rotation.mark_checking(index)
        account = self._accounts[index]
        try:
            await account.client.get_session()
        except CodebuffError as error:
            message = str(error)
            if error.status_code == 401 or "Invalid" in message or "invalid" in message:
                logger.warning(
                    "account validation failed index=%s: %s", index + 1, error
                )
                return False
            logger.warning(
                "account validation could not verify index=%s; keeping: %s",
                index + 1,
                error,
            )
            return None
        except Exception:
            logger.exception(
                "account validation unexpected error index=%s", index + 1
            )
            return None
        return True


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _extract_current_instance_id(message: str) -> str | None:
    """从 premium_slot_taken 错误信息中提取 currentInstanceId。"""
    match = re.search(r"\"currentInstanceId\":\"([^\"]+)\"", message)
    return match.group(1) if match else None


def _host_header(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or "www.codebuff.com"


def _queue_poll_delay(estimated_wait_ms: Any) -> float:
    if isinstance(estimated_wait_ms, int | float) and estimated_wait_ms > 0:
        return min(max(float(estimated_wait_ms) / 1000.0, 0.25), 2.0)
    return 0.25


def _network_error(method: str, url: str, error: httpx.RequestError) -> CodebuffError:
    detail = str(error).strip()
    suffix = f": {detail}" if detail else ""
    return CodebuffError(
        f"Codebuff request failed: {method} {url} network error "
        f"({type(error).__name__}){suffix}",
        502,
    )


def _upstream_error(
    response: httpx.Response,
    *,
    body: bytes | None = None,
    prefix: str = "Codebuff request failed",
) -> CodebuffError:
    raw_text = (
        body.decode("utf-8", errors="replace")
        if body is not None
        else response.text
    )
    text = raw_text[:500]
    if response.status_code == 429:
        return CodebuffError(
            f"{prefix}: {response.status_code} {text}",
            429,
        )
    # 428 waiting_room_required：缓存 session 已失效（僵尸实例）。保留状态码供上层
    # 清缓存重建（对齐 Worker 1.7.0 的 stale-session 重试逻辑），不要降级成 502。
    if response.status_code == 428:
        return CodebuffError(
            f"{prefix}: {response.status_code} {text}",
            428,
        )
    # 410 session_expired：session 超时（1h 限制）。保留状态码供上层自动重建 session。
    if response.status_code == 410:
        return CodebuffError(
            f"Codebuff session expired: {text}",
            410,
        )
    if response.status_code == 409:
        try:
            data = (
                response.json()
                if body is None
                else httpx.Response(
                    response.status_code,
                    content=body,
                    headers=response.headers,
                ).json()
            )
        except ValueError:
            data = {}
        if data.get("error") == "session_model_mismatch":
            upstream_message = data.get("message") or text
            return CodebuffError(
                f"Codebuff 409 session_model_mismatch: {upstream_message}",
                409,
            )
        if data.get("status") == "premium_slot_taken":
            return CodebuffError(
                f"Codebuff 409 premium_slot_taken: {text}",
                409,
            )

    # 解析上游 body 中的 status（官方 postAdmission 在 HTTP 200/4xx 都可能返回
    # {status:"banned"|"country_blocked"|"rate_limited"|...}）。
    data: dict[str, Any] = {}
    try:
        parsed = (
            response.json()
            if body is None
            else httpx.Response(
                response.status_code,
                content=body,
                headers=response.headers,
            ).json()
        )
        if isinstance(parsed, dict):
            data = parsed
    except ValueError:
        data = {}

    status = data.get("status")
    if status == "banned":
        return CodebuffError(
            f"{prefix}: account banned - {text}",
            403,
        )
    if status == "country_blocked":
        return CodebuffError(
            f"{prefix}: country_blocked - {text}",
            403,
        )
    if status == "rate_limited":
        return CodebuffError(
            f"{prefix}: 429 {text}",
            429,
        )
    if status == "spend_limited":
        return CodebuffError(
            f"{prefix}: 429 {text}",
            429,
        )
    if status == "ip_capped":
        return CodebuffError(
            f"{prefix}: 429 {text}",
            429,
        )
    if status == "model_unavailable":
        return CodebuffError(
            f"{prefix}: 429 {text}",
            429,
        )
    if status == "free_mode_capacity_deferred" or data.get("error") == "free_mode_capacity_deferred":
        return CodebuffError(
            f"{prefix}: 429 {text}",
            429,
        )

    if response.status_code == 402 or is_provider_usage_error(text):
        return CodebuffError(
            "Freebuff ran out of provider usage and needs a refill. "
            "This is on us, not your account. (upstream: " + text[:200] + ")",
            402,
        )

    return CodebuffError(
        f"{prefix}: {response.status_code} {text}",
        502,
    )


def _ad_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    return [
        {
            "role": _ad_message_role(message.get("role")),
            "content": _ad_message_content(message.get("content")),
        }
        for message in messages or []
    ]


def _ad_message_role(role: Any) -> str:
    if role == "developer":
        return "system"
    return str(role or "user")


def _ad_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = [
            str(part.get("text"))
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


def _token_prefix(token: str | None) -> str:
    return (token or "")[:8] or "---"


def _client_token(client: Any) -> str:
    return getattr(getattr(client, "settings", None), "codebuff_token", "") or ""


def _ad_chain_key(client: Any) -> str:
    token = _client_token(client)
    return token or f"client:{id(client)}"

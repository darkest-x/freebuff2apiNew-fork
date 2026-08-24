"""run_manager.py — 对话感知的 agent-run 生命周期（官方桌面端模式）。

[FP-6 落地] 2026-08-24。此前反代对 run 的处理是「START 后永不 FINISH」，
上游视角 = 一个从不结束任务的僵尸客户端；freebuff-proxy 的对照教训是
另一个极端：启动即 prewarm 全部 agent 类型 + 6h 才轮换 FINISH，
新号上线 10 秒创建几十个 run，是教科书级 farm signature（用户实测
「用一次封一次」的主要嫌疑）。

本模块按官方桌面端画像补全中间态：

1. 懒 START：首个请求才创建 run（与官方「用户发起任务才 START」一致）
2. 同对话复用：messages 是历史超集 → 复用同一 run，llm_step_number 递增
   （官方一个 run 多 step，totalSteps 分布 6~42）
3. 如实 FINISH：
   - 对话切换 / 空闲超时 / 进程退出 → status="completed"
   - chat 终态失败        → status="failed" + errorMessage
   - 客户端断连           → status="cancelled"
   （freebuff-proxy steps.go 原话：zero failed/cancelled runs looks synthetic）
4. steps 打包进 FINISH body（端点 POST /agent-runs/{id}/steps 不存在，
   见 codebuff.py finish_run 注释），messageId 必须取自上游 SSE chunk.id，
   绝不自造 UUID —— 自造 UUID 会当场暴露（VERIFY B3 警告）

边界方案采用 VERIFY-2026-08-19.md 第五节「方案一：按 messages 增量推断」，
并加三道兜底防误判放大：指纹不匹配时安全侧新建 run、空闲 TTL 强制轮换、
进程退出统一 FINISH。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注用，避免循环导入
    from .codebuff import CodebuffClient

logger = logging.getLogger("freebuff2api.runs")

# 单个 run 记录的 step 上限（freebuff-proxy maxRecordedSteps=512 同款思路）：
# 防止长会话把 FINISH body 撑到多 MB；totalSteps 用单调计数保持诚实
MAX_RECORDED_STEPS = 512


@dataclass
class RunStep:
    """一次成功 chat 的 step 记录，字段名/键序照官方 FINISH body。

    messageId 来自上游 SSE chunk.id（gen-<epoch>-<随机串>，OpenRouter 风格）；
    取不到时为 None —— 官方 schema 允许 null，绝不伪造。
    """

    step_number: int
    message_id: str | None
    start_time: str  # RFC3339 UTC


@dataclass
class ManagedRun:
    """一个活跃 run 的完整状态（内存态，进程重启即失，可接受）。

    fingerprint 是对话指纹（见 conversation_fingerprint），
    用于判定新请求是否属于同一对话。
    """

    run_id: str
    agent_id: str
    started_monotonic: float
    fingerprint: str
    last_used_monotonic: float = field(default_factory=time.monotonic)
    # llm_step_number 与 steps[].stepNumber 共用一个单调计数器，
    # 保证线上已发的编号与 FINISH 里报的对得上（freebuff-proxy #113/#114 同款约束）
    step_counter: int = 0
    total_steps: int = 0
    steps: list[RunStep] = field(default_factory=list)
    status: str = ""  # ""=进行中; "failed"/"cancelled" 由终态事件写入
    error_message: str | None = None

    def next_step_number(self) -> int:
        """领取下一个 step 编号（1-based）。每次 chat 尝试调用一次。"""
        self.step_counter += 1
        return self.step_counter

    def record_step(self, message_id: str | None) -> None:
        """chat 成功后记录一条 completed step（本地累积，随 FINISH 一次性提交）。"""
        self.total_steps += 1
        self.steps.append(
            RunStep(
                step_number=self.total_steps,
                message_id=message_id or None,
                start_time=_rfc3339_now(),
            )
        )
        if len(self.steps) > MAX_RECORDED_STEPS:
            # 只丢最旧的展示记录；totalSteps 保持单调诚实
            del self.steps[: len(self.steps) - MAX_RECORDED_STEPS]

    def mark_failed(self, error_message: str | None) -> None:
        """终态标记：chat 死于上游错误。只改状态，run 仍等正常路径 FINISH。"""
        self.status = "failed"
        if error_message and not self.error_message:
            # 截断防止 errorMessage 携带超长堆栈撑爆 body（官方也带堆栈但有界）
            self.error_message = error_message[:500]

    def mark_cancelled(self) -> None:
        """终态标记：客户端断连。failed 优先级更高，不覆盖。"""
        if not self.status:
            self.status = "cancelled"

    def finish_snapshot(self) -> tuple[str, int, list[dict[str, Any]], str | None]:
        """组装 FINISH 参数：(status, totalSteps, steps, errorMessage)。"""
        status = self.status or "completed"
        steps = [
            {
                "id": str(uuid.uuid4()),  # 官方语义：step.id 才是客户端 UUID
                "stepNumber": s.step_number,
                "credits": 0,
                "childRunIds": [],
                "messageId": s.message_id,  # 上游 SSE chunk.id；None 合法
                "status": "completed",
                "startTime": s.start_time,
            }
            for s in self.steps
        ]
        return status, self.total_steps, steps, self.error_message


def _rfc3339_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def conversation_fingerprint(messages: list[dict[str, Any]] | None) -> str:
    """从 messages 推断对话身份。

    方案：取首条 user 消息内容的前 N 字符做哈希前缀 —— 多轮对话里首条
    user 消息不变（Claude Code / Cline 等客户端每轮重发全量 history），
    而不同对话几乎不可能共享同一段开头。比逐条比较便宜且稳定。

    system 不参与：所有请求共享同一个 Buffy prompt，无区分度。
    """
    if not messages:
        return ""
    for msg in messages:
        role = (msg.get("role") or "").lower()
        if role != "user":
            continue
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Anthropic/OpenAI 分块格式：拼全部 text 块
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        if text.strip():
            digest = uuid.uuid5(uuid.NAMESPACE_URL, text[:2048])
            return f"{digest.int >> 64:016x}"
    return ""


class RunManager:
    """按 (token, agent_id) 管理当前 run；按对话指纹决定复用或切换。

    并发模型：单 asyncio.Lock 保护内存簿记；FINISH 走后台任务，
    绝不阻塞聊天响应主链路（freebuff-proxy bounded queue 的简化版 ——
    我们单机低并发，dict+task 即可，不需要完整 worker 队列）。
    """

    def __init__(
        self,
        *,
        idle_ttl_seconds: float = 1800.0,
        enabled: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        # key = f"{token_hash}:{agent_id}" → 当前 run（含对话指纹）
        self._runs: dict[str, ManagedRun] = {}
        self._token_hashes: dict[str, str] = {}  # token 原文 → 短哈希（日志脱敏）
        self._finishing: set[str] = set()  # 在途 FINISH 的 run_id，防重复提交
        self.idle_ttl_seconds = idle_ttl_seconds
        self.enabled = enabled

    # ---------- key 工具 ----------

    @staticmethod
    def token_key(token: str) -> str:
        """token → 稳定短哈希。日志与缓存键都不落 token 明文。"""
        if not token:
            return "anon"
        return f"{uuid.uuid5(uuid.NAMESPACE_URL, token).int >> 96:08x}"

    def _cache_key(self, token_hash: str, agent_id: str) -> str:
        return f"{token_hash}:{agent_id}"

    # ---------- 对外主入口 ----------

    async def acquire(
        self,
        client: CodebuffClient,
        *,
        token: str,
        agent_id: str,
        messages: list[dict[str, Any]] | None,
        now: float | None = None,
    ) -> tuple[ManagedRun, bool]:
        """取当前可用 run：同对话且未过期 → 复用；否则 FINISH 旧 + START 新。

        返回 (run, reused)。START 失败抛 CodebuffError 由上层走既有错误链路。
        """
        token_hash = self.token_key(token)
        cache_key = self._cache_key(token_hash, agent_id)
        fingerprint = conversation_fingerprint(messages)
        now = now if now is not None else time.monotonic()

        async with self._lock:
            current = self._runs.get(cache_key)
            if (
                current is not None
                and current.fingerprint == fingerprint
                and fingerprint  # 空指纹（解析不出 user 消息）一律新建，宁滥勿串
                and (now - current.last_used_monotonic) < self.idle_ttl_seconds
            ):
                current.last_used_monotonic = now
                return current, True

            # 需要新 run：先把旧 run 从活跃表摘下（锁内），FINISH 放锁外
            old = current

        if old is not None:
            await self._finish_async(client, token_hash, old)

        run_id = await client.start_run(agent_id)
        created = ManagedRun(
            run_id=run_id,
            agent_id=agent_id,
            started_monotonic=now,
            fingerprint=fingerprint,
            last_used_monotonic=now,
        )
        async with self._lock:
            # 并发窗口内可能有别的协程先放了新 run：保留较新者，旧的转后台 FINISH
            loser = self._runs.get(cache_key)
            self._runs[cache_key] = created
        if loser is not None and loser is not created:
            await self._finish_async(client, token_hash, loser)
        logger.info(
            "run %s agent=%s fp=%s…%s",
            "reused" if False else "started",
            agent_id,
            fingerprint[:8],
            fingerprint[-4:] if len(fingerprint) > 12 else "",
        )
        return created, False

    # ---------- step 记录 ----------

    def note_attempt(self, run: ManagedRun) -> int:
        """每次 chat 尝试领取 step 编号（写 llm_step_number 前调用）。"""
        return run.next_step_number()

    def note_success(self, run: ManagedRun, message_id: str | None) -> None:
        """chat 成功：记一条 completed step。同步方法，无 IO。"""
        run.record_step(message_id)

    def note_failure(self, run: ManagedRun, error_message: str | None) -> None:
        """chat 终态失败：如实标 failed。"""
        run.mark_failed(error_message)

    def note_cancelled(self, run: ManagedRun) -> None:
        """客户端断连：如实标 cancelled。"""
        run.mark_cancelled()

    # ---------- FINISH ----------

    async def release(
        self,
        client: CodebuffClient,
        *,
        token: str,
        run: ManagedRun,
    ) -> None:
        """显式交还并 FINISH 一个 run（当前仅 shutdown 兜底使用；
        常规轮换由 acquire 内部的 old→finish 完成）。"""
        token_hash = self.token_key(token)
        async with self._lock:
            cache_key = self._cache_key(token_hash, run.agent_id)
            if self._runs.get(cache_key) is run:
                del self._runs[cache_key]
        await self._finish_async(client, token_hash, run)

    async def _finish_async(self, client: CodebuffClient, token_hash: str, run: ManagedRun) -> None:
        """后台 FINISH：去重后丢给任务队列，失败只记日志（FINISH 是尽力而为，
        重试价值低——上游 run 自有超时回收）。"""
        if run.run_id in self._finishing:
            return
        self._finishing.add(run.run_id)
        task = asyncio.create_task(self._finish_call(client, run))
        task.add_done_callback(lambda _: self._finishing.discard(run.run_id))

    async def _finish_call(self, client: CodebuffClient, run: ManagedRun) -> None:
        status, total_steps, steps, error_message = run.finish_snapshot()
        try:
            await client.finish_run(
                run.run_id,
                total_steps=total_steps,
                steps=steps,
                status=status,
                error_message=error_message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "background finish failed run_id=%s status=%s: %s",
                run.run_id,
                status,
                error,
            )

    # ---------- 清扫 ----------

    async def sweep_idle(self, clients_by_token: dict[str, CodebuffClient]) -> int:
        """FINISH 所有空闲超过 idle_ttl 的 run（后台周期任务调用）。

        clients_by_token: token_hash → 该账号的 client（用于跨账号 FINISH）。
        返回清扫数量。
        """
        now = time.monotonic()
        expired: list[tuple[str, ManagedRun]] = []
        async with self._lock:
            for key, run in list(self._runs.items()):
                if now - run.last_used_monotonic >= self.idle_ttl_seconds:
                    token_hash = key.split(":", 1)[0]
                    expired.append((token_hash, run))
                    del self._runs[key]
        for token_hash, run in expired:
            client = clients_by_token.get(token_hash)
            if client is None:
                logger.warning(
                    "idle sweep skipped run_id=%s: no client for token %s",
                    run.run_id,
                    token_hash,
                )
                continue
            await self._finish_async(client, token_hash, run)
        if expired:
            logger.info("idle sweep finished count=%s", len(expired))
        return len(expired)

    async def finish_all(self, clients_by_token: dict[str, CodebuffClient]) -> None:
        """进程退出兜底：FINISH 全部活跃 run（尽力而为，限时由调用方控制）。"""
        async with self._lock:
            all_runs = [
                (key.split(":", 1)[0], run) for key, run in self._runs.items()
            ]
            self._runs.clear()
        for token_hash, run in all_runs:
            client = clients_by_token.get(token_hash)
            if client is None:
                continue
            with contextlib.suppress(Exception):
                await self._finish_call(client, run)

    # ---------- 观测 ----------

    def snapshot(self) -> dict[str, Any]:
        """admin 面板观测用：活跃 run 数与摘要（不含敏感信息）。"""
        runs = [
            {
                "run_id": run.run_id,
                "agent_id": run.agent_id,
                "fingerprint": run.fingerprint[:8],
                "steps": run.total_steps,
                "status": run.status or "active",
                "age_s": int(time.monotonic() - run.started_monotonic),
            }
            for run in self._runs.values()
        ]
        return {"enabled": self.enabled, "active": len(runs), "runs": runs}

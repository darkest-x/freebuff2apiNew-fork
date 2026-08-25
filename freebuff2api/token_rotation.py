"""Token 账号轮换与健康状态管理 — 适配 .env 逗号分隔的多账号。

每个逗号分隔的 FREEBUFF_TOKEN 视作一个账号（= 管理页一张卡片）。
- 429 限流：按 (账号, 模型) 冷却到 retry_after，选号跳过冷却/失效账号，全冷却选最早解封
- 轮换指针 CURRENT_TOKENNum 写入 .env，重启续轮
- 非 429 手动轮换带 30s 防抖
- 成功调用重置失败计数；冷却到期由账号池触发半开探测
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("freebuff2api.token_rotation")

SHA_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai

COOLDOWN_SECONDS = 30

# 账号状态
STATUS_ACTIVE = "active"      # 可用
STATUS_BLOCKED = "blocked"    # 429 限流冷却中
STATUS_INVALID = "invalid"    # 启动验证失败被剔除
STATUS_CHECKING = "checking"  # 验证中

MAX_FAILURES = 3  # 连续瞬时故障达到该次数标记失效

# 账号级冷却的模型键（任意模型 429 都会整账号冷却时使用）
GLOBAL_MODEL_KEY = ""


def is_rate_limit_error(error_message: str) -> bool:
    """Check if an error message string indicates a normal 429 rate limit."""
    return "429" in error_message and "rate_limited" in error_message


def is_ban_error(error_message: str) -> bool:
    """兼容旧调用：账号封禁或地区限制都算广义 ban。"""
    return is_account_banned(error_message) or is_country_blocked(error_message)


def is_account_banned(error_message: str) -> bool:
    """官方账号级封禁（status=banned）。这是真正的账号被停用。"""
    return "banned" in error_message.lower()


def parse_ban_resumes_at(error_message: str) -> float:
    """从 ban 错误里解析上游给出的解封时间戳 ``resumes_at``（epoch 秒）。

    [freebuff-proxy #198/#199 源码级实证] 官方临时封禁 body 形如
    ``403 {"status":"banned","resumes_at":"..."}``，resumes_at 支持三种形态：
    RFC3339 字符串 / unix 秒 / unix 毫秒。返回 0.0 表示没有该字段（硬封禁，
    user.banned 布尔位，无解封时间 —— 只能走申诉）。
    """
    m = re.search(r"403\s+(\{.*\})", error_message)
    if not m:
        return 0.0
    try:
        payload = json.loads(m.group(1))
    except ValueError:
        return 0.0
    resumes_at = payload.get("resumes_at")
    if resumes_at in (None, ""):
        return 0.0
    # 数字形态：unix 秒或毫秒
    try:
        value = float(resumes_at)
        if value > 1e12:  # 毫秒
            value /= 1000.0
        return value if value > time.time() else 0.0
    except (TypeError, ValueError):
        pass
    # 字符串形态：ISO8601 / RFC3339
    try:
        parsed = datetime.fromisoformat(str(resumes_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        epoch = parsed.timestamp()
        return epoch if epoch > time.time() else 0.0
    except ValueError:
        return 0.0


def is_country_blocked(error_message: str) -> bool:
    """官方地区限制（status=country_blocked）。账号本身没问题，换 IP 可恢复。"""
    return "country_blocked" in error_message.lower()


def is_insufficient_quota(error_message: str) -> bool:
    """官方上游负载饱和（insufficient_quota）。不是账号额度耗尽。"""
    return "insufficient_quota" in error_message.lower()


def is_capacity_deferred(error_message: str) -> bool:
    """官方免费通道容量已满（free_mode_capacity_deferred）。"""
    return "free_mode_capacity_deferred" in error_message.lower()


def is_spend_limited(error_message: str) -> bool:
    """官方高峰时段临时限流（spend_limited）。所有账号都受影响，不应轮换。"""
    return "spend_limited" in error_message.lower()


def is_ip_capped(error_message: str) -> bool:
    """官方出口 IP 人数上限（ip_capped）。换账号无效，换 IP 才有效。"""
    return "ip_capped" in error_message.lower()


def is_model_unavailable(error_message: str) -> bool:
    """官方模型暂时不可用（model_unavailable）。应切换模型，不应轮换账号。"""
    return "model_unavailable" in error_message.lower()


def is_policy_violation_error(error_message: str) -> bool:
    """OpenAI/Azure 上游策略违规：只封当前模型，不封账号。"""
    return "policy violation" in error_message.lower()


def is_provider_usage_error(error_message: str) -> bool:
    """Freebuff 官方上游 credit 耗尽（不是账号问题，是官方的问题）。

    官方 orchestrator.js 定义：
    FREEBUFF_PROVIDER_USAGE_ERROR_PATTERN = /\b(?:(?:not enough|insufficient|out of)\\s+credits?
        |(?:add|refill|top up)\\s+(?:more\\s+)?credits?)\\b/i
    FREEBUFF_PROVIDER_USAGE_MESSAGE = "Freebuff ran out of provider usage and
        needs a refill. This is on us, not your account."
    """
    lower = error_message.lower()
    return "provider usage" in lower or "refill" in lower or "out of credits" in lower


def next_beijing_1500_epoch(now: float | None = None) -> float:
    """Return the next 15:00 Asia/Shanghai time as epoch seconds.

    账号被 ban 后，限制模型直到下一个北京时间 15 点才解放。
    """
    now_dt = datetime.now(SHA_TZ) if now is None else datetime.fromtimestamp(now, SHA_TZ)
    target = now_dt.replace(hour=15, minute=0, second=0, microsecond=0)
    if target <= now_dt:
        target += timedelta(days=1)
    return target.timestamp()


def parse_429_info(error_message: str) -> dict:
    """Extract rate-limit info from a 429 error message. Returns dict with:
    - reset_at_utc / reset_at_sha
    - retry_after_ms / retry_after_str
    - model / limit
    """
    info = {
        "reset_at_utc": "",
        "reset_at_sha": "",
        "retry_after_ms": 0,
        "retry_after_str": "",
        "model": "",
        "limit": 0,
    }
    try:
        m = re.search(r'429\s+(\{.*"rate_limited".*?\})\s*$', error_message)
        if not m:
            m = re.search(r"429\s+(\{.*\})", error_message)
        if not m:
            return info
        payload = json.loads(m.group(1))
        info["model"] = payload.get("model", "")
        info["limit"] = payload.get("limit", 0)

        reset_at_str = payload.get("resetAt", "")
        if reset_at_str:
            dt_utc = datetime.fromisoformat(reset_at_str.replace("Z", "+00:00"))
            dt_sha = dt_utc.astimezone(SHA_TZ)
            info["reset_at_utc"] = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
            info["reset_at_sha"] = dt_sha.strftime("%Y-%m-%d %H:%M")

        ms = payload.get("retryAfterMs", 0)
        info["retry_after_ms"] = ms
        if ms > 0:
            total_min = ms // 60000
            hours = total_min // 60
            mins = total_min % 60
            if hours > 0:
                info["retry_after_str"] = f"{hours}小时{mins}分钟"
            else:
                info["retry_after_str"] = f"{mins}分钟"
    except Exception:
        pass
    return info


def read_current_token_num(env_path: Path) -> int:
    """Read CURRENT_TOKENNum from .env (0-based index). Default 0."""
    if not env_path.exists():
        return 0
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CURRENT_TOKENNum="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    return 0


def write_current_token_num(env_path: Path, index: int) -> None:
    """Write CURRENT_TOKENNum to .env, preserving other lines."""
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        found = False
        for line in lines:
            if line.startswith("CURRENT_TOKENNum="):
                out.append(f"CURRENT_TOKENNum={index}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"CURRENT_TOKENNum={index}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        env_path.write_text(f"CURRENT_TOKENNum={index}\n", encoding="utf-8")


class RotationState:
    """Per-account health + rotation pointer. One index per FREEBUFF_TOKEN account.

    冷却按 (账号, 模型) 记录：_blocked_until 键为 (index, model)。
    - model=="" 表示账号级冷却（429 未携带 model 时退化为整账号冷却）
    - 查询某模型的可用性时，账号级冷却对该模型同样生效
    """

    def __init__(self, account_count: int, env_path: Path) -> None:
        self._count = account_count
        self._env_path = env_path
        self._blocked_until: dict[tuple[int, str], float] = {}
        self._statuses: dict[int, str] = {}
        self._failure_count: dict[int, int] = {}
        self._last_429_info: dict = {}
        self._last_429_time: str = ""
        self._last_429_account: int | None = None
        self._total_rotations: int = 0
        self._last_rotation: float = 0.0
        idx = read_current_token_num(env_path)
        self._current_index = idx % account_count if account_count > 0 else 0
        logger.info(
            "rotation state initialized accounts=%s current_index=%s",
            account_count,
            self._current_index,
        )

    # ── Basic properties ─────────────────────────

    @property
    def account_count(self) -> int:
        return self._count

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def total_rotations(self) -> int:
        return self._total_rotations

    @property
    def last_429_info(self) -> dict:
        return dict(self._last_429_info)

    @property
    def last_429_time(self) -> str:
        return self._last_429_time

    @property
    def last_429_account(self) -> int | None:
        return self._last_429_account

    def status_of(self, index: int) -> str:
        return self._statuses.get(index, STATUS_ACTIVE)

    def failure_count_of(self, index: int) -> int:
        return self._failure_count.get(index, 0)

    # ── Blocked (per account-model) ──────────────

    def is_blocked(self, index: int, model: str | None = None) -> bool:
        """True if (index, model) is cooling. model=None → account-level view:
        any model cooling counts as blocked (used by admin/tests)."""
        now = time.time()
        if model is None:
            return any(
                t > now for (i, _m), t in self._blocked_until.items() if i == index
            )
        if self._blocked_until.get((index, GLOBAL_MODEL_KEY), 0) > now:
            return True
        return self._blocked_until.get((index, model), 0) > now

    def block_remaining(self, index: int, model: str | None = None) -> float:
        now = time.time()
        if model is None:
            until = max(
                (
                    t
                    for (i, _m), t in self._blocked_until.items()
                    if i == index and t > now
                ),
                default=0.0,
            )
        else:
            until = max(
                self._blocked_until.get((index, GLOBAL_MODEL_KEY), 0),
                self._blocked_until.get((index, model), 0),
            )
        return max(0.0, until - now)

    def blocked_accounts(self, model: str | None = None) -> dict[int, float]:
        """Accounts currently cooling (for `model`, or any model when None),
        with unblock epoch."""
        now = time.time()
        out: dict[int, float] = {}
        for index in range(self._count):
            if model is None:
                until = max(
                    (
                        t
                        for (i, _m), t in self._blocked_until.items()
                        if i == index and t > now
                    ),
                    default=0.0,
                )
            else:
                until = max(
                    self._blocked_until.get((index, GLOBAL_MODEL_KEY), 0),
                    self._blocked_until.get((index, model), 0),
                )
            if until > now:
                out[index] = until
        return out

    @property
    def all_blocked(self) -> bool:
        """Account-level view: True when no account is usable (any model)."""
        if self._count == 0:
            return False
        return all(
            self.is_blocked(i) or self.status_of(i) == STATUS_INVALID
            for i in range(self._count)
        )

    @property
    def available_count(self) -> int:
        """Account-level view: usable accounts (any model)."""
        return sum(
            1
            for i in range(self._count)
            if not self.is_blocked(i) and self.status_of(i) != STATUS_INVALID
        )

    def all_blocked_for(self, model: str) -> bool:
        if self._count == 0:
            return False
        return all(
            self.is_blocked(i, model) or self.status_of(i) == STATUS_INVALID
            for i in range(self._count)
        )

    def available_count_for(self, model: str) -> int:
        return sum(
            1
            for i in range(self._count)
            if not self.is_blocked(i, model)
            and self.status_of(i) != STATUS_INVALID
        )

    def model_status(self, index: int, model: str) -> str:
        """Per-(account, model) availability for the overview matrix."""
        if self.status_of(index) == STATUS_INVALID:
            return STATUS_INVALID
        if self.is_blocked(index, model):
            return STATUS_BLOCKED
        return STATUS_ACTIVE

    def model_availability(self, models: list[str]) -> list[dict]:
        """Overview matrix: [{model, accounts: [{index, status, block_remaining, is_current}]}]"""
        rows: list[dict] = []
        for model in models:
            accounts = [
                {
                    "index": index + 1,
                    "status": self.model_status(index, model),
                    "block_remaining": round(self.block_remaining(index, model), 1),
                    "is_current": index == self._current_index,
                }
                for index in range(self._count)
            ]
            rows.append({"model": model, "accounts": accounts})
        return rows

    # ── Health mutations ─────────────────────────

    def block(self, index: int, retry_after_ms: int = 0, model: str = "") -> None:
        """Cooldown (index, model); model="" cools the whole account."""
        if index < 0 or index >= self._count:
            return
        key = (index, model if model else GLOBAL_MODEL_KEY)
        self._blocked_until[key] = time.time() + (retry_after_ms / 1000)
        if self._statuses.get(index) != STATUS_INVALID:
            self._statuses[index] = STATUS_BLOCKED

    def unblock(self, index: int, model: str | None = None) -> None:
        """Remove cooldown for (index, model); model=None clears all models."""
        if model is None:
            self._blocked_until = {
                k: t for k, t in self._blocked_until.items() if k[0] != index
            }
        else:
            key = (index, model if model else GLOBAL_MODEL_KEY)
            self._blocked_until.pop(key, None)
        # Recompute account status: active unless another model is still cooling
        if self._statuses.get(index) != STATUS_INVALID:
            if not self._any_cooling(index):
                self._statuses[index] = STATUS_ACTIVE

    def _any_cooling(self, index: int) -> bool:
        now = time.time()
        return any(
            t > now for (i, _m), t in self._blocked_until.items() if i == index
        )

    def mark_checking(self, index: int) -> None:
        if 0 <= index < self._count:
            self._statuses[index] = STATUS_CHECKING

    def mark_active(self, index: int) -> None:
        if 0 <= index < self._count:
            self._blocked_until = {
                k: t for k, t in self._blocked_until.items() if k[0] != index
            }
            self._failure_count.pop(index, None)
            self._statuses[index] = STATUS_ACTIVE

    def mark_invalid(self, index: int) -> None:
        if 0 <= index < self._count:
            self._blocked_until = {
                k: t for k, t in self._blocked_until.items() if k[0] != index
            }
            self._statuses[index] = STATUS_INVALID

    def record_failure(self, index: int) -> None:
        if index < 0 or index >= self._count:
            return
        if self._statuses.get(index) == STATUS_INVALID:
            return
        count = self._failure_count.get(index, 0) + 1
        self._failure_count[index] = count
        if count >= MAX_FAILURES:
            logger.warning(
                "account %s failed %s times, marking invalid",
                index + 1,
                count,
            )
            self.mark_invalid(index)

    def reset_failures(self, index: int) -> None:
        self._failure_count.pop(index, None)

    def mark_success(self, index: int) -> None:
        """A request succeeded on this account: reset failure counter."""
        self.reset_failures(index)

    # ── Selection & rotation ─────────────────────

    def next_index(self, start: int | None = None, model: str = "") -> int | None:
        """Next usable account index (ring scan from start), skipping blocked/invalid."""
        if self._count == 0:
            return None
        start = start if start is not None else self._current_index
        for offset in range(self._count):
            idx = (start + offset) % self._count
            if not self.is_blocked(idx, model) and self.status_of(idx) != STATUS_INVALID:
                return idx
        return None

    def rotate(
        self,
        reason: str = "",
        error_message: str = "",
        *,
        is_429: bool = False,
        failed_index: int | None = None,
        model: str = "",
    ) -> tuple[int, str]:
        """Advance to the next usable account. 429 always switches immediately and
        cools down the failing account (per model); non-429 rotations are debounced."""
        if self._count == 0:
            return 0, STATUS_ACTIVE

        now = time.monotonic()
        if (
            not is_429
            and now - self._last_rotation < COOLDOWN_SECONDS
            and self._total_rotations > 0
        ):
            logger.warning(
                "rotation cooldown (%.1fs < %ds), skipping",
                now - self._last_rotation,
                COOLDOWN_SECONDS,
            )
            return self._current_index, self.status_of(self._current_index)

        old_index = self._current_index

        if is_429:
            failed = failed_index if failed_index is not None else old_index
            self._last_429_info = parse_429_info(error_message)
            self._last_429_time = datetime.now(SHA_TZ).strftime("%Y-%m-%d %H:%M")
            self._last_429_account = failed
            retry_ms = self._last_429_info.get("retry_after_ms", 0)
            if retry_ms <= 0:
                # 429 没有携带 retryAfterMs 时（例如 insufficient_quota 二次判定），
                # 给一个较短的冷却，避免同一请求周期内立刻复用刚失败的账号。
                retry_ms = 60_000
            # Prefer the model carried in the 429 payload; fall back to the
            # model that was being requested (passed by the pool).
            cooldown_model = self._last_429_info.get("model") or model
            self.block(failed, retry_ms, cooldown_model)
            logger.warning(
                "account %s blocked model=%s ~%s (retry_after=%s)",
                failed + 1,
                cooldown_model or "(all)",
                self._last_429_info.get("retry_after_str", "?"),
                self._last_429_info.get("reset_at_sha", "?"),
            )

        if self._count <= 1:
            return self._current_index, self.status_of(self._current_index)

        # Advance past the current account to the next usable one
        found: int | None = None
        skip = 0
        for offset in range(1, self._count + 1):
            idx = (old_index + offset) % self._count
            if not self.is_blocked(idx, model) and self.status_of(idx) != STATUS_INVALID:
                found = idx
                break
            skip += 1
        if found is None:
            # All blocked/invalid → pick the blocked account that unblocks earliest
            blocked_indices = [i for i in range(self._count) if self.is_blocked(i, model)]
            if blocked_indices:
                found = min(
                    blocked_indices,
                    key=lambda i: self.block_remaining(i, model),
                )
                suffix = " (all blocked, picked earliest unblock)"
            else:
                found = old_index  # everything invalid; keep pointer
                suffix = " (all invalid)"
        else:
            suffix = f" (skipped {skip} blocked/invalid)" if skip > 0 else ""

        self._current_index = found
        self._last_rotation = now
        self._total_rotations += 1
        self._persist()
        logger.warning(
            "token rotation #%d: account %s → %s (reason: %s)%s",
            self._total_rotations,
            old_index + 1,
            found + 1,
            reason,
            suffix,
        )
        return self._current_index, self.status_of(self._current_index)

    def set_active(self, index: int) -> None:
        if index < 0 or index >= self._count:
            raise IndexError(f"account index out of range: {index}")
        self._current_index = index
        self._persist()
        logger.info("set active account=%s", index + 1)

    def _persist(self) -> None:
        write_current_token_num(self._env_path, self._current_index)

    # ── Admin rows ───────────────────────────────

    def status_rows(self) -> list[dict]:
        now = time.time()
        rows = []
        for index in range(self._count):
            remaining = self.block_remaining(index)
            rows.append(
                {
                    "index": index + 1,
                    "status": self.status_of(index),
                    "blocked": self.is_blocked(index),
                    "block_remaining": round(remaining, 1),
                    "failure_count": self.failure_count_of(index),
                    "is_current": index == self._current_index,
                    "last_429": (
                        self._last_429_info
                        if self._last_429_account == index
                        else {}
                    ),
                }
            )
        return rows

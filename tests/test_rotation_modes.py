"""Tests for FREEBUFF_ROTATION_MODE account selection."""
from __future__ import annotations

import asyncio
import unittest

from freebuff2api.codebuff import CodebuffAccountPool, CodebuffError
from freebuff2api.config import Settings


def _settings(accounts: str = "token-a,token-b", *, mode: str, concurrency: int = 2) -> Settings:
    return Settings(
        codebuff_token=accounts,
        local_api_key=None,
        rotation_mode=mode,
        max_concurrency_per_account=concurrency,
    )


class RotationModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_balanced_unlimited_fans_out_across_accounts(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))

        # mimo 恒为 unlimited 池（flash 2026-08-18 入 premium → 2026-08-26 又回退 unlimited）。
        first = pool._next_available_index("mimo/mimo-v2.5")
        await pool._reserve_account("mimo/mimo-v2.5")
        second = pool._next_available_index("mimo/mimo-v2.5")

        self.assertEqual(first, 0)
        self.assertEqual(second, 1)
        await pool.release(0, "mimo/mimo-v2.5")
        await pool.aclose()

    async def test_balanced_premium_uses_only_one_account_at_a_time(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        # 🔴 2026-08-27：pro 已掉出 premium 池（bucket 仅 [luna, glm-5.2, glm-5.3-flash]），
        # premium 单账号语义用 luna 作为代表模型。
        first = await pool._reserve_account("openai/gpt-5.6-luna")
        # 第一条 premium 通道被占用后，第二条不可选（即使还有第二个账号）
        self.assertIsNone(pool._next_available_index("openai/gpt-5.6-luna"))

        await pool.release(first, "openai/gpt-5.6-luna")
        # 释放后仍优先使用原来的 premium 账号（串行轮换，不是并发）
        self.assertEqual(pool._next_available_index("openai/gpt-5.6-luna"), first)
        await pool.aclose()

    async def test_balanced_premium_rotates_on_normal_429(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 0)
        pool.handle_error(
            0,
            'Codebuff request failed: 429 {"status":"rate_limited","retryAfterMs":21600000}',
            status_code=429,
            model="deepseek/deepseek-v4-pro",
        )
        # 正常额度用完 → 切到下一个账号
        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 1)
        await pool.aclose()

    async def test_balanced_premium_ban_disables_premium(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))

        pool.handle_error(
            0,
            "Codebuff request failed: account banned - Freebuff account banned",
            status_code=403,
            model="openai/gpt-5.6-luna",
        )
        self.assertIsNone(pool._next_available_index("openai/gpt-5.6-luna"))

        with self.assertRaises(CodebuffError) as ctx:
            await pool.acquire_session("openai/gpt-5.6-luna")
        self.assertEqual(ctx.exception.status_code, 403)
        await pool.aclose()

    async def test_banned_marks_invalid_and_does_not_rotate(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))

        pool.handle_error(
            0,
            "Codebuff request failed: account banned - Freebuff account banned",
            status_code=403,
            model="openai/gpt-5.6-luna",
        )
        self.assertEqual(pool._invalid_reasons[0], "banned")
        self.assertGreater(pool._premium_banned_until, 0)
        self.assertIsNone(pool._next_available_index("openai/gpt-5.6-luna"))

        with self.assertRaises(CodebuffError):
            await pool.acquire_session("openai/gpt-5.6-luna")
        await pool.aclose()

    async def test_acquire_session_rate_limited_tries_next_account(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        async def fake_acquire(model, messages=None):
            raise CodebuffError(
                'Codebuff request failed: 429 {"status":"rate_limited","retryAfterMs":21600000}',
                429,
            )

        for account in pool._accounts:
            account.sessions.acquire_session = fake_acquire  # type: ignore[method-assign]

        with self.assertRaises(CodebuffError):
            await pool.acquire_session("deepseek/deepseek-v4-pro")

        # 第一个账号 429 后应被冷却，并轮换到第二个账号（第二个也 429 后同样被冷却）
        self.assertTrue(pool._rotation.is_blocked(0, "deepseek/deepseek-v4-pro"))
        self.assertTrue(pool._rotation.is_blocked(1, "deepseek/deepseek-v4-pro"))
        await pool.aclose()

    async def test_acquire_session_insufficient_quota_does_not_try_next_account(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        async def fake_acquire(model, messages=None):
            raise CodebuffError("Codebuff chat failed: 429 insufficient_quota", 429)

        for account in pool._accounts:
            account.sessions.acquire_session = fake_acquire  # type: ignore[method-assign]

        with self.assertRaises(CodebuffError):
            await pool.acquire_session("deepseek/deepseek-v4-pro")

        # 第一次上游拥堵：不轮换、不冷却账号，客户端看到拥堵提示即可
        self.assertEqual(pool._rotation.status_of(0), "active")
        self.assertFalse(pool._rotation.is_blocked(0, "deepseek/deepseek-v4-pro"))
        await pool.aclose()

    async def test_country_blocked_keeps_account_active_and_does_not_rotate(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        pool.handle_error(
            0,
            "Freebuff country_blocked: {}",
            status_code=403,
            model="deepseek/deepseek-v4-pro",
        )
        self.assertEqual(pool._invalid_reasons.get(0, ""), "")
        self.assertEqual(pool._rotation.status_of(0), "active")
        # 不轮换：仍返回原账号（IP 问题换账号无用）
        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 0)
        await pool.aclose()

    async def test_insufficient_quota_first_strike_no_rotate_second_rotates(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        pool.handle_error(
            0,
            "Codebuff chat failed: 429 insufficient_quota",
            status_code=429,
            model="deepseek/deepseek-v4-pro",
        )
        # 第一次：只提示上游拥堵，不轮换
        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 0)

        pool.handle_error(
            0,
            "Codebuff chat failed: 429 insufficient_quota",
            status_code=429,
            model="deepseek/deepseek-v4-pro",
        )
        # 连续第二次：按额度耗尽处理，轮换到下一个账号
        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 1)
        await pool.aclose()

    async def test_spend_limited_does_not_rotate(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))
        pool._premium_index = 0

        pool.handle_error(
            0,
            "Freebuff session spend_limited: 429 {}",
            status_code=429,
            model="deepseek/deepseek-v4-pro",
        )
        # 高峰限流是所有账号共同面对的上游状态，不轮换
        self.assertEqual(pool._rotation.status_of(0), "active")
        self.assertEqual(pool._next_available_index("deepseek/deepseek-v4-pro"), 0)
        await pool.aclose()

    async def test_unknown_403_marks_invalid_with_forbidden_reason(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="balanced"))

        pool.handle_error(
            0,
            "Codebuff chat stream error: Turn execution failed reason=auth_failed",
            status_code=403,
            model="deepseek/deepseek-v4-pro",
        )
        self.assertEqual(pool._invalid_reasons[0], "forbidden")
        self.assertEqual(pool._rotation.status_of(0), "invalid")
        await pool.aclose()

    async def test_conservative_unlimited_uses_only_first_account(self) -> None:
        pool = CodebuffAccountPool(_settings(mode="conservative"))

        # mimo 恒为 unlimited 池（flash 2026-08-18 入 premium → 2026-08-26 又回退 unlimited）。
        first = pool._next_available_index("mimo/mimo-v2.5")
        self.assertEqual(first, 0)

        await pool._reserve_account("mimo/mimo-v2.5")
        # 免费模型通道被占用后，不允许使用第二个账号
        self.assertIsNone(pool._next_available_index("mimo/mimo-v2.5"))
        await pool.release(0, "mimo/mimo-v2.5")
        self.assertEqual(pool._next_available_index("mimo/mimo-v2.5"), 0)
        await pool.aclose()


if __name__ == "__main__":
    unittest.main()

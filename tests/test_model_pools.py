"""test_model_pools.py — 模型池归属与每模型配额判定的单元测试。

背景（2026-08-24 用户指正）：
1. DeepSeek V4 Flash 已于 2026-08-18 被官方移入 premium 池
   （上游 freebuff-models.ts DEEPSEEK_V4_FLASH_MODEL.premium = true），
   旧硬编码表把它当 unlimited 是过期认知；
2. 各 premium 模型配额不同（共享池 + per-model caps：V4 Pro=1、Luna=2），
   判定耗尽必须按目标模型的 limit/recentCount，不能整池一刀切；
3. 动态注册表需周期刷新（默认 2h），上游挪模型时自动跟随。
"""

import unittest
from unittest.mock import patch

from freebuff2api.model_registry import DynamicModelEntry, DynamicModelTable, ModelRegistry
from freebuff2api.models import (
    GLM_POOL_MODEL_IDS,
    UNLIMITED_SESSION_MODEL_IDS,
    is_model_quota_exhausted,
    is_premium_quota_exhausted,
    model_quota_state,
    session_bucket_for_model,
    set_model_registry,
)


def _registry_with(premium_ids: set[str]) -> ModelRegistry:
    """构造带动态表的注册表桩（不触发网络）。"""
    registry = ModelRegistry()
    registry._table = DynamicModelTable(
        models=[DynamicModelEntry(id=m, agent_id="a") for m in premium_ids],
        premium_ids=premium_ids,
        glm_ids={"z-ai/glm-5.2"},
    )
    return registry


def _quota(limit: float, recent: float, reset_in_seconds: int = 3600) -> dict:
    return {
        "limit": limit,
        "recentCount": recent,
        "resetAt": __import__("time").time() + reset_in_seconds,
    }


class SessionBucketTests(unittest.TestCase):
    """bucket 判定：动态表优先，硬编码兜底。"""

    def setUp(self) -> None:
        set_model_registry(None)  # 默认无动态表 → 走兜底

    def tearDown(self) -> None:
        set_model_registry(None)

    def test_fallback_flash_is_no_longer_unlimited(self) -> None:
        # 🔴 核心回归：flash 不在兜底 unlimited 集合里（2026-08-18 起 premium）
        self.assertNotIn("deepseek/deepseek-v4-flash", UNLIMITED_SESSION_MODEL_IDS)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-flash"), "premium")

    def test_fallback_mimo_is_unlimited(self) -> None:
        self.assertEqual(session_bucket_for_model("mimo/mimo-v2.5"), "unlimited")

    def test_dynamic_table_moves_flash_back_when_upstream_does(self) -> None:
        # 官方哪天把 flash 移回 unlimited（premium_ids 里没有 flash）→ 自动跟随
        registry = _registry_with({"openai/gpt-5.6-luna", "deepseek/deepseek-v4-pro"})
        set_model_registry(registry)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-flash"), "unlimited")

    def test_dynamic_table_keeps_flash_premium(self) -> None:
        # 当前官方状态：flash 在 premium_ids → premium bucket
        registry = _registry_with(
            {
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
                "openai/gpt-5.6-luna",
            }
        )
        set_model_registry(registry)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-flash"), "premium")
        # mimo 不在 premium 集合 → unlimited
        self.assertEqual(session_bucket_for_model("mimo/mimo-v2.5"), "unlimited")

    def test_empty_model_defaults_premium(self) -> None:
        self.assertEqual(session_bucket_for_model(""), "premium")

    def test_glm_pool_constant_documented(self) -> None:
        # GLM 独立额度池的记录常量（并发桶上仍属 premium，见 session_bucket_for_model docstring）
        self.assertIn("z-ai/glm-5.2", GLM_POOL_MODEL_IDS)


class PerModelQuotaTests(unittest.TestCase):
    """按目标模型精确判定配额（各 premium 模型 limit 不同）。"""

    def test_exhausted_when_recent_ge_limit_and_reset_future(self) -> None:
        limits = {"openai/gpt-5.6-luna": _quota(2, 2)}
        self.assertTrue(is_model_quota_exhausted(limits, "openai/gpt-5.6-luna"))

    def test_not_exhausted_when_remaining(self) -> None:
        limits = {"openai/gpt-5.6-luna": _quota(2, 1)}
        exhausted, remaining = model_quota_state(limits, "openai/gpt-5.6-luna")
        self.assertFalse(exhausted)
        self.assertEqual(remaining, 1)

    def test_not_exhausted_when_reset_window_rolled(self) -> None:
        # recentCount >= limit 但 resetAt 已过 → 窗口已滚动，不判耗尽
        import time as _time

        limits = {"openai/gpt-5.6-luna": {"limit": 2, "recentCount": 2, "resetAt": _time.time() - 60}}
        self.assertFalse(is_model_quota_exhausted(limits, "openai/gpt-5.6-luna"))

    def test_unknown_model_not_exhausted(self) -> None:
        # 无条目/limit<=0 → 配额未知，绝不误杀
        self.assertFalse(is_model_quota_exhausted({}, "deepseek/deepseek-v4-flash"))
        self.assertFalse(
            is_model_quota_exhausted({"m": {"limit": 0, "recentCount": 0}}, "m")
        )

    def test_rfc3339_reset_at_parsed(self) -> None:
        import time as _time

        future = _time.time() + 3600
        # unix 秒 / RFC3339 两种形态都能识别"在未来"
        limits_unix = {"m": {"limit": 1, "recentCount": 1, "resetAt": future}}
        limits_iso = {"m": {"limit": 1, "recentCount": 1, "resetAt": "2099-01-01T00:00:00Z"}}
        self.assertTrue(is_model_quota_exhausted(limits_unix, "m"))
        self.assertTrue(is_model_quota_exhausted(limits_iso, "m"))

    def test_luna_capped_while_pro_still_has_quota(self) -> None:
        # 官方 per-model caps 实景：Luna cap=2 已用完，Pro cap=1 还没用
        limits = {
            "openai/gpt-5.6-luna": _quota(2, 2),
            "deepseek/deepseek-v4-pro": _quota(1, 0),
            "deepseek/deepseek-v4-flash": _quota(4, 1),
        }
        self.assertTrue(is_model_quota_exhausted(limits, "openai/gpt-5.6-luna"))
        self.assertFalse(is_model_quota_exhausted(limits, "deepseek/deepseek-v4-pro"))
        self.assertFalse(is_model_quota_exhausted(limits, "deepseek/deepseek-v4-flash"))

    def test_legacy_full_pool_check_still_works(self) -> None:
        # 兼容路径：全池都干涸才算 True（SessionManager 全局闸门）
        limits = {
            "openai/gpt-5.6-luna": _quota(2, 2),
            "deepseek/deepseek-v4-pro": _quota(1, 1),
        }
        with patch("freebuff2api.models.session_bucket_for_model", return_value="premium"):
            self.assertTrue(is_premium_quota_exhausted(limits))
        partial = {"openai/gpt-5.6-luna": _quota(2, 2), "deepseek/deepseek-v4-pro": _quota(1, 0)}
        with patch("freebuff2api.models.session_bucket_for_model", return_value="premium"):
            self.assertFalse(is_premium_quota_exhausted(partial))


class RefreshIntervalTests(unittest.TestCase):
    """周期刷新：默认 2h 且可通过环境变量覆盖。"""

    def test_default_interval_is_two_hours(self) -> None:
        from freebuff2api.model_registry import REFRESH_INTERVAL_SECONDS

        self.assertGreaterEqual(REFRESH_INTERVAL_SECONDS, 2 * 60 * 60)
        self.assertLessEqual(REFRESH_INTERVAL_SECONDS, 2 * 60 * 60)

    def test_env_override(self) -> None:
        import importlib

        import freebuff2api.model_registry as mr

        with patch.dict("os.environ", {"FREEBUFF_MODEL_REFRESH_SECONDS": "600"}):
            reloaded = importlib.reload(mr)
            self.assertEqual(reloaded.REFRESH_INTERVAL_SECONDS, 600)
        importlib.reload(mr)  # 恢复默认


if __name__ == "__main__":
    unittest.main()

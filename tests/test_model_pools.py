"""test_model_pools.py — 模型池归属与每模型配额判定的单元测试。

背景（🟢 2026-09-01 桌面版 orchestrator.js 0.0.79 更新）：
1. premium 池**三次收缩**：`FREEBUFF_PREMIUM_MODEL_IDS = [luna, solar-pro4]`
   （08-27 是 [luna, glm-5.3-flash]，08-26 是 [luna, pro]）→ GLM 5.3 Flash
   掉出 premium，归 unlimited 通道；新增 upstage/solar-pro4（premium:true
   + 独立 daily 池 limit=1 + spend=0.5）；
2. 新模型 `upstage/solar-pro4`（500K ctx，experimental:true，不支持 effort）；
3. `LIMITED_FREEBUFF_MODEL_IDS = [mimo]`（0.0.79 仍如此）、
   `FREEBUFF_WEB_GEO_EXEMPT_MODEL_IDS = [mimo]`；
4. GLM 5.3 Flash 现在是 unlimited（premium:false），客户端请求它不会再触发
   GLM_POOL 预检；
5. flash / ox-alpha / mimo / glm-5.3-flash 仍非 premium；判定耗尽须按目标模型
   limit/recentCount，不能整池一刀切；
6. 动态注册表周期刷新（默认 2h），上游挪模型时自动跟随。
"""

import unittest
from unittest.mock import patch

from freebuff2api.model_registry import DynamicModelEntry, DynamicModelTable, ModelRegistry
from freebuff2api.models import (
    GOD_ONLY_MODEL_IDS,
    GLM_POOL_MODEL_IDS,
    UNLIMITED_SESSION_MODEL_IDS,
    all_models,
    is_god_only_model,
    is_model_quota_exhausted,
    is_premium_quota_exhausted,
    model_quota_state,
    resolve_model,
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

    def test_fallback_flash_back_to_unlimited(self) -> None:
        # 🔴 核心回归：flash 已回到兜底 unlimited 集合（2026-08-26 premium 撤销）
        self.assertIn("deepseek/deepseek-v4-flash", UNLIMITED_SESSION_MODEL_IDS)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-flash"), "unlimited")

    def test_fallback_mimo_is_unlimited(self) -> None:
        self.assertEqual(session_bucket_for_model("mimo/mimo-v2.5"), "unlimited")

    def test_fallback_ox_alpha_is_unlimited(self) -> None:
        # 2026-08-26 新模型：premium:false → unlimited 兜底
        self.assertEqual(session_bucket_for_model("stealth/ox-alpha"), "unlimited")

    def test_fallback_pro_and_m3_moved_to_unlimited(self) -> None:
        # 🔴 2026-08-27：pro 与 minimax-m3 掉出官方 premium 池（bucket 仅
        # [luna, glm-5.2, glm-5.3-flash]）→ 兜底集合同步跟进
        self.assertIn("deepseek/deepseek-v4-pro", UNLIMITED_SESSION_MODEL_IDS)
        self.assertIn("minimax/minimax-m3", UNLIMITED_SESSION_MODEL_IDS)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-pro"), "unlimited")
        self.assertEqual(session_bucket_for_model("minimax/minimax-m3"), "unlimited")

    def test_fallback_glm_53_flash_is_unlimited(self) -> None:
        # 🟢 2026-09-01 0.0.79：GLM 5.3 Flash 掉出 premium 池，归 unlimited 通道
        # （premium:false，但仍走 unlimited 并发上限 3）。
        # ⚠️ 与 0.0.63 时（premium:true）相反 —— 旧测试断言已废弃。
        self.assertEqual(session_bucket_for_model("z-ai/glm-5.3-flash"), "unlimited")
        # 兜底集合同步跟进（动态表为准）
        self.assertIn("z-ai/glm-5.3-flash", UNLIMITED_SESSION_MODEL_IDS)

    def test_dynamic_table_keeps_flash_premium_when_upstream_says_so(self) -> None:
        # 假如上游哪天把 flash 挪回 premium（premium_ids 含 flash）→ 自动跟随
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

    def test_dynamic_table_matches_current_upstream_premium(self) -> None:
        # 🟢 2026-09-01 0.0.79 官方现状：premium_ids=[luna, solar-pro4]，
        # flash / pro / glm-5.3-flash / ox / mimo → unlimited；solar-pro4 新队员进 premium
        registry = _registry_with({"openai/gpt-5.6-luna", "upstage/solar-pro4"})
        set_model_registry(registry)
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-flash"), "unlimited")
        self.assertEqual(session_bucket_for_model("deepseek/deepseek-v4-pro"), "unlimited")
        self.assertEqual(session_bucket_for_model("openai/gpt-5.6-luna"), "premium")
        self.assertEqual(session_bucket_for_model("upstage/solar-pro4"), "premium")
        self.assertEqual(session_bucket_for_model("z-ai/glm-5.3-flash"), "unlimited")
        self.assertEqual(session_bucket_for_model("stealth/ox-alpha"), "unlimited")

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


class GodOnlyHoneypotTests(unittest.TestCase):
    """god-only（蜜罐）模型过滤：/v1/models 不广播、resolve 直接拒绝。

    依据 freebuff-proxy #201/#140 + 2026-08-26 官方源码：
    `FREEBUFF_WEB_GOD_ONLY_MODELS = [KIMI_K3_ECO_MODEL, GPT_5_6_LUNA_ES_MODEL]`
    （luna-es 是 2026-08-26 新增的第二个蜜罐）。kimi-k3-eco 为文档级蜜罐；
    第三方流量打过去形同"探测隐藏路由"，是封禁级暴露面。
    """

    def setUp(self) -> None:
        set_model_registry(None)  # 走硬编码兜底

    def tearDown(self) -> None:
        set_model_registry(None)

    def test_kimi_k3_eco_in_fallback_god_only(self) -> None:
        self.assertIn("crof/kimi-k3-eco", GOD_ONLY_MODEL_IDS)
        self.assertTrue(is_god_only_model("crof/kimi-k3-eco"))

    def test_luna_es_in_fallback_god_only(self) -> None:
        # 2026-08-26 新增第二个蜜罐
        self.assertIn("openai/gpt-5.6-luna-es", GOD_ONLY_MODEL_IDS)
        self.assertTrue(is_god_only_model("openai/gpt-5.6-luna-es"))

    def test_core_models_not_honeypot(self) -> None:
        for model in (
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-5.6-luna",
        ):
            self.assertFalse(is_god_only_model(model))

    def test_resolve_rejects_honeypot(self) -> None:
        with self.assertRaises(ValueError):
            resolve_model("crof/kimi-k3-eco")
        with self.assertRaises(ValueError):
            resolve_model("openai/gpt-5.6-luna-es")

    def test_models_list_does_not_broadcast_honeypot(self) -> None:
        ids = {m.id for m in all_models()}
        self.assertNotIn("crof/kimi-k3-eco", ids)
        self.assertNotIn("openai/gpt-5.6-luna-es", ids)
        # 三条主力线必须仍在服务列表里
        for required in (
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-5.6-luna",
        ):
            self.assertIn(required, ids)

    def test_dynamic_table_can_flag_new_honeypot(self) -> None:
        # 动态表添加新蜜罐（如 future god-only）→ 自动跟随过滤
        registry = _registry_with({"deepseek/deepseek-v4-pro"})
        registry._table.god_only_ids = {"some/new-mole"}
        registry._table.models.append(
            DynamicModelEntry(id="some/new-mole", agent_id="a")
        )
        set_model_registry(registry)
        self.assertTrue(is_god_only_model("some/new-mole"))
        with self.assertRaises(ValueError):
            resolve_model("some/new-mole")


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

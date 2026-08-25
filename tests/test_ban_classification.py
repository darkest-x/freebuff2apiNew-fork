"""test_ban_classification.py — 上游封禁/拒绝信号分类的行为测试。

背景（freebuff-proxy #183/#198/#199/#207 源码级实证）：
1. 临时封禁 body 带 ``resumes_at``（RFC3339/unix 秒/unix 毫秒三形态），
   到点自动解封；无该字段 = 硬封禁（user.banned 布尔位，只能申诉）。
2. ``free_mode_run_fanout`` 是上游对代理特征（run_id 扇出 / prewarm 风暴）
   的主动拒绝，必须按限流退避处理，不能落未知 502 被重试放大。
3. GLM 无权益账号请求 base2-free-glm 会直接 403 封号（不做 429 配额判定），
   必须在拿到额度快照后本地预检拦截。
"""

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from freebuff2api.codebuff import (
    CodebuffAccountPool,
    CodebuffClient,
    CodebuffError,
    SessionManager,
    _upstream_error,
)
from freebuff2api.config import Settings
from freebuff2api.token_rotation import parse_ban_resumes_at


def _rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ParseBanResumesAtTests(unittest.TestCase):
    """resumes_at 三形态解析；无字段返回 0（硬封禁）。"""

    def test_rfc3339_future(self) -> None:
        msg = 'Codebuff request failed: 403 {"status":"banned","resumes_at":"' + _rfc3339(
            time.time() + 3600
        ) + '"}'
        remaining = parse_ban_resumes_at(msg) - time.time()
        self.assertGreater(remaining, 3500)

    def test_unix_seconds_and_milliseconds(self) -> None:
        future = time.time() + 1800
        self.assertAlmostEqual(
            parse_ban_resumes_at(f'403 {{"status":"banned","resumes_at":{future}}}'),
            future,
            delta=1,
        )
        ms = int((future) * 1000)
        self.assertAlmostEqual(
            parse_ban_resumes_at(f'403 {{"status":"banned","resumes_at":{ms}}}'),
            future,
            delta=1,
        )

    def test_past_or_missing_returns_zero(self) -> None:
        # 已过期 → 视为可立即恢复
        past = time.time() - 100
        self.assertEqual(
            parse_ban_resumes_at(f'403 {{"status":"banned","resumes_at":{past}}}'), 0.0
        )
        # 无字段 = 硬封禁
        self.assertEqual(parse_ban_resumes_at('403 {"status":"banned"}'), 0.0)
        # 非 ban 格式
        self.assertEqual(parse_ban_resumes_at("plain text error"), 0.0)


class FanoutClassificationTests(unittest.TestCase):
    """free_mode_run_fanout 必须 429 分类，绝不落 502。"""

    @staticmethod
    def _classify(status_code: int, text: str) -> CodebuffError:
        response = httpx.Response(
            status_code,
            content=text.encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        return _upstream_error(response)

    def test_error_field_classified_429(self) -> None:
        error = self._classify(
            403,
            '{"error":"free_mode_run_fanout","message":"Free mode request rejected."}',
        )
        self.assertEqual(error.status_code, 429)
        self.assertIn("free_mode_run_fanout", str(error))

    def test_plain_text_body_also_caught(self) -> None:
        error = self._classify(502, "upstream rejected: free_mode_run_fanout")
        self.assertEqual(error.status_code, 429)


class GlmEntitlementGateTests(unittest.IsolatedAsyncioTestCase):
    """GLM 无权益本地拦截：快照缺 glm 条目（或尚无任何快照）时 fail-fast。

    预检读的是 client.last_rate_limits（POST /session admission 响应缓存），
    严格模式：无法证明有权益就拒绝，宁可误拒也不赌"一次接触即封号"。
    """

    def setUp(self) -> None:
        from freebuff2api import models as models_registry

        models_registry.set_model_registry(None)

    def tearDown(self) -> None:
        from freebuff2api import models as models_registry

        models_registry.set_model_registry(None)

    def _manager(self, snapshot: dict | None) -> tuple[SessionManager, "SimpleSnapshotClient"]:
        client = SimpleSnapshotClient(snapshot)
        manager = SessionManager(client, Settings(codebuff_token="token", local_api_key=None))
        return manager, client

    async def test_glm_without_snapshot_rejected_locally(self) -> None:
        # 首次请求、从未有过任何 admission 快照 → 无法证明权益 → 拒绝
        manager, client = self._manager({"status": "none"})
        with self.assertRaises(CodebuffError) as ctx:
            await manager.ensure_session("z-ai/glm-5.2")
        message = str(ctx.exception)
        self.assertIn("referral entitlement", message)
        # 绝不能触碰 create_session（上游一次接触即封号）
        self.assertNotIn("create_session", client.calls)

    async def test_glm_without_entitlement_rejected_locally(self) -> None:
        # 快照里没有 glm 条目 = 该账号确认无 referral 权益
        snapshot = {
            "status": "none",
            "rateLimitsByModel": {
                "deepseek/deepseek-v4-flash": {"limit": 4, "recentCount": 0},
            },
        }
        manager, client = self._manager(snapshot)
        client.seed_rate_limits()
        with self.assertRaises(CodebuffError):
            await manager.ensure_session("z-ai/glm-5.2")
        self.assertNotIn("create_session", client.calls)

    @patch("freebuff2api.codebuff._ad_chain_due", return_value=False)
    async def test_glm_with_entitlement_passes_gate(self, _mock_ads_due) -> None:
        snapshot = {
            "status": "none",
            "rateLimitsByModel": {
                "z-ai/glm-5.2": {"limit": 2, "recentCount": 0},
            },
        }
        manager, client = self._manager(snapshot)
        client.seed_rate_limits()
        client.create_result = object()
        try:
            await manager.ensure_session("z-ai/glm-5.2")
        except Exception:
            pass  # 桩没实现完整创建链路；本测试只验证闸门放行到 create
        self.assertIn("create_session", client.calls)


class SimpleSnapshotClient:
    """只实现 ensure_session 预检路径所需的最小桩。"""

    def __init__(self, snapshot: dict | None) -> None:
        self.snapshot = snapshot or {}
        self.calls: list[str] = []
        self.create_result = None
        self.settings = Settings(codebuff_token="token", local_api_key=None)
        self.last_rate_limits: dict | None = None

    def seed_rate_limits(self) -> None:
        """模拟历史 admission 已缓存过额度快照。"""
        limits = self.snapshot.get("rateLimitsByModel")
        if isinstance(limits, dict):
            self.last_rate_limits = limits

    async def get_session(self, instance_id=None):
        self.calls.append("get_session")
        return dict(self.snapshot)

    async def delete_session(self, instance_id=None) -> None:
        self.calls.append("delete_session")

    async def create_session(self, model):
        self.calls.append("create_session")
        if self.create_result is not None:
            return self.create_result
        raise CodebuffError("stub: create not needed for gate test", 500)


if __name__ == "__main__":
    unittest.main()

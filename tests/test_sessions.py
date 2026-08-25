import asyncio
import unittest
from unittest.mock import patch

from freebuff2api import models as models_registry
from freebuff2api.codebuff import (
    CodebuffAccountPool,
    CodebuffError,
    FreebuffSession,
    SessionManager,
)
from freebuff2api.config import Settings


class RegistryPinnedMixin:
    """钉住动态模型注册表，保证 session bucket 判定不依赖网络。

    models.py 导入时会后台抓取官方模型映射；抓取成功与否会改变
    session_bucket_for_model 对未知模型（如 kimi-k2.6）的归池，
    导致测试结果随网络状态漂移。这里统一钉为 None（走硬编码兜底表）。
    """

    def setUp(self) -> None:
        super().setUp()
        self._registry_backup = models_registry.get_model_registry()
        models_registry.set_model_registry(None)

    def tearDown(self) -> None:
        models_registry.set_model_registry(self._registry_backup)
        await_none = getattr(super(), "tearDown", None)
        if await_none is not None:
            await_none()


class SwitchModelClient:
    def __init__(self) -> None:
        self.deleted = False
        self.calls = []

    async def get_session(self, instance_id=None):
        self.calls.append(("get_session", instance_id))
        if self.deleted:
            return {"status": "none"}
        return {
            "status": "active",
            "instanceId": "deepseek-instance",
            "model": "deepseek/deepseek-v4-pro",
            "expiresAt": "2026-05-23T15:27:34.581Z",
            "remainingMs": 3_000_000,
        }

    async def delete_session(self, instance_id=None) -> None:
        self.calls.append(("delete_session", instance_id))
        self.deleted = True

    async def request_ad_chain(self, messages=None, *, surface=None) -> None:
        self.calls.append(("request_ad_chain", messages or [], surface))

    async def request_ads(self, provider, messages=None, *, surface=None) -> dict:
        self.calls.append(("request_ads", provider, messages or [], surface))
        return {"ads": []}

    async def get_streak(self) -> dict:
        self.calls.append(("get_streak",))
        return {"streak": 0}

    async def report_zeroclick_impressions(self, ids) -> None:
        self.calls.append(("report_zeroclick_impressions", ids))

    async def report_codebuff_impression(self, imp_url) -> None:
        self.calls.append(("report_codebuff_impression", imp_url))

    async def create_session(self, model):
        self.calls.append(("create_session", model))
        if not self.deleted:
            raise CodebuffError(
                'Codebuff request failed: 409 {"status":"model_locked"}',
                502,
            )
        return FreebuffSession(
            instance_id="kimi-instance",
            model=model,
            remaining_ms=3_000_000,
        )


class LeaseSwitchModelClient:
    def __init__(self) -> None:
        self.current_model = "deepseek/deepseek-v4-flash"
        self.calls = []

    async def get_session(self, instance_id=None):
        self.calls.append(("get_session", instance_id, self.current_model))
        return {
            "status": "active",
            "instanceId": f"{self.current_model}-instance",
            "model": self.current_model,
            "remainingMs": 3_000_000,
        }

    async def delete_session(self, instance_id=None) -> None:
        self.calls.append(("delete_session", instance_id, self.current_model))
        self.current_model = ""

    async def request_ad_chain(self, messages=None, *, surface=None) -> None:
        self.calls.append(("request_ad_chain", messages or [], surface))

    async def request_ads(self, provider, messages=None, *, surface=None) -> dict:
        self.calls.append(("request_ads", provider, messages or [], surface))
        return {"ads": []}

    async def get_streak(self) -> dict:
        self.calls.append(("get_streak",))
        return {"streak": 0}

    async def report_zeroclick_impressions(self, ids) -> None:
        self.calls.append(("report_zeroclick_impressions", ids))

    async def report_codebuff_impression(self, imp_url) -> None:
        self.calls.append(("report_codebuff_impression", imp_url))

    async def create_session(self, model):
        self.calls.append(("create_session", model))
        self.current_model = model
        return FreebuffSession(
            instance_id=f"{model}-instance",
            model=model,
            remaining_ms=3_000_000,
        )


class PoolClient:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def get_session(self, instance_id=None):
        token = self.settings.codebuff_token
        # 会话模型须与测试请求的模型同桶（mimo/unlimited）才能命中复用路径
        return {
            "status": "active",
            "instanceId": f"{token}-instance",
            "model": "mimo/mimo-v2.5",
            "remainingMs": 3_000_000,
        }


class SessionManagerTests(RegistryPinnedMixin, unittest.IsolatedAsyncioTestCase):
    @patch("freebuff2api.codebuff._ad_chain_due", return_value=True)
    async def test_switch_model_deletes_active_upstream_session_before_create(self, _mock_ads_due):
        client = SwitchModelClient()
        manager = SessionManager(
            client,
            Settings(codebuff_token="token", local_api_key=None),
        )

        session = await manager.ensure_session("moonshotai/kimi-k2.6")

        self.assertEqual(session.instance_id, "kimi-instance")
        self.assertEqual(session.model, "moonshotai/kimi-k2.6")
        self.assertEqual(
            client.calls,
            [
                ("get_session", None),
                ("delete_session", "deepseek-instance"),
                ("request_ads", "gravity", [], "waiting_room"),
                ("request_ads", "carbon", [], "waiting_room"),
                ("create_session", "moonshotai/kimi-k2.6"),
            ],
        )

    async def test_premium_and_unlimited_channels_do_not_block_each_other(self):
        client = LeaseSwitchModelClient()
        manager = SessionManager(
            client,
            Settings(codebuff_token="token", local_api_key=None),
        )

        # mimo（unlimited 池）与 pro（premium 池）分属两个并发桶；
        # 旧用例用 flash 当 unlimited 代表，2026-08-18 起 flash 已入 premium 池。
        first = await manager.acquire_session("mimo/mimo-v2.5")
        started = asyncio.Event()

        async def acquire_second():
            started.set()
            return await manager.acquire_session("deepseek/deepseek-v4-pro")

        task = asyncio.create_task(acquire_second())
        await started.wait()
        second = await asyncio.wait_for(task, timeout=1)
        try:
            # premium 通道不会被 unlimited 通道阻塞；两个会话同时存在
            self.assertEqual(first.session.model, "mimo/mimo-v2.5")
            self.assertEqual(second.session.model, "deepseek/deepseek-v4-pro")
            # unlimited 通道的 session 没有被删除
            self.assertNotIn(
                ("delete_session", "mimo/mimo-v2.5-instance", "mimo/mimo-v2.5"),
                client.calls,
            )
        finally:
            await second.aclose()
            await first.aclose()

    @patch("freebuff2api.codebuff._ad_chain_due", return_value=False)
    async def test_account_pool_uses_next_free_token_for_concurrent_requests(self, _mock_ads_due):
        settings = Settings(
            codebuff_token="token-a,token-b",
            local_api_key=None,
            rotation_mode="balanced",
        )

        with patch("freebuff2api.codebuff.CodebuffClient", PoolClient):
            # mimo 恒为 unlimited 池（flash 2026-08-18 起已入 premium，
            # balanced 模式下 premium 单账号单槽会串行化而非扇出）。
            pool = CodebuffAccountPool(settings)
            first = await pool.acquire_session("mimo/mimo-v2.5")
            second = await pool.acquire_session("mimo/mimo-v2.5")
            try:
                self.assertEqual(first.client.settings.codebuff_token, "token-a")
                self.assertEqual(second.client.settings.codebuff_token, "token-b")
                self.assertNotEqual(
                    first.session.instance_id,
                    second.session.instance_id,
                )
            finally:
                await second.aclose()
                await first.aclose()
                await pool.aclose()


if __name__ == "__main__":
    unittest.main()

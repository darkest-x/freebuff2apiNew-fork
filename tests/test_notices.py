import unittest

from freebuff2api.codebuff import CodebuffError
from freebuff2api.notices import describe_error, notice_for_error


class NoticeMappingTests(unittest.TestCase):
    def test_rate_limited_maps_to_quota_notice(self) -> None:
        error = CodebuffError(
            'Freebuff session rate_limited: 429 {"model":"deepseek/deepseek-v4-pro"}',
            429,
        )
        notice = notice_for_error(error)
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("中转提示", notice)
        self.assertIn("deepseek/deepseek-v4-pro", notice)
        self.assertIn("无限模型", notice)

    def test_banned_and_country_blocked_map_to_chinese_notices(self) -> None:
        self.assertIn("账号已被官方暂停", notice_for_error(CodebuffError("Freebuff account banned: {}", 403)) or "")
        self.assertIn(
            "美国节点",
            notice_for_error(CodebuffError("Freebuff country_blocked: {}", 403)) or "",
        )

    def test_session_expired_maps_to_retry_notice(self) -> None:
        notice = notice_for_error(CodebuffError("Codebuff session expired: {}", 410))
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("重新发送", notice)

    def test_global_ban_gate_maps_to_notice(self) -> None:
        notice = notice_for_error(
            CodebuffError(
                "Freebuff account banned; all models are disabled until the next 15:00 Asia/Shanghai.",
                403,
            )
        )
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("封禁", notice)

    def test_insufficient_quota_maps_to_congestion_notice(self) -> None:
        notice = notice_for_error(
            CodebuffError("Codebuff chat failed: 429 insufficient_quota", 429)
        )
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("负载较高", notice)

    def test_unknown_error_returns_none_for_hard_error_path(self) -> None:
        self.assertIsNone(notice_for_error(CodebuffError("Codebuff chat failed: 403 hierarchy", 502)))

    def test_describe_error_is_chinese_and_keeps_original_elsewhere(self) -> None:
        text = describe_error(CodebuffError("network error", 502))
        self.assertIn("网络", text)

    def test_session_ended_long_task_maps_to_soft_notice(self) -> None:
        # 🟢 2026-09-02 0.0.86 复核：官方 SESSION_ENDED_MESSAGE 134679，含义是
        # 长任务跑到一半 free session 被回收。客户端应看到"重新发送一次"的指引，
        # 而非被认成网络错误或硬错误。
        msg = (
            "Your free session ran out while this turn was running. "
            "Nothing is lost — send your message again to start a new session "
            "and pick up where it left off."
        )
        notice = notice_for_error(CodebuffError(msg, 0))
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("中转提示", notice)
        self.assertIn("重新发送", notice)

    def test_session_ended_short_code_maps_to_soft_notice(self) -> None:
        # 长任务场景不一定带整段英文信息，也可能是 code-only（如
        # `Codebuff session_ended: 409`）—— 也得能命中。
        notice = notice_for_error(CodebuffError("Codebuff session_ended: 409", 409))
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("会话在本次对话运行中被回收", notice)

    def test_turn_spend_limit_maps_to_soft_notice_0_0_96(self) -> None:
        # 🟢 2026-09-08 0.0.96 复核：turn_spend_limit（FREEBUFF_TURN_SPEND_LIMIT_ERROR_CODE）
        # —— 单 turn 额度上限，**会话不结束**：官方提示 "Your session is still
        # available — send a new message to continue from here."。反代应以软提示
        # 返回（引导发新消息继续），不能当 session 失效/重试处理。
        msg = (
            "APICallError: This turn reached its model usage limit. "
            "Your session is still available \u2014 send a new message "
            "to continue from here."
        )
        notice = notice_for_error(CodebuffError(msg, 429))
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("中转提示", notice)
        self.assertIn("发送新消息", notice)

    def test_turn_spend_limit_short_code_maps_to_soft_notice(self) -> None:
        notice = notice_for_error(CodebuffError("Codebuff turn_spend_limit: 429", 429))
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("发送新消息", notice)


if __name__ == "__main__":
    unittest.main()

"""test_run_manager.py — [FP-6] 对话感知 run 生命周期的单元测试。

覆盖：对话指纹稳定性、同对话复用/跨对话切换、终态标记（failed/cancelled）、
steps 打包与 messageId 透传、空闲清扫、shutdown 兜底、FINISH body 字段。
"""

import asyncio
import unittest

from freebuff2api.run_manager import (
    ManagedRun,
    RunManager,
    conversation_fingerprint,
)


class FakeCodebuffClient:
    """最小 client 桩：记录 start_run / finish_run 调用。"""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[dict] = []
        self._next = 0

    async def start_run(self, agent_id: str, ancestor_run_ids=None) -> str:
        self._next += 1
        run_id = f"run-{self._next}"
        self.started.append((agent_id, tuple(ancestor_run_ids or ())))
        return run_id

    async def finish_run(self, run_id, *, total_steps, steps=None, status="completed", error_message=None):
        self.finished.append(
            {
                "run_id": run_id,
                "total_steps": total_steps,
                "steps": steps or [],
                "status": status,
                "error_message": error_message,
            }
        )


def _msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


class ConversationFingerprintTests(unittest.TestCase):
    """对话指纹：同对话稳定、跨对话不同、空输入安全。"""

    def test_same_first_user_message_same_fp(self) -> None:
        # 多轮重发全量 history：首条 user 不变 → 指纹不变
        short = _msgs("帮我修这个 bug") + [{"role": "assistant", "content": "ok"}]
        long = short + [_msgs("继续")[0], {"role": "assistant", "content": "done"}]
        self.assertEqual(conversation_fingerprint(short), conversation_fingerprint(long))

    def test_different_conversations_different_fp(self) -> None:
        self.assertNotEqual(
            conversation_fingerprint(_msgs("对话 A")),
            conversation_fingerprint(_msgs("对话 B")),
        )

    def test_empty_or_system_only_gives_empty_fp(self) -> None:
        self.assertEqual(conversation_fingerprint([]), "")
        self.assertEqual(conversation_fingerprint(None), "")
        self.assertEqual(conversation_fingerprint([{"role": "system", "content": "x"}]), "")

    def test_block_content_format_supported(self) -> None:
        # Anthropic 分块格式
        fp1 = conversation_fingerprint(
            [{"role": "user", "content": [{"type": "text", "text": "分块内容"}]}]
        )
        fp2 = conversation_fingerprint([{"role": "user", "content": "分块内容"}])
        self.assertEqual(fp1, fp2)


class RunManagerLifecycleTests(unittest.TestCase):
    """acquire 复用/切换 + 终态标记 + FINISH 参数。"""

    def _run_async(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def tearDown(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass

    def test_first_acquire_starts_new_run(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager(idle_ttl_seconds=1800)
            run, reused = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("hi"))
            self.assertFalse(reused)
            self.assertEqual(run.run_id, "run-1")
            self.assertEqual(len(client.started), 1)

        self._run_async(case())

    def test_same_conversation_reuses_run(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            r1, reused1 = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("hi"))
            r2, reused2 = await mgr.acquire(
                client, token="tok", agent_id="a1",
                messages=_msgs("hi") + [{"role": "assistant", "content": "?"}],
            )
            self.assertFalse(reused1)
            self.assertTrue(reused2)
            self.assertIs(r2, r1)
            self.assertEqual(len(client.started), 1)  # 只 START 过一次
            self.assertEqual(len(client.finished), 0)  # 没有 FINISH

        self._run_async(case())

    def test_new_conversation_rotates_old_run(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            old, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("first"))
            new, reused = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("second"))
            self.assertFalse(reused)
            self.assertIsNot(new, old)
            self.assertEqual(new.run_id, "run-2")
            # 等后台 FINISH 任务执行完
            await asyncio.sleep(0.05)
            self.assertEqual(len(client.finished), 1)
            self.assertEqual(client.finished[0]["run_id"], old.run_id)
            self.assertEqual(client.finished[0]["status"], "completed")

        self._run_async(case())

    def test_different_agent_ids_isolated(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            ra, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            rb, _ = await mgr.acquire(client, token="tok", agent_id="a2", messages=_msgs("q"))
            self.assertNotEqual(ra.run_id, rb.run_id)
            self.assertEqual(len(client.started), 2)
            await asyncio.sleep(0.05)
            self.assertEqual(len(client.finished), 0)  # 各自仍活跃，互不挤掉

        self._run_async(case())

    def test_failed_then_finished_reports_failed_status(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            run, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            mgr.note_failure(run, "Provider Violation: blocked")
            await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("rotated away"))
            await asyncio.sleep(0.05)
            self.assertEqual(len(client.finished), 1)
            fin = client.finished[0]
            self.assertEqual(fin["status"], "failed")
            self.assertIn("blocked", fin["error_message"])

        self._run_async(case())

    def test_cancelled_then_finished_reports_cancelled(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            run, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            mgr.note_cancelled(run)
            await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("other"))
            await asyncio.sleep(0.05)
            self.assertEqual(client.finished[0]["status"], "cancelled")

        self._run_async(case())

    def test_step_recording_and_finish_payload_shape(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            run, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            n1 = mgr.note_attempt(run)
            mgr.note_success(run, "gen-1786967954-abc")
            n2 = mgr.note_attempt(run)
            mgr.note_success(run, None)  # 上游没给 id → messageId 为 null，不伪造
            status, total, steps, err = run.finish_snapshot()
            self.assertEqual((n1, n2), (1, 2))
            self.assertEqual(status, "completed")
            self.assertEqual(total, 2)
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[0]["messageId"], "gen-1786967954-abc")
            self.assertIsNone(steps[1]["messageId"])
            self.assertEqual(steps[0]["stepNumber"], 1)
            self.assertEqual(steps[0]["status"], "completed")
            # 官方键序字段齐全
            for key in ("id", "stepNumber", "credits", "childRunIds", "messageId", "status", "startTime"):
                self.assertIn(key, steps[0])
            self.assertIsNone(err)

        self._run_async(case())


class RunManagerSweepTests(unittest.TestCase):
    """空闲清扫与 shutdown 兜底。"""

    def _run_async(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def tearDown(self) -> None:
        try:
            asyncio.get_event_loop().close()
        except Exception:
            pass

    def test_sweep_finishes_expired_runs_only(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager(idle_ttl_seconds=100)
            expired, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("old"))
            fresh, _ = await mgr.acquire(client, token="tok", agent_id="a2", messages=_msgs("new"))
            # 把 expired 的 last_used 拨回 200 秒前
            expired.last_used_monotonic -= 200
            # 正确的 token 哈希映射（无映射的 token 会被跳过 —— 另有专门断言）
            token_hash = RunManager.token_key("tok")
            count = await mgr.sweep_idle({token_hash: client})
            self.assertGreaterEqual(count, 1)
            await asyncio.sleep(0.05)
            finished_ids = {f["run_id"] for f in client.finished}
            self.assertIn(expired.run_id, finished_ids)
            self.assertNotIn(fresh.run_id, finished_ids)

        self._run_async(case())

    def test_sweep_skips_run_without_client_mapping(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager(idle_ttl_seconds=100)
            run, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            run.last_used_monotonic -= 200
            # 映射表为空：清扫应安全跳过（只记 warning），不抛异常、不误 FINISH
            count = await mgr.sweep_idle({})
            self.assertEqual(count, 1)  # 已从活跃表摘除
            await asyncio.sleep(0.05)
            self.assertEqual(len(client.finished), 0)

        self._run_async(case())

    def test_sweep_uses_token_hash_mapping(self) -> None:
        async def case() -> None:
            from freebuff2api.run_manager import RunManager as RM

            client = FakeCodebuffClient()
            mgr = RM(idle_ttl_seconds=10)
            run, _ = await mgr.acquire(client, token="secret-token", agent_id="a1", messages=_msgs("q"))
            run.last_used_monotonic -= 100
            token_hash = RM.token_key("secret-token")
            count = await mgr.sweep_idle({token_hash: client})
            self.assertEqual(count, 1)
            await asyncio.sleep(0.05)
            self.assertEqual(len(client.finished), 1)
            self.assertEqual(client.finished[0]["run_id"], run.run_id)

        self._run_async(case())

    def test_finish_all_clears_everything(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q1"))
            await mgr.acquire(client, token="tok", agent_id="a2", messages=_msgs("q2"))
            token_hash = RunManager.token_key("tok")
            await mgr.finish_all({token_hash: client})
            snap = mgr.snapshot()
            self.assertEqual(snap["active"], 0)
            self.assertEqual(len(client.finished), 2)

        self._run_async(case())

    def test_double_finish_deduped(self) -> None:
        async def case() -> None:
            client = FakeCodebuffClient()
            mgr = RunManager()
            run, _ = await mgr.acquire(client, token="tok", agent_id="a1", messages=_msgs("q"))
            token_hash = RunManager.token_key("tok")
            await asyncio.gather(
                mgr.release(client, token="tok", run=run),
                mgr.release(client, token="tok", run=run),
            )
            await asyncio.sleep(0.05)
            # release 幂等：第二次时 run 已不在活跃表，但 FINISH 去重保证只发一次
            self.assertLessEqual(len(client.finished), 1)

        self._run_async(case())


class TokenKeyTests(unittest.TestCase):
    def test_token_key_stable_and_masked(self) -> None:
        k1 = RunManager.token_key("abc")
        k2 = RunManager.token_key("abc")
        k3 = RunManager.token_key("xyz")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)
        self.assertEqual(len(k1), 8)  # 短哈希，不落明文
        self.assertEqual(RunManager.token_key(""), "anon")


if __name__ == "__main__":
    unittest.main()

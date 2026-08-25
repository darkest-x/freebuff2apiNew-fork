import unittest

from freebuff2api.codebuff import CodebuffError, FreebuffSession
from freebuff2api.models import (
    ALL_MODELS,
    CONTEXT_PRUNER_AGENT_ID,
    GEMINI_THINKER_AGENT_ID,
    agent_validation_payload,
    models_response,
    resolve_model,
)
from freebuff2api.openai_compat import (
    CompletionAccumulator,
    build_upstream_payload,
    inject_end_turn_signature,
    sanitize_stream_chunk,
)


class OpenAICompatTests(unittest.TestCase):
    def test_models_response_lists_all_freebuff_models(self) -> None:
        response = models_response()

        # 2026-08-24：断言从「等于硬编码 ALL_MODELS」改为「包含硬编码全部 id」。
        # 原断言在动态注册表拉到上游新模型（luna-es / ox-alpha）后即红 ——
        # 动态表新增模型是正常行为，不应让测试失败（陈旧断言修正）。
        # 2026-08-25：蜜罐模型（kimi-k3-eco 等）不再广播，需从"全部 id"中排除。
        from freebuff2api.models import is_god_only_model

        hardcoded_ids = {
            model.id for model in ALL_MODELS if not is_god_only_model(model.id)
        }
        listed_ids = [item["id"] for item in response["data"]]
        self.assertTrue(hardcoded_ids.issubset(set(listed_ids)))
        first = response["data"][0]
        self.assertGreaterEqual(first["context_window"], 1)
        self.assertGreaterEqual(first["max_input_tokens"], 1)
        self.assertGreaterEqual(first["max_output_tokens"], 1)
        self.assertIn("reasoning_efforts", first)
        self.assertIn("default_reasoning_effort", first)

    def test_resolve_model_maps_agent_id(self) -> None:
        model = resolve_model("anthropic/claude-fable-5")

        self.assertEqual(model.agent_id, "base2-free-fable")

    def test_resolve_honeypot_model_rejected(self) -> None:
        # 🔴 2026-08-25：kimi-k3-eco 是官方 god-only 蜜罐，本地直接拒绝
        with self.assertRaises(ValueError):
            resolve_model("crof/kimi-k3-eco")

    def test_resolve_minimax_m3_maps_har_agent_id(self) -> None:
        model = resolve_model("minimax/minimax-m3")

        self.assertEqual(model.agent_id, "base2-free-minimax-m3")

    def test_resolve_gemini_model_maps_allowed_agent_combo(self) -> None:
        model = resolve_model("google/gemini-3.1-pro-preview")

        self.assertEqual(model.agent_id, GEMINI_THINKER_AGENT_ID)
        self.assertEqual(model.parent_agent_id, "base2-free-kimi-k3-eco")
        self.assertEqual(model.session_id, "crof/kimi-k3-eco")
        self.assertEqual(model.upstream_id, "google/gemini-3.1-pro-preview")

    def test_resolve_gemini_flash_lite_runs_under_session_root(self) -> None:
        model = resolve_model("google/gemini-3.1-flash-lite")

        self.assertEqual(model.agent_id, "file-picker")
        self.assertEqual(model.parent_agent_id, "base2-free-deepseek-flash")
        self.assertEqual(model.session_id, "deepseek/deepseek-v4-flash")

    def test_resolve_gemini_flash_preview_uses_program_default_agent(self) -> None:
        model = resolve_model("google/gemini-3.5-flash-lite")

        self.assertEqual(model.agent_id, "file-picker-max")
        self.assertEqual(model.parent_agent_id, "base2-free-deepseek-flash")
        self.assertEqual(model.upstream_id, "google/gemini-3.5-flash-lite")

    def test_agent_validation_payload_defines_spawnable_agents(self) -> None:
        payload = agent_validation_payload()
        definitions = payload["agentDefinitions"]
        ids = {definition["id"] for definition in definitions}
        spawnable_ids = {
            agent_id
            for definition in definitions
            for agent_id in definition.get("spawnableAgents", [])
        }

        self.assertIn(CONTEXT_PRUNER_AGENT_ID, ids)
        self.assertLessEqual(spawnable_ids, ids)

    def test_agent_validation_payload_has_spawn_agent_tool_when_spawnable(self) -> None:
        payload = agent_validation_payload()

        for definition in payload["agentDefinitions"]:
            if definition.get("spawnableAgents"):
                self.assertIn("spawn_agents", definition["toolNames"])

    def test_build_upstream_payload_uses_explicit_client_id(self) -> None:
        payload = build_upstream_payload(
            {"model": "deepseek/deepseek-v4-pro", "messages": []},
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-pro",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(payload["provider"], {"data_collection": "deny"})
        self.assertEqual(
            payload["codebuff_metadata"],
            {
                "freebuff_instance_id": "instance-1",
                "freebuff_multi_session": "1",
                "trace_session_id": "trace-1",
                "run_id": "run-1",
                "client_id": "client-1",
                "cost_mode": "free",
            },
        )

    def test_inject_end_turn_signature_appends_when_missing(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "get_weather", "parameters": {}}},
        ]
        out = inject_end_turn_signature(tools)

        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["function"]["name"], "end_turn")
        # 原列表不被改动（复制新列表）
        self.assertEqual(len(tools), 1)

    def test_inject_end_turn_signature_idempotent(self) -> None:
        tools = [
            {"type": "function", "function": {"name": "end_turn"}},
            {"type": "function", "function": {"name": "foo"}},
        ]
        self.assertEqual(inject_end_turn_signature(tools), tools)

    def test_inject_end_turn_signature_passthrough_empty(self) -> None:
        self.assertIsNone(inject_end_turn_signature(None))
        self.assertEqual(inject_end_turn_signature([]), [])

    def test_build_upstream_payload_injects_end_turn_with_tools(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {"type": "function", "function": {"name": "get_weather", "parameters": {}}},
                ],
            },
            session=FreebuffSession(instance_id="instance-1", model="deepseek/deepseek-v4-pro"),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertEqual(payload["tools"][-1]["function"]["name"], "end_turn")
        self.assertEqual(len(payload["tools"]), 2)

    def test_build_upstream_payload_no_tools_no_end_turn(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
            },
            session=FreebuffSession(instance_id="instance-1", model="deepseek/deepseek-v4-pro"),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertNotIn("tools", payload)

    def test_build_upstream_payload_can_override_upstream_model(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "google/gemini-3.1-flash-lite-preview",
                "messages": [],
            },
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-flash",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
            upstream_model_id="google/gemini-3.1-flash-lite-preview",
        )

        self.assertEqual(payload["model"], "google/gemini-3.1-flash-lite-preview")

    def test_build_upstream_payload_maps_developer_role_to_system(self) -> None:
        body = {
            "model": "deepseek/deepseek-v4-pro",
            "messages": [
                {"role": "developer", "content": "be helpful"},
                {"role": "user", "content": "hello"},
            ],
            "temperature": 0.2,
        }

        payload = build_upstream_payload(
            body,
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-pro",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertTrue(payload["messages"][0]["content"].startswith("You are Buffy"))
        self.assertEqual(body["messages"][0]["role"], "developer")

    def test_build_upstream_payload_adds_buffy_system_prompt_when_missing(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hello"}],
            },
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-pro",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertTrue(payload["messages"][0]["content"].startswith("You are Buffy"))
        self.assertEqual(payload["messages"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(payload["messages"][1]["role"], "user")

    def test_build_upstream_payload_filters_unknown_request_fields(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-pro",
                "messages": [],
                "temperature": 0.2,
                "provider": {"data_collection": "allow"},
                "codebuff_metadata": {"cost_mode": "paid"},
                "unexpected": "client-owned",
            },
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-pro",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        self.assertEqual(payload["temperature"], 0.2)
        self.assertNotIn("unexpected", payload)
        self.assertEqual(payload["provider"], {"data_collection": "deny"})
        self.assertEqual(payload["codebuff_metadata"]["cost_mode"], "free")

    def test_build_upstream_payload_clamps_max_tokens(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-flash",
                "messages": [],
                "max_tokens": 64000,
                "max_completion_tokens": 100000,
            },
            session=FreebuffSession(
                instance_id="instance-1",
                model="deepseek/deepseek-v4-flash",
            ),
            run_id="run-1",
            client_id="client-1",
            trace_session_id="trace-1",
        )

        # 超模型上限 32768 → 钳制（chat completions 路径保留原字段名）
        self.assertEqual(payload["max_tokens"], 32_768)
        self.assertEqual(payload["max_completion_tokens"], 32_768)

    def test_accumulator_keeps_reasoning_content_separate(self) -> None:
        accumulator = CompletionAccumulator("deepseek/deepseek-v4-flash")

        accumulator.add(
            {
                "id": "chunk-1",
                "created": 1,
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": None, "reasoning_content": "hello"},
                        "finish_reason": None,
                    }
                ],
            }
        )

        response = accumulator.final_response()

        message = response["choices"][0]["message"]
        # 对齐 Worker 1.7.2：上游只回 reasoning 未回 content 时，用 reasoning 兜底 content，
        # 避免客户端收到空响应（reasoning_used_as_content 标记）。
        self.assertEqual(message["content"], "hello")
        self.assertTrue(message["reasoning_used_as_content"])
        self.assertNotIn("reasoning_content", message)

    def test_accumulator_keeps_final_answer_as_content(self) -> None:
        accumulator = CompletionAccumulator("deepseek/deepseek-v4-flash")

        accumulator.add(
            {
                "id": "chunk-1",
                "created": 1,
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": None, "reasoning_content": "thinking"},
                        "finish_reason": None,
                    },
                    {
                        "index": 0,
                        "delta": {"content": "answer", "reasoning_content": None},
                        "finish_reason": "stop",
                    },
                ],
            }
        )

        message = accumulator.final_response()["choices"][0]["message"]

        self.assertEqual(message["content"], "answer")
        self.assertEqual(message["reasoning_content"], "thinking")

    def test_stream_chunk_keeps_reasoning_separate_by_default(self) -> None:
        """默认关闭折叠：reasoning_content 作为扩展字段保留，不混入 content。"""
        chunk = sanitize_stream_chunk(
            {
                "id": "chunk-1",
                "created": 1,
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": None, "reasoning_content": "hello"},
                        "finish_reason": None,
                    }
                ],
            }
        )

        delta = chunk["choices"][0]["delta"]
        self.assertNotIn("content", delta)
        self.assertEqual(delta["reasoning_content"], "hello")

    def test_stream_chunk_folds_reasoning_with_think_tags_when_enabled(self) -> None:
        """开启折叠后，reasoning_content 以 <think> 标签写入 content 并保留原字段。"""
        chunk = sanitize_stream_chunk(
            {
                "id": "chunk-1",
                "created": 1,
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": None, "reasoning_content": "hello"},
                        "finish_reason": None,
                    }
                ],
            },
            fold_reasoning_in_content=True,
        )

        delta = chunk["choices"][0]["delta"]
        self.assertEqual(delta["content"], "<think>hello</think>")
        self.assertEqual(delta["reasoning_content"], "hello")

    def test_stream_chunk_raises_on_upstream_error(self) -> None:
        """上游 200 但 SSE 流内下发 error chunk 时必须抛错，不能当空 chunk 丢弃。"""
        with self.assertRaises(CodebuffError) as ctx:
            sanitize_stream_chunk(
                {
                    "id": "chunk-1",
                    "created": 1,
                    "model": "openai/gpt-5.6-luna",
                    "choices": [],
                    "error": {
                        "code": 502,
                        "message": "Policy Violation: this user has been blocked for a previous policy violation.",
                        "metadata": {"error_type": "provider_unavailable"},
                    },
                }
            )

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("Policy Violation", str(ctx.exception))

    def test_accumulator_raises_on_upstream_error(self) -> None:
        accumulator = CompletionAccumulator("openai/gpt-5.6-luna")

        with self.assertRaises(CodebuffError):
            accumulator.add(
                {
                    "id": "chunk-1",
                    "created": 1,
                    "model": "openai/gpt-5.6-luna",
                    "choices": [],
                    "error": {"code": 502, "message": "blocked by policy"},
                }
            )

    def test_reasoning_effort_clamps_to_official_model_table(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "ultra",
            },
            session=FreebuffSession(instance_id="i", model="deepseek/deepseek-v4-flash"),
            run_id="run-1",
            client_id="client-1",
        )

        # flash official efforts: low/high/max；ultra 不对齐官方允许列表 → 回退默认 high
        # 且按官方 free-mode 传法放进 codebuff_metadata.freebuff_reasoning_effort
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["codebuff_metadata"]["freebuff_reasoning_effort"], "high")

    def test_reasoning_effort_valid_value_passes_through(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "max",
            },
            session=FreebuffSession(instance_id="i", model="deepseek/deepseek-v4-flash"),
            run_id="run-1",
            client_id="client-1",
        )

        self.assertEqual(payload["codebuff_metadata"]["freebuff_reasoning_effort"], "max")

    def test_reasoning_effort_dropped_for_model_without_effort_table(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "minimax/minimax-m3",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
            session=FreebuffSession(instance_id="i", model="minimax/minimax-m3"),
            run_id="run-1",
            client_id="client-1",
        )

        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("freebuff_reasoning_effort", payload["codebuff_metadata"])

    def test_reasoning_effort_passthrough_unknown_model(self) -> None:
        payload = build_upstream_payload(
            {
                "model": "deepseek/deepseek-v4-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "medium",
            },
            session=FreebuffSession(instance_id="i", model="deepseek/deepseek-v4-flash"),
            run_id="run-1",
            client_id="client-1",
        )

        # flash 官方 efforts 不含 medium → 字段不对齐，回退默认 high
        self.assertEqual(payload["codebuff_metadata"]["freebuff_reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()

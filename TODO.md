# 待优化清单

> 来源：2026-08-10 对照 `pingmike2/freebuff2api-wokers` v1.7.2 源码 + issues(#13/#10/#9/#6) 审查结果。
> 分支：`fix/desktop-protocol-1.7`。已完成的已标注 commit，未完成的按优先级排列。

---

## ✅ 已完成(commit 944012e,2026-08-10)

### 0. 2026-09-01 桌面版 0.0.79 复核（模型池四度更正 + 心跳/超时收紧）
- 状态：**已完成**（含动态表/兜底/测试，提交见本次 commit）
- 官方桌面版 0.0.79（09-01 16:14 安装，orchestrator.js 8,996,741 B）与 08-27 副本 diff：
  仅 4 个 FREEBUFF_* 常量新增（DESKTOP_IDLE_RELEASE_MS / SUBSCRIBER_DESKTOP_SESSION_LIMITS /
  PER_MODEL_SESSION_SPEND_CAPS / SOLAR_PRO_4_MODEL_ID），无删除 → 协议层无大变动；
- **premium 池换血**：`[luna, glm-5.3-flash]` → `[luna, solar-pro4]`（glm-5.3-flash 掉出，
  归 unlimited 通道）；`DEFAULT_FREEBUFF_MODEL_ID` = glm-5.3-flash（luna 被挤下）；
- **新模型** `upstage/solar-pro4`：premium:true、500K ctx、独立 daily 池（limit=1）、
  spend=0.5、无 efforts 档位；models.py 已新增（agent/base3/reviewer 三映射齐）；
- **CLI/web 侧**：claude-fable-5 重入 SUPPORTED（dataUse=training），Web/CLI 完整可见；
- **心跳严格化**：GET /session 分「心跳（7 字段）」vs「refresh-tier（拉额度快照）」两路，
  codebuff.py 的 get_session 增 refresh_tier 参数；POST /session 头清单收紧（不带 heartbeat）；
- **codebuff_metadata**：必含 llm_step_number（openai/anthropic 两条路径默认 "1"）
  与 client_id/run_id（此前仅条件携带）；
- **notices.py**：新增 context_overflow / connection / account_changed 三类中文提示；
- 测试：`tests/` 全套通过 251 passed（test_new_features.py 为已知遗留问题，ignore）。
- ⚠️ codebuff.py 超时/重试仍为 httpx 默认（30s+0 retry），官方是 15s + 3 次指数退避；
  已记录但**本轮未动**（涉及读超时语义，改动风险 > 收益），留作后续 P3 项。

### 1. 模型列表补齐(对齐 Worker 1.7.2 MODELS 表)
- 状态：**已完成** — `freebuff2api/models.py` 补 8 个新模型：
  `openai/gpt-5.6-luna`、`z-ai/glm-5.2`、`poolside/laguna-s-2.1`、`openrouter/poolside/laguna-s-2.1`、`inclusionai/ling-3.0-flash:free`、`crof/greg-2-ultra`、`crof/greg-2-super`、`anthropic/claude-fable-5`、`meta/muse-spark-1.2-contributor`
- 来源：Worker `MODELS`(orchestrator.js `FREEBUFF_ROOT_AGENT_ID_BY_MODEL`,2026-08-07 实测同步)
- 影响：此前客户端请求这些模型直接 400。

### 2. 非流式 reasoning 兜底(缓解"模型响应为空")
- 状态：**已完成** — `openai_compat.py::CompletionAccumulator.final_response`
- 逻辑：上游只返回 `reasoning_content` 而未返回 `content` 时(推理模型常见),用 reasoning 填充 `content` 并标记 `reasoning_used_as_content: true`,避免客户端收到空响应。
- 对齐：Worker `streamToNonStream`。

### 3. Anthropic 流式 usage 返回
- 状态：**已完成** — `anthropic_compat.py::build_anthropic_upstream_payload`
- 逻辑：Anthropic 流式请求时设置 `stream_options: {include_usage: true}`,确保上游返回 usage,Claude Code 流式输出能拿到 token 统计。
- 对齐：Worker `anthropicToChat`。

---

## ⏳ 待办(未完成)

### P1 — 实现 `/v1/responses` 端点
- 优先级：**高**(CC Switch / Codex / 部分 OpenAI Responses SDK 客户端依赖)
- 现状：项目仅 `/v1/chat/completions`、`/v1/messages`、`/v1/models`,无 `/v1/responses`
- 参考：Worker `handleResponses` + `responsesToChatParams` + `responsesInputToMessages` + `pipeUpstreamToResponsesStream` + `responsesToNonStream` + `chatUsageToResponsesUsage`
- ⚠️ 必须做 usage 归一化(`prompt_tokens→input_tokens`,issue #10:缺 `input_tokens` 客户端解析直接报错)
- ⚠️ 多轮转换需保留 `function_call` / `reasoning` / `previous_response_id`(issue #6:当前 Worker 实现也丢这些导致重复思考/重复调工具)

### P2 — 实现 `/v1/messages/count_tokens`
- 优先级：中(部分 Claude Code 客户端启动时调用)
- 参考：Worker `handleAnthropicCountTokens`(本地估算,`estimateAnthropicTokens`,约 40 行)

### P3 — run 缓存
- 优先级：低(纯性能)
- 参考：Worker `runCache`(10 分钟 TTL,run_id 可跨请求复用,省两次上游调用)
- 注意：需按 `(token, agentId)` 键控,防多账号串号

### P3 — 额度池感知选号
- 优先级：低(仅选号策略,不影响请求协议)
- 参考：Worker `PREMIUM_QUOTA_MODELS`(4 个)/ `STANDARD_MODELS`(2 个) + `remainingQuota` + `pickToken`
- 背景：官方三种额度池(PREMIUM 共享 6 次/天、STANDARD 6 次/天、GLM 独立),都是 session 次数非 token 数

### P3 — 并发策略评估
- 优先级：低(观察项)
- 差异：Worker 全池**串行**(注释"免费通道并发>1 就出问题");我们每账号并发=1、多账号可并行
- 观察：若 429 空响应增多,考虑降低 `FREEBUFF_ACCOUNT_CONCURRENCY` 或加全局串行

### P3 — 旧模型清理评估
- 优先级：低(观察项)
- 现状：我们保留 `moonshotai/kimi-k2.6`、`minimax/minimax-m2.7`、`mimo/mimo-v2.5-pro`、3 个 Gemini(Worker 1.7.2 已精简掉)
- 观察：请求这些模型若出现 400/降级,从 `models.py` 移除

### P4 — client_id 格式对齐(可选)
- 差异：我们 `uuid4().hex[:11]` vs Worker `"wf-" + random(8)`
- 判断：语义等价,上游未校验格式;如遇异常再对齐

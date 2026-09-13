# 待优化清单

> 来源：2026-08-10 对照 `pingmike2/freebuff2api-wokers` v1.7.2 源码 + issues(#13/#10/#9/#6) 审查结果。
> 分支：`fix/desktop-protocol-1.7`。已完成的已标注 commit，未完成的按优先级排列。

---

## ✅ 已完成(commit 944012e,2026-08-10)

### 0. 2026-09-01 桌面版 0.0.79 复核（模型池四度更正 + 心跳/超时收紧）

### 0a. 2026-09-02 桌面版 0.0.86 复核（long-task session_ended 友好化）
- 状态：**已完成**（含长任务中途被回收的中文提示与 4 路径空流重试条件扩展，提交见本次 commit）
- 官方桌面版 0.0.86（09-03 06:10 安装，orchestrator.js 9,386,864 B）与 0.0.79 diff：
  - **新增 4 个常量**：`FREEBUFF_DEFAULT_MODEL_MIGRATION_ID`（"deepseek-v4-flash-2026-09-02"）、
    `FREEBUFF_MODEL_MISMATCH_MESSAGE`、`FREEBUFF_MUSE_SPARK_MODEL_IDS`、
    `FREEBUFF_SERVICE_ONLY_MODEL_IDS`；**删除 1 个**：`FREEBUFF_PAUSED_MODEL_NOTICE`
    （信息被并入 `FREEBUFF_TIER_CHANGE_NOTICE`）；
  - **default model 调整**：`DEFAULT_FREEBUFF_MODEL_ID = deepseek-v4-flash`（0.0.79 是
    glm-5.3-flash），`PREVIOUS_DEFAULT = glm-5.3-flash`；`FREEBUFF_DEFAULT_MODEL_MIGRATION_ID`
    用于把旧客户端存盘的 `glm-5.3-flash` 默认值一次性迁移到 `deepseek-v4-flash`；
  - **protocol 实体变更**：长任务跑到一半被 free session 回收会返回
    `SESSION_ENDED_MESSAGE` 完整英文（"Your free session ran out while this turn was
    running..."）—— 这是用户实际遇到的现象。4 个 GATE_CODE
    `waiting_room_required` / `session_expired` / `session_superseded` /
    `session_model_mismatch` 全部 `endsTheSession: !0`，统一折叠为 `session_ended`；
  - **错误分类**：`KIND_BY_FREEBUFF_STATUS` 新增 `premium_slot_taken → freebuff_concurrency`、
    `spend_limited → freebuff_quota`、`ip_capped → freebuff_quota`；客户端会自动
    `onFreebuffSessionExpired → admitFreebuffSession` + `CHECKPOINT_CONTINUATION_PROMPT` 重放；
  - **REWRITTEN_FAILURES**：仅 `context_overflow` 与 `connection`（与 0.0.79 一致），无新增；
  - **常量总数**：0.0.79 = 106 → 0.0.86 = 109（净 +3）。
- **对反代的修改**：
  - `notices.py` 新增 `session_ended` / `free session ran out` / `free session ended`
    三段关键词的中文软提示（"官方免费会话在本次对话运行中被回收，已保留此前输出，
    请直接重新发送一次以在新会话中继续"）—— 长任务场景不再被认成网络错误；
  - `app.py` 4 条 chat 路径（OpenAI 流式 / OpenAI 非流 / Anthropic 流 / Anthropic 非流）
    的空流重试条件扩展为同时识别 `session_ended` / `free session ran out` /
    `session_superseded` / `session_model_mismatch`，并把"清缓存"的触发码从仅 428
    扩展为 409/410/428（因为 `session_ended` 错误也可能落到 410）；
  - `tests/test_notices.py` 新增 2 个测试：`session_ended` 长任务英文原文与短码
    `Codebuff session_ended: 409` 均能命中软提示；
  - 不动 default model 反代默认（仍由 `DEFAULT_FREEBUFF_MODEL_ID` 决定，桌面端迁移
    仅是给老用户一次性更新存盘值，反代没有持久化历史，不受影响）。
- 测试：`tests/` 全套通过 255 passed（0.0.79 时的 251 + 0.0.86 新增 4 个）。

### 0b. 2026-09-08 桌面版 0.0.96 复核（模型池推导式 + slot-bound 并发体系 + gemini-3.8-flash）
- 状态：**已完成**（模型表 / slot-bound 桶判定 / turn_spend_limit 软提示，提交见本次 commit）
- 官方桌面版 0.0.96（09-08 21:50 安装，orchestrator.js 10,408,947 B）与 0.0.86 diff：
  - **新增 16 常量 / 删除 10 常量**：净 +6（0.0.86=109 → 0.0.96=115）。
    新增重点：`FREEBUFF_DESKTOP_CONCURRENCY_LIMITS`、`FREEBUFF_DESKTOP_SLOT_BOUND_MODEL_IDS`、
    `FREEBUFF_HERO_MODEL_ID`、`FREEBUFF_TURN_SPEND_LIMIT_ERROR_CODE/MESSAGE`、
    `FREEBUFF_PLAN_METERED_CATALOG_MODEL_IDS`、`FREEBUFF_PRO_ONLY_CATALOG_MODEL_IDS`、
    `FREEBUFF_UNTRACED_TRAINING_MODEL_IDS`、`FREEBUFF_GEMINI_38_FLASH`（模型）等。
    删除重点：**整个旧并发三件套** `FREEBUFF_DESKTOP_PREMIUM_BUCKET_MODEL_IDS` /
    `FREEBUFF_DESKTOP_SESSION_LIMITS` / `FREEBUFF_SUBSCRIBER_DESKTOP_SESSION_LIMITS`；
    `FREEBUFF_EARN_*` / `FREEBUFF_LEVELS` / `FREEBUFF_TRUST_CURRENCY_NAME`（earn 体系重构为 freebucks）。
  - **并发体系重构（桌面端）**：slot-bound vs multi-tab 两档。
    `FREEBUFF_DESKTOP_SLOT_BOUND_MODEL_IDS = [luna, gemini-3.8-flash, muse-spark-1.3,
    muse-spark-1.2]` 占 1 个 slot；`FREEBUFF_DESKTOP_CONCURRENCY_LIMITS =
    { free:{slot-bound:1, multi-tab:3}, subscriber:{slot-bound:3, multi-tab:8} }`。
    反代语义映射：slot-bound → 旧 premium（并发 1）；multi-tab → 旧 unlimited（并发 3）。
  - **模型池推导式**：`FREEBUFF_PREMIUM_MODEL_IDS` 改为 `FREEBUFF_MODELS.filter(model.premium)`
    → 推导结果 **[luna, muse-spark-1.2]**。solar-pro4 因
    `FREEBUFF_SOLAR_PRO_4_ENTITLEMENT.fullAccess.premium = !1` **掉出 premium**（归
    multi-tab 无限通道，Freebucks 常态 5 定价，Labor Day 0 价到 Sep 7 PT）。
  - **新模型**：`google/gemini-3.8-flash`（premium:true、multimodal、PRO_ONLY 目录、
    spend cap 0.5 —— 顶替 solar-pro4 的原 0.5 cap；agent base2-free-gemini-3-8-flash /
    code-reviewer-gemini-3-8-flash）；`meta/muse-spark-1.3-contributor`（premium:true、
    training、agent base2-free-muse-spark-1-3 / code-reviewer-muse-spark-1-3）。
  - **默认模型**：`DEFAULT_FREEBUFF_MODEL_ID = glm-5.3-flash`（0.0.86 曾迁到
    deepseek-v4-flash，2026-09-05 又迁回）；`PREVIOUS_DEFAULT = deepseek-v4-flash`。
  - **新错误** `turn_spend_limit`：单 turn 用量上限，会话**不结束**（isRetryable:false），
    官方提示 "send a new message to continue from here."。
  - **新响应/遥测头**：`X-Freebuff-Event-Id` / `X-Freebuff-Render-Delay-Ms`（first-party
    view ack）、`X-Freebuff-Dwell-Ms`（停留）—— 客户端→vack 网关，**不进 chat 上游头**，反代无关。
  - **协议核心未变**：SESSION_ENDED_MESSAGE / GATE_CODES / 45s 心跳 / 30min grace /
    600s idle release / codebuff_metadata（freebuff_instance_id + multi_session "1" +
    llm_step_number）全部与 0.0.86 一致。
- **对反代的修改**：
  - `models.py`：新增 gemini-3.8-flash / muse-spark-1.3 条目（agent 三映射齐）；
    solar-pro4 注释更新（premium → false）；UNLIMITED_SESSION_MODEL_IDS 兜底加
    solar-pro4；DEFAULT_MODEL 不变（glm-5.3-flash，0.0.96 官方一致）；
  - `model_registry.py`：desktop_bucket 解析源改为 `FREEBUFF_DESKTOP_SLOT_BOUND_MODEL_IDS`
    （新增常量），旧 `FREEBUFF_DESKTOP_PREMIUM_BUCKET_MODEL_IDS` 保留为兼容回退；
    premium 池 0.0.96 为推导式、静态解析拿不到 → 以 snapshot 承载 [luna, muse-1.2]；
  - `model_registry_snapshot.json`：**重写**（修复此前快照被污染——desktop_bucket 混入
    注释文本垃圾值）；premium=[luna, muse-1.2]；desktop_bucket(slot-bound)=
    [luna, gemini-3.8-flash, muse-1.3, muse-1.2]；新增 gemini-38/muse-13 模型条目；
  - `notices.py`：新增 `turn_spend_limit` 中文软提示（会话仍有效，引导发新消息继续）；
  - `tests/test_model_pools.py`：`test_dynamic_table_matches_current_upstream_premium` 重写
    为 0.0.96 语义（solar-pro4 → unlimited，gemini/muse → premium）；新增
    `test_dynamic_table_0_0_96_slot_bound_semantics` 验证回退链；
    `tests/test_notices.py`：新增 turn_spend_limit 长/短两个用例。
- 测试：`tests/` 全套通过 **257 passed**（0.0.86 的 255 + 0.0.96 新增 2）。

### 0c. 2026-09-13 桌面版 0.0.109 复核（session admission 新端点 + 新头）
- 状态：**已完成**（codebuff.py 端点/头更新 + 测试，提交见本次 commit）
- 官方桌面版 0.0.109（09-12 12:49 安装，orchestrator.js 10,488,815 B）与 0.0.96 diff：
  - **常量 115 → 122**（+7 / -0）：新增 `FREEBUFF_SESSION_ADMISSION_PATH`、
    `FREEBUFF_WALLET_SPEND_LIMIT_HEADER` / `FREEBUFF_DESKTOP_ATTEMPT_HEADER` /
    `FREEBUFF_DESKTOP_ADMITTED_AT_HEADER` / `FREEBUFF_PURCHASE_CONTINUITY_HEADER` /
    `FREEBUFF_SESSION_UNSUPPORTED_MESSAGE` / `FREEBUFF_BYOK_CONFIG_DIR`。
  - **会话创建端点迁移（协议级）**：`POST /api/v1/freebuff/session` →
    **`POST /api/v1/freebuff/session/admission`**（orchestrator.js 101042 /
    169480-169511 postSessionAdmission）。新头：
    - `x-freebuff-wallet-spend-limit: "0"`（无消费同意时 0，免费用户即 "0"）
    - `x-freebuff-purchase-continuity: "1"`（购买连续性）
    - `x-freebuff-desktop-attempt-id: <uuid>`（每次 admission 幂等尝试 id，重试沿用）
    - 404/405 → `server_error` + "SESSION_UNSUPPORTED_MESSAGE"（服务端未升级降级提示）
  - **DELETE /session 不变**，但无 attempt receipt 时新增 `x-freebuff-purchase-continuity: 1`
    （orchestrator.js 169556）；GET 心跳/refresh-tier 路径不变；
    新增 `GET /session?refundClaim=...`（退款，反代不需要）。
  - **额度/并发/模型层无变化**：FREEBUCKS_SESSION_PRICES、FREEBUCKS_PLANS、
    slot-bound 并发、premium 推导池、DEFAULT=glm-5.3-flash 全部与 0.0.96 一致。
  - **`SESSION_RETRY_AFTER_CAP_MS = 10000`**（0.0.96 是 3000）；
    **`FREEBUFF_DESKTOP_IDLE_RELEASE_MS = 900000`**（15 分钟，0.0.96 是 600000）。
  - **BYOK**：`~/.config/freebuff/byok/connections.json`（自带 key 模式，反代无关）。
- **对反代的修改**：
  - `codebuff.py`：create_session POST 路径 → `/admission` + 3 新头（attempt-id 复用
    于 premium_slot_taken 重试）；delete_session 加 purchase-continuity；
  - `tests/test_codebuff_client.py`：POST 断言更新；
- 测试：定向 25 passed；全套待确认。

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

# 项目交接文档（HANDOFF）

> 更新：2026-08-26 | 分支：`main` | 最新提交：`83e3c8f` → 待本次提交

## 1. 项目是什么
将 Freebuff/Codebuff 的免费模型（DeepSeek V4 Pro/Flash、Luna、MiniMax M3、GLM 等）转成标准 OpenAI Chat Completions / Anthropic Messages API 的反代服务。

- 仓库：`darkest-x/freebuff2apiNew-fork`
- 技术栈：Python 3.11 + FastAPI + httpx；前端 React 19 + TypeScript + Vite
- 部署：Railway（生产）/ 本地 / Vercel

## 2. 核心架构
- 每个上游账号（`FREEBUFF_TOKEN` 逗号分隔）有两条串行通道：
  - premium 通道 1 条（Pro/Luna/MiniMax/GLM 等）
  - unlimited 通道 1 条（Flash/Mimo）
- 两条通道独立锁，互不阻塞、互不删除；同通道内请求串行复用 session
- 账号池按 `rotation_mode` 选号（throughput / balanced / conservative）
- 动态模型注册表每 6h 从 GitHub/jsDelivr 拉取，本地快照兜底，硬编码表最终兜底

## 3. 额度与 session（官方 2026-08-26 确认）
- 扣费按 session 创建计，不是按对话条数
- 并发桶：`FREEBUFF_DESKTOP_SESSION_LIMITS = {premium: 1, unlimited: 3}`（非每日额度）
- 2026-08-26：`FREEBUFF_PER_MODEL_SESSION_CAPS` 已移除，配额全靠服务端 `rateLimitsByModel` 下发
- 2026-08-26：flash 又被拨回 unlimited 语义（`FREEBUFF_PREMIUM_MODEL_IDS=[luna,pro]`）
- 太平洋日重置 = 北京时间 15:00
- session 跨 15:00 不会中断，按自身 expiresAt 存活；15:00 后新建才用新额度
- 免费/unlimited 池：Flash/Mimo/Ox-Alpha，官方并发上限 3（我们只开 1 条保守）

## 4. 协议对齐（0.0.63）
已对齐官方桌面端（通过 Anything Analyzer 会话 123 抓包对比）：
- session POST/GET/DELETE 带 `x-freebuff-multi-session: 1`
- chat 不发送 `x-freebuff-instance-id` 头
- chat UA：`ai-sdk/openai-compatible/0.0.0-test/codebuff ai-sdk/provider-utils/3.0.25 runtime/bun/1.3.14`
- system prompt 开头：`You are Buffy, the coding agent behind Codebuff...`
- `codebuff_metadata` 字段：`freebuff_instance_id`、`freebuff_multi_session`、`trace_session_id`、`run_id`、`client_id`、`cost_mode`、`freebuff_reasoning_effort`（可选）、`llm_step_number`（每 run 递增）
- `provider`: `{"data_collection":"deny"}`
- `stop`: `["\"cb_easp\""]`
- reasoning_effort：支持模型（Pro/Flash/Luna/Fable/Muse）合法值放行，非法回退默认；不支持模型不发送

## 5. 三个轮换模式
| 模式 | unlimited | premium | ban 后 |
|:---|:---|:---|:---|
| throughput | 全部账号扇出 | 全部账号扇出 | 原行为 |
| balanced | 全部账号扇出 | 全局只 1 个，正常 429 才轮换 | premium 停到下个 15:00 |
| conservative（默认） | 只用第 1 个账号 | 同上 | premium 停到下个 15:00 |

- 正常额度：429 `rate_limited` → 冷却账号/模型，premium 轮换
- 账号 ban：403 / `banned` / `country_blocked` → 标记 invalid，premium 全局停到下个 15:00
- Policy Violation：**只封当前模型**到下个 15:00，不封账号（Luna 官方问题）
- Provider usage error（402 + refill）：**不封账号**，透传友好错误给客户端
- insufficient_quota 429：**不封账号**（是上游负载饱和，不是额度耗尽）

## 6. 请求体保护与工具指纹（2026-08-25 官方 27 工具骨架 + 客户端混入）
- 官方桌面端发送 ~27 个工具（base3 8 + DESKTOP_EXTRA 4 + THREAD_TOOL_SPECS 13 + MCP 网关 2）
- 我们的 `rewrite_tools_for_upstream`：优先注入官方 27 工具骨架（带简化 schema），
  客户端工具重命名后追加（防冲突）；MCP 网关 `search_mcp_tools`/`call_mcp_tool` 保留
- `FREEBUFF_MAX_REQUEST_BODY_BYTES` 默认 2097152（2MB）
- `FREEBUFF_MAX_TOOLS` 启动时从官方 27 算起，MCP 网关不变，重命名后的客户端工具在上限内保留
- 超过工具数上限：只保留前 N 个，不报错
- 超过请求体：413 `request_body_too_large`，并写入日志
- `FREEBUFF_MAX_MESSAGES` 默认 0（不限制），因为截断会导致模型丢上下文反复读文件

## 7. 会话保活与自动恢复
- **心跳保活**：流式响应期间每 45 秒发一次心跳（`x-freebuff-heartbeat: 1`），防止服务器 30 分钟无心跳回收 session
- **session 到期自动恢复**：遇到 `session_expired`(410) / `waiting_room_required`(428) / `session_superseded`(409) / 空流时，如果还没给客户端发内容，自动重建 session + run 重试一次
- **premium_slot_taken 恢复**：重启后旧实例占槽 → 提取 `currentInstanceId` → DELETE 旧实例 → 重试创建
- 流式/非流式、OpenAI/Anthropic 四个路径都覆盖

## 8. 已知问题与修复
| 问题 | 现象 | 修复 |
|:---|:---|:---|
| premium_slot_taken | 重启后无法创建 session，客户端重试 | 提取 currentInstanceId → DELETE → 重试 |
| 空流 | 上游 200 但无内容 | 重建 session+run 重试一次；仍失败返回友好错误 |
| 520 | 官方上游服务器崩溃（Render） | 不是模型上下文限制；请求体保护 + 工具上限降低风险 |
| Luna Policy Violation | 上游 Azure/OpenAI 策略封禁 | 只封 Luna 模型，不封账号 |
| Provider usage error | 官方 credit 耗尽 | 返回 402 + 友好提示，不封账号 |
| insufficient_quota 429 | 上游负载饱和 | 不封账号，返回友好提示 |
| 消息截断导致慢 | 截断到 100 条导致模型丢上下文 | 默认不截断（0 = 不限制） |
| 日志洪水 | 流式 chunk 逐条记录 | `FREEBUFF_LOG_STREAM_CHUNKS` 默认 false |
| 30 分钟断连 | 没发心跳 | 流式响应期间 45 秒心跳 |

## 9. 抓包方法
- Anything Analyzer 已安装：`D:\AppHub\Network\Anything Analyzer\Anything Analyzer.exe`
- 官方 EXE 的 chat/session 请求由 Bun 进程发，不走系统代理
- 正确姿势：Anything Analyzer MITM 代理 + Proxifier 强制 `bun.exe` 走代理
- 已有抓包会话：Anything Analyzer 会话 123
- 或者直接用 `orchestrator.js` 源码分析，不需要抓包

## 10. 恢复上下文检查清单
1. 读本文件
2. 读 `../AGENTS.md`
3. 读 `../SKILL.md`
4. 读 `../TODO.md`
5. 检查 git 分支：`git -C ../freebuff2apiNew-fork status -sb`
6. 检查最新日志：`../log.txt`
7. 官方客户端源码：`/mnt/c/Users/Administrator/AppData/Local/Programs/@codebufffreebuff-desktop/resources/orchestrator/orchestrator.js`
8. 跑测试：`cd ../freebuff2apiNew-fork && PYTHONPATH=/home/ovo/workspace/revew/.pipdeps:. python3 -m pytest -q`

## 11. 关键环境变量速查
| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `FREEBUFF_ROTATION_MODE` | `conservative` | 轮换模式 |
| `FREEBUFF_MAX_REQUEST_BODY_BYTES` | `2097152` | 请求体上限 |
| `FREEBUFF_MAX_TOOLS` | `50` | 工具数上限 |
| `FREEBUFF_MAX_MESSAGES` | `0` | 消息数上限（0=不限制） |
| `FREEBUFF_LOG_STREAM_CHUNKS` | `false` | 是否逐块记录 SSE |
| `FREEBUFF_EMPTY_STREAM_TIMEOUT` | `120` | 空流超时（秒） |
| `FREEBUFF_ADMIN_LOG_LINES` | `1000` | 内存日志保留条数 |

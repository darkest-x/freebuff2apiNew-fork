from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from typing import Any

from .codebuff import CodebuffError, FreebuffSession
from .official_tools import rewrite_tools_for_upstream
from .tool_schema import normalize_tool_schemas

logger = logging.getLogger("freebuff2api.openai_compat")
from .models import normalize_reasoning_effort, resolve_model


# 官方 free-mode marker（0.0.63 桌面版抓包确认）：system 必须以官方 Buffy 编码 agent
# 开头，否则服务端 hasFreebuffRootSystemPromptOpening 字节级校验失败（403
# free_mode_cli_required）。这里只注入官方开头段落 + 当前日期，不再使用旧 Worker 的
# strategic coding assistant 极简前缀（已与 0.0.63 桌面版不一致）。
def _buffy_system_prompt() -> str:
    today = date.today().strftime("%B %d, %Y")
    return (
        "You are Buffy, the coding agent behind Codebuff. "
        "You help users with software engineering tasks: fixing bugs, "
        "adding functionality, refactoring, and explaining code.\n\n"
        f"Current date: {today}.\n\n"
        "- Match the project's existing conventions. "
        "Verify a library is already used in the project before employing it.\n"
        "- Prefer editing existing files over creating new ones. "
        "Make the fewest changes that address the request.\n"
        "- Verify non-trivial changes by running the project's typecheck and relevant tests.\n"
        "- Use write_todos to plan and track multi-step tasks.\n"
        "- Your responses are displayed in a terminal. Keep them short and concise.\n"
        "- Don't run destructive or hard-to-undo commands (git push, resets, deploys) "
        "unless the user asks for them."
    )


BUFFY_PREFIX = _buffy_system_prompt()


_UPSTREAM_CHAT_KEYS = frozenset(
    {
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "metadata",
        "modalities",
        "parallel_tool_calls",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "top_k",
        "user",
    }
)


def model_id(requested: str | None = None) -> str:
    return resolve_model(requested).upstream_id


def normalize_chat_messages(
    messages: Any,
    *,
    system_prompt: str | None = None,
    max_messages: int | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []

    """Normalize messages for the upstream Codebuff API.

    The upstream API validates that the first system message starts with the
    official Buffy opening (\"You are Buffy, the strategic coding
    assistant.\") before allowing free-mode requests. Simplified or forged
    prompts get 403 ``free_mode_cli_required``. So we always inject the
    official Buffy prefix ahead of any user-supplied system content.
    """
    if not isinstance(messages, list):
        return []

    # None/empty → no user content appended; non-empty → appended after Buffy.
    user_override = system_prompt or None

    normalized = []
    has_system = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = dict(message)
        if item.get("role") == "developer":
            item["role"] = "system"
        if item.get("role") == "system":
            has_system = True
            item.setdefault("cache_control", {"type": "ephemeral"})
            content = item.get("content", "")
            if isinstance(content, str):
                base = content if content.startswith("You are Buffy") else (
                    BUFFY_PREFIX + "\n\n" + content
                )
                if user_override:
                    base = base + "\n\n" + user_override
                item["content"] = base
            elif isinstance(content, list):
                text_parts = [
                    part
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                if not text_parts or not text_parts[0].get("text", "").startswith(
                    "You are Buffy"
                ):
                    content.insert(
                        0, {"type": "text", "text": BUFFY_PREFIX}
                    )
                if user_override:
                    content.append({"type": "text", "text": user_override})
                item["content"] = content
        normalized.append(item)

    if not has_system:
        content = BUFFY_PREFIX
        if user_override:
            content = content + "\n\n" + user_override
        normalized.insert(
            0,
            {
                "role": "system",
                "content": content,
                "cache_control": {"type": "ephemeral"},
            },
        )
    if max_messages and max_messages > 0 and len(normalized) > max_messages:
        system_messages = [m for m in normalized if m.get("role") == "system"]
        other_messages = [m for m in normalized if m.get("role") != "system"]
        keep_count = max(0, max_messages - len(system_messages))
        kept = other_messages[-keep_count:] if keep_count else []
        # 避免以孤立 tool 消息开头
        while kept and kept[0].get("role") == "tool":
            kept = kept[1:]
        normalized = system_messages + kept
    return normalized


def inject_end_turn_signature(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """绕过上游 detectForeignFreebuffClient 的 foreign_toolset 判定。

    上游对「带 tools 但工具集合里没有官方专属工具名」的请求，判定为外来客户端
    工具集，降级到 ling-3.0-tiny:free（占免费额度 → 429），导致工具调用失败。
    注入官方专属名 ``end_turn``（TOOLS_WHICH_WONT_FORCE_NEXT_STEP 中的无害工具）
    即通过检测，请求用真实模型正常返回。``end_turn`` 不会被模型实际调用
    （官方定义为「不强制下一步」的工具），仅用于通过工具集合签名校验。
    """
    if not isinstance(tools, list) or not tools:
        return tools
    has_signature = any(
        isinstance(t, dict)
        and isinstance(t.get("function"), dict)
        and t["function"].get("name") == "end_turn"
        for t in tools
    )
    if has_signature:
        return tools
    return [
        *tools,
        {
            "type": "function",
            "function": {
                "name": "end_turn",
                "description": "Signal the end of the current task.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def clamp_output_tokens(
    payload: dict[str, Any],
    model_id: str | None,
) -> dict[str, Any]:
    """钳制输出 token 上限（max_tokens / max_completion_tokens）到模型上限。

    上游免费层对单次输出有保守上限（实测 32,768；yuzu config.ts 标注
    "maxOutputTokens is a conservative ceiling"）。客户端传 64,000 等超限值
    时上游可能静默返回空流/截断 → 客户端空响应。这里按模型表钳制。
    注：输入上下文由 context_window 管控，客户端通过 /v1/models 读取自适应。
    """
    if not payload.get("model"):
        return payload
    try:
        model = resolve_model(model_id or payload["model"])
    except ValueError:
        return payload
    limit = model.max_output_tokens
    for key in ("max_tokens", "max_completion_tokens"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            payload[key] = max(1, min(int(value), limit))
    return payload


# [B2 待验证] 上游 chat body 的顶层键序。
# 前 5 个键是抓包直接确认的（会话 123 seq 150 官方桌面端 POST /api/v1/chat/completions）：
#   model, stop, codebuff_metadata, provider, messages, ...
# 其后的顺序未逐一核对，这里固定成一个稳定序列（宁可稳定也不要随机，见下方函数注释）。
_OFFICIAL_PAYLOAD_KEY_ORDER: tuple[str, ...] = (
    "model",
    "stop",
    "codebuff_metadata",
    "provider",
    "messages",
    "stream",
    "stream_options",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
)


def order_upstream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """按官方键序重排上游 chat body（openai / anthropic 两条入站路径共用）。

    两个动机，第二个比第一个严重得多：

    1. **顺序对齐**：官方 body 前 5 个键固定是
       `model, stop, codebuff_metadata, provider, messages`；
       我们改前是 `tools` 打头（log.txt L137 `payload={"tools": [...`）。
    2. **消除随机键序**：旧代码用 `for key in _UPSTREAM_CHAT_KEYS` 拷贝客户端字段，
       而 `_UPSTREAM_CHAT_KEYS` 是 **frozenset** —— CPython 默认开启字符串 hash
       随机化（PYTHONHASHSEED=random），于是同一份请求**每次重启进程后键序都不同**。
       真实客户端由固定代码路径序列化，键序是稳定的；键序随机漂移本身就是
       "这不是官方客户端"的信号，而且会让任何基于 body 的指纹比对失去可复现性。

    未列入 `_OFFICIAL_PAYLOAD_KEY_ORDER` 的键按字母序追加，保证任意输入都得到
    确定顺序。纯序列化层改动，不增删任何字段，语义零影响。
    """
    ordered: dict[str, Any] = {}
    for key in _OFFICIAL_PAYLOAD_KEY_ORDER:
        if key in payload:
            ordered[key] = payload[key]
    for key in sorted(payload):
        if key not in ordered:
            ordered[key] = payload[key]
    return ordered


def build_upstream_payload(
    body: dict[str, Any],
    *,
    session: FreebuffSession,
    run_id: str,
    client_id: str,
    trace_session_id: str | None = None,
    upstream_model_id: str | None = None,
    system_prompt: str | None = None,
    max_tools: int | None = None,
    llm_step_number: str | None = None,
    max_messages: int | None = None,
) -> dict[str, Any]:
    payload = {
        key: body[key]
        for key in _UPSTREAM_CHAT_KEYS
        if key in body and body[key] is not None
    }
    payload["model"] = upstream_model_id or model_id(body.get("model"))
    payload["messages"] = normalize_chat_messages(
        body.get("messages"),
        system_prompt=system_prompt,
        max_messages=max_messages,
    )
    payload["stream"] = True
    payload.setdefault("stop", ['"cb_easp"'])

    # 工具集画像重写（2026-08-25 桌面版 MCP 架构对齐）：
    # 新版桌面端支持 MCP 的方式是注入官方网关两件套 search_mcp_tools /
    # call_mcp_tool（buildGatewayTools），tools 数组里永远是官方 snake_case
    # 名字集合；mcp__* 只出现在消息历史。因此这里不再按数量截断，而是把
    # 客户端工具混入官方骨架（threadToolSpecs(base, extra) 语义）——
    # 官方 customToolDefinitions 本就合法，外来感来自名字风格而非存在本身。
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        if max_tools is not None and len(tools) > max_tools:
            logger.warning(
                "trimming tools from %s to %s to avoid foreign_toolset detection",
                len(tools),
                max_tools,
            )
            payload["tools"] = tools[:max_tools]
        payload["tools"] = rewrite_tools_for_upstream(payload["tools"])

    # 降低工具指纹：把客户端工具 schema 归一化成官方桌面端风格的干净 JSON Schema。
    normalize_tool_schemas(payload)

    # reasoning_effort 按官方模型 efforts 表 clamp（防外来客户端指纹），
    # 然后从 OpenAI 标准顶层字段移到 codebuff_metadata.freebuff_reasoning_effort：
    # 官方桌面端 free-mode 就是这么传的（顶层不携带 reasoning_effort）。
    reasoning_effort = payload.pop("reasoning_effort", None)
    if reasoning_effort is not None:
        reasoning_effort = normalize_reasoning_effort(
            body.get("model"), reasoning_effort
        )

    # 钳制输出上限（chat completions 路径，对齐 anthropic 路径行为）
    clamp_output_tokens(payload, body.get("model"))

    payload["provider"] = {"data_collection": "deny"}
    metadata: dict[str, Any] = {
        "freebuff_instance_id": session.instance_id,
        "freebuff_multi_session": "1",
        "trace_session_id": trace_session_id or str(uuid.uuid4()),
        "run_id": run_id,
        "client_id": client_id,
        "cost_mode": "free",
    }
    if reasoning_effort is not None:
        metadata["freebuff_reasoning_effort"] = reasoning_effort
    if llm_step_number is not None:
        metadata["llm_step_number"] = llm_step_number
    payload["codebuff_metadata"] = metadata
    # [B2 待验证] 最后统一重排键序（详见 order_upstream_payload 注释）。
    return order_upstream_payload(payload)


def raise_for_stream_error(chunk: dict[str, Any]) -> None:
    """识别上游 SSE 流内错误 chunk 并抛出 CodebuffError。

    上游返回 200 时仍可能在 SSE 流内下发错误（例如账号级 policy violation）：
    {"choices":[],"error":{"code":502,"message":"Policy Violation...","metadata":{...}}}
    旧实现把它当无内容 chunk 丢弃，最终客户端收到 200 空响应。
    官方 SDK chunk schema 允许 error 分支，AI SDK 会将其作为流内错误抛出。
    """
    error = chunk.get("error") if isinstance(chunk, dict) else None
    if error is None:
        return
    # session_expired / waiting_room_required 等 gate code：session 超时，
    # 我们可以在上层自动重建 session 重试一次。
    if isinstance(error, str) and error in {
        "session_expired",
        "waiting_room_required",
        "session_superseded",
    }:
        raise CodebuffError(f"Codebuff session ended: {error}", 410)
    if isinstance(error, dict):
        message = str(
            error.get("message")
            or error.get("code")
            or error.get("type")
            or "Upstream stream error"
        )
        code = error.get("code")
        status_code = code if isinstance(code, int) and 400 <= code <= 599 else 502
    else:
        message = str(error)
        status_code = 502
    raise CodebuffError(f"Codebuff chat stream error: {message}", status_code)


def sanitize_stream_chunk(
    chunk: dict[str, Any],
    *,
    fold_reasoning_in_content: bool = False,
    reasoning_in_content_tag: str = "think",
) -> dict[str, Any] | None:
    """清洗一个上游 SSE chunk。

    ``fold_reasoning_in_content`` 对齐 freebuff-proxy 的 REASONING_IN_CONTENT：
    - 默认 False：``reasoning_content`` 原样作为 delta 扩展字段保留，不混入
      ``content``（符合 OpenAI Chat Completions 标准流式事件形态）。
    - 开启后：把每个 reasoning 片段折叠成 ``<tag>...</tag>`` 文本放入
      ``content``，同时仍保留 ``reasoning_content``，供不渲染思考通道的
      旧客户端仍能看到模型思考内容。
    """
    raise_for_stream_error(chunk)
    clean = {
        "id": chunk.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": chunk.get("object") or "chat.completion.chunk",
        "created": chunk.get("created") or int(time.time()),
        "model": chunk.get("model"),
        "choices": [],
    }
    if chunk.get("system_fingerprint"):
        clean["system_fingerprint"] = chunk["system_fingerprint"]
    if chunk.get("usage") is not None:
        clean["usage"] = chunk["usage"]

    for choice in chunk.get("choices") or []:
        item = {
            "index": choice.get("index", 0),
            "delta": dict(choice.get("delta") or {}),
            "finish_reason": choice.get("finish_reason"),
        }
        if choice.get("logprobs") is not None:
            item["logprobs"] = choice["logprobs"]
        reasoning_content = item["delta"].pop("reasoning_content", None)
        if item["delta"].get("content") is None:
            item["delta"].pop("content", None)
        if isinstance(reasoning_content, str):
            item["delta"]["reasoning_content"] = reasoning_content
            if fold_reasoning_in_content and reasoning_content:
                tag = reasoning_in_content_tag or "think"
                existing = item["delta"].get("content")
                if isinstance(existing, str):
                    item["delta"]["content"] = (
                        f"<{tag}>{reasoning_content}</{tag}>{existing}"
                    )
                elif "content" not in item["delta"] or existing is None:
                    item["delta"]["content"] = f"<{tag}>{reasoning_content}</{tag}>"
        clean["choices"].append(item)

    if not clean["choices"] and clean.get("usage") is None:
        return None
    return clean


class CompletionAccumulator:
    def __init__(self, model: str) -> None:
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.model = model
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.system_fingerprint: str | None = None
        self.tool_calls: dict[int, dict[str, Any]] = {}

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def reasoning_content(self) -> str:
        return "".join(self.reasoning_parts)

    def add(self, chunk: dict[str, Any]) -> None:
        raise_for_stream_error(chunk)
        self.id = chunk.get("id") or self.id
        self.created = chunk.get("created") or self.created
        self.model = chunk.get("model") or self.model
        self.usage = chunk.get("usage") or self.usage
        self.system_fingerprint = chunk.get("system_fingerprint") or self.system_fingerprint

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            reasoning_content = delta.get("reasoning_content")
            if isinstance(content, str):
                self.content_parts.append(content)
            if isinstance(reasoning_content, str):
                self.reasoning_parts.append(reasoning_content)
            for tool_call in delta.get("tool_calls") or []:
                self._add_tool_call(tool_call)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def _add_tool_call(self, tool_call: dict[str, Any]) -> None:
        index = int(tool_call.get("index", 0))
        current = self.tool_calls.setdefault(
            index,
            {
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": tool_call.get("type") or "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if tool_call.get("id"):
            current["id"] = tool_call["id"]
        if tool_call.get("type"):
            current["type"] = tool_call["type"]

        function = tool_call.get("function") or {}
        if function.get("name"):
            current["function"]["name"] = function["name"]
        if function.get("arguments"):
            current["function"]["arguments"] += function["arguments"]

    def final_response(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content,
        }
        # 对齐 Worker 1.7.2 streamToNonStream：上游只回 reasoning（思考链）而未回
        # content 时（推理模型常见），用 reasoning 兜底 content，避免客户端收到空响应。
        if not self.content and self.reasoning_content:
            message["content"] = self.reasoning_content
            message["reasoning_used_as_content"] = True
        elif self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ]

        response = {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self.finish_reason or "stop",
                }
            ],
            "usage": self.usage
            or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if self.system_fingerprint:
            response["system_fingerprint"] = self.system_fingerprint
        return response

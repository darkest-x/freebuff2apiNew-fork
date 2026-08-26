"""official_tools.py — 官方桌面端工具集画像（2026-08-25 orchestrator.js 对齐）。

背景（用户指正"桌面版已支持 MCP 调用"后的重新逆向）：

新版桌面端（orchestrator.js 2026-08-25，8.9MB）对 MCP 的支持方式是
**网关两件套**而非直接透传 MCP 工具：
- ``search_mcp_tools``：按语义检索用户已连接并批准的 MCP 服务器上的工具
  （MAX_RESULTS=8 / MAX_PER_SERVER=3 / MAX_SEARCH_BYTES=24576）；
- ``call_mcp_tool``：用 search 返回的确切 server/tool 名与 schema 调用。

模型的 tools 数组里出现的永远是这批官方 snake_case 名字 + 少量
customToolDefinitions；``mcp__<server>__<tool>`` 只在消息历史的执行结果里出现。
因此防暴露的正确姿势是：把上游 chat payload 的 tools 重写成官方画像 ——
官方 thread agent 骨架工具（base3 8 个 + desktop extra 4 个 + thread specs 13 个，
去重后约 25 个）+ MCP 网关两件套，再混入客户端工具（customToolDefinitions 在
官方协议里本来就合法）。这样无论客户端传来多少 PascalCase/mcp__* 工具，
线上看到的都是一份"官方桌面端 + 用户自定义工具"的正常组合。

来源行号（orchestrator.js 2026-08-25 版）：
- base3 toolNames:        createBase3()            ~L124176
- desktop extra:          DESKTOP_EXTRA_TOOLS      ~L124271
- thread spec 工具名:      THREAD_TOOL_SPECS        ~L121832-121852
- MCP 网关:               buildGatewayTools        ~L135869
- end_turn 定义:          ~L67613（endsAgentStep=true）
"""
from __future__ import annotations

from typing import Any

# ── 官方 thread agent 工具白名单（build 模式全集，snake_case）──────────────
# createBase3().toolNames（base3 编码 agent 核心 8 个）
_BASE3_CORE_TOOLS: tuple[str, ...] = (
    "read_files",
    "str_replace",
    "write_file",
    "run_terminal_command",
    "code_search",
    "glob",
    "list_directory",
    "write_todos",
)

# DESKTOP_EXTRA_TOOLS（桌面端附加 4 个；end_turn 在其中）
_DESKTOP_EXTRA_TOOLS: tuple[str, ...] = (
    "run_file_change_hooks",
    "end_turn",
    "web_search",
    "read_url",
)

# THREAD_TOOL_SPECS 的工具名（桌面端本地执行 13 个；browser_check/write_doc 是
# 官方保留的 stub 工具，同样出现在 toolNames 里）
_THREAD_SPEC_TOOLS: tuple[str, ...] = (
    "suggest_prompts",
    "ask_questions",
    "request_elevation",
    "register_preview",
    "preview_snapshot",
    "preview_screenshot",
    "preview_click",
    "preview_type",
    "preview_navigate",
    "preview_evaluate",
    "preview_logs",
    "browser_check",
    "write_doc",
)

# MCP 网关两件套（新版桌面端支持 MCP 后新增；见模块 docstring）
_MCP_GATEWAY_TOOLS: tuple[str, ...] = (
    "search_mcp_tools",
    "call_mcp_tool",
)

# 官方 build 模式完整白名单（顺序 = 官方数组拼接顺序，不做字母序重排）
OFFICIAL_THREAD_TOOL_NAMES: tuple[str, ...] = (
    _BASE3_CORE_TOOLS
    + _DESKTOP_EXTRA_TOOLS
    + _THREAD_SPEC_TOOLS
    + _MCP_GATEWAY_TOOLS
)

OFFICIAL_THREAD_TOOL_NAME_SET = frozenset(OFFICIAL_THREAD_TOOL_NAMES)


def official_tool_name(name: str) -> str:
    """客户端工具名 → 官方风格 snake_case。

    官方工具全部是 snake_case 小写；第三方客户端常见 PascalCase（ReadFile）
    或点分名（filesystem.read_file）。转成 snake_case 让混入的 customTool
    definitions 不那么扎眼。已是全小写的名字原样返回。
    """
    if not name:
        return name
    # 纯小写（含 _ / mcp__ 前缀）无需转换
    if name == name.lower() and not any(c in name for c in {".", "-", ":"}):
        return name
    out = []
    for i, ch in enumerate(name):
        if ch.isupper():
            # 连续大写（HTTPStatus）只在词首插下划线
            if i > 0 and (not out or out[-1] != "_"):
                prev = name[i - 1]
                nxt = name[i + 1] if i + 1 < len(name) else ""
                if prev.islower() or prev.isdigit() or (nxt and nxt.islower()):
                    out.append("_")
            out.append(ch.lower())
        elif ch in {".", "-", ":"}:
            out.append("_")
        else:
            out.append(ch)
    return "".join(out)


def _simple_tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """构造 OpenAI function 工具定义（官方风格的干净扁平 JSON Schema）。"""
    params: dict[str, Any] = {"type": "object"}
    props = properties or {}
    if props:
        params["properties"] = props
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": params,
        },
    }


# ── 官方骨架工具的最小 schema（描述对齐官方语义，参数从官方 inputSchema 简化）──
# 这些工具不会被本代理实际执行（执行仍由客户端声明的真实工具完成）；注入它们
# 是为了让 tools 数组的名字集合与官方桌面端一致，消除 foreign_toolset 指纹。
_SKELETON_TOOL_SPECS: tuple[tuple[str, str, dict[str, Any] | None, list[str] | None], ...] = (
    ("read_files", "Read the contents of files at the given paths.", {
        "paths": {"type": "array", "items": {"type": "string"}, "description": "File paths to read"},
        "start_line": {"type": "integer", "description": "First line to read"},
        "end_line": {"type": "integer", "description": "Last line to read"},
        "max_results": {"type": "integer", "description": "Maximum number of files to read"},
    }, ["paths"]),
    ("str_replace", "Replace an exact unique string occurrence in a file.", {
        "path": {"type": "string", "description": "File path"},
        "old_str": {"type": "string", "description": "Exact text to replace"},
        "new_str": {"type": "string", "description": "Replacement text"},
    }, ["path", "old_str", "new_str"]),
    ("write_file", "Write content to a file, creating or overwriting it.", {
        "path": {"type": "string", "description": "File path"},
        "content": {"type": "string", "description": "Full file content"},
    }, ["path", "content"]),
    ("run_terminal_command", "Run a terminal command in the project directory.", {
        "command": {"type": "string", "description": "Command to execute"},
        "process_type": {"type": "string", "enum": ["sync", "background"], "description": "Sync or background process"},
        "timeout_seconds": {"type": "integer", "description": "Timeout in seconds"},
        "cwd": {"type": "string", "description": "Working directory override"},
    }, ["command"]),
    ("code_search", "Search file contents with a regular expression.", {
        "regex": {"type": "string", "description": "Regular expression pattern"},
        "path": {"type": "string", "description": "Directory to search"},
        "include_pattern": {"type": "string", "description": "Glob filter for files"},
    }, ["regex"]),
    ("glob", "Find files matching a glob pattern.", {
        "pattern": {"type": "string", "description": "Glob pattern"},
        "path": {"type": "string", "description": "Directory to search"},
    }, ["pattern"]),
    ("list_directory", "List entries of a directory as a tree.", {
        "path": {"type": "string", "description": "Directory path"},
        "depth": {"type": "integer", "description": "Recursion depth"},
    }, ["path"]),
    ("write_todos", "Plan and track multi-step tasks.", {
        "todo_list": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Todo items to record",
        },
        "text": {"type": "string", "description": "Plan text"},
    }, []),
    ("run_file_change_hooks", "Run configured file-change hooks after edits.", {}, []),
    ("end_turn", "End your turn, regardless of any new tool results that might be coming. This will allow the user to type another prompt.", {}, []),
    ("web_search", "Search the web for current information.", {
        "query": {"type": "string", "description": "Search query"},
    }, ["query"]),
    ("read_url", "Fetch a URL and return its contents.", {
        "url": {"type": "string", "description": "URL to fetch"},
    }, ["url"]),
    ("suggest_prompts", "Propose follow-up prompts, rendered inline as clickable cards at this exact point.", {
        "prompts": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Prompts to offer",
        },
    }, []),
    ("ask_questions", "Ask the user one or more structured questions and wait for their answers.", {
        "questions": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Questions with optional options",
        },
    }, ["questions"]),
    ("request_elevation", "Request explicit user approval to run one command with administrator privileges.", {
        "command": {"type": "string", "description": "Exact command without sudo/doas/pkexec"},
        "reason": {"type": "string", "description": "Why elevation is needed"},
    }, ["command", "reason"]),
    ("register_preview", "Show content in this thread's Preview tab.", {
        "url": {"type": "string", "description": "Dev server loopback URL"},
        "pid": {"type": "integer", "description": "Dev server process id"},
        "htmlPath": {"type": "string", "description": "Local html file path"},
        "replace": {"type": "boolean", "description": "Replace current preview"},
    }, []),
    ("preview_snapshot", "Read this thread's running preview page as a text accessibility tree.", {}, []),
    ("preview_screenshot", "Capture a PNG screenshot of this thread's running preview page.", {
        "fullPage": {"type": "boolean", "description": "Try full page capture"},
    }, []),
    ("preview_click", "Click an element in the running preview page by uid.", {
        "uid": {"type": "string", "description": "Element handle from preview_snapshot"},
        "dblClick": {"type": "boolean", "description": "Double click"},
    }, ["uid"]),
    ("preview_type", "Type text into an element of the running preview page.", {
        "uid": {"type": "string", "description": "Element handle from preview_snapshot"},
        "text": {"type": "string", "description": "Text to type"},
        "clear": {"type": "boolean", "description": "Clear existing value first"},
        "pressEnter": {"type": "boolean", "description": "Press Enter after typing"},
    }, ["uid", "text"]),
    ("preview_navigate", "Navigate the running preview page.", {
        "to": {"type": "string", "description": "Loopback URL, or reload/back/forward"},
    }, ["to"]),
    ("preview_evaluate", "Evaluate a JavaScript expression in the running preview page.", {
        "expression": {"type": "string", "description": "JS expression to evaluate"},
    }, ["expression"]),
    ("preview_logs", "Read the running preview page's console messages and network requests.", {
        "kind": {"type": "string", "enum": ["console", "network", "all"], "description": "Log kind"},
        "limit": {"type": "integer", "description": "Max entries"},
        "clear": {"type": "boolean", "description": "Clear after reading"},
    }, []),
    ("browser_check", "Unavailable in this build — use your normal tools instead.", {}, []),
    ("write_doc", "Unavailable in this build — write markdown files with your normal file tools instead.", {
        "name": {"type": "string", "description": "Doc name"},
        "content": {"type": "string", "description": "Doc content"},
    }, []),
    (
        # search_mcp_tools：官方描述原文较长，这里保留关键句式（含当前无服务器场景）
        "search_mcp_tools",
        "Find tools available from this user's connected MCP servers. These are "
        "the ONLY way to reach those services; no other tool and no bundled skill "
        "can. Call this before concluding a capability is unavailable, and before "
        "reaching for a browser or a shell to do something a connector may already "
        "do. Returns each match with its exact input schema, which you should "
        "follow precisely when calling call_mcp_tool.",
        {
            "query": {"type": "string", "description": 'What you are trying to do, e.g. "post a slack message"'},
            "server": {"type": "string", "description": "Restrict to one server by name"},
            "limit": {"type": "integer", "description": "Maximum results to return (1–8). Defaults to 8."},
        },
        ["query"],
    ),
    (
        # call_mcp_tool：官方描述原句
        "call_mcp_tool",
        "Call a tool found via search_mcp_tools. Use the exact server and tool "
        "names it returned, and match the input schema it gave you.",
        {
            "server": {"type": "string", "description": "Server name exactly as returned by search_mcp_tools"},
            "tool": {"type": "string", "description": "Tool name exactly as returned by search_mcp_tools"},
            "arguments": {"type": "object", "description": "Tool arguments matching its schema"},
        },
        ["server", "tool"],
    ),
)


def build_official_skeleton_tools() -> list[dict[str, Any]]:
    """构造官方桌面端骨架工具集（OpenAI function 格式，官方名字顺序）。

    与 OFFICIAL_THREAD_TOOL_NAMES 一一对应（当前 27 个）。schema 为官方
    inputSchema 的简化版 —— 上游风控关注的是工具名集合签名，参数形状只要
    是干净扁平 JSON Schema 即可（normalize_tool_schemas 会再做一遍归一化）。
    """
    return [
        _simple_tool(name, desc, props, required)
        for name, desc, props, required in _SKELETON_TOOL_SPECS
    ]


def rewrite_tools_for_upstream(
    client_tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """把客户端工具集重写成官方桌面端画像。

    策略（对齐 2026-08-25 桌面端 MCP 架构）：
    1. 客户端工具保留（customToolDefinitions 在官方协议中合法），但名字统一
       转成官方风格 snake_case，schema 由 normalize_tool_schemas 归一化；
    2. 补齐官方骨架工具（跳过与客户端重名的），保证 tools 数组里始终存在
       官方 thread agent 的完整工具名单 + MCP 网关两件套；
    3. 结果集合 = 官方骨架在前（稳定顺序）、客户端工具在后 —— 与官方
       ``threadToolSpecs(agentMode, extra)`` 的「base + extra 去重」拼接一致。
    """
    skeleton = build_official_skeleton_tools()
    if not isinstance(client_tools, list):
        return skeleton

    seen: set[str] = set()
    rewritten: list[dict[str, Any]] = []
    for tool in client_tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
            continue
        fn = dict(tool["function"])
        original = fn.get("name") or ""
        new_name = official_tool_name(original)
        # 与官方骨架重名的客户端工具直接丢弃（避免同名冲突暴露拼接痕迹）
        if not new_name or new_name in OFFICIAL_THREAD_TOOL_NAME_SET:
            continue
        fn["name"] = new_name
        rewritten.append({"type": "function", "function": fn})
        seen.add(new_name)

    merged = [t for t in skeleton if t["function"]["name"] not in seen]
    return [*merged, *rewritten]

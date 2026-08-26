"""test_official_tools.py — 官方桌面端工具集画像重写的单元测试。

背景（2026-08-25 orchestrator.js 重新逆向，用户指正"桌面版已支持 MCP"）：
新版桌面端对 MCP 的支持是**网关两件套**（search_mcp_tools / call_mcp_tool），
tools 数组里永远是官方 snake_case 名字集合；mcp__* 只出现在消息历史。
因此防暴露 = 官方骨架 + 客户端工具混入（threadToolSpecs(base, extra) 语义），
而非按数量截断。
"""

import unittest

from freebuff2api.official_tools import (
    OFFICIAL_THREAD_TOOL_NAMES,
    OFFICIAL_THREAD_TOOL_NAME_SET,
    build_official_skeleton_tools,
    official_tool_name,
    rewrite_tools_for_upstream,
)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class OfficialSkeletonTests(unittest.TestCase):
    def test_official_names_match_skeleton(self) -> None:
        skeleton = build_official_skeleton_tools()
        names = [t["function"]["name"] for t in skeleton]
        self.assertEqual(tuple(names), OFFICIAL_THREAD_TOOL_NAMES)

    def test_core_official_tools_present(self) -> None:
        # base3 核心 8 个 + desktop extra（end_turn 等）+ MCP 网关两件套
        for name in (
            "read_files",
            "str_replace",
            "write_file",
            "run_terminal_command",
            "code_search",
            "glob",
            "list_directory",
            "write_todos",
            "end_turn",
            "web_search",
            "read_url",
            "search_mcp_tools",
            "call_mcp_tool",
        ):
            self.assertIn(name, OFFICIAL_THREAD_TOOL_NAME_SET)

    def test_no_pascal_or_mcp_prefix_in_official_set(self) -> None:
        for name in OFFICIAL_THREAD_TOOL_NAME_SET:
            self.assertEqual(name, name.lower())
            self.assertFalse(name.startswith("mcp__"))

    def test_schema_shapes_are_clean(self) -> None:
        for tool in build_official_skeleton_tools():
            params = tool["function"].get("parameters", {})
            self.assertEqual(params.get("type"), "object")
            self.assertNotIn("$defs", params)
            self.assertNotIn("nullable", params)


class OfficialToolNameTests(unittest.TestCase):
    def test_snake_case_conversion(self) -> None:
        self.assertEqual(official_tool_name("ReadFile"), "read_file")
        self.assertEqual(official_tool_name("WebSearchTool"), "web_search_tool")
        self.assertEqual(official_tool_name("HTTPStatus"), "http_status")

    def test_dotted_and_dashed_names(self) -> None:
        self.assertEqual(official_tool_name("filesystem.read_file"), "filesystem_read_file")
        self.assertEqual(official_tool_name("get-user-info"), "get_user_info")

    def test_lowercase_and_mcp_prefix_passthrough(self) -> None:
        self.assertEqual(official_tool_name("already_snake"), "already_snake")
        # mcp__ 前缀保留：官方消息历史就是这种格式
        self.assertEqual(official_tool_name("mcp__github__get_issue"), "mcp__github__get_issue")


class RewriteToolsTests(unittest.TestCase):
    def test_none_returns_full_skeleton(self) -> None:
        out = rewrite_tools_for_upstream(None)
        self.assertEqual(
            [t["function"]["name"] for t in out],
            list(OFFICIAL_THREAD_TOOL_NAMES),
        )

    def test_client_tools_appended_after_skeleton(self) -> None:
        client = [_tool("ReadFile"), _tool("custom_search")]
        out = rewrite_tools_for_upstream(client)
        names = [t["function"]["name"] for t in out]
        # 官方骨架在前、客户端转换后的工具在后
        self.assertEqual(names[: len(OFFICIAL_THREAD_TOOL_NAMES)], list(OFFICIAL_THREAD_TOOL_NAMES))
        self.assertIn("read_file", names)
        self.assertIn("custom_search", names)
        self.assertNotIn("ReadFile", names)

    def test_client_tool_colliding_with_official_is_dropped(self) -> None:
        client = [_tool("end_turn"), _tool("my_own")]
        out = rewrite_tools_for_upstream(client)
        names = [t["function"]["name"] for t in out]
        # end_turn 与官方骨架重名 → 客户端版本被丢弃，骨架版本保留一份
        self.assertEqual(names.count("end_turn"), 1)
        self.assertIn("my_own", names)

    def test_mcp_prefixed_client_tool_kept_as_custom_definition(self) -> None:
        client = [_tool("mcp__puppeteer__navigate")]
        out = rewrite_tools_for_upstream(client)
        names = [t["function"]["name"] for t in out]
        self.assertIn("mcp__puppeteer__navigate", names)
        # 官方网关两件套仍然在位
        for gateway in ("search_mcp_tools", "call_mcp_tool"):
            self.assertIn(gateway, names)

    def test_empty_client_list_still_gets_skeleton(self) -> None:
        out = rewrite_tools_for_upstream([])
        self.assertEqual(len(out), len(OFFICIAL_THREAD_TOOL_NAMES))

    def test_malformed_entries_skipped(self) -> None:
        client = ["not-a-dict", {"no_function": 1}, _tool("valid_one")]
        out = rewrite_tools_for_upstream(client)
        names = [t["function"]["name"] for t in out]
        self.assertIn("valid_one", names)


if __name__ == "__main__":
    unittest.main()

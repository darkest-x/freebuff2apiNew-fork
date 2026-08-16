from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("freebuff2api.model_registry")

# 动态模型注册表（仿 pingmike2/freebuff2api-wokers 设计）。
# 官方模型映射（FREEBUFF_ROOT_AGENT_ID_BY_MODEL 等）会随官方客户端更新而漂移；
# 这里从 CodebuffAI/freebuff 的公开 GitHub 镜像拉取常量文件并解析，避免每次改代码重新部署。
# 每源都有 raw + jsDelivr 两个地址，拉取失败时回退到 models.py 的硬编码表。

SOURCES: dict[str, list[str]] = {
    "agents": [
        "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/free-agents.ts",
        "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/free-agents.ts",
    ],
    "models": [
        "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/freebuff-models.ts",
        "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/freebuff-models.ts",
    ],
    "model_ids": [
        "https://raw.githubusercontent.com/CodebuffAI/freebuff/main/common/src/constants/freebuff-model-ids.ts",
        "https://cdn.jsdelivr.net/gh/CodebuffAI/freebuff@main/common/src/constants/freebuff-model-ids.ts",
    ],
}

REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
FETCH_TIMEOUT_SECONDS = 10.0

_MODEL_ID_CONST_RE = re.compile(
    r"export\s+const\s+([A-Z0-9_]+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z0-9_.]+))"
)

_KNOWN_DEFAULTS = {
    "mimoV25": "mimo/mimo-v2.5",
}

_AGENT_BLOCKS: dict[str, str] = {
    "root": "FREEBUFF_ROOT_AGENT_ID_BY_MODEL",
    "base3_web": "FREEBUFF_WEB_BASE3_AGENT_ID_BY_MODEL",
    "reviewer": "FREEBUFF_REVIEWER_AGENT_ID_BY_MODEL",
}


@dataclass
class DynamicModelEntry:
    id: str
    agent_id: str
    base3_agent_id: str | None = None
    reviewer_agent_id: str | None = None


@dataclass
class DynamicModelTable:
    models: list[DynamicModelEntry]
    premium_ids: set[str] = field(default_factory=set)
    glm_ids: set[str] = field(default_factory=set)
    fetched_at: float = field(default_factory=time.time)

    def find(self, model_id: str) -> DynamicModelEntry | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None


class ModelRegistry:
    """In-memory dynamic model registry.

    - 模块导入时通过 ``start_background_refresh()`` 在后台线程同步抓取一次；
    - 之后可由 admin 接口手动触发 ``refresh()``（异步）或 ``refresh_sync()``（线程）；
    - 抓取失败时 models.py 的硬编码表兜底，不影响服务启动。
    """

    def __init__(self) -> None:
        self._table: DynamicModelTable | None = None
        self._last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def table(self) -> DynamicModelTable | None:
        return self._table

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._table is not None,
            "model_count": len(self._table.models) if self._table else 0,
            "fetched_at": self._table.fetched_at if self._table else None,
            "last_error": self._last_error,
            "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        }

    def find(self, model_id: str) -> DynamicModelEntry | None:
        if not self._table:
            return None
        return self._table.find(model_id)

    def is_stale(self) -> bool:
        if self._table is None:
            return True
        return time.time() - self._table.fetched_at > REFRESH_INTERVAL_SECONDS

    # ── Async refresh (admin UI / FastAPI endpoints) ───────────────

    async def refresh(self) -> DynamicModelTable:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
                follow_redirects=True,
                trust_env=False,
            ) as client:
                agents_src = await _fetch_first_async(client, SOURCES["agents"])
                models_src = await _fetch_first_async(client, SOURCES["models"])
                model_ids_src = await _fetch_first_async(client, SOURCES["model_ids"])
            table = self._build_table(agents_src, models_src, model_ids_src)
            self._apply_table(table)
            return table
        except Exception as error:
            self._last_error = str(error)
            logger.warning(
                "dynamic model registry refresh failed; keeping hardcoded fallback: %s",
                error,
            )
            raise

    # ── Sync refresh (background thread / startup) ─────────────────

    def refresh_sync(self) -> DynamicModelTable:
        agents_src = _fetch_first_sync(SOURCES["agents"])
        models_src = _fetch_first_sync(SOURCES["models"])
        model_ids_src = _fetch_first_sync(SOURCES["model_ids"])
        table = self._build_table(agents_src, models_src, model_ids_src)
        self._apply_table(table)
        return table

    def start_background_refresh(self) -> None:
        def _run() -> None:
            try:
                self.refresh_sync()
            except Exception as error:
                logger.info(
                    "background model registry refresh failed; hardcoded fallback active: %s",
                    error,
                )

        threading.Thread(
            target=_run,
            name="model-registry-refresh",
            daemon=True,
        ).start()

    # ── Shared table construction ──────────────────────────────────

    def _build_table(
        self,
        agents_src: str | None,
        models_src: str | None,
        model_ids_src: str | None,
    ) -> DynamicModelTable:
        if not agents_src or not models_src:
            raise RuntimeError("dynamic model sources unavailable (agents or models missing)")

        model_id_constants = _parse_model_id_constants(model_ids_src or "")
        model_id_constants.update(_parse_model_id_constants(models_src))
        mappings = _parse_agent_mappings(agents_src, model_id_constants)
        root = mappings["root"]
        if not root:
            raise RuntimeError("FREEBUFF_ROOT_AGENT_ID_BY_MODEL is empty after parsing")

        pools = _parse_model_pools(models_src, model_id_constants)
        models = [
            DynamicModelEntry(
                id=model_id,
                agent_id=root[model_id],
                base3_agent_id=mappings["base3_web"].get(model_id),
                reviewer_agent_id=mappings["reviewer"].get(model_id),
            )
            for model_id in root
        ]
        return DynamicModelTable(
            models=models,
            premium_ids=pools["premium"],
            glm_ids=pools["glm"],
        )

    def _apply_table(self, table: DynamicModelTable) -> None:
        with self._lock:
            self._table = table
            self._last_error = None
        logger.info(
            "dynamic model registry refreshed models=%s premium=%s glm=%s",
            len(table.models),
            len(table.premium_ids),
            len(table.glm_ids),
        )


async def _fetch_first_async(client: httpx.AsyncClient, urls: list[str]) -> str | None:
    for url in urls:
        try:
            response = await client.get(url)
            if response.status_code == 200 and response.text and len(response.text) > 100:
                return response.text
        except Exception as error:
            logger.debug("dynamic model source fetch failed url=%s error=%s", url, error)
            continue
    return None


def _fetch_first_sync(urls: list[str]) -> str | None:
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "text/plain", "User-Agent": "freebuff2api/1.0"},
            )
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
                text = resp.read().decode("utf-8")
            if text and len(text) > 100:
                return text
        except Exception as error:
            logger.debug("dynamic model source sync fetch failed url=%s error=%s", url, error)
            continue
    return None


def _parse_model_id_constants(source: str) -> dict[str, str]:
    """Parse ``export const NAME = 'value'`` / ``export const NAME = expr`` lines."""
    table: dict[str, str] = {}
    for match in _MODEL_ID_CONST_RE.finditer(source):
        name = match.group(1)
        lit = match.group(2) or match.group(3) or ""
        expr = match.group(4) or ""
        if lit:
            table[name] = lit
        elif expr:
            member = expr.rsplit(".", 1)[-1]
            if member in _KNOWN_DEFAULTS:
                table[name] = _KNOWN_DEFAULTS[member]
            elif re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.:/-]+", expr):
                table[name] = expr
    return table


def _parse_agent_mappings(
    source: str,
    model_id_constants: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Parse FREEBUFF_*_AGENT_ID_BY_MODEL object literals."""
    result: dict[str, dict[str, str]] = {"root": {}, "base3_web": {}, "reviewer": {}}
    entry_re = re.compile(r"\[\s*([A-Z0-9_]+)\s*\]\s*:\s*'([^']+)'")
    for kind, block_name in _AGENT_BLOCKS.items():
        block_match = re.search(block_name + r"[^=]*=\s*\{([^}]*)\}", source)
        if not block_match:
            continue
        for entry in entry_re.finditer(block_match.group(1)):
            model_id = model_id_constants.get(entry.group(1))
            if model_id:
                result[kind][model_id] = entry.group(2)
    return result


def _parse_model_pools(
    source: str,
    model_id_constants: dict[str, str],
) -> dict[str, set[str]]:
    """Parse FREEBUFF_PREMIUM_MODEL_IDS / FREEBUFF_GLM_V52_MODEL_IDS etc.

    Simplistic but sufficient: expand array literals with spread (``...FOO``) using
    previously parsed const-array definitions.
    """
    pools: dict[str, set[str]] = {"premium": set(), "glm": set()}

    const_arrays: dict[str, list[str]] = {}
    array_re = re.compile(r"export\s+const\s+([A-Z0-9_]+)\s*=\s*\[([^\]]*)\]")
    item_re = re.compile(r"\.\.\.([A-Z0-9_]+)|'([^']*)'|\"([^\"]*)\"|([A-Za-z0-9_]+)")
    for match in array_re.finditer(source):
        name = match.group(1)
        items: list[str] = []
        for item in item_re.finditer(match.group(2)):
            spread = item.group(1)
            lit = item.group(2) or item.group(3)
            expr = item.group(4)
            if spread:
                items.append("__SPREAD__" + spread)
            elif lit:
                items.append(lit)
            elif expr and expr in model_id_constants:
                items.append(model_id_constants[expr])
        const_arrays[name] = items

    pool_names = {
        "premium": ("FREEBUFF_PREMIUM_MODEL_IDS", "FREEBUFF_WEB_PREMIUM_MODEL_IDS"),
        "glm": ("FREEBUFF_GLM_V52_MODEL_IDS",),
    }
    for pool_kind, names in pool_names.items():
        for pool_name in names:
            match = re.search(pool_name + r"\s*=\s*\[([^\]]*)\]", source)
            if not match:
                continue
            for item in item_re.finditer(match.group(1)):
                spread = item.group(1)
                lit = item.group(2) or item.group(3)
                expr = item.group(4)
                if spread and spread in const_arrays:
                    _expand_const_array(const_arrays, spread, pools[pool_kind])
                elif lit:
                    pools[pool_kind].add(lit)
                elif expr and expr in model_id_constants:
                    pools[pool_kind].add(model_id_constants[expr])
    return pools


def _expand_const_array(
    const_arrays: dict[str, list[str]],
    name: str,
    target: set[str],
) -> None:
    for item in const_arrays.get(name, []):
        if item.startswith("__SPREAD__"):
            _expand_const_array(const_arrays, item[len("__SPREAD__"):], target)
        else:
            target.add(item)

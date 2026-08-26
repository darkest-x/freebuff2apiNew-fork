from __future__ import annotations

import json
import logging
import os
from pathlib import Path
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

REFRESH_INTERVAL_SECONDS = int(
    os.getenv("FREEBUFF_MODEL_REFRESH_SECONDS", str(2 * 60 * 60))
)  # 默认 2h：上游挪模型/改配额（flash 2026-08-18 入 premium → 2026-08-26 又回退）也能当天跟随
FETCH_TIMEOUT_SECONDS = 10.0
SNAPSHOT_PATH = Path(__file__).parent / "model_registry_snapshot.json"

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
    # 🔴 蜜罐/god-only 模型（FREEBUFF_WEB_GOD_ONLY_MODELS）：上游隐藏的评测路由，
    # 真实客户端不可达；第三方流量打过去形同"探测隐藏路由"（#201 luna-es 实证，
    # kimi-k3-eco 为文档级蜜罐）。必须过滤出服务列表且拒绝 resolve。
    god_only_ids: set[str] = field(default_factory=set)
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
        # 先从本地快照恢复，避免启动时网络不可用导致动态表完全不可用。
        # 后台刷新成功后会用最新数据覆盖快照。
        snapshot = self._load_snapshot()
        if snapshot is not None:
            self._table = snapshot
            logger.info(
                "model registry loaded from local snapshot models=%s fetched_at=%s",
                len(snapshot.models),
                snapshot.fetched_at,
            )

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
        """启动后台守护线程：立即抓取一次，之后每 REFRESH_INTERVAL_SECONDS 循环。

        🔴 2026-08-26 修复：旧实现只抓一次就退出线程，上游变动（flash 入 premium 又回退）
        默认 2h 一拍（用户要求"最少两小时更新"），失败不中断（沿用上一张表）。
        """
        def _run() -> None:
            while True:
                try:
                    table = self.refresh_sync()
                    logger.info(
                        "dynamic model registry refreshed models=%s premium=%s glm=%s god_only=%s fetched_at=%s",
                        len(table.models),
                        len(table.premium_ids),
                        len(table.glm_ids),
                        len(table.god_only_ids),
                        table.fetched_at,
                    )
                except Exception as error:
                    # 失败保留当前表（快照/硬编码兜底），下一拍重试
                    logger.info(
                        "periodic model registry refresh failed; keeping current table: %s",
                        error,
                    )
                # Event.wait 而非 sleep：进程退出时 daemon 线程随事件循环结束即可，
                # 不需要额外的停止信号
                stop_event.wait(REFRESH_INTERVAL_SECONDS)
                if stop_event.is_set():
                    return

        stop_event = threading.Event()
        self._refresh_stop = stop_event
        threading.Thread(
            target=_run,
            name="model-registry-refresh",
            daemon=True,
        ).start()

    def stop_background_refresh(self) -> None:
        """停止周期刷新循环（lifespan 关停时调用；daemon 线程本会随进程退出）。"""
        event = getattr(self, "_refresh_stop", None)
        if event is not None:
            event.set()

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
            god_only_ids=pools["god_only"],
        )

    def _table_to_dict(self, table: DynamicModelTable) -> dict[str, Any]:
        return {
            "fetched_at": table.fetched_at,
            "models": [
                {
                    "id": model.id,
                    "agent_id": model.agent_id,
                    "base3_agent_id": model.base3_agent_id,
                    "reviewer_agent_id": model.reviewer_agent_id,
                }
                for model in table.models
            ],
            "premium_ids": sorted(table.premium_ids),
            "glm_ids": sorted(table.glm_ids),
            "god_only_ids": sorted(table.god_only_ids),
        }

    def _table_from_dict(self, data: dict[str, Any]) -> DynamicModelTable:
        return DynamicModelTable(
            models=[
                DynamicModelEntry(
                    id=item["id"],
                    agent_id=item["agent_id"],
                    base3_agent_id=item.get("base3_agent_id"),
                    reviewer_agent_id=item.get("reviewer_agent_id"),
                )
                for item in data.get("models", [])
            ],
            premium_ids=set(data.get("premium_ids", [])),
            glm_ids=set(data.get("glm_ids", [])),
            god_only_ids=set(data.get("god_only_ids", [])),
            fetched_at=float(data.get("fetched_at") or time.time()),
        )

    def _save_snapshot(self, table: DynamicModelTable) -> None:
        try:
            SNAPSHOT_PATH.write_text(
                json.dumps(self._table_to_dict(table), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as error:
            logger.debug("could not save model registry snapshot: %s", error)

    def _load_snapshot(self) -> DynamicModelTable | None:
        if not SNAPSHOT_PATH.exists():
            return None
        try:
            data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
            table = self._table_from_dict(data)
            if not table.models:
                return None
            return table
        except Exception as error:
            logger.warning("could not load model registry snapshot: %s", error)
            return None

    def _apply_table(self, table: DynamicModelTable) -> None:
        with self._lock:
            self._table = table
            self._last_error = None
        self._save_snapshot(table)
        logger.info(
            "dynamic model registry refreshed models=%s premium=%s glm=%s god_only=%s",
            len(table.models),
            len(table.premium_ids),
            len(table.glm_ids),
            len(table.god_only_ids),
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

    2026-08-25 增强：god-only 等池数组引用的是**模型对象常量**（如
    ``KIMI_K3_ECO_MODEL``），其 id 藏在对象的 ``id: FREEBUFF_..._MODEL_ID``
    字段里。这里额外解析「模型对象常量名 → id 值」映射，让这类引用也能展开。
    """
    pools: dict[str, set[str]] = {"premium": set(), "glm": set(), "god_only": set()}

    # 模型对象常量（const NAME = { ... id: SOME_ID_CONST, ... }）→ 解析 id 值
    model_object_ids: dict[str, str] = {}
    for obj_match in re.finditer(
        r"const\s+([A-Z0-9_]+_MODEL)\s*=\s*\{([^{}]*)", source
    ):
        obj_name = obj_match.group(1)
        body = obj_match.group(2)
        id_match = re.search(r"\bid:\s*([A-Za-z0-9_.]+)", body)
        if not id_match:
            continue
        id_expr = id_match.group(1)
        if id_expr in model_id_constants:
            model_object_ids[obj_name] = model_id_constants[id_expr]
        elif re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.:/-]+", id_expr):
            model_object_ids[obj_name] = id_expr

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
            elif expr and expr in model_object_ids:
                items.append(model_object_ids[expr])
        const_arrays[name] = items

    pool_names = {
        "premium": ("FREEBUFF_PREMIUM_MODEL_IDS", "FREEBUFF_WEB_PREMIUM_MODEL_IDS"),
        "glm": ("FREEBUFF_GLM_V52_MODEL_IDS",),
        # god-only：官方隐藏评测路由（kimi-k3-eco / luna-es 等），见 DynamicModelTable 注释
        "god_only": ("FREEBUFF_WEB_GOD_ONLY_MODELS",),
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
                elif expr and expr in model_object_ids:
                    pools[pool_kind].add(model_object_ids[expr])
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

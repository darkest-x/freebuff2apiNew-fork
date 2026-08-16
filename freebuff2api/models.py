from __future__ import annotations

from dataclasses import dataclass

from .model_registry import DynamicModelEntry, ModelRegistry


@dataclass(frozen=True)
class FreebuffModel:
    id: str
    agent_id: str
    owned_by: str = "freebuff"
    upstream_model_id: str | None = None
    session_model_id: str | None = None
    parent_agent_id: str | None = None
    base3_agent_id: str | None = None
    reviewer_agent_id: str | None = None
    # 模型参数（供 /v1/models 下发，客户端据此自适应钳制输出/上下文）。
    context_window: int = 131_072  # 保守默认（未实测模型）
    max_output_tokens: int = 32_768  # 统一保守输出上限（上游实测）
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)

    @property
    def upstream_id(self) -> str:
        return self.upstream_model_id or self.id

    @property
    def session_id(self) -> str:
        return self.session_model_id or self.upstream_id


# 硬编码兜底表（2026-08 从官方 orchestrator.txt freebuff-model-ids.ts / free-agents.ts 提取）。
# 动态注册表刷新失败或官方源不可用时使用；正常情况下 resolve_model 优先查动态表。
FREEBUFF_MODELS: tuple[FreebuffModel, ...] = (
    FreebuffModel(
        "deepseek/deepseek-v4-flash",
        "base2-free-deepseek-flash",
        base3_agent_id="base3-free-deepseek-flash",
        reviewer_agent_id="code-reviewer-deepseek-flash",
        context_window=1_048_576,
    ),
    FreebuffModel(
        "deepseek/deepseek-v4-pro",
        "base2-free-deepseek",
        base3_agent_id="base3-free-deepseek",
        reviewer_agent_id="code-reviewer-deepseek",
        context_window=131_072,
    ),
    FreebuffModel(
        "mimo/mimo-v2.5",
        "base2-free-mimo",
        base3_agent_id="base3-free-mimo",
        reviewer_agent_id="code-reviewer-mimo",
        context_window=131_072,
    ),
    FreebuffModel(
        "minimax/minimax-m3",
        "base2-free-minimax-m3",
        base3_agent_id="base3-free-minimax-m3",
        reviewer_agent_id="code-reviewer-minimax-m3",
        context_window=524_288,
        input_modalities=("text", "image"),
    ),
    FreebuffModel(
        "openai/gpt-5.6-luna",
        "base2-free-luna",
        base3_agent_id="base3-free-luna",
        reviewer_agent_id="code-reviewer-luna",
        context_window=1_000_000,
    ),
    FreebuffModel(
        "z-ai/glm-5.2",
        "base2-free-glm",
        base3_agent_id="base3-free-glm",
        reviewer_agent_id="code-reviewer-glm",
        context_window=131_072,
    ),
    FreebuffModel(
        "crof/kimi-k3-eco",
        "base2-free-kimi-k3-eco",
        base3_agent_id="base3-free-kimi-k3-eco",
        context_window=131_072,
    ),
    FreebuffModel(
        "anthropic/claude-fable-5",
        "base2-free-fable",
        base3_agent_id="base3-free-fable",
        reviewer_agent_id="code-reviewer-fable",
        context_window=131_072,
    ),
    FreebuffModel(
        "meta/muse-spark-1.2-contributor",
        "base2-free-muse-spark",
        base3_agent_id="base3-free-muse-spark",
        context_window=1_000_000,
    ),
)

DEFAULT_MODEL = FREEBUFF_MODELS[0]
CONTEXT_PRUNER_AGENT_ID = "context-pruner"
GEMINI_THINKER_AGENT_ID = "thinker-with-files-gemini"
GEMINI_THINKER_PARENT_AGENT_ID = "base2-free-kimi-k3-eco"
GEMINI_THINKER_PARENT_MODEL_ID = "crof/kimi-k3-eco"
GEMINI_FLASH_LITE_SESSION_MODEL_ID = DEFAULT_MODEL.id

GEMINI_FREE_MODELS: tuple[FreebuffModel, ...] = (
    FreebuffModel(
        "google/gemini-3.1-flash-lite",
        "file-picker",
        owned_by="google",
        session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
        parent_agent_id=DEFAULT_MODEL.agent_id,
    ),
    FreebuffModel(
        "google/gemini-3.5-flash-lite",
        "file-picker-max",
        owned_by="google",
        session_model_id=GEMINI_FLASH_LITE_SESSION_MODEL_ID,
        parent_agent_id=DEFAULT_MODEL.agent_id,
    ),
    FreebuffModel(
        "google/gemini-3.1-pro-preview",
        GEMINI_THINKER_AGENT_ID,
        owned_by="google",
        session_model_id=GEMINI_THINKER_PARENT_MODEL_ID,
        parent_agent_id=GEMINI_THINKER_PARENT_AGENT_ID,
    ),
)

HARDCODED_MODELS = FREEBUFF_MODELS + GEMINI_FREE_MODELS
# 兼容旧引用（admin.py overview 等仍导入 ALL_MODELS）。
ALL_MODELS = HARDCODED_MODELS

# 运行时动态注册表：模块导入即创建，并启动后台线程抓取一次官方模型映射。
# 抓取完成前 resolve_model 回退硬编码表，不阻塞服务启动。
_registry = ModelRegistry()
_registry.start_background_refresh()


def set_model_registry(registry: ModelRegistry | None) -> None:
    global _registry
    _registry = registry


def get_model_registry() -> ModelRegistry:
    return _registry


def _model_from_dynamic(entry: DynamicModelEntry) -> FreebuffModel:
    return FreebuffModel(
        entry.id,
        entry.agent_id,
        base3_agent_id=entry.base3_agent_id,
        reviewer_agent_id=entry.reviewer_agent_id,
    )


def _hardcoded_by_id(model_id: str) -> FreebuffModel | None:
    for model in HARDCODED_MODELS:
        if model.id == model_id:
            return model
    return None


def all_models() -> list[FreebuffModel]:
    """Merged model list: hardcoded first, then dynamic entries not already present."""
    models = list(HARDCODED_MODELS)
    seen = {model.id for model in models}
    if _registry is not None and _registry.table is not None:
        for entry in _registry.table.models:
            if entry.id not in seen:
                models.append(_model_from_dynamic(entry))
                seen.add(entry.id)
    return models


def resolve_model(requested: str | None) -> FreebuffModel:
    if not requested:
        return DEFAULT_MODEL

    # Dynamic registry first (auto-updated every 6h from official sources).
    if _registry is not None:
        dynamic = _registry.find(requested)
        if dynamic is not None:
            return _model_from_dynamic(dynamic)

    hardcoded = _hardcoded_by_id(requested)
    if hardcoded is not None:
        return hardcoded

    raise ValueError(f"Unsupported Freebuff model: {requested}")


def _model_entry(model: FreebuffModel) -> dict[str, object]:
    """模型条目：OpenAI 标准字段 + Anthropic Models API 字段（附加，客户端自适应）。"""
    return {
        "id": model.id,
        "object": "model",
        "created": 0,
        "owned_by": model.owned_by,
        # Anthropic Models API 字段（Claude Code / anthropic-sdk 读取，
        # 用于 context sizing 与输出上限自适应）。
        "type": "model",
        "display_name": model.id,
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "input_modalities": list(model.input_modalities),
        "output_modalities": list(model.output_modalities),
    }


def models_response() -> dict[str, object]:
    return {
        "object": "list",
        "data": [_model_entry(model) for model in all_models()],
    }


def model_response(model_id: str) -> dict[str, object] | None:
    for model in all_models():
        if model.id == model_id:
            return _model_entry(model)
    return None


def agent_validation_payload() -> dict[str, object]:
    models_by_agent: dict[str, FreebuffModel] = {}
    spawnable_by_agent: dict[str, set[str]] = {}
    for model in all_models():
        models_by_agent.setdefault(model.agent_id, model)
        spawnable_by_agent.setdefault(model.agent_id, set()).add(CONTEXT_PRUNER_AGENT_ID)
        if model.parent_agent_id:
            spawnable_by_agent.setdefault(model.parent_agent_id, set()).add(model.agent_id)

    definitions = [
        _agent_definition(
            agent_id=model.agent_id,
            model_id=model.upstream_id,
            display_name=f"Freebuff {model.upstream_id}",
            spawnable_agents=sorted(spawnable_by_agent.get(model.agent_id, set())),
        )
        for model in models_by_agent.values()
    ]
    definitions.append(
        _agent_definition(
            agent_id=CONTEXT_PRUNER_AGENT_ID,
            model_id=DEFAULT_MODEL.id,
            display_name="Context Pruner",
            spawnable_agents=[],
        )
    )

    return {"agentDefinitions": definitions}


def _agent_definition(
    *,
    agent_id: str,
    model_id: str,
    display_name: str,
    spawnable_agents: list[str],
) -> dict[str, object]:
    return {
        "id": agent_id,
        "publisher": "codebuff",
        "model": model_id,
        "displayName": display_name,
        "spawnerPrompt": "Freebuff OpenAI-compatible orchestrator",
        "inputSchema": {
            "prompt": {
                "type": "string",
                "description": "A coding task to complete",
            },
            "params": {"type": "object", "properties": {}, "required": []},
        },
        "outputMode": "last_message",
        "includeMessageHistory": True,
        "toolNames": ["spawn_agents"] if spawnable_agents else [],
        "spawnableAgents": spawnable_agents,
        "systemPrompt": "Act as a helpful coding assistant.",
    }

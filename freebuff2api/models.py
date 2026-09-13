from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

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
    # 官方 per-model reasoning effort 限制（来自 orchestrator.js freebuff-models.ts）。
    # None 表示官方未定义 efforts（不干预透传）。
    reasoning_efforts: tuple[str, ...] | None = None
    default_reasoning_effort: str | None = None

    @property
    def upstream_id(self) -> str:
        return self.upstream_model_id or self.id

    @property
    def session_id(self) -> str:
        return self.session_model_id or self.upstream_id


# 硬编码兜底表（2026-09-13 从官方 orchestrator.js 0.0.109 freebuff-models.ts 提取，
# 即官方 SUPPORTED_FREEBUFF_MODELS 全集，排除 god-only 蜜罐 kimi/luna-es —— 它们只
# 属于 FREEBUFF_WEB_GOD_ONLY_MODELS，绝不能进 /v1/models）。
# 动态注册表刷新失败或官方源不可用时使用；正常情况下 resolve_model 优先查动态表。
#
# 🔴 2026-09-13 0.0.109 复核落地（相对 0.0.96/0.0.84 的变化）：
# - SUPPORTED_FREEBUFF_MODELS 顺序：**ox-alpha 升到首位**，pro 第 2 位；kimi-k3-eco
#   与 luna-es 不在 SUPPORTED（仍是 WEB god-only 蜜罐，见 GOD_ONLY_MODEL_IDS）。
# - `deepseek/deepseek-v4-flash`：displayName 改「DeepSeek **V4.1** Flash」、premium:false、
#   multimodal:true、`unavailableFallback: luna`、`isNew:true`；efforts 仍
#   [low, high, max]。**用户决策 2026-09-13：中转默认思考深度改 max**（官方 max 档
#   合法，见 default_reasoning_effort_for）。
# - `deepseek/deepseek-v4-pro`：**premium 回归 true**（0.0.96 为 false），但因不在
#   FREEBUFF_MODELS（premium 池源）→ 并发仍走 multi-tab（unlimited）通道。
# - `FREEBUFF_MODELS`（premium 池源）= [glm-5.3-flash, flash, luna, mimo,
#   solar-pro4, muse-1.2] → 推导 premium 池 = [luna, muse-1.2]（与 0.0.96 一致）。
# - `LIMITED_FREEBUFF_MODEL_IDS = [glm-5.3-flash, flash, mimo, solar-pro4]`
#   （0.0.96 仅 [mimo]，0.0.109 扩到 4 个；limited hero = glm-5.3-flash）。
# - `FREEBUFF_MODEL_CONTEXT_WINDOWS` 表（100813-100823）全量收录（含 m3=524288、
#   pro/flash=1048576、luna/luna-es/muse/ox/glm-5.3=1e6/372000、solar=500000）。
# - `GLM_V53_FLASH_REASONING_EFFORTS = ["low", "high", "max"]`（0.0.84 已扩）、
#   defaultEffort="max"、reasoningEffort="max" —— 保持 3 档 max。
# - `FREEBUFF_SOLAR_PRO_4_ENTITLEMENT.fullAccess.premium = false`（保持 0.0.96 语义），
#   Freebucks 定价走动态 solarOfferAt（当前 2026-09-13 促销价 0）。
# - GLM 5.2 仍 referral 解锁 + streak 加成（FREEBUFF_REWARD_MODEL_ID= glm-5.3-flash）。
FREEBUFF_MODELS: tuple[FreebuffModel, ...] = (
    # --- 官方 SUPPORTED_FREEBUFF_MODELS（0.0.109）顺序逐项对齐 ---
    # 2026-08-26 新增：ox-alpha，Anonymous provider，premium:false，1M 上下文，
    # multimodal:true，efforts=[low, high, max]，defaultEffort="high"（orchestrator.js
    # 100974-100986）。0.0.109 SUPPORTED 首位。
    FreebuffModel(
        "stealth/ox-alpha",
        "base2-free-ox-alpha",
        base3_agent_id="base3-free-ox-alpha",
        reviewer_agent_id="code-reviewer-ox-alpha",
        context_window=1_000_000,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "high", "max"),
        default_reasoning_effort="high",
    ),
    # 0.0.109：premium 回归 true（0.0.96 false）；multimodal:false；dataUse:"training"
    # （warning "May use data for AI training"）；efforts=[low, high, max]、
    # defaultEffort="high"。不在 FREEBUFF_MODELS → 并发走 multi-tab（unlimited）。
    FreebuffModel(
        "deepseek/deepseek-v4-pro",
        "base2-free-deepseek",
        base3_agent_id="base3-free-deepseek",
        reviewer_agent_id="code-reviewer-deepseek",
        context_window=1_048_576,
        reasoning_efforts=("low", "high", "max"),
        default_reasoning_effort="high",
    ),
    # minimax-m3：premium:true，multimodal:true，tagline "Fastest"，无 efforts。
    FreebuffModel(
        "minimax/minimax-m3",
        "base2-free-minimax-m3",
        base3_agent_id="base3-free-minimax-m3",
        reviewer_agent_id="code-reviewer-minimax-m3",
        context_window=524_288,
        input_modalities=("text", "image"),
    ),
    # gpt-5.6-luna：premium:true，multimodal:true，efforts=EFFORTS_THROUGH_MAX、
    # defaultEffort="high"（FREEBUFF_GPT_5_6_LUNA_REASONING_EFFORT）。premium 池成员。
    FreebuffModel(
        "openai/gpt-5.6-luna",
        "base2-free-luna",
        base3_agent_id="base3-free-luna",
        reviewer_agent_id="code-reviewer-luna",
        context_window=1_000_000,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="high",
    ),
    # Solar Pro 4：0.0.109 `FREEBUFF_SOLAR_PRO_4_ENTITLEMENT.fullAccess.premium = false`，
    # multimodal:false，上下文 500,000，不支持 effort 调整（experimental）。
    # Freebucks 按 solarOfferAt() 动态定价：09-09T15:49Z 起促销 **0** Freebucks
    # （常态 5，SOLAR_REGULAR_OFFER）。0.0.96 起在桌面端走 multi-tab 无限通道。
    FreebuffModel(
        "upstage/solar-pro4",
        "base2-free-solar-pro4",
        base3_agent_id="base3-free-solar-pro4",
        reviewer_agent_id="code-reviewer-solar-pro4",
        context_window=500_000,
        # reasoning_efforts 留空 → 客户端传 effort 时由 normalize_reasoning_effort
        # 直接返回 None，不发送 effort 字段（避免触发 foreign_client）。
    ),
    # gemini-3.8-flash：premium:true / multimodal:true / isNew:true，efforts=
    # EFFORTS_THROUGH_MAX、defaultEffort="high"。FREEBUFF_PRO_ONLY_CATALOG_MODEL_IDS
    # 唯一成员 + PLAN_METERED + slot-bound（单 session spend 0.5，顶替 solar-pro4）。
    # ctx 官方常量表未收录，tagline "1M context" → 1_000_000 暂记。
    FreebuffModel(
        "google/gemini-3.8-flash",
        "base2-free-gemini-3-8-flash",
        base3_agent_id="base3-free-gemini-3-8-flash",
        reviewer_agent_id="code-reviewer-gemini-3-8-flash",
        context_window=1_000_000,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="high",
    ),
    # muse-spark-1.3：premium:true / dataUse:training / isNew:true，
    # efforts=EFFORTS_THROUGH_XHIGH、defaultEffort="xhigh"；slot-bound 成员；
    # UNTRACED_TRAINING_MODEL_IDS 成员；队列长回退 "DeepSeek V4.1 Flash"。
    FreebuffModel(
        "meta/muse-spark-1.3-contributor",
        "base2-free-muse-spark-1-3",
        base3_agent_id="base3-free-muse-spark-1-3",
        reviewer_agent_id="code-reviewer-muse-spark-1-3",
        context_window=1_000_000,
        reasoning_efforts=("minimal", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="xhigh",
    ),
    # muse-spark-1.2：premium:true / dataUse:training，同上 efforts=xhigh；slot-bound。
    FreebuffModel(
        "meta/muse-spark-1.2-contributor",
        "base2-free-muse-spark",
        base3_agent_id="base3-free-muse-spark",
        context_window=1_000_000,
        reasoning_efforts=("minimal", "low", "medium", "high", "xhigh"),
        default_reasoning_effort="xhigh",
    ),
    # glm-5.2：premium:true，multimodal:false，referral 解锁 + streak 加成；
    # 独立日池，绝不落入共享 premium 日额度（GLM_POOL fail-fast 门）。
    FreebuffModel(
        "z-ai/glm-5.2",
        "base2-free-glm",
        base3_agent_id="base3-free-glm",
        reviewer_agent_id="code-reviewer-glm",
        context_window=131_072,
    ),
    # glm-5.3-flash：0.0.84 起 efforts 扩为 [low, high, max]、reasoningEffort/
    # defaultEffort="max"（0.0.109 保持，orchestrator.js 100905-100916）；premium:false
    # → multi-tab 无限通道；multimodal:true；FREEBUFF_REWARD_MODEL_ID（referral 权益
    # 等价解锁 streak 计数）；官方 DEFAULT_FREEBUFF_MODEL_ID。
    FreebuffModel(
        "z-ai/glm-5.3-flash",
        "base2-free-glm-5-3-flash",
        base3_agent_id="base3-free-glm-5-3-flash",
        reviewer_agent_id="code-reviewer-glm-5-3-flash",
        context_window=1_000_000,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "high", "max"),
        default_reasoning_effort="max",
    ),
    # deepseek-v4-flash：0.0.109 displayName "DeepSeek V4.1 Flash"、premium:false、
    # multimodal:true、unavailableFallback=luna、isNew:true；efforts=[low, high, max]、
    # defaultEffort="high"。🔴 用户决策 2026-09-13：中转默认思考深度改 **max**
    # （官方最大档，见 default_reasoning_effort_for）。Freebucks 15/时，高峰 +20
    # （至 3 AM PT，limited 未付费档 25/40）。
    FreebuffModel(
        "deepseek/deepseek-v4-flash",
        "base2-free-deepseek-flash",
        base3_agent_id="base3-free-deepseek-flash",
        reviewer_agent_id="code-reviewer-deepseek-flash",
        context_window=1_048_576,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "high", "max"),
        default_reasoning_effort="max",  # 🔴 用户决策：默认按官方最大档 max
    ),
    # mimo-v2.5：premium:false，multimodal:true，无 efforts（不干预）；
    # FALLBACK_FREEBUFF_MODEL_ID 兜底模型。
    FreebuffModel(
        "mimo/mimo-v2.5",
        "base2-free-mimo",
        base3_agent_id="base3-free-mimo",
        reviewer_agent_id="code-reviewer-mimo",
        context_window=131_072,
        input_modalities=("text", "image"),
    ),
    # claude-fable-5：SUPPORTED 末尾，premium:true / dataUse:"training"（**官方会用
    # prompts 训练**）+ multimodal:true + efforts=EFFORTS_THROUGH_MAX、
    # defaultEffort="high"。桌面端不在 FREEBUFF_DESKTOP_MODELS（CLI limited offer）。
    FreebuffModel(
        "anthropic/claude-fable-5",
        "base2-free-fable",
        base3_agent_id="base3-free-fable",
        reviewer_agent_id="code-reviewer-fable",
        context_window=131_072,
        input_modalities=("text", "image"),
        reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="high",
    ),
)

# 默认模型：官方 `DEFAULT_FREEBUFF_MODEL_ID = glm-5.3-flash`（含
# 2026-09-05 迁移；PREVIOUS_DEFAULT_FREEBUFF_MODEL_ID = deepseek-v4-flash）。
DEFAULT_MODEL = next(m for m in FREEBUFF_MODELS if m.id == "z-ai/glm-5.3-flash")

# 官方 desktop session bucket 的**硬编码兜底**（仅动态注册表不可用时生效）。
#
# 🟢 2026-09-08 桌面版 0.0.96 复核（并发体系重构，取代 0.0.79-0.0.86 三常量）：
#   - 删除 `FREEBUFF_DESKTOP_PREMIUM_BUCKET_MODEL_IDS` / `FREEBUFF_DESKTOP_SESSION_LIMITS` /
#     `FREEBUFF_SUBSCRIBER_DESKTOP_SESSION_LIMITS`，改为：
#     - `FREEBUFF_DESKTOP_SLOT_BOUND_MODEL_IDS = [luna, gemini-3.8-flash,
#       muse-spark-1.3, muse-spark-1.2]`（占 1 个"slot-bound"并发槽；limited 无订阅
#       用户 getAllModels 也归 slot-bound）
#     - `FREEBUFF_DESKTOP_CONCURRENCY_LIMITS = {free:{slot-bound:1, multi-tab:3},
#       subscriber:{slot-bound:3, multi-tab:8}}`（普通用户 slot-bound 1 + multi-tab 3；
#       订阅用户 slot-bound 3 + multi-tab 8）
#   - 反代映射：slot-bound → 旧 `premium` 桶语义（并发 1）；multi-tab → 旧 `unlimited`
#     （并发 3）。因此本兜底集合 = 「旧 premium 桶补集」仍成立，只是把 solar-pro4 移出
#     premium（0.0.96 `SOLAR_PRO_4_ENTITLEMENT.fullAccess.premium = !1`，走 multi-tab
#     无限通道 + Freebucks 计费）。
# 正确性由 model_registry 动态维护（2h 刷新跟随），这里只保留
# "注册表从未成功加载过"时的最后兜底，镜像官方桌面 multi-tab 集合补集。
UNLIMITED_SESSION_MODEL_IDS = frozenset(
    {
        "mimo/mimo-v2.5",
        # 🔴 2026-09-13 0.0.109：pro 在 SUPPORTED 里 premium 回归 true，但不在 premium
        # 池源 FREEBUFF_MODELS → 桌面端仍走 multi-tab（unlimited）通道，兜底保留。
        "deepseek/deepseek-v4-pro",
        "minimax/minimax-m3",
        # 2026-08-26：flash 一直为 unlimited 通道（0.0.109 premium:false）
        "deepseek/deepseek-v4-flash",
        # 2026-08-26：ox-alpha 为 premium:false 的免费模型
        "stealth/ox-alpha",
        # 2026-09-01 0.0.79：GLM 5.3 Flash 从 premium 池移除，归 unlimited 通道
        "z-ai/glm-5.3-flash",
        # 🔴 2026-09-13 0.0.109：GLM 5.2 premium:true 但不进 slot-bound/premium 池源 →
        # 桌面端 multi-tab（unlimited），兜底补集补齐。
        "z-ai/glm-5.2",
        # 🟢 2026-09-08 0.0.96：solar-pro4 premium → false，归 multi-tab 无限通道
        "upstage/solar-pro4",
        # 🔴 2026-09-13 0.0.109：fable-5 premium:true 但不在 FREEBUFF_MODELS（premium 池
        # 源）→ 桌面端 multi-tab（unlimited）。gemini-3.8 / luna / muse-1.2 / muse-1.3
        # 为 slot-bound（premium 槽），**不**在此补集。
        "anthropic/claude-fable-5",
    }
)

# GLM 5.2：referral 解锁、独立周/日池，绝不落入共享 premium 日额度。
# ⚠️ 2026-08-27：glm-5.3-flash 是独立的 per-model cap 池（limit=2），不是 referral 门，
# 不进本 fail-fast 集合（app 里 GLM_POOL 同时驱动"无权益 403 fail-fast"逻辑）。
GLM_POOL_MODEL_IDS = frozenset({"z-ai/glm-5.2"})

# 🔴 蜜罐/god-only 模型（官方 FREEBUFF_WEB_GOD_ONLY_MODELS 兜底）：上游隐藏评测
# 路由，真实客户端不可达；第三方流量打过去形同"探测隐藏路由"，是封禁级暴露面
# （freebuff-proxy #201 luna-es 实证 + AGENTS.md kimi-k3-eco 记载）。动态注册表
# 优先，此处仅兜底。
#   - 2026-08-26：`FREEBUFF_WEB_GOD_ONLY_MODELS = [KIMI_K3_ECO_MODEL, GPT_5_6_LUNA_ES_MODEL]`
#     （luna-es `openai/gpt-5.6-luna-es` 是新版的第二个蜜罐）
#   - 2026-08-27 复核：god-only 集合未变，仍为这两个蜜罐
GOD_ONLY_MODEL_IDS = frozenset(
    {
        "crof/kimi-k3-eco",
        "openai/gpt-5.6-luna-es",
    }
)


def _dynamic_god_only_ids() -> frozenset[str] | None:
    """从动态模型注册表读取当前 god-only 集合；注册表未加载时返回 None（调用方走兜底）。"""
    registry = get_model_registry()
    if registry is None or registry.table is None:
        return None
    return frozenset(registry.table.god_only_ids)


def is_god_only_model(model: str) -> bool:
    """该模型是否为上游隐藏评测路由（蜜罐）。动态表优先，硬编码兜底。"""
    if not model:
        return False
    dynamic = _dynamic_god_only_ids()
    if dynamic is not None:
        return model in dynamic
    return model in GOD_ONLY_MODEL_IDS


def _dynamic_premium_ids() -> frozenset[str] | None:
    """从动态模型注册表读取当前 premium 池（上游 freebuff-models.ts 的
    FREEBUFF_PREMIUM_MODEL_IDS）。注册表未加载时返回 None（调用方走兜底）。

    🟢 0.0.96 语义：FREEBUFF_PREMIUM_MODEL_IDS 改为
    ``FREEBUFF_MODELS.filter(model.premium)`` **动态推导**，静态数组解析拿不到
    → 以 snapshot/兜底为准。推导结果 [luna, muse-spark-1.2]（solar-pro4 因
    ENTITLEMENT premium:false 掉出）。桌面端并发槽判定优先走 desktop_bucket_ids
    （slot-bound = [luna, gemini-3.8-flash, muse-spark-1.3, muse-spark-1.2]），见
    :func:`session_bucket_for_model`。
    """
    registry = get_model_registry()
    if registry is None or registry.table is None:
        return None
    return frozenset(registry.table.premium_ids)


def _dynamic_desktop_bucket_ids() -> frozenset[str] | None:
    """动态表里的桌面端 slot-bound 并发桶（0.0.96 起解析
    FREEBUFF_DESKTOP_SLOT_BOUND_MODEL_IDS = [luna, gemini-3.8-flash,
    muse-spark-1.3, muse-spark-1.2]，旧 0.0.79-0.0.86 的
    FREEBUFF_DESKTOP_PREMIUM_BUCKET_MODEL_IDS 已删除不再出现）。GitHub main
    未同步该常量时集合为空 → 视为未提供，调用方回退 premium_ids。"""
    registry = get_model_registry()
    if registry is None or registry.table is None:
        return None
    ids = registry.table.desktop_bucket_ids
    if not ids:
        return None
    return frozenset(ids)


def session_bucket_for_model(model: str) -> str:
    """返回官方 desktop session bucket：``premium``（slot-bound 槽）或 ``unlimited``
    （multi-tab 槽）。

    判定优先级（🟢 2026-09-08 0.0.96 更新）：
    1. 动态表的 desktop_bucket_ids —— 官方桌面端 slot-bound 并发桶（0.0.96 =
       [luna, gemini-3.8-flash, muse-spark-1.3, muse-spark-1.2]）在池间挪动时
       自动跟随，无需改代码部署；solar-pro4 / glm-5.2 不在其中 → multi-tab（unlimited）；
    2. 动态表 premium_ids（0.0.96 推导 = [luna, muse-spark-1.2]，以 snapshot 承载）；
    3. 硬编码 UNLIMITED_SESSION_MODEL_IDS 兜底（注册表不可用时）。

    ⚠️ 旧实现把 FREEBUFF_WEB_PREMIUM_MODEL_IDS 混入 premium_ids，导致
    glm-5.3-flash / kimi / luna-es / muse 被误判 premium bucket —— 已修复
    （model_registry._parse_model_pools 只解析 FREEBUFF_PREMIUM_MODEL_IDS）。
    """
    if not model:
        return "premium"
    bucket_ids = _dynamic_desktop_bucket_ids()
    if bucket_ids is not None:
        return "premium" if model in bucket_ids else "unlimited"
    dynamic = _dynamic_premium_ids()
    if dynamic is not None:
        return "premium" if model in dynamic else "unlimited"
    if model in UNLIMITED_SESSION_MODEL_IDS:
        return "unlimited"
    return "premium"


def is_premium_quota_exhausted(rate_limits_by_model: dict[str, Any]) -> bool:
    """检测上游 rateLimitsByModel 是否 premium 池已耗尽（全池判定，保留兼容）。

    ⚠️ 官方各 premium 模型的 limit 并不相同（shared pool + per-model caps），
    新代码应使用 :func:`is_model_quota_exhausted` 做按目标模型的精确判定；
    本函数保留给"整池是否全干涸"的粗粒度场景（SessionManager 全局闸门）。
    """
    if not isinstance(rate_limits_by_model, dict):
        return False
    premium_items = [
        v for k, v in rate_limits_by_model.items()
        if session_bucket_for_model(k) == "premium" and isinstance(v, dict)
    ]
    if not premium_items:
        return False
    return all(
        float(v.get("limit") or 0) > 0
        and float(v.get("recentCount") or 0) >= float(v.get("limit") or 0)
        for v in premium_items
    )


def model_quota_state(
    rate_limits_by_model: dict[str, Any], model: str
) -> tuple[bool, int]:
    """查询单个模型在最近一次 session 响应里的配额状态。

    返回 (exhausted, remaining)。规则对齐 freebuff-proxy quota.go：
    - limit <= 0 或无该模型条目 → 配额未知，不算耗尽（不误杀）
    - recentCount >= limit 且 resetAt 在未来 → 耗尽；resetAt 缺失/已过
      视为窗口已滚动，不判耗尽（等下次 admission 给出新鲜计数）
    """
    if not isinstance(rate_limits_by_model, dict):
        return False, 0
    entry = rate_limits_by_model.get(model)
    if not isinstance(entry, dict):
        return False, 0
    try:
        limit = float(entry.get("limit") or 0)
        recent = float(entry.get("recentCount") or 0)
    except (TypeError, ValueError):
        return False, 0
    if limit <= 0:
        return False, 0
    reset_future = _reset_at_in_future(entry.get("resetAt"))
    if reset_future and recent >= limit:
        return True, 0
    if recent < limit:
        return False, max(0, int(limit - recent))
    return False, 0


def is_model_quota_exhausted(rate_limits_by_model: dict[str, Any], model: str) -> bool:
    """按目标模型精确判断额度是否耗尽（各 premium 模型配额不同的正确姿势）。"""
    exhausted, _ = model_quota_state(rate_limits_by_model, model)
    return exhausted


def _reset_at_in_future(reset_at: Any) -> bool:
    """解析上游 quota 的 resetAt（RFC3339 / unix 秒 / unix 毫秒，对齐官方 CLI
    parseFlexTime 三种形态），判断是否仍在未来。解析失败视为不在未来。"""
    if reset_at is None or reset_at == "":
        return False
    # 数字形态：unix 秒或毫秒
    try:
        value = float(reset_at)
        if value > 1e12:  # 毫秒时间戳
            value /= 1000.0
        return value > time.time()
    except (TypeError, ValueError):
        pass
    # 字符串形态：ISO8601 / RFC3339
    text = str(reset_at).strip()
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() > time.time()
    except ValueError:
        return False


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

# ── Reasoning effort 档位与钳制 ─────────────────────────────────────
# 官方 per-model efforts 来自 orchestrator.js freebuff-models.ts：
#   deepseek-v4-flash / pro: ["low", "high", "max"]（default high）
#   gpt-5.6-luna:              ["low", "medium", "high", "xhigh", "max"]（default high）
#   muse-spark-1.2:            ["minimal", "low", "medium", "high", "xhigh"]（default xhigh）
#   claude-fable-5:            ["low", "medium", "high", "xhigh", "max"]（default high）
# 其余模型官方未定义 efforts（minimax-m3 官方 adaptive/disabled thinking，不设档位）。
# 第三方客户端传了模型不支持的档位时，按官方 efforts 表钳制到最近可用档位，
# 不拒绝、不换模型（与 worker.js normalizeReasoningEffort 语义一致）。
REASONING_EFFORT_RANK = {
    "minimal": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "max": 5,
    "ultra": 6,
}


def clamp_reasoning_effort(requested: str, allowed: tuple[str, ...]) -> str:
    """Clamp one reasoning effort to the nearest allowed value (never raise).

    Unknown requested values are passed through unchanged (upstream can decide).
    """
    if not allowed:
        return requested
    wanted = REASONING_EFFORT_RANK.get(requested)
    if wanted is None:
        return requested
    best: str | None = None
    best_rank = -1
    for candidate in allowed:
        rank = REASONING_EFFORT_RANK.get(candidate)
        if rank is None or rank > wanted:
            continue
        if rank > best_rank:
            best = candidate
            best_rank = rank
    if best is not None:
        return best
    # All allowed values are higher than requested → use the lowest allowed.
    return min(allowed, key=lambda candidate: REASONING_EFFORT_RANK.get(candidate, 99))


def normalize_reasoning_effort(model_id: str | None, effort: str | None) -> str | None:
    """按官方 0.0.63 模型表校验并归一化 ``reasoning_effort``。

    规则（对标桌面端 0.0.63）：
    - 模型官方支持 effort 调整（``reasoning_efforts`` 非空）：
      客户端传的值在允许列表内 → 放行；不在列表内 / 字段不对齐 → 回退官方默认值。
    - 模型官方不支持 effort 调整（``reasoning_efforts`` 为空）：
      一律返回 None（即不发送该字段，交给上游默认）。
    - 未知模型：返回 None（不干预，也不透传）。
    """
    if effort is None:
        return None
    try:
        model = resolve_model(model_id)
        allowed = model.reasoning_efforts
        default = model.default_reasoning_effort
    except ValueError:
        allowed = None
        default = None
    if not allowed:
        return None
    requested = str(effort)
    if requested in allowed:
        return requested
    return default


def default_reasoning_effort_for(model_id: str | None) -> str | None:
    """客户端**未传** effort 时回填官方默认档（走 ``codebuff_metadata``）。

    🔴 2026-09-13 用户决策（0.0.109）：官方客户端免费模式下用户不动档位时，
    ``turn.effort`` 为 null 且**不发送** ``freebuff_reasoning_effort``（orchestrator.js
    150233）。但我们落地"服务端替用户显式选档"：
    - deepseek-v4-flash 的 ``default_reasoning_effort`` 已按用户要求设为 **max**
      （官方 DEEPSEEK_V4_REASONING_EFFORTS 最大档，合法），不传 effort 即发 max；
    - 其余支持 effort 的模型（glm-5.3-flash/luna/muse/ox/gemini/fable/pro）按其官方
      ``defaultEffort`` 回填（都是官方允许列表内的值，不构成外来指纹）；
    - 官方不支持 effort 的模型（mimo/minimax/solar 等）或未知模型 → 返回 None，不发。
    """
    if not model_id:
        return None
    try:
        model = resolve_model(model_id)
    except ValueError:
        return None
    if not model.reasoning_efforts or model.default_reasoning_effort is None:
        return None
    return model.default_reasoning_effort


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
    # 动态表只提供 agent 映射；模型参数/effort 限制优先继承硬编码兜底表，
    # 避免动态刷新后 reasoning_effort 钳制、context_window/max_output_tokens 等丢失。
    hardcoded = _hardcoded_by_id(entry.id)
    return FreebuffModel(
        entry.id,
        entry.agent_id,
        base3_agent_id=entry.base3_agent_id,
        reviewer_agent_id=entry.reviewer_agent_id,
        context_window=hardcoded.context_window if hardcoded else 131_072,
        max_output_tokens=hardcoded.max_output_tokens if hardcoded else 32_768,
        input_modalities=hardcoded.input_modalities if hardcoded else ("text",),
        output_modalities=hardcoded.output_modalities if hardcoded else ("text",),
        reasoning_efforts=hardcoded.reasoning_efforts if hardcoded else None,
        default_reasoning_effort=hardcoded.default_reasoning_effort if hardcoded else None,
    )


def _hardcoded_by_id(model_id: str) -> FreebuffModel | None:
    for model in HARDCODED_MODELS:
        if model.id == model_id:
            return model
    return None


def all_models() -> list[FreebuffModel]:
    """Merged model list: hardcoded first, then dynamic entries not already present.

    god-only（蜜罐）模型绝不广播到 /v1/models —— 上游隐藏评测路由，第三方
    请求它形同自曝（见 GOD_ONLY_MODEL_IDS 注释）。
    """
    models = [m for m in HARDCODED_MODELS if not is_god_only_model(m.id)]
    seen = {model.id for model in models}
    if _registry is not None and _registry.table is not None:
        for entry in _registry.table.models:
            if entry.id in seen or is_god_only_model(entry.id):
                continue
            models.append(_model_from_dynamic(entry))
            seen.add(entry.id)
    return models


def resolve_model(requested: str | None) -> FreebuffModel:
    if not requested:
        return DEFAULT_MODEL

    # 🔴 蜜罐模型直接拒绝：不打上游、不进 session 链路，本地 fail-fast。
    if is_god_only_model(requested):
        raise ValueError(
            f"Model '{requested}' is a hidden upstream evaluation route "
            f"(god-only) and cannot be served"
        )

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
        "max_input_tokens": max(1, model.context_window - model.max_output_tokens),
        "max_output_tokens": model.max_output_tokens,
        "reasoning_efforts": list(model.reasoning_efforts) if model.reasoning_efforts else None,
        "default_reasoning_effort": model.default_reasoning_effort,
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

"""把上游错误/限流状态翻译成客户端可读的中文提示。

设计目标：
- 已知的**上游业务状态**（额度耗尽、高峰限流、地区限制、会话失效等）返回
  ``notice_for_error`` 非 None，由上层以**正常 200 响应**把提示作为模型内容
  返回给客户端（客户端不会当成错误，但用户能看到提示）。
- 未知错误返回 None，上层继续走标准错误响应；``describe_error`` 提供中文描述，
  附带原始英文信息，方便用户/管理员排查。
"""
from __future__ import annotations

from typing import Any

from .token_rotation import parse_429_info

NOTICE_PREFIX = "中转提示："

UNLIMITED_HINT = (
    "可以先切换到无限模型（deepseek/deepseek-v4-flash 或 mimo/mimo-v2.5）"
    "继续使用。"
)

# 🔴 2026-09-13 0.0.109 Freebucks 每小时定价表（FREEBUCKS_SESSION_PRICES，
# orchestrator.js 149717-149732）—— 小时价为"创建 session 一次性扣费"单位。
# 0.0.109 相对 0.0.96 的变化：
#   - 峰值加价 `FREEBUCKS_PEAK_SURCHARGE = 20`（0.0.96 是 +10），仅 deepseek-v4-flash
#     生效（`FREEBUCKS_PEAK_SURCHARGED_MODEL_IDS = [flash]`），至 3 AM PT；
#   - limited 未付费档 `FREEBUCKS_LIMITED_UNPAID_PRICES = {flash: {offPeak: 25,
#     peak: 40}}`（不在此表体现，属付费分级提示）；
#   - solar-pro4 改**动态定价** `solarOfferAt().price`：09-09T15:49Z 起促销 **0**
#     Freebucks（常态 5，SOLAR_REGULAR_OFFER）—— 当前 2026-09-13 按 0 记；
#   - 订阅档每日 Freebucks（FREEBUCKS_PLANS，149741-149778）：
#     full:  free 100 / starter 150+300 / plus 250+500 / pro 400+800
#     limited: free 25 / starter 105+300 / plus 200+500 / pro 350+800
#     每天太平洋午夜重置；kimi-k3-eco 与 luna-es 也在定价表（蜜罐，不计入建议）。
FREEBUCKS_HOURLY_PRICES = {
    "z-ai/glm-5.3-flash": 5,
    "mimo/mimo-v2.5": 10,
    "deepseek/deepseek-v4-flash": 15,  # 高峰 +20（至 3 AM PT）；limited 未付费 25/40
    "openai/gpt-5.6-luna": 20,
    "openai/gpt-5.6-luna-es": 20,
    "upstage/solar-pro4": 0,  # 🔴 solarOfferAt 动态：当前促销 0（常态 5）
    "crof/kimi-k3-eco": 5,
    "meta/muse-spark-1.3-contributor": 15,
    "meta/muse-spark-1.2-contributor": 15,
    "google/gemini-3.8-flash": 50,
}


def _freebucks_hint(model: str = "") -> str:
    """按 0.0.96 定价给出换便宜模型建议（免费档每日 100/25 Freebucks）。"""
    if not model:
        return ""
    price = FREEBUCKS_HOURLY_PRICES.get(model)
    if price is None:
        return ""
    cheaper = [
        mid for mid, p in FREEBUCKS_HOURLY_PRICES.items() if p < price
    ]
    if not cheaper:
        return ""
    names = " / ".join(cheaper)
    return f"当前模型每小时 {price} Freebucks，可先切换更便宜的模型（{names}）节省额度。"


def _rate_limit_notice(message: str, model: str = "") -> str:
    """官方 rate_limited（429）—— 0.0.96 起含 Freebucks 余额/月度/每日三分支。"""
    info = parse_429_info(message)
    reset_at = info.get("reset_at_sha") or "北京时间次日 15:00"
    lower = message.lower()

    # 🟢 0.0.96：freebucksShortfall（429 body 携带字段名，必含 "freebucksShortfall"；
    #   渲染文案 "costs Y Freebucks an hour and you have Z" 是客户端侧 errorFor 生成，
    #   上游 body 不一定带 → 以字段名/关键组合命中）
    if (
        "freebucksshortfall" in lower
        or ("freebucks" in lower and "an hour" in lower)
    ):
        return (
            NOTICE_PREFIX
            + "账号 Freebucks 余额不足，无法按该模型的小时价创建会话"
            + f"（{_freebucks_hint(model)}）。"
            + f"每日 Freebucks 将于 {reset_at} 自动重置，或等待今天额度恢复后重试。"
        )
    # 🟢 0.0.96：pacific_month —— 月度用量额度用尽（订阅计量）
    if "pacific_month" in lower or "month's usage allowance" in lower:
        return (
            NOTICE_PREFIX
            + "本账号本月免费用量配额已用完（月度重置），"
            + f"预计恢复时间：{reset_at}。{UNLIMITED_HINT}"
        )
    if info.get("model"):
        model_part = f"涉及模型：{info.get('model')}。"
    elif model:
        model_part = f"涉及模型：{model}。"
    else:
        model_part = ""
    return (
        f"官方免费额度已用完（每日限额）。{model_part}"
        f"预计恢复时间：{reset_at}。{UNLIMITED_HINT}"
    )


def notice_for_error(error: Exception, model: str = "") -> str | None:
    """返回可当作正常模型回复内容返回给客户端的中文提示；未知错误返回 None。"""
    original = str(error)
    lower = original.lower()
    status_code = getattr(error, "status_code", 0)

    # 我们自己的全局封禁闸门（某个账号被官方封禁后停止所有模型）
    if "disabled until the next 15:00" in lower:
        return (
            NOTICE_PREFIX
            + "检测到账号被官方封禁，服务已停止所有模型请求，"
            + "预计北京时间 15:00 自动恢复；请到 Token 页面检查被标记的账号。"
        )
    if "quota exhausted" in lower:
        return (
            NOTICE_PREFIX
            + "premium 免费额度已用完，预计北京时间 15:00 自动恢复。"
            + UNLIMITED_HINT
        )
    if "rate_limited" in lower:
        return NOTICE_PREFIX + _rate_limit_notice(original, model)
    if "spend_limited" in lower:
        return (
            NOTICE_PREFIX
            + "官方高峰时段限流中（上游模型价格翻倍，官方暂停消耗免费额度），"
            + "高峰结束后自动恢复；正在运行的任务不受影响。"
            + UNLIMITED_HINT
        )
    if "ip_capped" in lower:
        return (
            NOTICE_PREFIX
            + "当前出口 IP 使用免费服务的用户数已达官方上限，"
            + "请稍等几分钟后重试；若频繁出现，建议更换出口节点。"
        )
    if "model_unavailable" in lower:
        return (
            NOTICE_PREFIX
            + "当前模型暂不可用（官方限制），请切换到其他模型后重试。"
        )
    if "banned" in lower:
        return (
            NOTICE_PREFIX
            + "当前账号已被官方暂停，无法继续使用免费额度，请更换账号。"
        )
    if "country_blocked" in lower:
        return (
            NOTICE_PREFIX
            + "当前出口 IP 所在地区不支持官方免费模式，请更换到美国节点后重试。"
        )
    if "premium_slot_taken" in lower:
        return (
            NOTICE_PREFIX
            + "当前账号的 premium 通道被另一个实例占用，"
            + "服务已尝试释放旧会话，请重新发送一次。"
        )
    if "session_limit_reached" in lower:
        return (
            NOTICE_PREFIX
            + "官方会话并发达到上限，请稍后重试。"
        )
    if "free_mode_capacity_deferred" in lower or (
        "capacity" in lower and status_code == 429
    ):
        return (
            NOTICE_PREFIX
            + "官方免费通道当前容量已满，请稍后重试或切换模型。"
        )
    if "waiting_room_required" in lower or status_code == 428:
        return (
            NOTICE_PREFIX
            + "上游会话已失效（官方等待室），服务会自动重建会话，"
            + "请重新发送一次。"
        )
    if "session_expired" in lower or status_code == 410:
        return (
            NOTICE_PREFIX
            + "上游会话已过期，服务会自动重建会话，请重新发送一次。"
        )
    if "session_superseded" in lower:
        return (
            NOTICE_PREFIX
            + "上游会话被新实例占用，服务会自动重建会话，请重新发送一次。"
        )
    if "policy violation" in lower:
        return (
            NOTICE_PREFIX
            + "当前模型触发官方上游策略限制（Policy Violation，常见于 luna），"
            + "已临时停用该模型；请切换到其他模型，"
            + "或等北京时间 15:00 后自动恢复。"
        )
    if (
        "provider usage" in lower
        or "refill" in lower
        or "out of credits" in lower
        or status_code == 402
    ):
        return (
            NOTICE_PREFIX
            + "Freebuff 官方上游额度已用完（Provider usage error），"
            + "这是官方的问题，不是你的账号；请稍后重试。"
        )
    if "insufficient_quota" in lower:
        return (
            NOTICE_PREFIX
            + "官方免费通道当前负载较高（insufficient_quota），"
            + "请稍后重试或切换模型。"
        )
    if "empty stream" in lower or "空流" in lower:
        return (
            NOTICE_PREFIX
            + "上游返回空响应，通常是免费额度状态异常或会话过长导致；"
            + "请稍后重试或切换模型。"
        )
    if "session is not active" in lower:
        return (
            NOTICE_PREFIX
            + "官方会话未激活，可能是额度状态异常或账号受限；"
            + "请稍后重试或切换模型。"
        )
    # 🟢 2026-09-01 0.0.79 复核（orchestrator.js classifyTurnFailure 131580-131604）：
    # 桌面端新增两类改写提示：context_overflow（输入超长）和 connection（网络中断）。
    # 我们的反代用 httpx 也会抛 ECONNRESET / ETIMEDOUT / ENOTFOUND 等，被 _network_error
    # 包装后落到这里的 describe_error；为了让客户端看到更友好的中文提示，先在
    # notice_for_error 里识别。
    if (
        "context length" in lower
        or "input length exceeds" in lower
        or "input length should be" in lower
        or "context_overflow" in lower
    ):
        return (
            NOTICE_PREFIX
            + "当前会话上下文超出模型窗口，请开启新对话，"
            + "或切换到上下文更大的模型（如 deepseek-v4-flash 1M / glm-5.3-flash 1M / luna 1M）。"
        )
    if (
        "connection was interrupted" in lower
        or "econnreset" in lower
        or "etimedout" in lower
        or "enotfound" in lower
        or "eai_again" in lower
        or "fetch failed" in lower
    ):
        return (
            NOTICE_PREFIX
            + "网络连接被中断（ECONNRESET / ETIMEDOUT 等），"
            + "已保留到此为止的内容，请重新发送消息以继续。"
        )
    # 🟢 2026-09-01 0.0.79 复核：桌面端新增 account_changed（同一 tab 启动期间
    # token 切换），我们只在 token_rotation 触发时才会见到，这里也兜一份。
    if "account_changed" in lower:
        return (
            NOTICE_PREFIX
            + "上游账号信息发生变化（account_changed），"
            + "请稍后重试或联系管理员检查 token 配置。"
        )
    # 🟢 2026-09-02 0.0.86 复核（orchestrator.js SESSION_ENDED_MESSAGE 134679 + freebuffSessionGateError 134680-134688）：
    # 桌面端新增 `session_ended` 错误（`endsTheSession: !0` 的 GATE_CODE 都会折叠到这个 status）。
    # 含义：上游的 free session 在 turn 跑的过程中被回收了（per-model cap 耗尽 / 并发抢占 /
    # session_expired 30 分钟 grace / 主动 invalidate）。**官方客户端会自动
    # onFreebuffSessionExpired → admitFreebuffSession → 走 CHECKPOINT_CONTINUATION_PROMPT
    # 重放（orchestrator.js 136067-136072）**。我们反代需要做的是：把这条信息以
    # 200 正常 completion 返回给客户端，并附"重新发送一次以续接"指引。
    if (
        "session_ended" in lower
        or "free session ran out" in lower
        or "free session ended" in lower
    ):
        return (
            NOTICE_PREFIX
            + "官方免费会话在本次对话运行中被回收（session_ended），"
            + "通常由额度耗尽/会话被抢占/30 分钟无活动回收引起。"
            + "已保留此前输出，请直接重新发送一次以在新会话中继续。"
        )
    # 🟢 2026-09-08 0.0.96 复核（orchestrator.js 101052 + 102620-102629）：
    # 新增 `turn_spend_limit` 错误（FREEBUFF_TURN_SPEND_LIMIT_ERROR_CODE =
    # "turn_spend_limit"）。含义：单个 turn 达到了模型用量上限——**会话本身仍
    # 有效**，官方提示 "Your session is still available — send a new message to
    # continue from here."（isRetryable:false，不结束会话）。反代不要当
    # session 失效处理，也不要重试；返回软提示让客户端直接发新消息继续。
    if "turn_spend_limit" in lower or "reached its model usage limit" in lower:
        return (
            NOTICE_PREFIX
            + "本回合达到模型用量上限（turn_spend_limit），"
            + "当前会话仍然有效，请直接发送新消息从当前位置继续。"
        )
    return None


def describe_error(error: Exception) -> str:
    """给未知/中转自身错误提供中文描述，供标准错误响应使用。"""
    original = str(error)
    lower = original.lower()

    if "network error" in lower or "network error" in original.lower():
        return "中转服务无法连接官方上游服务器，请检查服务端网络/代理配置，或稍后重试。"
    if "FREEBUFF_TOKEN or CODEBUFF_TOKEN is required" in original:
        return "中转服务未配置上游账号 Token（FREEBUFF_TOKEN），请联系管理员在管理页配置。"
    if "no account available" in lower:
        return "中转服务当前没有可用账号，请稍后重试或联系管理员。"
    if "request body too large" in lower or "请求体过大" in original:
        return "请求体超过中转服务限制，请减小上下文/附件大小。"
    if "session_model_mismatch" in lower:
        return (
            "上游返回会话模型不匹配（session_model_mismatch），"
            "通常是账号被 limited tier 限制，上游把所有模型请求强制转为 mimo/mimo-v2.5。"
        )
    if "520" in original:
        return "官方上游服务器崩溃（520），与请求内容无关；请稍后重试或切换模型。"
    if "session is not active" in lower:
        return "官方未激活会话，可能是额度状态异常或账号受限，请稍后重试或切换模型。"
    return "中转服务遇到未分类错误，请稍后重试；若持续出现，请把原始信息发给管理员排查。"


def truncate_detail(text: str, limit: int = 300) -> str:
    """截断原始错误信息，避免过长的英文原文撑爆客户端显示。"""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"

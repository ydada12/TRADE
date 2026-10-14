"""模块3：事件轨（三分支预案）—— 核心模块。

流程：
  发布前：从真实经济日历取【公布北京时间 + 市场一致预期】，结合真实的支撑/压力位，
          让大模型生成三分支剧本（A 高于预期 / B 低于预期 / C 符合预期）。
  发布后：再拉【实际值 + 最新价】，判断落在哪个剧本，推送对号入座结果。

硬要求：
  - 公布时间、预期值、实际值必须来自真实 API（akshare / OKX），没有就写「数据源未提供」
  - 方向逻辑符合品种常识，不许写反
  - 只写「历史上通常怎么反应 + 当前落在哪个剧本」，禁止必涨必跌
  - 大模型不可用或输出可疑时，退回下面的确定性剧本模板（保证系统永远可用）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# 事件类别 -> 方向常识
#   strong: 数据「强于预期」时，对以下资产的历史影响
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("employment", ["非农", "就业", "失业率", "ADP", "初请", "续请"]),
    ("inflation", ["CPI", "PPI", "PCE", "通胀", "物价"]),
    ("rates", ["利率决议", "FOMC", "加息", "降息", "鲍威尔", "央行"]),
    ("oil_inventory", ["EIA", "API", "原油库存", "库欣", "汽油库存", "精炼油"]),
    ("growth", ["GDP", "零售", "PMI", "工业产出", "消费"]),
]

# 品种 -> 该资产对「风险偏好 / 美元 / 油价」的敏感方向
#   risk:  +1 表示风险偏好上升时该品种倾向上涨
#   oil:   +1 表示油价上升时该品种倾向上涨
INSTRUMENT_SENSITIVITY = {
    "TQQQ-USDT-SWAP": {"risk": 1.0, "oil": 0.0, "label": "纳指三倍做多"},
    "ETH-USDT": {"risk": 1.0, "oil": 0.0, "label": "以太坊"},
    "ETH-USDT-SWAP": {"risk": 1.0, "oil": 0.0, "label": "以太坊永续"},
    "CL-USDT-SWAP": {"risk": 0.3, "oil": 1.0, "label": "WTI 原油"},
}


def categorize(event_name: str) -> str:
    for category, keywords in CATEGORY_KEYWORDS:
        if any(k in event_name for k in keywords):
            return category
    return "generic"


@dataclass
class Branch:
    key: str            # A / B / C
    label: str
    text: str


def determine_branch(actual: float | None, forecast: float | None,
                     previous: float | None) -> tuple[str, str]:
    """返回 (分支, 判断依据说明)。"""
    if actual is None:
        return "unknown", "实际值数据源未提供"
    if forecast is not None:
        if actual > forecast:
            return "A", f"实际值 {actual:g} 高于预期 {forecast:g}"
        if actual < forecast:
            return "B", f"实际值 {actual:g} 低于预期 {forecast:g}"
        return "C", f"实际值 {actual:g} 与预期 {forecast:g} 一致"
    if previous is not None:
        if actual > previous:
            return "A", f"预期值数据源未提供，实际值 {actual:g} 高于前值 {previous:g}"
        if actual < previous:
            return "B", f"预期值数据源未提供，实际值 {actual:g} 低于前值 {previous:g}"
        return "C", f"预期值数据源未提供，实际值 {actual:g} 与前值 {previous:g} 一致"
    return "unknown", f"实际值 {actual:g}，但数据源既没给预期值也没给前值"


def _impact_sentence(category: str, strength: str, inst_id: str, region: str = "",
                     reference: str = "预期") -> str:
    """给定事件类别 + 强弱 + 地区，输出「历史上通常怎么反应」。

    地区很关键：中国 CPI 不该套「美联储偏鹰」的说法。
    reference：比较基准的措辞（预期 / 前值），保证与剧本标题口径一致。
    """
    sens = INSTRUMENT_SENSITIVITY.get(inst_id, {"risk": 0.5, "oil": 0.0, "label": inst_id})
    risk_sign = sens["risk"] >= 0
    oil_sign = sens["oil"] >= 0
    is_us = "美国" in (region or "")

    if category in {"employment", "inflation", "rates", "growth"}:
        if is_us:
            bank = "美联储"
            ccy = "美元"
        else:
            bank = "该国央行"
            ccy = "该国货币"

        common: dict[str, tuple[str, str]] = {
            "employment": (
                f"就业强于{reference}，历史上通常解读为就业市场偏紧、{bank}偏鹰，压制风险资产、利多{ccy}，黄金通常承压",
                f"就业弱于{reference}，历史上通常解读为经济动能转弱、降息预期升温，利多黄金、利空{ccy}，风险资产通常受益",
            ),
            "inflation": (
                f"通胀强于{reference}，历史上通常解读为降息推迟、{bank}偏鹰，压制风险资产、利空黄金",
                f"通胀弱于{reference}，历史上通常解读为降息预期升温，通常利多风险资产与黄金",
            ),
            "rates": (
                f"偏紧的利率信号（加息/鹰派措辞），历史上通常压制风险资产与黄金、利好{ccy}",
                f"偏松的利率信号（降息/鸽派措辞），历史上通常利好风险资产与黄金、压制{ccy}",
            ),
            "growth": (
                f"增长数据强于{reference}，历史上通常提振风险偏好",
                f"增长数据弱于{reference}，历史上通常打压风险偏好",
            ),
        }
        strong_text, weak_text = common[category]
    elif category == "oil_inventory":
        strong_text = f"库存增加（强于{reference}），历史上通常解读为供应宽松、利空油价"
        weak_text = f"库存下降（弱于{reference}），历史上通常解读为供应偏紧、利多油价"
    else:
        strong_text = f"数据强于{reference}，历史上通常先看它对{ccy if is_us else '美元'}与风险偏好的影响，再传导到本品种"
        weak_text = f"数据弱于{reference}，历史上通常先看它对{ccy if is_us else '美元'}与风险偏好的影响，再传导到本品种"

    base = strong_text if strength == "strong" else weak_text

    # 品种自身的方向修正
    if category in {"employment", "inflation", "rates", "growth"} and not risk_sign:
        base += f"（对 {sens['label']} 这类反向资产，历史方向通常相反）"
    if category == "oil_inventory" and not oil_sign:
        base += f"（{sens['label']} 通常与油价反向）"
    return base


def build_branch_facts(
    event: Any,
    symbol: dict[str, Any],
    levels_dict: dict[str, Any] | None,
) -> dict[str, Any]:
    """整理成给大模型 / 模板使用的纯事实包。每个数字都来自真实 API。"""
    return {
        "事件名称": event.name,
        "地区": event.region,
        "公布北京时间": f"{event.date} {event.time}",
        "重要性": event.importance_text(),
        "市场一致预期值": event.forecast_text(),
        "前值": event.previous_text(),
        "实际值": event.actual_text(),
        "品种": symbol.get("name"),
        "品种代码": symbol.get("inst_id"),
        "支撑位": (levels_dict or {}).get("nearest_support"),
        "压力位": (levels_dict or {}).get("nearest_resistance"),
        "支撑位列表": (levels_dict or {}).get("supports"),
        "压力位列表": (levels_dict or {}).get("resistances"),
    }


def render_fallback_script(facts: dict[str, Any], inst_id: str,
                           region: str = "") -> str:
    """确定性三分支剧本（大模型不可用时的兜底，方向逻辑仍然正确）。

    关键：数据源没给预期值时，绝不能假装「高于/低于预期」，
    而要老实切换成「高于/低于前值」的口径。
    """
    category = categorize(str(facts.get("事件名称") or ""))
    region = region or str(facts.get("地区") or "")
    support = facts.get("支撑位")
    resistance = facts.get("压力位")
    support_txt = f"{support:g}" if isinstance(support, (int, float)) else "数据源未提供"
    resistance_txt = f"{resistance:g}" if isinstance(resistance, (int, float)) else "数据源未提供"

    forecast_missing = facts.get("市场一致预期值") in (None, "数据源未提供预期值")
    reference = "前值" if forecast_missing else "预期"
    strong = _impact_sentence(category, "strong", inst_id, region, reference)
    weak = _impact_sentence(category, "weak", inst_id, region, reference)

    if forecast_missing:
        # 口径切换：只能拿前值当参照
        head_a = "── 剧本A：实际 > 前值（偏强口径）──\n"
        head_b = "── 剧本B：实际 < 前值（偏弱口径）──\n"
        head_c = "── 剧本C：实际 = 前值 ──\n"
        note = (
            "注意：数据源未提供市场一致预期值，因此这里只能按「实际值相对前值」判断，"
            "强度不如有预期值时可靠。\n"
        )
        tail_c = "与前值持平，历史上通常视作中性，先观望，等价格给方向。"
    else:
        head_a = "── 剧本A：实际 > 预期（偏强）──\n"
        head_b = "── 剧本B：实际 < 预期（偏弱）──\n"
        head_c = "── 剧本C：符合预期 ──\n"
        note = ""
        tail_c = "市场通常反应平淡，先观望，等价格给出方向再判断。"

    return (
        note
        + head_a
        + f"{strong}；若跌破支撑位 {support_txt}，偏弱确认。\n"
        + head_b
        + f"{weak}；若上破压力位 {resistance_txt}，偏强确认。\n"
        + head_c
        + tail_c
    )


def render_post_fallback(branch: str, reason: str, event: Any,
                         levels_dict: dict[str, Any] | None,
                         last_price: float | None) -> str:
    """确定性「对号入座」文案。

    注意：reason 里已经包含「实际值 X 高于/低于预期 Y」，这里不要再重复一遍。
    """
    from ..alert import fmt_num

    branch_label = {
        "A": "剧本A（强于预期）",
        "B": "剧本B（弱于预期）",
        "C": "剧本C（符合预期）",
        "unknown": "无法归类（数据源缺预期值与前值）",
    }.get(branch, "无法归类")

    parts = [f"{reason}，落在{branch_label}。"]
    support = (levels_dict or {}).get("nearest_support")
    resistance = (levels_dict or {}).get("nearest_resistance")
    if last_price is not None and support is not None and last_price < support:
        parts.append(f"当前价 {fmt_num(last_price)} 已跌破支撑位 {fmt_num(support)}。")
    elif last_price is not None and resistance is not None and last_price > resistance:
        parts.append(f"当前价 {fmt_num(last_price)} 已上破压力位 {fmt_num(resistance)}。")
    elif last_price is not None:
        parts.append(f"当前价 {fmt_num(last_price)} 仍在关键区间内。")
    parts.append("请按预案自行判断，注意控制仓位风险。")
    return "".join(parts)


def publish_time_text(event: Any, now: datetime) -> str:
    """把公布时间写成「今晚 20:30 / 明天 09:30」这种一眼懂的说法。"""
    publish = event.publish_dt
    if publish is None:
        return f"{event.date} {event.time}（北京时间）"
    delta_days = (publish.date() - now.date()).days
    clock = publish.strftime("%H:%M")
    if delta_days == 0:
        day_text = "今天"
    elif delta_days == 1:
        day_text = "明天"
    elif delta_days == 2:
        day_text = "后天"
    else:
        day_text = publish.strftime("%m月%d日")
    hours = (publish - now).total_seconds() / 3600.0
    if delta_days == 0 and 0 <= hours <= 8:
        return f"今晚 {clock}（北京时间）"
    return f"{day_text} {clock}（北京时间）"

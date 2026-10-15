"""告警文案渲染（严格遵守指定格式）+ 风控措辞过滤。

对应红线 1 / 3：
  - 【建议动作】永远只能出现「建议人工查看/等待确认」
  - 末尾永远带统一风险提示
  - 出现任何买卖措辞的输出一律拦截，改用安全文案
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

UTC8 = timezone(timedelta(hours=8))

SIGNAL_TREND = "趋势"
SIGNAL_EVENT = "事件"
SIGNAL_SHORT = "做空观察位"


def fmt_num(value: Any, digits: int | None = None) -> str:
    """统一的数字呈现：按数量级自适应精度，去掉多余的 0。

    为什么要自适应：ETH 是 2550 级别，TQQQ 是 81 级别，而某些 ETF 代币只有
    1.24 甚至 0.57 级别。固定 4 位小数会把 1.24433 截成 1.2443，丢掉有效数字。
    """
    if value is None:
        return "数据源未提供"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if digits is None:
        magnitude = abs(number)
        if magnitude >= 1000:
            digits = 2
        elif magnitude >= 100:
            digits = 2
        elif magnitude >= 10:
            digits = 3
        elif magnitude >= 1:
            digits = 5
        elif magnitude >= 0.01:
            digits = 6
        else:
            digits = 8

    text = f"{number:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def now_utc8_str() -> str:
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Alert:
    """推送内容。

    排序原则：把「一眼要看到的东西」放最前面。
    微信通知栏只显示标题，所以标题也必须带品种 + 发生了什么 + 现价。
    """

    symbol: str                      # 品种名（完整）
    what_happened: str               # 发生了什么（一句话）
    time_text: str                   # 发生/取数时间（北京时间 + UTC）
    price: float | None              # 当前位置
    support: float | None = None     # 支撑位
    resistance: float | None = None  # 压力位
    action_text: str = ""            # 当下该做什么（只允许固定话术）
    ai_text: str = ""                # AI 解读
    signal_type: str = ""            # 盘中波动 / 趋势 / 事件
    short_name: str = ""             # 标题用的短名，如 TQQQ
    title_hint: str = ""             # 标题用的极短概括，如「趋势多头未变」
    extra_lines: list[str] = field(default_factory=list)
    disclaimer: str = ""

    # 面板标注用：品种标识 + 数据时间（epoch ms）。带默认值，兼容现网全部关键字构造。
    # ts_ms 是「这条告警当时看到的价格对应的时间」，面板靠它把告警钉到对应的 K 线上。
    inst_id: str = ""
    ts_ms: int | None = None

    # ---------- 标题（微信通知栏只显示这一行，必须短且信息量大）----------
    def title(self) -> str:
        short = self.short_name or self.symbol
        hint = self.title_hint or self.what_happened_short()
        return f"【{short}】{hint}｜现价 {fmt_num(self.price)}"

    def what_happened_short(self) -> str:
        text = (self.what_happened or "").split("；")[0].split("　")[0].strip()
        return text[:18] + ("…" if len(text) > 18 else "")

    # ---------- 正文 ----------
    def render(self) -> str:
        """按「先看什么 → 再看什么」排版的 Markdown 正文（Server酱 desp 支持 Markdown）。

        层级：二级标题=哪条通知；粗体=发生了什么/当下该做什么；
             emoji 承担颜色语义（🔴 压力 / 🟢 支撑 / ⚠️ 行动）；块级空行=换行；
             --- 只用于切出「行动」与「风险提示」两段。
        """
        short = self.short_name or self.symbol
        kind = self.signal_type or "监控提醒"
        blocks: list[str] = [f"## 【{short}】{kind}", f"**{self.what_happened}**"]

        numbers = [f"🔴 **压力位** {self._level_line(self.resistance, above=True)}",
                   f"🟢 **支撑位** {self._level_line(self.support, above=False)}"]
        if self.price is not None:
            numbers.insert(0, f"📊 **现价** {fmt_num(self.price)}")
        blocks.append("　｜　".join(numbers))
        blocks.append(f"🕒 {self.time_text}")

        blocks.append("---")
        blocks.append("### ⚠️ 当下该做什么")
        blocks.append(f"**{self.action_text}**")

        if self.ai_text:
            blocks.append("### 🤖 AI 解读")
            blocks.append(self.ai_text)

        if self.extra_lines:
            blocks.append("### 📋 细节")
            blocks.extend(md_bullet(line) for line in self.extra_lines)

        blocks.append("---")
        blocks.append(f"> 【风险提示】{self.disclaimer}")
        # 段间空行：Server酱³ 要求两个换行才是真正的换行
        return "\n\n".join(blocks)

    def _level_line(self, level: float | None, above: bool) -> str:
        if level is None:
            return "数据源未提供"
        gap = ""
        if self.price:
            pct = (level - self.price) / self.price * 100.0
            gap = f"（{'上方' if pct >= 0 else '下方'} {abs(pct):.2f}%）"
        return f"{fmt_num(level)}{gap}"


def short_symbol(inst_id: str, name: str = "") -> str:
    """标题用的短名。TQQQ-USDT-SWAP -> TQQQ；拿不到就用名字前几个字。"""
    base = (inst_id or "").split("-")[0].strip()
    if base:
        return base
    return (name or "品种")[:10]


def zone_hint(price: float | None, support: float | None,
              resistance: float | None) -> str:
    """由「现价 vs 关键位」给出客观位置描述（不含任何买卖方向）。

    这是「当下该做什么」的事实依据，方向判断永远交给人。
    """
    if price is None:
        return "关键位数据不可用"
    if support is not None and price < support:
        return f"已跌破支撑位 {fmt_num(support)}，先看能否快速收回"
    if resistance is not None and price > resistance:
        return f"已上破压力位 {fmt_num(resistance)}，先看能否站稳"
    if support is not None and resistance is not None:
        return (f"价格仍在支撑 {fmt_num(support)} 与压力 {fmt_num(resistance)} 之间，"
                f"等它选方向")
    return "关键位不完整，等价格自己给方向"


@dataclass
class EventAlert:
    """事件轨推送（公布前剧本 / 公布后对号入座）。

    同样按「最重要 → 次要」排序：先看清楚哪个品种、发生了什么、什么时间。
    """

    headline: str                    # 事件名，例：美国9月CPI年率未季调(%)
    stage: str                       # pre / post
    happened: str                    # 发生了什么（一句话）
    time_text: str                   # 公布/发生时间
    body: str                        # 三分支剧本 或 对号入座结论
    symbol: str = ""
    short_name: str = ""
    price: float | None = None
    support: float | None = None
    resistance: float | None = None
    action_text: str = ""
    forecast_text: str = ""
    previous_text: str = ""
    title_hint: str = ""
    extra_lines: list[str] = field(default_factory=list)
    disclaimer: str = ""

    # 面板标注用（与 Alert 同口径）：品种标识 + 数据时间（epoch ms）。
    inst_id: str = ""
    ts_ms: int | None = None

    def title(self) -> str:
        prefix = "事件预警" if self.stage == "pre" else "事件结果"
        short = self.short_name or self.symbol or "事件"
        hint = self.title_hint or self.happened[:20]
        return f"【{short}】{prefix}：{hint}"

    def render(self) -> str:
        prefix = "事件预警" if self.stage == "pre" else "事件结果"
        short = self.short_name or self.symbol or "事件"
        blocks: list[str] = [f"## 【{short}】{prefix}", f"**{self.happened}**"]

        numbers = [f"🔴 **压力位** {_level_line(self.resistance, self.price, above=True)}",
                   f"🟢 **支撑位** {_level_line(self.support, self.price, above=False)}"]
        if self.price is not None:
            numbers.insert(0, f"📊 **现价** {fmt_num(self.price)}")
        blocks.append("　｜　".join(numbers))
        blocks.append(f"🕒 {self.time_text}")

        blocks.append("---")
        blocks.append("### ⚠️ 当下该做什么")
        blocks.append(f"**{self.action_text}**")

        blocks.append(f"📌 **信号类型** 事件（{'公布前' if self.stage == 'pre' else '公布后'}）")
        if self.stage == "pre":
            blocks.append(f"📈 **市场预期** {self.forecast_text or '数据源未提供'}"
                          f"　｜　📉 **前值** {self.previous_text or '数据源未提供'}")

        blocks.append("---")
        blocks.append(self.body)

        if self.extra_lines:
            blocks.extend(md_bullet(line) for line in self.extra_lines)

        blocks.append("---")
        blocks.append(f"> 【风险提示】{self.disclaimer}")
        return "\n\n".join(blocks)


def _level_line(level: float | None, price: float | None, above: bool) -> str:
    if level is None:
        return "数据源未提供"
    gap = ""
    if price:
        pct = (level - price) / price * 100.0
        gap = f"（{'上方' if pct >= 0 else '下方'} {abs(pct):.2f}%）"
    return f"{fmt_num(level)}{gap}"


class SafetyFilter:
    """拦截买卖措辞与确定性断言。"""

    def __init__(self, forbidden_words: list[str], fallback_text: str,
                 allowed_action_text: str, disclaimer: str) -> None:
        self.forbidden = [w for w in forbidden_words if w]
        self.fallback_text = fallback_text
        self.allowed_action_text = allowed_action_text
        self.disclaimer = disclaimer

    def violations(self, text: str) -> list[str]:
        return [word for word in self.forbidden if word in (text or "")]

    def sanitize_ai_text(self, text: str, fallback: str) -> tuple[str, bool]:
        """返回 (安全文本, 是否被拦截替换)。"""
        bad = self.violations(text)
        if bad:
            log.warning("AI 文案命中禁用词 %s —— 已替换为安全模板", bad)
            return fallback, True
        if not (text or "").strip():
            return fallback, True
        return text, False

    def enforce_action(self, text: str) -> str:
        """建议动作永远是固定话术，不给任何越界空间。"""
        return self.allowed_action_text

    def ensure_disclaimer(self, text: str) -> str:
        if self.disclaimer not in text:
            return f"{text}\n【风险提示】{self.disclaimer}"
        return text


# ------------------------------------------------------------
# 持仓盈亏（本地确定性计算，数字全部可追溯）
# ------------------------------------------------------------
@dataclass
class PositionPnl:
    side: str
    entry_price: float
    margin_usdt: float
    leverage: float
    last_price: float
    notional: float
    qty: float
    unrealized: float
    roe_pct: float
    price_change_pct: float


def compute_position_pnl(position: dict, last_price: float) -> PositionPnl | None:
    """由「你提供的持仓数据 + 真实最新价」算浮盈亏。

    公式（纯本地计算，不涉及任何交易接口）：
      notional = margin * leverage
      qty      = notional / entry
      盈亏     = (last - entry) * qty * (+1 多 / -1 空)
    """
    if not position:
        return None
    side = position.get("side")
    entry = position.get("entry_price")
    margin = position.get("margin_usdt")
    leverage = position.get("leverage")
    if side not in {"long", "short"} or entry in (None, 0) or margin in (None, 0) or leverage in (None, 0):
        return None
    try:
        entry_f, margin_f, lev_f = float(entry), float(margin), float(leverage)
    except (TypeError, ValueError):
        return None

    notional = margin_f * lev_f
    qty = notional / entry_f
    direction = 1.0 if side == "long" else -1.0
    unrealized = (last_price - entry_f) * qty * direction
    return PositionPnl(
        side=side,
        entry_price=entry_f,
        margin_usdt=margin_f,
        leverage=lev_f,
        last_price=last_price,
        notional=notional,
        qty=qty,
        unrealized=unrealized,
        roe_pct=unrealized / margin_f * 100.0,
        price_change_pct=(last_price - entry_f) / entry_f * 100.0 * direction,
    )


def render_position_line(pnl: PositionPnl) -> str:
    side_cn = "多单" if pnl.side == "long" else "空单"
    arrow = "浮盈" if pnl.unrealized >= 0 else "浮亏"
    return (
        f"【你的持仓】{side_cn} {pnl.leverage:g}x｜开仓价 {fmt_num(pnl.entry_price)}｜"
        f"保证金 {fmt_num(pnl.margin_usdt, 2)}U｜当前{arrow} {fmt_num(abs(pnl.unrealized), 2)}U"
        f"（约 {pnl.roe_pct:+.2f}% 保证金收益，价格{'' if pnl.side=='long' else '反向'}"
        f"{pnl.price_change_pct:+.2f}%）"
    )


def md_bullet(line: str) -> str:
    """把内部统一的「【标签】内容」转成 Markdown 列表项：- **标签**：内容。

    内部数据结构（含 extra_lines）保持【】写法不变，只在推送渲染时转成 Markdown。
    """
    text = re.sub(r"【([^】]+)】", r"**\1**：", (line or "").strip())
    return f"- {text}" if text else "-"


def sanitize_for_push(text: str) -> str:
    """微信通道不支持 HTML：剥掉原始 HTML 标签，并压缩多余空行，保留 Markdown。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

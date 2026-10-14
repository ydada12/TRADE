"""做空观察位打分（纯本地确定性计算）。

红线 1：这里只产出「观察位 / 失效位」这类客观描述，不下单、不给开仓指令、不给止损指令。
红线 2：所有数字都来自真实盘口 / 逐笔成交 / K 线，不由大模型生成。

盘口数据走 OKX 只读 REST（/market/books 盘口、/market/trades 逐笔成交），
与其它数据源一致地放在 datasource 层拉取；本模块只做纯函数计算，便于单测。

打分因子（对应「只提醒做空位置」方案）：
  1. 触及压力位       +2
  2. 被拒绝的确认     +2
  3. 盘口卖压失衡     +2
  4. 主动卖出占比偏高 +1
  5. 压力位附近卖墙   +1
  6. 卖压连续（上一轮同样是卖压） +1
  7. 周线多头（逆势做空） -counter_trend_penalty
达到阈值才产出「做空观察位」提醒。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..datasource.okx import OrderBook, Trade


@dataclass(frozen=True)
class OrderflowMetrics:
    """盘口 + 逐笔成交的汇总指标（全部来自真实数据，None 表示该项数据缺失）。"""
    imbalance: float | None          # 买/卖量失衡 (bid-ask)/(bid+ask)，负值=卖压
    taker_sell_ratio: float | None   # 主动卖出量 / 总成交量
    sell_wall_price: float | None    # 压力位附近卖墙价位
    sell_wall_ratio: float | None    # 卖墙挂单量 / 同侧中位挂单量
    trade_count: int
    depth_levels: int
    book_age_ms: int | None
    notes: list[str] = field(default_factory=list)

    def usable(self) -> bool:
        """盘口失衡与主动卖出占比都拿到，才认为订单流数据可用。"""
        return self.imbalance is not None and self.taker_sell_ratio is not None


def compute_orderflow_metrics(
    orderbook: OrderBook | None,
    trades: list[Trade] | None,
    price: float,
    resistance: float | None,
    *,
    top_levels: int = 10,
    wall_window_pct: float = 0.5,
    now_ms: int | None = None,
) -> OrderflowMetrics:
    """把盘口快照 + 逐笔成交压成一组可打分的指标。纯函数，无 I/O。"""
    notes: list[str] = []

    bids = list(orderbook.bids[:top_levels]) if orderbook else []
    asks = list(orderbook.asks[:top_levels]) if orderbook else []
    bid_vol = sum(lv.size for lv in bids)
    ask_vol = sum(lv.size for lv in asks)

    imbalance: float | None = None
    if bid_vol + ask_vol > 0:
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        notes.append(f"盘口前 {len(bids) + len(asks)} 档买/卖量失衡 {imbalance:+.2f}（负=卖压）")

    depth_levels = (len(orderbook.bids) + len(orderbook.asks)) if orderbook else 0

    book_age_ms: int | None = None
    if now_ms and orderbook and orderbook.ts_ms > 0:
        book_age_ms = max(0, now_ms - orderbook.ts_ms)

    # 主动卖出占比
    taker_sell_ratio: float | None = None
    trade_list = list(trades or [])
    if trade_list:
        total = sum(t.size for t in trade_list)
        if total > 0:
            sell = sum(t.size for t in trade_list if t.side == "sell")
            taker_sell_ratio = sell / total
            notes.append(f"近 {len(trade_list)} 笔主动卖出量占比 {taker_sell_ratio * 100:.0f}%")

    # 压力位附近的卖墙：同侧中位挂单量为基准，超过 wall 倍视为异常大单
    sell_wall_price: float | None = None
    sell_wall_ratio: float | None = None
    reference = resistance if resistance and resistance > 0 else price
    if asks and reference > 0:
        median_size = statistics.median([lv.size for lv in asks]) if asks else 0.0
        if median_size > 0:
            lo = reference * (1 - wall_window_pct / 100.0)
            hi = reference * (1 + wall_window_pct / 100.0)
            nearby = [lv for lv in asks if lo <= lv.price <= hi]
            if nearby:
                wall = max(nearby, key=lambda lv: lv.size)
                ratio = wall.size / median_size
                sell_wall_price = wall.price
                sell_wall_ratio = ratio
                notes.append(f"压力位附近卖墙 {wall.price:g} 挂单为同侧中位的 {ratio:.1f} 倍")

    return OrderflowMetrics(
        imbalance=imbalance,
        taker_sell_ratio=taker_sell_ratio,
        sell_wall_price=sell_wall_price,
        sell_wall_ratio=sell_wall_ratio,
        trade_count=len(trade_list),
        depth_levels=depth_levels,
        book_age_ms=book_age_ms,
        notes=notes,
    )


@dataclass
class ShortDecision:
    """做空观察位判断结果（纯计算结果，可直接落进推送）。"""
    alert: bool
    score: int
    reasons: list[str] = field(default_factory=list)
    data_ok: bool = False
    observation_level: float | None = None        # 观察区价位（通常=日内压力位）
    invalidation_level: float | None = None       # 失效位（站上则该观察条件不再成立）
    distance_pct: float | None = None             # 观察区相对现价（正=在上方）
    invalidation_distance_pct: float | None = None
    touched: bool = False
    rejected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert": self.alert,
            "score": self.score,
            "data_ok": self.data_ok,
            "observation_level": self.observation_level,
            "invalidation_level": self.invalidation_level,
            "distance_pct": self.distance_pct,
            "invalidation_distance_pct": self.invalidation_distance_pct,
            "touched": self.touched,
            "rejected": self.rejected,
            "reasons": self.reasons,
        }


def score_short_signal(
    current_price: float,
    resistance: float | None,
    recent_high: float | None,
    orderflow: OrderflowMetrics | None,
    weekly_state: str | None,
    *,
    pressure_persist: bool = False,
    touch_pct: float = 0.5,
    pullback_pct: float = 0.2,
    invalidation_pct: float = 1.0,
    imbalance_threshold: float = -0.2,
    taker_sell_ratio_threshold: float = 0.55,
    wall_ratio_threshold: float = 2.0,
    counter_trend_penalty: int = 2,
    score_threshold: int = 4,
) -> ShortDecision:
    """把「日内压力位 + 盘口卖压 + 周线环境」合成为一条做空观察判断。

    纯函数：同样输入必得同样输出，方便写单测。数据不足时绝不强推。
    """
    if not current_price or current_price <= 0 or not resistance or resistance <= 0:
        return ShortDecision(alert=False, score=0, data_ok=False, reasons=["缺少日内压力位或现价"])
    if orderflow is None or not orderflow.usable():
        return ShortDecision(alert=False, score=0, data_ok=False, reasons=["盘口/逐笔数据缺失"])

    reasons: list[str] = []
    score = 0

    distance_pct = (resistance - current_price) / current_price * 100.0
    invalidation_level = resistance * (1 + invalidation_pct / 100.0)
    invalidation_distance_pct = (invalidation_level - current_price) / current_price * 100.0

    # 1) 触及压力位 —— 这是做空观察的前提；下面的盘口确认只在触及后才计入，
    #    避免「价格离压力位还很远、只是盘口偏空」就误报。
    touched = False
    rejected = False
    gap = resistance * touch_pct / 100.0
    if recent_high is not None and recent_high > 0:
        touched = recent_high >= resistance - gap and abs(current_price - resistance) <= gap

    if touched:
        score += 2
        reasons.append(f"价格触及日内压力位 {resistance:g}")

        # 2) 被拒绝的确认：摸过压力位后又回落到下方，且回落幅度可见
        if recent_high is not None and current_price < resistance:
            pullback = (recent_high - current_price) / current_price * 100.0
            rejected = pullback >= pullback_pct
            if rejected:
                score += 2
                reasons.append(f"触及后回落 {pullback_pct:.1f}% 以上，上冲被拒绝")

        # 3) 盘口卖压失衡
        if orderflow.imbalance is not None and orderflow.imbalance <= imbalance_threshold:
            score += 2
            reasons.append(f"盘口卖压失衡 {orderflow.imbalance:+.2f}")

        # 4) 主动卖出占比偏高
        if (
            orderflow.taker_sell_ratio is not None
            and orderflow.taker_sell_ratio >= taker_sell_ratio_threshold
        ):
            score += 1
            reasons.append(f"主动卖出占比 {orderflow.taker_sell_ratio * 100:.0f}%")

        # 5) 压力位附近卖墙
        if (
            orderflow.sell_wall_price is not None
            and orderflow.sell_wall_ratio is not None
            and orderflow.sell_wall_ratio >= wall_ratio_threshold
        ):
            score += 1
            reasons.append(f"压力位附近挂出 {orderflow.sell_wall_ratio:.1f} 倍卖墙")

        # 6) 卖压连续（上一轮采样同样是卖压）
        if pressure_persist:
            score += 1
            reasons.append("盘口卖压连续两轮出现")

    # 7) 周线多头 → 逆势做空，扣分
    if weekly_state == "bull":
        score -= counter_trend_penalty
        reasons.append(f"周线多头，本次为逆势观察（-{counter_trend_penalty}）")

    alert = score >= score_threshold
    return ShortDecision(
        alert=alert,
        score=score,
        reasons=reasons,
        data_ok=True,
        observation_level=resistance,
        invalidation_level=invalidation_level,
        distance_pct=distance_pct,
        invalidation_distance_pct=invalidation_distance_pct,
        touched=touched,
        rejected=rejected,
    )

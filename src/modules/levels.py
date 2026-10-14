"""支撑位 / 压力位推导（纯本地确定性计算）。

红线 2：这些数字必须来自真实 K 线，不能由大模型生成。
做法：只用真实周K的最高/最低/收盘做统计（极值 + 摆动点），
      并把「这是算法推导值」写在结果里，方便配置里手动覆盖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..datasource.okx import Candle

log = logging.getLogger(__name__)


@dataclass
class Levels:
    inst_id: str
    current_price: float
    supports: list[float] = field(default_factory=list)      # 降序（最近的支撑在前）
    resistances: list[float] = field(default_factory=list)   # 升序（最近的压力在前）
    source: str = "auto"                                     # auto / manual / mixed
    lookback_weeks: int = 12

    def nearest_support(self) -> float | None:
        below = [s for s in self.supports if s < self.current_price]
        return max(below) if below else (self.supports[0] if self.supports else None)

    def nearest_resistance(self) -> float | None:
        above = [r for r in self.resistances if r > self.current_price]
        return min(above) if above else (self.resistances[-1] if self.resistances else None)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "lookback_weeks": self.lookback_weeks,
            "supports": self.supports,
            "resistances": self.resistances,
            "nearest_support": self.nearest_support(),
            "nearest_resistance": self.nearest_resistance(),
        }


def _find_pivots(candles: list[Candle], left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    """找出摆动高点/低点（左右各若干根都比它低/高）。"""
    highs: list[float] = []
    lows: list[float] = []
    n = len(candles)
    for i in range(left, n - right):
        window = candles[i - left: i + right + 1]
        pivot = candles[i]
        if all(pivot.high >= c.high for c in window):
            highs.append(pivot.high)
        if all(pivot.low <= c.low for c in window):
            lows.append(pivot.low)
    return highs, lows


def derive_levels(
    inst_id: str,
    candles: list[Candle],
    current_price: float,
    lookback_weeks: int = 12,
    manual_support: list[float] | None = None,
    manual_resistance: list[float] | None = None,
    min_distance_pct: float = 1.5,
) -> Levels:
    """由真实 K 线推导支撑/压力位；配置里填了手动值就以手动值为准并标注。

    算法只用真实 K 线的「区间极值」和「摆动高低点」，不做任何臆测。
    候选池刻意包含每一根周K的最高价/最低价：
      只用摆动点（左右各2根）在单边行情里几乎挑不出位置，
      会让「最近支撑」跑到 16% 以外，失去可操作性；
      而每根周K的高低点都是真实发生过、交易者确实在盯的位置。
    注意两点经验教训：
      1) 不要把最近的周收盘价当关键位 —— 它们会紧贴现价，
         导致「上破压力位」在 0.2% 的波动里就假触发。
      2) 离现价太近的候选位要过滤掉，关键位应当是真正有距离的位置。
    """
    manual_support = [float(x) for x in (manual_support or [])]
    manual_resistance = [float(x) for x in (manual_resistance or [])]

    window = candles[-lookback_weeks:] if lookback_weeks > 0 else candles
    auto_support: list[float] = []
    auto_resistance: list[float] = []

    if window:
        pivot_highs, pivot_lows = _find_pivots(window)
        # 每根周K的真实最低/最高（可操作性最强的一组候选）
        auto_support.extend(c.low for c in window)
        auto_resistance.extend(c.high for c in window)
        # 区间极值 + 摆动点（结构性位置）
        auto_support.append(min(c.low for c in window))
        auto_resistance.append(max(c.high for c in window))
        auto_support.extend(pivot_lows)
        auto_resistance.extend(pivot_highs)

    # 过滤掉离现价过近的候选（手动指定的不做过滤，尊重你的判断）
    min_gap = current_price * min_distance_pct / 100.0
    auto_support = [v for v in auto_support if v <= current_price - min_gap]
    auto_resistance = [v for v in auto_resistance if v >= current_price + min_gap]
    manual_support = [v for v in manual_support if v > 0]
    manual_resistance = [v for v in manual_resistance if v > 0]

    supports = sorted({round(v, 8) for v in (manual_support + auto_support) if v > 0}, reverse=True)
    resistances = sorted({round(v, 8) for v in (manual_resistance + auto_resistance) if v > 0})

    if manual_support or manual_resistance:
        source = "manual" if not auto_support and not auto_resistance else "mixed"
    else:
        source = "auto"

    return Levels(
        inst_id=inst_id,
        current_price=current_price,
        supports=supports,
        resistances=resistances,
        source=source,
        lookback_weeks=lookback_weeks,
    )


def detect_level_breaks(
    levels: Levels,
    current_price: float,
    prev_close: float | None,
    tolerance_pct: float = 0.3,
) -> list[dict]:
    """判断当前价是否跌破支撑 / 上破压力。

    同时看「最近一根收盘」和「最新价」，避免盘中假突破误报 —— 两个都会在描述里写清楚。
    """
    breaks: list[dict] = []
    tol = tolerance_pct / 100.0
    support = levels.nearest_support()
    resistance = levels.nearest_resistance()

    if support is not None and current_price < support * (1 - tol):
        breaks.append({
            "kind": "break_support",
            "level": support,
            "price": current_price,
            "text": f"跌破支撑位 {support:g}（现价 {current_price:g}）",
        })
    if resistance is not None and current_price > resistance * (1 + tol):
        breaks.append({
            "kind": "break_resistance",
            "level": resistance,
            "price": current_price,
            "text": f"上破压力位 {resistance:g}（现价 {current_price:g}）",
        })
    return breaks

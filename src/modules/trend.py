"""模块2：趋势轨（周线）—— 全部确定性计算，大模型不参与。

规则（可在 config 里开关）：
  1) 价格站上/跌破均线
  2) 均线上穿/下穿（金叉/死叉）

状态机：
  bull  = 收盘 > 主均线（含容差） 且 主均线向上
  bear  = 收盘 < 主均线（含容差） 且 主均线向下
  range = 其他

硬要求：只在【趋势状态发生变化】时告警，状态没变不刷屏。
数据不足时如实标注，绝不伪造均线值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..datasource.okx import Candle

log = logging.getLogger(__name__)

UTC8 = timezone(timedelta(hours=8))

STATE_LABEL = {
    "bull": "多头（价格站上主均线且均线上行）",
    "bear": "空头（价格跌破主均线且均线下行）",
    "range": "震荡（价格与均线关系不明确）",
    "unknown": "未知（历史数据不足）",
}


def to_utc8(dt: datetime) -> datetime:
    return dt.astimezone(UTC8)


def week_label(candle: Candle) -> str:
    """周K所属周的标签（北京时间）。"""
    return to_utc8(candle.dt_utc).strftime("%Y-%m-%d")


def sma(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(closes: list[float], period: int) -> list[float | None]:
    """返回与 closes 等长的 MA 序列，前 period-1 个为 None。"""
    out: list[float | None] = []
    running = 0.0
    for i, value in enumerate(closes):
        running += value
        if i >= period:
            running -= closes[i - period]
        out.append(running / period if i >= period - 1 else None)
    return out


@dataclass
class TrendSignal:
    kind: str          # price_cross_up / price_cross_down / ma_golden_cross / ma_dead_cross / state_change
    text: str
    detail: dict = field(default_factory=dict)


@dataclass
class TrendSnapshot:
    inst_id: str
    name: str
    as_of_week: str
    close: float                     # 最近一根【已收盘】周K的收盘价
    last_price: float                # 实时最新价
    ma_values: dict[int, float | None]
    ma_rising: dict[int, bool | None]
    state: str
    prev_state: str | None
    signals: list[TrendSignal]
    insufficient: list[str]
    weeks_available: int
    current_week_open: float | None
    current_week_label: str | None

    @property
    def state_label(self) -> str:
        return STATE_LABEL.get(self.state, self.state)

    @property
    def prev_state_label(self) -> str:
        if self.prev_state is None:
            return "首次运行（无历史状态）"
        return STATE_LABEL.get(self.prev_state, self.prev_state)

    @property
    def state_changed(self) -> bool:
        return self.prev_state is not None and self.prev_state != self.state

    def render_ma(self) -> str:
        parts = []
        for period, value in sorted(self.ma_values.items()):
            if value is None:
                parts.append(f"MA{period}=数据不足")
            else:
                arrow = ""
                rising = self.ma_rising.get(period)
                if rising is True:
                    arrow = "↑"
                elif rising is False:
                    arrow = "↓"
                parts.append(f"MA{period}={value:.4f}{arrow}")
        return "，".join(parts) if parts else "无可用均线"


def evaluate_trend(
    inst_id: str,
    name: str,
    candles: list[Candle],
    last_price: float,
    ma_periods: list[int],
    cross_pairs: list[list[int]] | None = None,
    primary_ma: str = "fast",
    tolerance_pct: float = 0.3,
    prev_state: str | None = None,
    price_vs_ma: bool = True,
    ma_cross: bool = True,
) -> TrendSnapshot:
    """计算趋势快照。candles 必须按时间升序，且包含未收盘的当前周K。"""
    confirmed = [c for c in candles if c.confirmed]
    current = [c for c in candles if not c.confirmed]
    current_week = current[-1] if current else None

    closes = [c.close for c in confirmed]
    insufficient: list[str] = []

    if not confirmed:
        raise ValueError(f"{inst_id} 没有任何已收盘周K，无法计算趋势")
    if len(confirmed) < 2:
        insufficient.append(f"已收盘周K只有 {len(confirmed)} 根，无法判断均线方向")

    # ---- 均线 ----
    ma_values: dict[int, float | None] = {}
    ma_rising: dict[int, bool | None] = {}
    series_cache: dict[int, list[float | None]] = {}
    for period in sorted(set(ma_periods)):
        series = sma_series(closes, period)
        series_cache[period] = series
        value = series[-1]
        ma_values[period] = value
        if value is None:
            insufficient.append(
                f"MA{period} 需要 {period} 根已收盘周K，当前只有 {len(confirmed)} 根 —— 该均线不可用"
            )
            ma_rising[period] = None
        else:
            previous = series[-2] if len(series) >= 2 else None
            ma_rising[period] = (value > previous) if previous is not None else None

    # ---- 主均线 ----
    ordered = sorted(set(ma_periods))
    if not ordered:
        raise ValueError(f"{inst_id} 的 ma_periods 为空")
    primary_period = ordered[0] if primary_ma == "fast" else ordered[-1]
    primary_value = ma_values.get(primary_period)
    primary_series = series_cache.get(primary_period, [])

    # ---- 状态机 ----
    tol = tolerance_pct / 100.0
    if primary_value is None:
        state = "unknown"
    else:
        above = closes[-1] > primary_value * (1 + tol)
        below = closes[-1] < primary_value * (1 - tol)
        rising = ma_rising.get(primary_period)
        if above and rising:
            state = "bull"
        elif below and rising is False:
            state = "bear"
        else:
            state = "range"

    # ---- 信号 ----
    signals: list[TrendSignal] = []

    if price_vs_ma and primary_value is not None and len(primary_series) >= 2:
        prev_ma = primary_series[-2]
        prev_close = closes[-2]
        if prev_ma is not None:
            if prev_close <= prev_ma and closes[-1] > primary_value * (1 + tol):
                signals.append(TrendSignal(
                    "price_cross_up",
                    f"周线收盘 {closes[-1]:.4f} 站上 MA{primary_period}（{primary_value:.4f}）",
                    {"ma_period": primary_period, "ma": primary_value, "close": closes[-1]},
                ))
            elif prev_close >= prev_ma and closes[-1] < primary_value * (1 - tol):
                signals.append(TrendSignal(
                    "price_cross_down",
                    f"周线收盘 {closes[-1]:.4f} 跌破 MA{primary_period}（{primary_value:.4f}）",
                    {"ma_period": primary_period, "ma": primary_value, "close": closes[-1]},
                ))

    if ma_cross:
        for pair in (cross_pairs or []):
            if len(pair) != 2:
                continue
            fast, slow = int(pair[0]), int(pair[1])
            fast_series, slow_series = series_cache.get(fast), series_cache.get(slow)
            if not fast_series or not slow_series or len(fast_series) < 2:
                continue
            f_now, s_now = fast_series[-1], slow_series[-1]
            f_prev, s_prev = fast_series[-2], slow_series[-2]
            if None in (f_now, s_now, f_prev, s_prev):
                continue
            if f_prev <= s_prev and f_now > s_now:
                signals.append(TrendSignal(
                    "ma_golden_cross",
                    f"MA{fast} 上穿 MA{slow}（金叉）：{f_now:.4f} / {s_now:.4f}",
                    {"fast": fast, "slow": slow, "fast_value": f_now, "slow_value": s_now},
                ))
            elif f_prev >= s_prev and f_now < s_now:
                signals.append(TrendSignal(
                    "ma_dead_cross",
                    f"MA{fast} 下穿 MA{slow}（死叉）：{f_now:.4f} / {s_now:.4f}",
                    {"fast": fast, "slow": slow, "fast_value": f_now, "slow_value": s_now},
                ))

    if prev_state is not None and prev_state != state:
        signals.insert(0, TrendSignal(
            "state_change",
            f"趋势状态由「{STATE_LABEL.get(prev_state, prev_state)}」变为「{STATE_LABEL.get(state, state)}」",
            {"from": prev_state, "to": state},
        ))

    return TrendSnapshot(
        inst_id=inst_id,
        name=name,
        as_of_week=week_label(confirmed[-1]),
        close=closes[-1],
        last_price=last_price,
        ma_values=ma_values,
        ma_rising=ma_rising,
        state=state,
        prev_state=prev_state,
        signals=signals,
        insufficient=insufficient,
        weeks_available=len(confirmed),
        current_week_open=current_week.open if current_week else None,
        current_week_label=week_label(current_week) if current_week else None,
    )

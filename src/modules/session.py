"""盘中波动监控（重点时段）。

补的缺口：趋势轨只在**周线状态变化**时告警，太慢。
实测 2026-10-12 12:00 → 10-13 11:00 北京，OKX TQQQ 永续从 82.55 跌到 78.04（-5.46%），
周线状态仍是 bull，系统一声不吭。

本模块做的事：
  在你配置的【重点时段】内（比如美股开盘 20:00-22:30 北京时间），
  用真实盘中K线计算「最近 N 分钟涨跌幅」和「相对时段开盘涨跌幅」，
  超过阈值就推送。

所有涨跌幅都由本地确定性计算得出，数据来自 OKX 真实K线，大模型不参与。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

from ..datasource.okx import Candle

log = logging.getLogger(__name__)

UTC8 = timezone(timedelta(hours=8))

DEFAULT_WINDOWS: list[dict[str, Any]] = [
    {"name": "美股盘前+开盘", "start": "20:00", "end": "22:30"},
    {"name": "午后监察", "start": "14:30", "end": "15:30"},
    {"name": "美股尾盘/收盘", "start": "03:00", "end": "05:00"},
]


def _parse_hhmm(text: str) -> dtime | None:
    try:
        hh, _, mm = str(text).partition(":")
        return dtime(int(hh), int(mm or 0))
    except (ValueError, TypeError):
        return None


@dataclass
class TimeWindow:
    name: str
    start: dtime
    end: dtime
    weekdays: list[int] = field(default_factory=list)   # 0=周一 ... 6=周日；空=每天

    def contains(self, moment: datetime) -> bool:
        if self.weekdays and moment.weekday() not in self.weekdays:
            return False
        current = moment.time()
        if self.start <= self.end:
            return self.start <= current <= self.end
        # 跨零点窗口，例如 23:00-01:00
        return current >= self.start or current <= self.end

    def start_moment(self, moment: datetime) -> datetime:
        """该窗口在 moment 所在的这一次开启时间点。"""
        start = moment.replace(hour=self.start.hour, minute=self.start.minute,
                               second=0, microsecond=0)
        if self.start > self.end and moment.time() <= self.end:
            # 处于跨零点窗口的「凌晨」那一段，窗口是昨天开的
            start -= timedelta(days=1)
        return start

    def label(self) -> str:
        return f"{self.name} {self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


def parse_windows(raw: list[dict[str, Any]] | None) -> list[TimeWindow]:
    windows: list[TimeWindow] = []
    for item in (raw or DEFAULT_WINDOWS):
        if not isinstance(item, dict):
            continue
        start = _parse_hhmm(item.get("start", ""))
        end = _parse_hhmm(item.get("end", ""))
        if start is None or end is None:
            log.warning("时段配置不合法，已跳过：%s", item)
            continue
        windows.append(TimeWindow(
            name=str(item.get("name") or "重点时段"),
            start=start,
            end=end,
            weekdays=[int(d) for d in (item.get("weekdays") or [])],
        ))
    return windows


def active_window(windows: list[TimeWindow], now_bj: datetime) -> TimeWindow | None:
    for window in windows:
        if window.contains(now_bj):
            return window
    return None


def _ref_candle(candles: list[Candle], target_ts_ms: int) -> Candle | None:
    """找 ts <= target 的最后一根（即 target 时刻的参考价）。"""
    found = None
    for candle in candles:
        if candle.ts_ms <= target_ts_ms:
            found = candle
        else:
            break
    return found


def detect_moves(
    candles: list[Candle],
    now_ms: int,
    rules: list[dict[str, Any]] | None,
    since_open_ms: int | None = None,
    since_open_pct: float | None = None,
    tolerance_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """计算所有触发阈值的波动。candles 必须按时间升序。"""
    if not candles:
        return []
    last = candles[-1]
    hits: list[dict[str, Any]] = []

    for rule in (rules or []):
        try:
            minutes = int(rule.get("window_minutes", 0))
            threshold = float(rule.get("pct", 0))
        except (TypeError, ValueError):
            continue
        if minutes <= 0 or threshold <= 0:
            continue
        ref = _ref_candle(candles, now_ms - minutes * 60_000)
        if ref is None or ref.close <= 0:
            continue
        change_pct = (last.close - ref.close) / ref.close * 100.0
        if abs(change_pct) >= threshold + tolerance_pct:
            hits.append({
                "kind": "window_move",
                "minutes": minutes,
                "threshold": threshold,
                "change_pct": change_pct,
                "ref_price": ref.close,
                "ref_time": ref.dt_utc.astimezone(UTC8).strftime("%m-%d %H:%M"),
                "last_price": last.close,
                "direction": "上涨" if change_pct > 0 else "下跌",
                "text": (
                    f"{minutes} 分钟内{direction_cn(change_pct)} {abs(change_pct):.2f}%"
                    f"（{ref.close:g} → {last.close:g}），超过设定阈值 {threshold:g}%"
                ),
            })

    if since_open_ms is not None:
        ref = _ref_candle(candles, since_open_ms)
        if ref is not None and ref.close > 0:
            change_pct = (last.close - ref.close) / ref.close * 100.0
            threshold = float(since_open_pct) if since_open_pct else 0.0
            if abs(change_pct) >= threshold:
                hits.append({
                    "kind": "since_open",
                    "minutes": None,
                    "threshold": threshold,
                    "change_pct": change_pct,
                    "ref_price": ref.close,
                    "ref_time": ref.dt_utc.astimezone(UTC8).strftime("%m-%d %H:%M"),
                    "last_price": last.close,
                    "direction": "上涨" if change_pct > 0 else "下跌",
                    "text": (
                        f"本时段开盘至今{direction_cn(change_pct)} {abs(change_pct):.2f}%"
                        f"（{ref.close:g} → {last.close:g}）"
                        + (f"，超过阈值 {threshold:g}%" if threshold > 0 else "")
                    ),
                })
    return hits


def direction_cn(change_pct: float) -> str:
    return "上涨" if change_pct > 0 else "下跌"


@dataclass
class GateDecision:
    """防刷屏判定结果。"""
    alert: bool
    reason: str
    bucket: int


def magnitude_bucket(pct: float, bucket_pct: float = 1.0) -> int:
    """把幅度映射成档位。1.0 表示 -1%~-2% 同一档，-2%~-3% 下一档。"""
    width = bucket_pct if bucket_pct > 0 else 1.0
    return int(abs(pct) // width)


def decide_alert(
    biggest_pct: float,
    last_ts: float | None,
    last_bucket: int,
    now_ts: float,
    cooldown_minutes: float,
    bucket_pct: float = 1.0,
) -> GateDecision:
    """决定这次波动要不要推送。

    规则（按优先级）：
      1) 幅度档位【更高】-> 立即推送（行情在恶化/升级，值得马上知道）
      2) 档位【更低】-> 不推送（小幅反弹不该重复打扰）
      3) 档位持平 且 冷却已过 -> 推送（提醒「还在持续」）
      4) 档位持平 且 冷却未过 -> 不推送
    """
    bucket = magnitude_bucket(biggest_pct, bucket_pct)

    if last_ts is None:
        return GateDecision(True, "首次触发", bucket)

    if bucket > last_bucket:
        return GateDecision(True, f"幅度升级（档位 {last_bucket} → {bucket}）", bucket)
    if bucket < last_bucket:
        return GateDecision(False, f"幅度回落（档位 {last_bucket} → {bucket}），不打扰", bucket)

    cooling = (now_ts - float(last_ts)) < cooldown_minutes * 60
    if cooling:
        remaining = cooldown_minutes * 60 - (now_ts - float(last_ts))
        return GateDecision(False, f"档位持平且冷却中（剩 {remaining/60:.1f} 分钟）", bucket)
    return GateDecision(True, "冷却已过，仍在持续", bucket)


_MISSING = object()


def record_for_window(record: dict[str, Any] | None,
                      window_start_ts: int | None) -> dict[str, Any]:
    """防刷屏记录只在【同一次时段开启】内有效，换了一次时段就当作没有记录。

    为什么需要：状态 key 是「品种|时段名」，每天都一样。如果不按时段实例区分，
    昨天晚上跌到 3 档会让今天 1~2 档的下跌全部被判成「幅度回落，不打扰」，
    直到跌破 3 档才会再响。

    window_start_ts：该时段本次开启时刻的秒级时间戳；手动不限时段的测试传 None。
    没有 window_start_ts 字段的旧记录（修复前写入的）一律视为过期，自动清掉。
    """
    if not record:
        return {}
    if record.get("window_start_ts", _MISSING) != window_start_ts:
        return {}
    return record


def render_fallback_text(symbol_name: str, window_label: str,
                         hits: list[dict[str, Any]],
                         levels: dict[str, Any] | None) -> str:
    """确定性文案（大模型不可用时的兜底）。

    注意：支撑/压力位已经显示在推送的顶部字段里，这里不再重复数字，只讲规律。
    """
    biggest = max(hits, key=lambda h: abs(h["change_pct"])) if hits else None
    parts: list[str] = []
    if biggest:
        parts.append(
            f"{symbol_name} 在「{window_label}」时段出现明显波动，"
            f"最大幅度 {biggest['direction']} {abs(biggest['change_pct']):.2f}%。"
        )
    parts.append(
        "这类时段波动通常由数据公布或资金集中进出放大，"
        "历史上往往需要观察价格能否在关键位附近站稳，再判断方向是否成立。"
    )
    return "".join(parts)

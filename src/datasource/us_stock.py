"""美股本土历史数据源（用于补齐 OKX 永续/代币化股票的历史深度）。

背景：
  OKX 的 TQQQ-USDT-SWAP 2026-06 才上线，只有 15 根已收盘周K，算不了 MA20/MA30。
  而美股本土 TQQQ 有多年历史，可以补上。

实测跟踪质量（70 个共同交易日）：
  日收益相关 0.9451，同向日占比 94.2%，日均价差 +0.205%
  但最大日收益差达 4.44 个百分点（周末跳空 + 资金费率 + 溢价）
  -> 因此本模块只用于「补历史均线」，实时价永远用 OKX 的真实价格，
     并且在告警里显式标注均线来自哪个数据源、以及两边的偏离度。

数据源（都是公开只读接口，无需 key）：
  主：Yahoo Finance chart API
  备：Stooq 公开 CSV（Yahoo 被限流时自动切换）
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..http_util import HttpClient, HttpError
from .okx import Candle

log = logging.getLogger(__name__)

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
STOOQ_CSV = "https://stooq.com/q/d/l/?s={symbol}&i=d"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trade-signal-monitor/1.0"


class UsStockClient:
    """美股日线/周线读取器。只读，无 key。"""

    def __init__(self, timeout: float = 30.0, max_retries: int = 4, backoff: float = 2.0) -> None:
        self.http = HttpClient(timeout=timeout, max_retries=max_retries, backoff=backoff,
                               user_agent=BROWSER_UA)

    # ---------------------------------------------------------------- 日线
    def get_daily_yahoo(self, symbol: str, range_: str = "5y") -> list[Candle]:
        payload = self.http.get_json(
            YAHOO_CHART.format(symbol=symbol),
            {"range": range_, "interval": "1d"},
        )
        try:
            result = payload["chart"]["result"][0]
            ts = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise HttpError(f"Yahoo 返回结构异常（{symbol}）：{exc}") from exc

        candles: list[Candle] = []
        for i, t in enumerate(ts):
            close = quote["close"][i]
            if close is None:
                continue
            candles.append(Candle(
                ts_ms=int(t) * 1000,
                open=float(quote["open"][i] if quote["open"][i] is not None else close),
                high=float(quote["high"][i] if quote["high"][i] is not None else close),
                low=float(quote["low"][i] if quote["low"][i] is not None else close),
                close=float(close),
                volume=float(quote["volume"][i] or 0),
                confirmed=True,
            ))
        return candles

    def get_daily_stooq(self, symbol: str) -> list[Candle]:
        stooq_symbol = symbol.lower() if symbol.endswith(".us") else f"{symbol.lower()}.us"
        resp = self.http.request("GET", STOOQ_CSV.format(symbol=stooq_symbol))
        text = resp.text
        if "Date" not in text[:200]:
            raise HttpError(f"Stooq 返回异常（{symbol}）：{text[:120]}")
        reader = csv.DictReader(io.StringIO(text))
        candles: list[Candle] = []
        for row in reader:
            try:
                dt = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                candles.append(Candle(
                    ts_ms=int(dt.timestamp() * 1000),
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row.get("Volume") or 0), confirmed=True,
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return candles

    def get_daily(self, symbol: str, range_: str = "5y") -> list[Candle]:
        """按时间升序返回日线；Yahoo 失败自动退回 Stooq。"""
        try:
            candles = self.get_daily_yahoo(symbol, range_)
            if candles:
                log.info("美股日线 %s：Yahoo 返回 %s 根", symbol, len(candles))
                return sorted(candles, key=lambda c: c.ts_ms)
            log.warning("Yahoo 返回 %s 空数据，改用 Stooq", symbol)
        except Exception as exc:
            log.warning("Yahoo 拉取 %s 失败（改用 Stooq）：%s", symbol, exc)

        candles = self.get_daily_stooq(symbol)
        log.info("美股日线 %s：Stooq 返回 %s 根", symbol, len(candles))
        return sorted(candles, key=lambda c: c.ts_ms)

    # ---------------------------------------------------------------- 周线
    @staticmethod
    def to_weekly(daily: list[Candle], today: date | None = None) -> list[Candle]:
        """把日线聚合成自然周（周一起算），并按是否收周决定 confirmed。

        和 OKX 周K的口径差异（重要）：
          - 美股周K：周一开盘 ~ 周五收盘，不含周末
          - OKX 1W ：周日 16:00 UTC ~ 下周日 16:00 UTC，含周末加密盘
        所以两边周K的收线本来就不完全等价。
        """
        today = today or datetime.now(timezone.utc).date()
        buckets: dict[date, list[Candle]] = {}
        for candle in daily:
            day = candle.dt_utc.date()
            monday = day - timedelta(days=day.weekday())
            buckets.setdefault(monday, []).append(candle)

        weekly: list[Candle] = []
        for monday in sorted(buckets):
            rows = sorted(buckets[monday], key=lambda c: c.ts_ms)
            friday = monday + timedelta(days=4)
            # 该周是否已经走完（以周五收盘为界）
            week_closed = today > friday
            weekly.append(Candle(
                ts_ms=rows[0].ts_ms,
                open=rows[0].open,
                high=max(r.high for r in rows),
                low=min(r.low for r in rows),
                close=rows[-1].close,
                volume=sum(r.volume for r in rows),
                confirmed=week_closed,
            ))
        return weekly

    def get_weekly(self, symbol: str, range_: str = "5y",
                   today: date | None = None) -> list[Candle]:
        return self.to_weekly(self.get_daily(symbol, range_), today=today)

    # ---------------------------------------------------------------- 校验
    def describe(self, symbol: str, range_: str = "5y") -> dict[str, Any]:
        weekly = self.get_weekly(symbol, range_)
        confirmed = [c for c in weekly if c.confirmed]
        return {
            "symbol": symbol,
            "weekly_total": len(weekly),
            "weekly_confirmed": len(confirmed),
            "first_week": weekly[0].dt_utc.strftime("%Y-%m-%d") if weekly else None,
            "last_week": weekly[-1].dt_utc.strftime("%Y-%m-%d") if weekly else None,
            "last_close": confirmed[-1].close if confirmed else None,
        }

"""OKX 公开行情接口（只用 market 行情端点，绝不碰 trade/account）。

所有数字都来自这个文件里的真实 API 响应，大模型不参与任何取数。

OKX candle 返回格式为「时间倒序」，元素：
  [ts, open, high, low, close, vol, volCcy, (volCcyQuote), confirm]
  SPOT 为 8 列，SWAP/FUTURES 为 9 列，因此这里按「前 6 位 + 最后一位 confirm」解析。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..http_util import HttpClient, HttpError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirmed: bool

    @property
    def dt_utc(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class Ticker:
    inst_id: str
    last: float
    open_24h: float
    high_24h: float
    low_24h: float
    vol_24h: float
    ts_ms: int

    @property
    def fetched_at_utc(self) -> datetime:
        return datetime.fromtimestamp(self.ts_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class BookLevel:
    """盘口一档：price=挂单价，size=挂单量（张/币）。"""
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    """OKX 盘口快照（只读）。bids 买盘降序（买一在前），asks 卖盘升序（卖一在前）。"""
    inst_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    ts_ms: int


@dataclass(frozen=True)
class Trade:
    """逐笔成交（只读）。side 是主动方方向：buy=主动买入，sell=主动卖出。"""
    ts_ms: int
    price: float
    size: float
    side: str


class OkxClient:
    def __init__(
        self,
        base_url: str = "https://www.okx.com",
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(timeout=timeout, max_retries=max_retries, backoff=backoff)
        self._inst_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    # ---------- 内部 ----------
    def _api(self, path: str, params: dict[str, Any]) -> list[Any]:
        url = f"{self.base_url}{path}"
        payload = self.http.get_json(url, params=params)
        if not isinstance(payload, dict):
            raise HttpError(f"OKX 返回结构异常：{url}")
        code = str(payload.get("code"))
        if code != "0":
            raise HttpError(f"OKX 报错 code={code} msg={payload.get('msg')} ({path})")
        data = payload.get("data")
        if not isinstance(data, list):
            raise HttpError(f"OKX 返回 data 非列表：{url}")
        return data

    @staticmethod
    def _parse_candle(row: list[str]) -> Candle | None:
        if not isinstance(row, list) or len(row) < 7:
            return None
        try:
            return Candle(
                ts_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                confirmed=str(row[-1]) == "1",
            )
        except (TypeError, ValueError):
            return None

    # ---------- 公开方法 ----------
    INSTRUMENT_TTL_SECONDS = 6 * 3600

    def get_instrument(self, inst_id: str, inst_type: str) -> dict[str, Any] | None:
        """确认交易对真实存在。返回 None 表示不存在（调用方必须报错而不是猜）。

        存在的合约缓存 6 小时（上市状态不会每分钟变）；不存在的不缓存，每次都如实报错。
        """
        key = (inst_type, inst_id)
        hit = self._inst_cache.get(key)
        if hit and time.time() - hit[0] < self.INSTRUMENT_TTL_SECONDS:
            return hit[1]
        rows = self._api("/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
        inst = rows[0] if rows else None
        if inst is not None:
            self._inst_cache[key] = (time.time(), inst)
        return inst

    def get_ticker(self, inst_id: str) -> Ticker:
        rows = self._api("/api/v5/market/ticker", {"instId": inst_id})
        if not rows:
            raise HttpError(f"OKX 没有返回 {inst_id} 的行情")
        d = rows[0]
        return Ticker(
            inst_id=inst_id,
            last=float(d["last"]),
            open_24h=float(d.get("open24h") or 0),
            high_24h=float(d.get("high24h") or 0),
            low_24h=float(d.get("low24h") or 0),
            vol_24h=float(d.get("vol24h") or 0),
            ts_ms=int(d["ts"]),
        )

    def get_weekly_candles(self, inst_id: str, bar: str = "1W", limit: int = 300) -> list[Candle]:
        """拉周K，返回按时间【升序】排列的列表。"""
        return self.get_candles(inst_id, bar=bar, limit=limit)

    @staticmethod
    def _parse_levels(raw: Any) -> list[BookLevel]:
        levels: list[BookLevel] = []
        for row in raw or []:
            try:
                levels.append(BookLevel(price=float(row[0]), size=float(row[1])))
            except (IndexError, TypeError, ValueError):
                continue
        return levels

    def get_orderbook(self, inst_id: str, depth: int = 50) -> OrderBook:
        """只读拉取盘口快照。depth 建议 50（够了，太多档位噪声更大）。"""
        rows = self._api("/api/v5/market/books", {"instId": inst_id, "sz": str(int(depth))})
        if not rows:
            raise HttpError(f"OKX 没有返回 {inst_id} 的盘口")
        d = rows[0]
        return OrderBook(
            inst_id=inst_id,
            bids=self._parse_levels(d.get("bids")),
            asks=self._parse_levels(d.get("asks")),
            ts_ms=int(d.get("ts") or 0),
        )

    def get_trades(self, inst_id: str, limit: int = 100) -> list[Trade]:
        """只读拉取最近逐笔成交（side 为主动方方向）。"""
        rows = self._api("/api/v5/market/trades", {"instId": inst_id, "limit": str(int(limit))})
        trades: list[Trade] = []
        for r in rows or []:
            try:
                trades.append(
                    Trade(
                        ts_ms=int(r["ts"]),
                        price=float(r["px"]),
                        size=float(r["sz"]),
                        side=str(r.get("side") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return trades

    def get_candles(self, inst_id: str, bar: str = "5m", limit: int = 300) -> list[Candle]:
        """拉任意周期K线，返回按时间【升序】排列的列表。

        合并 /market/candles（最近）与 /market/history-candles（更早），
        这样可以拿到该周期能提供的全部历史。
        盘中监控用 bar="5m"、limit=288 即可覆盖 24 小时。
        """
        collected: dict[int, Candle] = {}

        for path in ("/api/v5/market/candles", "/api/v5/market/history-candles"):
            page_limit = 100 if "history" in path else min(limit, 300)
            after: str | None = None
            while True:
                params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": page_limit}
                if after:
                    params["after"] = after
                try:
                    rows = self._api(path, params)
                except HttpError as exc:
                    log.warning("%s 拉取失败（继续用已有数据）：%s", path, exc)
                    break
                if not rows:
                    break
                parsed = [c for c in (self._parse_candle(r) for r in rows) if c]
                if not parsed:
                    break
                for candle in parsed:
                    collected[candle.ts_ms] = candle
                oldest = min(c.ts_ms for c in parsed)
                if after is not None and oldest >= int(after):
                    break  # 没有更早的数据了，防死循环
                after = str(oldest)
                if len(collected) >= limit:
                    break
            if len(collected) >= limit:
                break

        candles = sorted(collected.values(), key=lambda c: c.ts_ms)
        if limit and len(candles) > limit:
            candles = candles[-limit:]
        return candles

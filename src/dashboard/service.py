"""面板的数据聚合层：只读地取真实数据，带 TTL 缓存与单飞锁。

为什么要有缓存 + 单飞锁：
  面板每次刷新都会打 OKX 公开接口（ticker / K线），而 OKX 有限流。
  TTL 缓存让「同一份数据」在周期内只打一次；
  单飞锁让「同一品种同一周期」的并发请求只真的打一次上游，其余等结果。

接口失败怎么办：**返回 None，并且不缓存**。
  宁可让页面显示「数据源未提供」并下次重试，也不把上一次的旧值当新值显示——
  那等于对用户编造行情。

趋势状态怎么拿：直接读 `StateStore` 里监控进程写好的记录，**不重算 evaluate_trend**。
  重算会引入 LLM/推送链路，而面板的定位是「只读展示」，不该有副作用。

控制页（/api/control）：本地控制台唯一允许写的地方，只改运行期的监控与推送偏好
  （品种开关 / 免打扰 / 重点提醒 / 持仓覆盖），写进 state/control.json。
  仍然不接交易接口、不下单、不发推送、不调大模型。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from ..config import Config
from ..control import ControlError, ControlStore
from ..dashboard.history import AlertHistory, AlertRecord, bar_ms, ms_to_bj
from ..datasource.econ_calendar import BEIJING
from ..datasource.okx import Candle, HttpError, OkxClient
from ..datasource.us_stock import UsStockClient
from ..modules.levels import derive_levels
from ..modules.trend import STATE_LABEL
from ..state import StateStore
from ..alert import short_symbol

log = logging.getLogger(__name__)

# 面板支持的周期（前端按钮组与白名单校验共用这一份）
BARS = ("5m", "15m", "1H", "4H", "1D", "1W")
DEFAULT_BAR = "5m"
KLINE_MAX = 500

_LABEL_FMT = {
    "1W": "%Y-%m-%d",
    "1D": "%Y-%m-%d",
}


class DashboardService:
    def __init__(self, cfg: Config, *, ttl_seconds: float = 60.0,
                 kline_max: int = KLINE_MAX,
                 history: AlertHistory | None = None,
                 state_store: StateStore | None = None) -> None:
        self.cfg = cfg
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.kline_max = max(50, int(kline_max))
        app = cfg.get("app", {}) or {}
        market = cfg.get("market", {}) or {}

        self.okx = OkxClient(
            base_url=str(market.get("base_url", "https://www.okx.com")),
            timeout=float(market.get("timeout_seconds", 20)),
            max_retries=int(market.get("max_retries", 4)),
            backoff=float(market.get("retry_backoff_seconds", 2)),
        )
        self.us = UsStockClient()
        self.history = history or AlertHistory(
            cfg.resolve_path(str(app.get("alert_history_file", "state/alerts.jsonl")))
        )
        self.state = state_store or StateStore(
            cfg.resolve_path(str(app.get("state_file", "state/state.json")))
        )
        # 运行期控制项：控制页读写它；监控进程每轮重读，改完即刻生效
        self.control = ControlStore(
            cfg.resolve_path(str(app.get("control_file", "state/control.json"))),
            valid_inst_ids=[str(s.get("inst_id")) for s in cfg.symbols],
        )
        # 周K一周才收一根，沿用监控进程的缓存口径，避免面板反复拉 Yahoo/OKX
        self.weekly_ttl_seconds = float(market.get("weekly_cache_minutes", 30)) * 60.0

        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._flight: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------
    # 缓存骨架
    # ------------------------------------------------------------
    def _flight_lock(self, key: str) -> threading.Lock:
        with self._cache_lock:
            lock = self._flight.get(key)
            if lock is None:
                lock = threading.Lock()
                self._flight[key] = lock
            return lock

    def _cached(self, key: str, ttl: float, producer: Callable[[], Any]) -> Any:
        """命中则返回缓存；未命中则只让一个请求真的去拉，其余等它。

        上游失败返回 None 且**不写缓存**，这样下一次刷新还会重试，
        页面也会如实显示「数据源未提供」。
        """
        hit = self._cache.get(key)
        if hit and time.time() < hit[0]:
            return hit[1]
        with self._flight_lock(key):
            hit = self._cache.get(key)
            if hit and time.time() < hit[0]:
                return hit[1]
            try:
                value = producer()
            except (HttpError, OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("面板取数失败（不缓存，下次重试）%s：%s", key, exc)
                return None
            except Exception as exc:  # 上游异常类型不可控，面板绝不能因此 500
                log.warning("面板取数异常（不缓存，下次重试）%s：%s", key, exc)
                return None
            if value is not None:
                self._cache[key] = (time.time() + ttl, value)
            return value

    # ------------------------------------------------------------
    # 品种
    # ------------------------------------------------------------
    def symbols(self) -> list[dict[str, Any]]:
        """全部品种（含已停用的，前端灰显但仍可看 K 线）。

        enabled 展示的是「生效值」：控制页开过关就按控制台，否则按 config。
        """
        out: list[dict[str, Any]] = []
        for symbol in self.cfg.symbols:
            inst_id = str(symbol.get("inst_id") or "")
            if not inst_id:
                continue
            name = str(symbol.get("name") or inst_id)
            source = str(symbol.get("history_source") or "okx")
            override = self.control.symbol_override(inst_id)
            config_enabled = bool(symbol.get("enabled", True))
            out.append({
                "inst_id": inst_id,
                "name": name,
                "short_name": short_symbol(inst_id, name),
                "enabled": self._effective_enabled(symbol),
                "config_enabled": config_enabled,
                "override": override,
                "priority": inst_id in self.control.priority_symbols(),
                "inst_type": str(symbol.get("inst_type") or ""),
                "history_source": source,
                "source_desc": self._source_desc(symbol),
                "ma_periods": [int(p) for p in (symbol.get("ma_periods") or [])],
                "has_position": (symbol.get("position") or {}).get("entry_price") is not None,
            })
        return out

    @staticmethod
    def _source_desc(symbol: dict[str, Any]) -> str:
        source = str(symbol.get("history_source") or "okx")
        if source == "us_stock":
            return f"周K历史来自美股本土 {symbol.get('us_symbol')}，实时价来自 OKX"
        return "周K与实时价均来自 OKX"

    def _symbol(self, inst_id: str) -> dict[str, Any]:
        symbol = self.cfg.symbol_by_inst_id(inst_id)
        if symbol is None:
            raise ValueError(f"配置里没有这个品种：{inst_id}")
        return symbol

    # ------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------
    def ticker(self, inst_id: str) -> dict[str, Any] | None:
        self._symbol(inst_id)  # 白名单校验：只允许配置里存在的品种
        raw = self._cached(f"ticker|{inst_id}", self.ttl_seconds,
                           lambda: self.okx.get_ticker(inst_id))
        if raw is None:
            return None
        return {
            "inst_id": inst_id,
            "last": raw.last,
            "open_24h": raw.open_24h,
            "high_24h": raw.high_24h,
            "low_24h": raw.low_24h,
            "vol_24h": raw.vol_24h,
            "ts_ms": raw.ts_ms,
            "ts_text": ms_to_bj(raw.ts_ms),
        }

    def weekly_candles(self, inst_id: str) -> list[Candle] | None:
        """周K：按 history_source 分流（与监控进程保持同一来源）。"""
        symbol = self._symbol(inst_id)
        source = str(symbol.get("history_source") or "okx")
        limit = int(self.cfg.get("market.weekly_candle_limit", 300))

        def produce() -> list[Candle]:
            if source == "us_stock":
                return self.us.get_weekly(str(symbol.get("us_symbol")),
                                          str(symbol.get("us_history_range", "5y")))
            return self.okx.get_weekly_candles(inst_id, str(self.cfg.get("market.weekly_bar", "1W")),
                                               limit)

        return self._cached(f"weekly|{inst_id}", self.weekly_ttl_seconds, produce)

    def kline(self, inst_id: str, bar: str = DEFAULT_BAR,
              limit: int | None = None) -> dict[str, Any] | None:
        """K 线（只读）。返回值里已含 epoch ms 与北京时间的分类标签，前端直接画。"""
        bar = bar if bar in BARS else DEFAULT_BAR
        want = min(int(limit or self.kline_max), self.kline_max)
        ttl = self.weekly_ttl_seconds if bar == "1W" else self.ttl_seconds

        if bar == "1W":
            candles = self.weekly_candles(inst_id)
        else:
            candles = self._cached(f"kline|{inst_id}|{bar}|{want}", ttl,
                                   lambda: self.okx.get_candles(inst_id, bar, want))
        if not candles:
            return None
        candles = list(candles)[-want:]

        fmt = _LABEL_FMT.get(bar, "%Y-%m-%d %H:%M")
        return {
            "inst_id": inst_id,
            "bar": bar,
            "bar_ms": bar_ms(bar),
            "source_desc": self._source_desc(self._symbol(inst_id)),
            "count": len(candles),
            "times": [c.ts_ms for c in candles],
            "labels": [ms_to_bj(c.ts_ms, fmt) for c in candles],
            "ohlc": [[c.open, c.close, c.low, c.high] for c in candles],
            "volumes": [c.volume for c in candles],
            "confirmed": [c.confirmed for c in candles],
        }

    def levels(self, inst_id: str) -> dict[str, Any] | None:
        """关键位（真实周K推导，复用监控进程同一套算法）。"""
        symbol = self._symbol(inst_id)
        ticker = self.ticker(inst_id)
        price = ticker["last"] if ticker else None
        if price is None:
            return None  # 没有现价就没法推导「离现价最近的关键位」

        def produce() -> dict[str, Any]:
            candles = self.weekly_candles(inst_id)
            if not candles:
                return None
            confirmed = [c for c in candles if c.confirmed]
            cfg_levels = symbol.get("levels") or {}
            result = derive_levels(
                inst_id=inst_id,
                candles=confirmed,
                current_price=float(price),
                lookback_weeks=int(cfg_levels.get("lookback_weeks", 12)),
                manual_support=cfg_levels.get("manual_support"),
                manual_resistance=cfg_levels.get("manual_resistance"),
                min_distance_pct=float(cfg_levels.get("min_distance_pct", 1.5)),
            )
            return result.as_dict()

        return self._cached(f"levels|{inst_id}", self.weekly_ttl_seconds, produce)

    def trend_state(self, inst_id: str) -> dict[str, Any] | None:
        """直接读监控进程落盘的趋势状态，不重算。"""
        try:
            record = self.state.get_trend_state(inst_id)
        except Exception as exc:  # state.json 可能正被监控进程写入，读坏了就如实报缺
            log.warning("读取趋势状态失败（%s）：%s", inst_id, exc)
            return None
        if not isinstance(record, dict) or not record:
            return None
        state = record.get("state")
        return {
            "state": state,
            "label": STATE_LABEL.get(str(state), "未记录"),
            "as_of_week": record.get("as_of_week"),
            "close": record.get("close"),
            "ma_values": record.get("ma_values") or {},
            "updated_at": record.get("updated_at"),
        }

    def snapshot(self, inst_id: str) -> dict[str, Any]:
        """面板左上角那张卡的全部内容。缺失一律 null，由前端显示「数据源未提供」。"""
        symbol = self._symbol(inst_id)
        ticker = self.ticker(inst_id)
        levels = self.levels(inst_id)
        action = str(self.cfg.get("allowed_action_text", "建议人工查看/等待确认"))
        notes: list[str] = [self._source_desc(symbol)]

        if levels:
            sup, res = levels.get("nearest_support"), levels.get("nearest_resistance")
        else:
            sup = res = None
        if not ticker:
            notes.append("行情接口取数失败，本卡片数字不可用（下次刷新会自动重试）")
        if not levels:
            notes.append("关键位推导失败，本卡片不含支撑/压力（下次刷新会自动重试）")

        # 24h 涨跌幅只能由 OKX 真实返回的 last / open24h 两个字段推算。
        # open24h 缺失（None 或 0）时给 None，由前端显示「数据源未提供」，绝不编造。
        price = ticker["last"] if ticker else None
        open_24h = ticker["open_24h"] if ticker else None
        change_pct = None
        if price is not None and open_24h:
            change_pct = round((float(price) - float(open_24h)) / float(open_24h) * 100.0, 2)

        position, position_source = self._position_for(symbol)

        return {
            "inst_id": inst_id,
            "name": symbol.get("name"),
            "short_name": short_symbol(inst_id, str(symbol.get("name") or "")),
            "enabled": self._effective_enabled(symbol),
            "position": position,
            "position_source": position_source,
            "price": price,
            "ts_ms": ticker["ts_ms"] if ticker else None,
            "ts_text": ticker["ts_text"] if ticker else "",
            "open_24h": open_24h,
            "change_24h_pct": change_pct,
            "high_24h": ticker["high_24h"] if ticker else None,
            "low_24h": ticker["low_24h"] if ticker else None,
            "vol_24h": ticker["vol_24h"] if ticker else None,
            "support": sup,
            "resistance": res,
            "supports": (levels or {}).get("supports") or [],
            "resistances": (levels or {}).get("resistances") or [],
            "lookback_weeks": (levels or {}).get("lookback_weeks"),
            "trend": self.trend_state(inst_id),
            "action_text": action,
            "notes": notes,
        }

    # ------------------------------------------------------------
    # 告警历史
    # ------------------------------------------------------------
    def alerts(self, inst_id: str | None = None, limit: int = 200,
               since_ms: int | None = None, signal_type: str | None = None) -> list[dict[str, Any]]:
        records = self.history.load(inst_id=inst_id, limit=limit,
                                    since_ms=since_ms, signal_type=signal_type)
        return [self._alert_to_dict(r) for r in records]

    @staticmethod
    def _alert_to_dict(record: AlertRecord) -> dict[str, Any]:
        return {
            "ts_ms": record.ts_ms,
            "ts_text": ms_to_bj(record.ts_ms),
            "pushed_at": record.pushed_at,
            "inst_id": record.inst_id,
            "symbol": record.symbol,
            "short_name": record.short_name,
            "signal_type": record.signal_type,
            "title": record.title,
            "what_happened": record.what_happened,
            "body": record.body,
            "price": record.price,
            "support": record.support,
            "resistance": record.resistance,
            "source": record.source,
            "source_label": "日志回填" if record.source == "log_backfill" else "实时",
        }

    # ------------------------------------------------------------
    # 控制项（本地控制台：读 + 写运行期偏好）
    # ------------------------------------------------------------
    def _effective_enabled(self, symbol: dict[str, Any]) -> bool:
        """生效的开关：控制台覆盖优先，否则用 config 里的设定。"""
        inst_id = str(symbol.get("inst_id") or "")
        override = self.control.symbol_override(inst_id) if inst_id else None
        return bool(symbol.get("enabled", True)) if override is None else bool(override)

    def _position_for(self, symbol: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """返回 (持仓视图, 来源)。来源：override=控制台填的 / config=配置里的 / none=无。"""
        inst_id = str(symbol.get("inst_id") or "")
        override = self.control.position(inst_id) if inst_id else None
        if override is not None:
            return self._position_view(override), "override"
        cfg_pos = symbol.get("position") or {}
        if cfg_pos.get("entry_price") is None:
            return None, "none"
        return self._position_view(cfg_pos), "config"

    @staticmethod
    def _position_view(raw: dict[str, Any]) -> dict[str, Any]:
        def num(value: Any) -> Any:
            return None if value is None else value

        return {
            "side": (str(raw.get("side")) if raw.get("side") else None),
            "entry_price": num(raw.get("entry_price")),
            "margin_usdt": num(raw.get("margin_usdt")),
            "leverage": num(raw.get("leverage")),
            "note": str(raw.get("note") or ""),
        }

    def control_snapshot(self) -> dict[str, Any]:
        """控制页需要的全部内容（会在返回前重读 control.json，保证所见即最新）。"""
        self.control.reload()
        symbols_out: list[dict[str, Any]] = []
        for symbol in self.cfg.symbols:
            inst_id = str(symbol.get("inst_id") or "")
            if not inst_id:
                continue
            name = str(symbol.get("name") or inst_id)
            position, position_source = self._position_for(symbol)
            override = self.control.symbol_override(inst_id)
            symbols_out.append({
                "inst_id": inst_id,
                "name": name,
                "short_name": short_symbol(inst_id, name),
                "enabled": self._effective_enabled(symbol),
                "config_enabled": bool(symbol.get("enabled", True)),
                "override": override,
                "priority": inst_id in self.control.priority_symbols(),
                "position": position,
                "position_source": position_source,
            })
        raw = self.control.snapshot()
        dnd = raw.get("dnd") or {}
        return {
            "symbols": symbols_out,
            "dnd": {
                "enabled": bool(dnd.get("enabled")),
                "windows": dnd.get("windows") or [],
                "active_now": self.control.is_dnd_active(datetime.now(BEIJING)),
            },
            "priority": {
                "symbols": self.control.priority_symbols(),
                "tracks": self.control.priority_tracks(),
            },
            "tracks": self.control.valid_tracks(),
            "updated_at": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def set_symbol_enabled(self, inst_id: Any, enabled: Any) -> dict[str, Any]:
        self.control.set_symbol_enabled(str(inst_id or ""), enabled)
        return self.control_snapshot()

    def set_dnd(self, enabled: Any, windows: Any) -> dict[str, Any]:
        self.control.set_dnd(enabled, windows)
        return self.control_snapshot()

    def set_priority(self, symbols: Any, tracks: Any) -> dict[str, Any]:
        self.control.set_priority(symbols, tracks)
        return self.control_snapshot()

    def set_position(self, inst_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
        self.control.set_position(str(inst_id or ""), payload)
        return self.control_snapshot()

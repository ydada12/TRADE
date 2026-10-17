"""双轨交易信号监控与告警系统 —— 主程序。

【只读监控 + 只发微信提醒】
  本程序不包含任何下单/平仓/撤单能力，也不连接任何券商或交易所的交易接口。
  行情只管读，信号只管发，买卖永远由人决定。

用法：
  python run.py --check                # 自检：配置、数据源、key、依赖
  python run.py --once                 # 跑一轮（趋势轨 + 事件轨）
  python run.py --loop                 # 常驻循环（配合崩溃重启脚本使用）
  python run.py --once --force         # 忽略「状态未变化」和冷却，强制出信号（演示用）
  python run.py --test-push            # 只测推送链路
  python run.py --list-events          # 打印未来经济日历（真实数据）
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

# Windows 控制台默认 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.alert import (
    Alert,
    EventAlert,
    SafetyFilter,
    SIGNAL_EVENT,
    SIGNAL_SHORT,
    SIGNAL_TREND,
    SIGNAL_WORLD,
    WorldAlert,
    compute_position_pnl,
    fmt_num,
    now_utc8_str,
    render_position_line,
    sanitize_for_push,
    short_symbol,
    zone_hint,
)
from src.config import PROJECT_ROOT, Config, load_config
from src.control import ControlStore, PendingQueue, inst_from_fingerprint
from src.datasource.econ_calendar import BEIJING, EconCalendarClient, EconEvent
from src.datasource.okx import Candle, OkxClient
from src.datasource.us_stock import UsStockClient
from src.datasource.world_news import (
    DEFAULT_GDELT_QUERY,
    DEFAULT_RSS_FEEDS,
    NewsItem,
    WorldNewsClient,
    normalize_url,
)
from src.dashboard.history import AlertHistory, AlertRecord, record_from_alert
from src.llm.deepseek import DeepSeekClient
from src.llm.prompts import (
    SYSTEM_PROMPT,
    WORLD_SYSTEM_PROMPT,
    build_event_post_user_message,
    build_event_pre_user_message,
    build_session_user_message,
    build_short_user_message,
    build_trend_user_message,
    build_world_judge_user_message,
)
from src.logutil import get_logger, setup_logging
from src.modules.event import (
    branch_from_previous,
    build_branch_facts,
    categorize,
    determine_branch,
    publish_time_text,
    render_fallback_script,
    render_post_fallback,
    verdict_for_event,
)
from src.modules import world as world_mod
from src.modules.levels import derive_intraday_levels, derive_levels, detect_level_breaks
from src.modules.session import (
    UTC8,
    active_window,
    decide_alert,
    detect_moves,
    parse_windows,
    record_for_window,
    render_fallback_text as render_session_fallback,
)
from src.modules.short_signal import compute_orderflow_metrics, score_short_signal
from src.modules.trend import STATE_LABEL, evaluate_trend
from src.state import StateStore

# 推送策略层：规则与文案都在 src/strategy/，run.py 只做编排与委托。
# 这里的常量 import 即「re-export 兼容层」——外部仍可 from run import PUSH_*。
from src.strategy.deferral import DndDeferral
from src.strategy.gate import (
    MAX_PUSH_RETRIES,
    PUSH_COOLDOWN,
    PUSH_DEFERRED,
    PUSH_FAILED,
    PUSH_SENT,
    PUSH_SKIPPED,
    PushGate,
)
# 轨道共用的纯计算助手（原 run.py 顶部定义，随轨道搬到 src/strategy/helpers.py）。
# 调用点沿用旧名 _gap_pct，保持零改动。
from src.strategy.helpers import bar_minutes, gap_pct as _gap_pct
from src.strategy.memo import DirectionMemo
from src.strategy.tracks.event import EventTrack
from src.strategy.tracks.session import SessionTrack
from src.strategy.tracks.short import ShortTrack
from src.strategy.tracks.trend import TrendTrack
from src.strategy.tracks.world import WorldTrack
from src.strategy.wording import PRIORITY_PREFIX, STATE_SHORT

log = get_logger("run")


# push_status 的返回值与最大重试次数由 src/strategy/gate.py 定义并 import（见顶部）。


class Monitor(DirectionMemo, TrendTrack, EventTrack, WorldTrack, SessionTrack, ShortTrack):
    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.app = cfg.get("app", {}) or {}
        self.dry_run = bool(dry_run or self.app.get("dry_run", False))

        log_dir = cfg.resolve_path(str(self.app.get("log_dir", "logs")))
        setup_logging(log_dir, str(self.app.get("log_level", "INFO")))
        self.log_file = log_dir

        self.state = StateStore(cfg.resolve_path(str(self.app.get("state_file", "state/state.json"))))
        # 告警历史（面板用来在 K 线上标注历史位置）。只在推送成功后才追加，
        # 写失败不影响推送判定——见 AlertHistory.append。
        self.history = AlertHistory(cfg.resolve_path(
            str(self.app.get("alert_history_file", "state/alerts.jsonl"))
        ))

        # 运行期控制项（本地控制台写入，这里每轮重读）。放 state/ 下，不进公开仓库。
        self.control = ControlStore(
            cfg.resolve_path(str(self.app.get("control_file", "state/control.json"))),
            valid_inst_ids=[str(s.get("inst_id")) for s in cfg.symbols],
        )
        # 免打扰期间被「延迟补发」的信号队列（落盘，跨进程重启也不丢）
        self.pending = PendingQueue(
            cfg.resolve_path(str(self.app.get("pending_file", "state/pending.json")))
        )

        market = cfg.get("market", {}) or {}
        self.okx = OkxClient(
            base_url=market.get("base_url", "https://www.okx.com"),
            timeout=float(market.get("timeout_seconds", 20)),
            max_retries=int(market.get("max_retries", 4)),
            backoff=float(market.get("retry_backoff_seconds", 2)),
        )
        self.weekly_bar = market.get("weekly_bar", "1W")
        self.weekly_limit = int(market.get("weekly_candle_limit", 300))
        # 周K一周才收一根，没必要每 60 秒重拉一遍 Yahoo/OKX
        self.weekly_cache_seconds = float(market.get("weekly_cache_minutes", 30)) * 60
        self._weekly_cache: dict[str, tuple[float, list[Candle], str]] = {}

        us_cfg = market.get("us_stock", {}) or {}
        self.us = UsStockClient(
            timeout=float(us_cfg.get("timeout_seconds", 30)),
            max_retries=int(us_cfg.get("max_retries", 4)),
            backoff=float(us_cfg.get("retry_backoff_seconds", 2)),
        )
        self.default_divergence_warn_pct = float(
            market.get("divergence_warn_pct", 3.0)
        )

        notify = cfg.get("notify", {}) or {}
        from src.notify.serverchan import ServerChanNotifier

        self.notifier = ServerChanNotifier(
            send_key=cfg.read_secret(str(notify.get("send_key_env", "SERVERCHAN_SENDKEY"))),
            endpoint=notify.get("endpoint", "https://sctapi.ftqq.com"),
            max_retries=int(notify.get("max_retries", 2)),
            retry_backoff_seconds=float(notify.get("retry_backoff_seconds", 3)),
            timeout=float(notify.get("timeout_seconds", 20)),
            dry_run=self.dry_run,
        )
        self.cooldown_minutes = float(notify.get("cooldown_minutes", 240))

        llm_cfg = cfg.get("llm", {}) or {}
        self.llm = DeepSeekClient(
            api_key=cfg.read_secret(str(llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))),
            base_url=llm_cfg.get("base_url", "https://api.deepseek.com"),
            model=llm_cfg.get("model", "deepseek-chat"),
            temperature=float(llm_cfg.get("temperature", 0.2)),
            max_tokens=int(llm_cfg.get("max_tokens", 800)),
            timeout=float(llm_cfg.get("timeout_seconds", 60)),
            max_retries=int(llm_cfg.get("max_retries", 3)),
            guard=bool(llm_cfg.get("guard_against_fabricated_numbers", True)),
        )

        self.safety = SafetyFilter(
            forbidden_words=cfg.get("forbidden_words", []) or [],
            fallback_text="",
            allowed_action_text=str(
                cfg.get("allowed_action_text", "建议人工查看/等待确认")
            ),
            disclaimer=str(cfg.get("risk_disclaimer", "以上为历史规律梳理，仅供参考，决策与风险自负")),
        )

        cal_cfg = cfg.get("calendar", {}) or {}
        # 日历缓存：平时 30 分钟；有事件即将公布/刚公布时缩短，才能及时拿到实际值
        self.calendar_cache_seconds = float(cal_cfg.get("cache_minutes", 30)) * 60
        self.calendar_hot_cache_seconds = float(cal_cfg.get("hot_cache_minutes", 3)) * 60
        self._calendar_cache: tuple[float, list[EconEvent]] | None = None
        # 已经提醒过「公布了但数据源还没给实际值」的事件，避免每轮重复刷日志
        self._post_pending_warned: set[str] = set()
        self.calendar = EconCalendarClient(
            days_ahead=int(cal_cfg.get("days_ahead", 7)),
            min_importance=int(cal_cfg.get("min_importance", 2)),
            keywords=list(cal_cfg.get("keywords", []) or []),
            regions=list(cal_cfg.get("regions", []) or []),
            lookback_days=int(cal_cfg.get("lookback_days", 1)),
        )

        # ---- 重点时段盘中波动监控 ----
        sw_cfg = cfg.get("session_watch", {}) or {}
        self.session_cfg = sw_cfg
        self.session_enabled = bool(sw_cfg.get("enabled", True))
        self.session_windows = parse_windows(sw_cfg.get("windows"))
        self.session_interval = float(sw_cfg.get("poll_interval_seconds", 60))
        self.session_bar = str(sw_cfg.get("candle_bar", "5m"))
        self.session_limit = int(sw_cfg.get("candle_limit", 600))
        self.session_rules = sw_cfg.get("move_thresholds") or []
        self.session_since_open_pct = sw_cfg.get("since_open_pct")
        self.session_cooldown = float(sw_cfg.get("cooldown_minutes", 30))
        self.session_bucket_pct = float(sw_cfg.get("magnitude_bucket_pct", 1.0)) or 1.0

        # ---- 做空观察位（日内压力位 + 盘口卖压，只提醒不下单）----
        short_cfg = cfg.get("short_watch", {}) or {}
        self.short_cfg = short_cfg
        self.short_enabled = bool(short_cfg.get("enabled", True))
        raw_short_windows = short_cfg.get("windows")
        # 留空则复用重点时段；否则用单独的时段
        self.short_windows = parse_windows(raw_short_windows) if raw_short_windows else self.session_windows
        self.short_symbols = list(short_cfg.get("symbols") or [])
        self.short_bar = str(short_cfg.get("candle_bar", "5m"))
        self.short_limit = int(short_cfg.get("candle_limit", 300))
        self.short_book_depth = int(short_cfg.get("book_depth", 50))
        self.short_trades_limit = int(short_cfg.get("trades_limit", 100))
        self.short_top_levels = int(short_cfg.get("book_top_levels", 10))
        self.short_resistance_max_pct = float(short_cfg.get("resistance_max_pct", 3.0))
        self.short_cooldown = float(short_cfg.get("cooldown_minutes", 180))
        self.short_params: dict[str, Any] = {
            "touch_pct": float(short_cfg.get("touch_pct", 0.5)),
            "pullback_pct": float(short_cfg.get("pullback_pct", 0.2)),
            "invalidation_pct": float(short_cfg.get("invalidation_pct", 1.0)),
            "imbalance_threshold": float(short_cfg.get("imbalance_threshold", -0.2)),
            "taker_sell_ratio_threshold": float(short_cfg.get("taker_sell_ratio_threshold", 0.55)),
            "wall_ratio_threshold": float(short_cfg.get("wall_ratio_threshold", 2.0)),
            "counter_trend_penalty": int(short_cfg.get("counter_trend_penalty", 2)),
            "score_threshold": int(short_cfg.get("score_threshold", 4)),
        }

        # ---- 国际大事轨（GDELT 主 + RSS 辅；DeepSeek 研判 + 确定性兜底）----
        world_cfg = cfg.get("world_events", {}) or {}
        self.world_cfg = world_cfg
        self.world_enabled = bool(world_cfg.get("enabled", True))
        self.world_interval = float(world_cfg.get("interval_minutes", 20)) * 60
        self.world_cache_seconds = float(world_cfg.get("cache_minutes", 15)) * 60
        self._world_cache: tuple[float, list[NewsItem]] | None = None
        self._world_last_judge = 0.0
        self.world_per_run = int(world_cfg.get("max_pushes_per_run", 3))
        self.world_per_day = int(world_cfg.get("max_pushes_per_day", 6))
        self.world_judge_batch = int(world_cfg.get("judge_batch", 12))
        # 国际大事是一次性返回整批 JSON（含每条的事实/传导链/思考），
        # 12 条 × 每条两百来个 token 远超通用的 800 上限；上限太小会让回复被截断成
        # 非法 JSON，整批退回规则兜底、一条 AI 思考都发不出去。
        self.world_judge_max_tokens = int(world_cfg.get("judge_max_tokens", 3000))
        self.world_min_score = int(world_cfg.get("min_score", 2))
        self.world_require_title_hit = bool(world_cfg.get("require_title_hit", True))
        groups_cfg = world_cfg.get("keyword_groups")
        self.world_keyword_groups = (
            dict(groups_cfg) if isinstance(groups_cfg, dict) and groups_cfg
            else world_mod.DEFAULT_KEYWORD_GROUPS
        )
        self.world_watch = list(world_cfg.get("watch_symbols") or [])

        # ---- 跨轨方向备忘（仅内存，不落盘）----
        # 方向备忘的规则与文案在 src/strategy/memo.py（含有效期 _dir_memo_hours）。
        self._dir_memo: dict[str, dict[str, Any]] = {}

        # ---- 推送策略层：闸口与延迟补发队列（注入下面这些同实例）----
        # 关键：以下对象必须与 Monitor 持有的是【同一个】实例，否则
        # 测试里 `m.control.is_dnd_active = ...` 之类的 monkeypatch 会失效。
        self._deferral = DndDeferral(
            state=self.state,
            notifier=self.notifier,
            history=self.history,
            pending=self.pending,
            make_record=record_from_alert,
            record_from_line=AlertRecord.from_line,
        )
        self._gate = PushGate(
            state=self.state,
            control=self.control,
            notifier=self.notifier,
            history=self.history,
            safety=self.safety,
            deferral=self._deferral,
            dry_run=lambda: self.dry_run,
            cooldown_minutes=self.cooldown_minutes,
            make_record=record_from_alert,
        )

        gdelt_cfg = world_cfg.get("gdelt", {}) or {}
        rss_cfg = world_cfg.get("rss", {}) or {}
        self.world_news = WorldNewsClient(
            gdelt_query=str(gdelt_cfg.get("query", DEFAULT_GDELT_QUERY)),
            rss_feeds=list(rss_cfg.get("feeds") or DEFAULT_RSS_FEEDS),
            timespan_hours=int(world_cfg.get("timespan_hours", 12)),
            timeout=float(world_cfg.get("timeout_seconds", 20)),
            gdelt_maxrecords=int(gdelt_cfg.get("maxrecords", 75)),
            gdelt_retries=int(gdelt_cfg.get("retries", 2)),
        )
        if not bool(gdelt_cfg.get("enabled", True)):
            def _gdelt_disabled() -> list[NewsItem]:
                raise RuntimeError("GDELT 已在配置中禁用")
            self.world_news.fetch_gdelt = _gdelt_disabled  # type: ignore[assignment]
        if not bool(rss_cfg.get("enabled", True)):
            self.world_news.rss_feeds = []

    # ================= 推送（委托给 src/strategy/gate.py） =================
    def push_status(self, title: str, body: str, fingerprint: str,
                    force: bool = False, cooldown_minutes: float | None = None,
                    alert: Alert | EventAlert | WorldAlert | None = None,
                    track: str | None = None, inst_id: str | None = None) -> str:
        """推送并返回结果：PUSH_SENT / PUSH_COOLDOWN / PUSH_FAILED / PUSH_DEFERRED。

        规则（冷却 / 免打扰 / 重点标记 / 落历史）见 src/strategy/gate.py。
        """
        return self._gate.push_status(
            title, body, fingerprint, force=force, cooldown_minutes=cooldown_minutes,
            alert=alert, track=track, inst_id=inst_id,
        )

    def push(self, title: str, body: str, fingerprint: str,
             force: bool = False, cooldown_minutes: float | None = None,
             alert: Alert | EventAlert | None = None) -> bool:
        """兼容旧调用：只在真的发出去时返回 True。"""
        return self._gate.push(title, body, fingerprint, force, cooldown_minutes, alert)

    def _delivery_settled(self, retry_key: str, status: str, force: bool) -> bool:
        """这条信号算不算「处理完了」（可以落状态/标记已发）。见 src/strategy/gate.py。"""
        return self._gate._delivery_settled(retry_key, status, force)

    # ================= 运行期控制项（本地控制台） =================
    def reload_control(self) -> None:
        """每轮/每次轨道执行前重读控制项，让本地控制台的改动能即刻生效。"""
        try:
            self.control.reload()
        except Exception as exc:  # pragma: no cover - 防御性
            log.error("控制项重读失败，沿用已加载的值：%s", exc)

    def _active_symbols(self) -> list[dict]:
        """当前参与监控的品种 = config 默认，叠加控制台的逐品种覆盖。"""
        out: list[dict] = []
        for symbol in self.cfg.symbols:
            inst_id = str(symbol.get("inst_id") or "")
            override = self.control.symbol_override(inst_id) if inst_id else None
            enabled = bool(symbol.get("enabled", True)) if override is None else override
            if enabled:
                out.append(symbol)
        return out

    def _effective_position(self, symbol: dict) -> dict:
        """持仓：控制台填过就用控制台的，否则用 config 里的真实持仓。"""
        inst_id = str(symbol.get("inst_id") or "")
        override = self.control.position(inst_id) if inst_id else None
        if override is not None:
            return override
        return symbol.get("position") or {}

    # ---- 免打扰延迟补发（委托给 src/strategy/deferral.py）----
    def _defer_push(self, track: str | None, inst_id: str | None, title: str, body: str,
                    fingerprint: str, alert: Alert | EventAlert | WorldAlert | None) -> None:
        """免打扰期间的信号：不发、也不丢，进延迟队列等退出免打扰后补发。"""
        self._deferral.defer_push(track, inst_id, title, body, fingerprint, alert)

    def _flush_pending(self) -> int:
        """退出免打扰后补发延迟队列。返回本轮补发成功条数。见 src/strategy/deferral.py。"""
        return self._deferral.flush_pending()

    # ================= 周K来源调度 =================
    # 拉取失败时，最多容忍用多久以前的旧周K（周线指标变化很慢，旧数据比没数据强）
    WEEKLY_STALE_MAX_SECONDS = 6 * 3600

    def _weekly_candles(self, symbol: dict) -> tuple[list[Candle], str]:
        """周K（带缓存）。缓存 weekly_cache_minutes 分钟；拉取失败时回退到不太旧的缓存。

        趋势轨、事件轨、盘中监控每轮都要用周K推关键位；60 秒一轮时
        不加缓存就是每分钟重拉一次 Yahoo 5 年数据，既浪费又容易被限流。
        """
        inst_id = symbol["inst_id"]
        now = time.time()
        cached = self._weekly_cache.get(inst_id)
        if cached and now - cached[0] < self.weekly_cache_seconds:
            return list(cached[1]), cached[2]
        try:
            candles, desc = self._fetch_weekly_candles(symbol)
        except Exception as exc:
            if cached and now - cached[0] < self.WEEKLY_STALE_MAX_SECONDS:
                log.warning("%s 周K刷新失败，沿用 %.0f 分钟前的缓存：%s",
                            inst_id, (now - cached[0]) / 60, exc)
                return list(cached[1]), cached[2]
            raise
        self._weekly_cache[inst_id] = (now, candles, desc)
        return list(candles), desc

    def _fetch_weekly_candles(self, symbol: dict) -> tuple[list[Candle], str]:
        """按 symbol.history_source 选择周K来源（每次都真实拉取）。

        okx      : 用 OKX 自己的周K（口径：周日16:00 UTC 收线，含周末加密盘）
        us_stock : 用美股本土周K（口径：周一~周五，不含周末），历史长得多，
                   用来解决 OKX 永续上市太晚、算不了 MA20/MA30 的问题。
        """
        source = str(symbol.get("history_source") or "okx").lower()
        if source == "us_stock":
            us_symbol = symbol.get("us_symbol")
            if not us_symbol:
                raise ValueError(
                    f"{symbol.get('inst_id')} 设了 history_source=us_stock，但没填 us_symbol"
                )
            candles = self.us.get_weekly(
                str(us_symbol), str(symbol.get("us_history_range", "5y"))
            )
            return candles, f"美股本土 {us_symbol}（Yahoo/Stooq）"

        candles = self.okx.get_weekly_candles(
            symbol["inst_id"], self.weekly_bar, self.weekly_limit
        )
        return candles, "OKX 周K"

    def _divergence_warning(self, symbol: dict, candles: list[Candle],
                            last_price: float) -> dict | None:
        """当用美股数据补历史时，检查 OKX 实时价是否跟美股收盘价明显脱钩。

        两者本来就有差异（实测日均价差 +0.2%，最大 3.5%），
        偏离过大说明跟踪关系可能失效，必须让用户知道。
        """
        if str(symbol.get("history_source") or "okx").lower() != "us_stock":
            return None
        confirmed = [c for c in candles if c.confirmed]
        if not confirmed:
            return None
        ref = confirmed[-1].close
        if not ref:
            return None
        divergence_pct = (last_price - ref) / ref * 100.0
        threshold = float(symbol.get("divergence_warn_pct", self.default_divergence_warn_pct))
        return {
            "us_close": ref,
            "us_week": confirmed[-1].dt_utc.strftime("%Y-%m-%d"),
            "divergence_pct": divergence_pct,
            "threshold": threshold,
            "breached": abs(divergence_pct) > threshold,
        }

    # 趋势轨（run_trend / _build_trend_alert / _trend_fallback_text）由 src/strategy/tracks/trend.py 的 TrendTrack mixin 提供。

    # ================= 事件轨 =================
    def run_events(self, force: bool = False,
                   pre_minutes_override: float | None = None,
                   max_events_override: int | None = None) -> list[str]:
        event_cfg = self.cfg.get("event", {}) or {}
        if not event_cfg.get("enabled", True):
            return []

        out: list[str] = []
        pre_minutes = float(
            pre_minutes_override
            if pre_minutes_override is not None
            else event_cfg.get("pre_alert_minutes", 180)
        )
        post_minutes = float(event_cfg.get("post_check_minutes", 20))
        # 「对号入座」的观察窗上限。数据源的「公布」列常滞后几十分钟到几小时才填上
        # （晚间的美国数据经常要跨过零点），窗口开得太小会永远等不到实际值。
        post_window_minutes = max(post_minutes, float(event_cfg.get("post_max_minutes", 1440)))
        max_events = int(
            max_events_override
            if max_events_override is not None
            else event_cfg.get("max_events_per_run", 3)
        )
        watch = event_cfg.get("watch_symbols") or []
        now = datetime.now(BEIJING)

        try:
            events = self._calendar_events(now, fresh=force)
        except Exception as exc:
            log.error("经济日历获取失败（本轮跳过事件轨）：%s", exc)
            return out

        emitted = 0
        for event in events:
            if emitted >= max_events:
                log.info("本轮事件推送已达上限 %s 条，剩余事件下轮再处理", max_events)
                break
            if not self._is_significant(event):
                continue
            publish = event.publish_dt
            if publish is None:
                continue

            minutes_to_publish = (publish - now).total_seconds() / 60.0
            fp = f"event|{event.fingerprint}"

            # ---------- 发布前：生成剧本 ----------
            if 0 <= minutes_to_publish <= pre_minutes:
                record = self.state.get_event(fp) or {}
                if record.get("pre_sent") and not force:
                    continue
                try:
                    status = self._emit_pre_script(event, now, watch, fp, force)
                except Exception:
                    log.error("事件预警生成异常：\n%s", traceback.format_exc())
                    status = PUSH_FAILED
                # 推送失败不标记已发，下一轮重试（带次数上限）
                if not self._delivery_settled(f"retry|{fp}|pre", status, force):
                    continue
                self.state.set_event(fp, {**record, "pre_sent": True, "pre_at": now_utc8_str()})
                self.state.save()
                out.append(event.name)
                emitted += 1

            # ---------- 发布后：对号入座 ----------
            minutes_since = -minutes_to_publish
            if 0 <= minutes_since <= post_window_minutes and not event.released:
                # 数据源的实际值常滞后几十分钟到几小时。这里只记一次，别让「没等到值」
                # 变成无声无息——出问题时至少能在日志里看到它一直在等。
                if minutes_since >= 60 and fp not in self._post_pending_warned:
                    self._post_pending_warned.add(fp)
                    log.warning("事件 %s 已公布 %.0f 分钟，数据源仍未给出实际值（继续等，最多等 %.0f 分钟）",
                                event.name, minutes_since, post_window_minutes)
            if 0 <= minutes_since <= post_window_minutes and event.released:
                record = self.state.get_event(fp) or {}
                if record.get("post_sent") and not force:
                    continue
                try:
                    status = self._emit_post_result(event, now, watch, fp, force)
                except Exception:
                    log.error("事件结果生成异常：\n%s", traceback.format_exc())
                    status = PUSH_FAILED
                if not self._delivery_settled(f"retry|{fp}|post", status, force):
                    continue
                self.state.set_event(fp, {
                    **record, "post_sent": True, "post_at": now_utc8_str(),
                    "actual": event.actual,
                })
                self.state.save()
                out.append(event.name)
                emitted += 1
        return out

    def _calendar_hot(self, events: list[EconEvent], now: datetime) -> bool:
        """是否有关注事件即将公布（5 分钟内）或刚公布（45 分钟内）且结果还没推过。

        这段时间要频繁刷新日历，才能尽快拿到「今值」；其余时间缓存即可。
        """
        for event in events:
            publish = event.publish_dt
            if publish is None or not self._is_significant(event):
                continue
            minutes_to_publish = (publish - now).total_seconds() / 60.0
            if not (-45 <= minutes_to_publish <= 5):
                continue
            record = self.state.get_event(f"event|{event.fingerprint}") or {}
            if not record.get("post_sent"):
                return True
        return False

    def _calendar_events(self, now: datetime, fresh: bool = False) -> list[EconEvent]:
        """经济日历（带缓存）。

        原先每 60 秒就向百度抓 7 天日历（每天一次请求），正是 403 限流的诱因。
        现在平时 30 分钟刷新一次，事件前后改为 3 分钟；刷新失败时回退旧缓存。
        """
        cached = self._calendar_cache
        if cached and not fresh:
            age = time.time() - cached[0]
            ttl = (self.calendar_hot_cache_seconds if self._calendar_hot(cached[1], now)
                   else self.calendar_cache_seconds)
            if age < ttl:
                return cached[1]
        try:
            events = self.calendar.fetch_range()
        except Exception as exc:
            if cached:
                log.warning("经济日历刷新失败，沿用 %.0f 分钟前的缓存：%s",
                            (time.time() - cached[0]) / 60, exc)
                return cached[1]
            raise
        self._calendar_cache = (time.time(), events)
        return events

    def _is_significant(self, event: EconEvent) -> bool:
        """重要性达标 且 地区符合 且 命中关键词 —— 三者都要满足。

        单看关键词会把印度/德国的 CPI 全拉进来刷屏；
        单看重要性又会漏掉被数据商标成「中」的美国 CPI。
        """
        if (event.importance or 0) < int(self.cfg.get("calendar.min_importance", 2)):
            return False
        if not self.calendar.region_ok(event):
            return False
        return self.calendar.is_watched(event)

    # 事件轨的规则与文案（_target_symbols / _primary_symbol_for_event / _symbol_levels / _emit_pre_script / _emit_post_result）由 src/strategy/tracks/event.py 的 EventTrack mixin 提供。

    # ================= 国际大事（GDELT + RSS） =================
    def _world_items(self, force: bool = False) -> list[NewsItem]:
        """国际新闻（带缓存）。刷新失败时回退旧缓存，与经济日历同口径。"""
        cached = self._world_cache
        if cached and not force and time.time() - cached[0] < self.world_cache_seconds:
            return cached[1]
        try:
            items, sources = self.world_news.fetch_all()
        except Exception:
            if cached:
                log.warning("国际大事源刷新失败，沿用 %.0f 分钟前的缓存",
                            (time.time() - cached[0]) / 60)
                return cached[1]
            raise
        self._world_cache = (time.time(), items)
        log.info("国际大事取数完成：%d 条（来源 %s）", len(items), sources)
        return items

    # 国际大事轨（_world_actions / run_world_events）由 src/strategy/tracks/world.py 的 WorldTrack mixin 提供。

    # ================= 重点时段盘中波动 =================
    def current_window(self, now_bj: datetime | None = None):
        now_bj = now_bj or datetime.now(BEIJING)
        return active_window(self.session_windows, now_bj)

    # 重点时段盘中波动轨（run_session_watch / _build_session_alert）由 src/strategy/tracks/session.py 的 SessionTrack mixin 提供。

    # ================= 做空观察位 =================
    # 方向备忘（_note_direction / _cross_track_note）由 src/strategy/memo.py 的
    # DirectionMemo mixin 提供（Monitor 已继承）。

    # 做空观察位轨（ANCHOR_STALE_HOURS / _anchor_key / _open_short_anchor / _fake_break_followup / run_short_watch / _short_fallback_text / _build_short_alert）由 src/strategy/tracks/short.py 的 ShortTrack mixin 提供。

    # ================= 自检 =================
    def check(self) -> bool:
        ok = True
        print("=" * 70)
        print("自检报告　", now_utc8_str(), "（北京时间）")
        print("=" * 70)
        print(f"配置文件：{self.cfg.path}")
        print(f"日志目录：{self.cfg.resolve_path(str(self.app.get('log_dir', 'logs')))}")
        print(f"运行模式：{'DRY-RUN（不实际推送）' if self.dry_run else '真实推送'}")

        print("\n[1] 推送通道（Server酱）")
        if self.notifier.configured:
            n_keys = len(self.notifier.send_keys)
            suffix = f"（{n_keys} 个推送目标）" if n_keys > 1 else ""
            print(f"    SendKey：已从环境变量读取 ✓{suffix}")
        else:
            print("    SendKey：未配置 ✗（设置环境变量 SERVERCHAN_SENDKEY）")
            ok = False

        print("\n[2] 大模型（DeepSeek）")
        if self.llm.available:
            print(f"    API key：已读取 ✓　模型：{self.llm.model}")
        else:
            print("    API key：未配置 ✗（设置环境变量 DEEPSEEK_API_KEY）")
            print("    注：没有 key 系统仍可运行，解读会退回本地规则模板（数字依然真实）")

        print("\n[3] 行情数据源（OKX 只读接口 + 可选美股本土历史）")
        for symbol in self._active_symbols():
            inst_id = symbol["inst_id"]
            try:
                inst = self.okx.get_instrument(inst_id, symbol.get("inst_type", "SWAP"))
                if inst is None:
                    print(f"    {inst_id}：不存在 ✗")
                    ok = False
                    continue
                ticker = self.okx.get_ticker(inst_id)
                candles, history_desc = self._weekly_candles(symbol)
                closed = [c for c in candles if c.confirmed]
                needed = max([int(p) for p in (symbol.get("ma_periods") or [20])])
                note = "✓" if len(closed) >= needed else f"⚠ 周K仅 {len(closed)} 根，不足 MA{needed}"
                print(f"    {inst_id}：现价 {ticker.last:g}，已收盘周K {len(closed)} 根 {note}")
                print(f"        历史来源：{history_desc}")
                divergence = self._divergence_warning(symbol, candles, ticker.last)
                if divergence:
                    flag = "⚠ 超阈值" if divergence["breached"] else "正常"
                    print(f"        与美股跟踪偏差：{divergence['divergence_pct']:+.2f}%"
                          f"（阈值 {divergence['threshold']:g}%，{flag}）")
            except Exception as exc:
                print(f"    {inst_id}：拉取失败 ✗ {exc}")
                ok = False

        print("\n[4] 经济日历（AKShare）")
        try:
            events = self.calendar.fetch_range()
            important = [e for e in events if self._is_significant(e)]
            regions = self.cfg.get("calendar.regions") or ["（不限地区）"]
            print(f"    未来 {self.calendar.days_ahead} 天共 {len(events)} 条，"
                  f"其中关注事件 {len(important)} 条（地区：{', '.join(regions)}）")
            for event in important[:8]:
                print(f"      · {event.date} {event.time} {event.region} {event.name}"
                      f"　预期={event.forecast_text()}　实际={event.actual_text()}")
        except Exception as exc:
            print(f"    经济日历拉取失败 ✗ {exc}")
            ok = False

        print("\n[5] 重点时段盘中波动监控")
        if not self.session_enabled:
            print("    已禁用（config 里 session_watch.enabled = false）")
        else:
            now_bj = datetime.now(BEIJING)
            current = self.current_window(now_bj)
            print(f"    当前北京时间：{now_bj:%Y-%m-%d %H:%M:%S}")
            print(f"    时段内轮询间隔：{self.session_interval:.0f}s"
                  f"（平时 {self.app.get('poll_interval_seconds', 300)}s）")
            for window in self.session_windows:
                mark = " ← 当前处于该时段" if current and current.name == window.name else ""
                print(f"      · {window.label()}{mark}")
            print(f"    波动阈值：{self.session_rules}")
            print(f"    时段开盘累计阈值：{self.session_since_open_pct}%")
            print(f"    同品种同规则冷却：{self.session_cooldown:.0f} 分钟")
            if current is None:
                print("    当前不在任何重点时段（只按平时的慢节奏轮询）")

        print("\n[6] 做空观察位监控（日内关键位 + OKX 盘口/逐笔，只提醒不下单）")
        if not self.short_enabled:
            print("    已禁用（config 里 short_watch.enabled = false）")
        else:
            print(f"    日内 K线：{self.short_bar} × {self.short_limit} 根｜盘口 {self.short_book_depth} 档"
                  f"｜逐笔 {self.short_trades_limit} 条")
            print(f"    评估时段：{' '.join(w.label() for w in self.short_windows) or '（未配置）'}")
            print(f"    打分阈值：{self.short_params['score_threshold']}｜"
                  f"触及带宽 {self.short_params['touch_pct']:g}%｜"
                  f"失效位 {self.short_params['invalidation_pct']:g}%｜"
                  f"同观察区冷却 {self.short_cooldown:.0f} 分钟")
            print("    观察与失效条件均为客观描述，不含开仓/止损指令 ✓")

        print("\n[7] 红线自查")
        print("    · 本程序只调用 OKX market 公开只读接口（含 /market/books、/market/trades），"
              "无任何下单/平仓/撤单调用 ✓")
        print("    · 所有数字来自真实 API，大模型输出带「防编造数字」校验 ✓")
        print(f"    · 每条告警固定结尾：{self.safety.disclaimer} ✓")
        print(f"    · 建议动作固定为：{self.safety.enforce_action('')} ✓")
        print("=" * 70)
        return ok


def replay_session(monitor: Monitor, hours: float) -> int:
    """回放最近 N 小时，列出重点时段内「本应触发」的盘中告警（只打印，不推送）。

    用途：验证这套阈值是否合理，以及确认昨晚那波下跌到底会不会被抓到。
    """
    bar = monitor.session_bar
    limit = monitor.session_limit
    cooldown_ms = int(monitor.session_cooldown * 60_000)

    print("=" * 78)
    print(f"重点时段盘中监控 —— 回放最近 {hours:g} 小时（只读，不推送）")
    print(f"K线周期 {bar}｜判定阈值 {monitor.session_rules}｜时段开盘累计阈值 "
          f"{monitor.session_since_open_pct}%")
    print("=" * 78)

    total = 0
    for symbol in monitor._active_symbols():
        inst_id = symbol["inst_id"]
        try:
            candles = monitor.okx.get_candles(inst_id, bar, limit)
        except Exception as exc:
            print(f"\n{inst_id}: 拉取失败 {exc}")
            continue
        if not candles:
            continue

        cutoff = candles[-1].ts_ms - int(hours * 3600_000)
        rows = [c for c in candles if c.ts_ms >= cutoff]
        fired: dict[str, dict] = {}     # 按「时段」记录上次告警，与线上逻辑一致
        print(f"\n--- {inst_id}（{len(rows)} 根 {bar} K线）---")
        count = 0
        for candle in candles:
            if candle.ts_ms < cutoff:
                continue
            now_bj = candle.dt_utc.astimezone(UTC8)
            window = active_window(monitor.session_windows, now_bj)
            if window is None:
                continue
            since_open_ms = int(window.start_moment(now_bj).timestamp() * 1000)
            hits = detect_moves(
                candles=[c for c in candles if c.ts_ms <= candle.ts_ms],
                now_ms=candle.ts_ms,
                rules=monitor.session_rules,
                since_open_ms=since_open_ms,
                since_open_pct=monitor.session_since_open_pct,
            )
            if not hits:
                continue

            # 与线上完全相同的防刷屏判定
            # 注意：decide_alert 的时间单位是【秒】，candle.ts_ms 是毫秒，必须换算，
            # 否则冷却判定永远失效（离线回放就是这么发现这个坑的）。
            biggest = max(abs(h["change_pct"]) for h in hits)
            # 按「时段的某一次开启」分组，而不是只按时段名：
            # 回放跨了两个晚上时，前一晚的档位不能压住后一晚（与线上 record_for_window 一致）
            fired_key = f"{window.name}|{since_open_ms}"
            rec = fired.get(fired_key) or {}
            decision = decide_alert(
                biggest_pct=biggest,
                last_ts=rec.get("ts"),
                last_bucket=rec.get("bucket", -1),
                now_ts=candle.ts_ms / 1000.0,
                cooldown_minutes=monitor.session_cooldown,
                bucket_pct=monitor.session_bucket_pct,
            )
            if not decision.alert:
                continue
            fired[fired_key] = {
                "ts": candle.ts_ms / 1000.0,
                "bucket": max(decision.bucket, rec.get("bucket", -1)),
            }
            count += 1
            total += 1
            print(f"  {now_bj:%m-%d %H:%M} 北京 [{window.name}] "
                  f"[{decision.reason}] 最大 {biggest:.2f}%  档位 {decision.bucket}")
            for hit in hits:
                print(f"        · {hit['text']}")
        if count == 0:
            print("  （该时段窗口内没有触发告警）")

    print("\n" + "=" * 78)
    print(f"合计本应触发 {total} 条告警")
    print("=" * 78)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="双轨交易信号监控与告警系统（只读监控）")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 config.yaml")
    parser.add_argument("--once", action="store_true", help="跑一轮后退出")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--check", action="store_true", help="自检")
    parser.add_argument("--test-push", action="store_true", help="只测试推送链路")
    parser.add_argument("--list-events", action="store_true", help="打印未来经济日历")
    parser.add_argument("--force", action="store_true", help="忽略状态未变化与冷却（演示用）")
    parser.add_argument("--dry-run", action="store_true", help="不实际推送，只写日志")
    parser.add_argument("--symbol", default=None, help="只跑某个 inst_id 的趋势轨")
    parser.add_argument("--trend-only", action="store_true")
    parser.add_argument("--event-only", action="store_true")
    parser.add_argument("--session-only", action="store_true", help="只跑重点时段盘中监控")
    parser.add_argument("--short-only", action="store_true", help="只跑做空观察位监控")
    parser.add_argument("--world-only", action="store_true",
                        help="只跑国际大事轨（忽略轮询间隔与冷却，调试用）")
    parser.add_argument("--ignore-window", action="store_true",
                        help="无视时段限制，立即做一次盘中波动检查（测试用）")
    parser.add_argument("--session-replay", type=float, default=None,
                        metavar="HOURS",
                        help="回放最近 N 小时，列出重点时段内本应触发的告警（不推送）")
    parser.add_argument(
        "--pre-window-minutes", type=float, default=None,
        help="临时覆盖「事件公布前多久推剧本」，用于演示（如 4320 = 提前3天）",
    )
    parser.add_argument(
        "--max-events", type=int, default=None,
        help="临时覆盖单轮最多推几条事件",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"配置加载失败：{exc}")
        return 2

    monitor = Monitor(cfg, dry_run=args.dry_run)

    if args.check:
        return 0 if monitor.check() else 1

    if args.list_events:
        for event in monitor.calendar.important_events():
            print(f"{event.date} {event.time} [{event.region}] {event.name} "
                  f"重要性={event.importance_text()} 预期={event.forecast_text()} "
                  f"前值={event.previous_text()} 今值={event.actual_text()}")
        return 0

    if args.test_push:
        body = Alert(
            symbol="推送链路测试（无品种）",
            short_name="测试",
            what_happened="手动测试 Server酱推送通道",
            time_text=f"{datetime.now(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间",
            price=None,
            support=None,
            resistance=None,
            action_text=monitor.safety.enforce_action(""),
            ai_text="如果你在微信收到这条消息，说明推送链路已经打通，格式与真实告警一致。",
            signal_type="测试",
            disclaimer=monitor.safety.disclaimer,
        )
        ok = monitor.notifier.send(body.title(), sanitize_for_push(body.render())).ok
        print("推送成功" if ok else "推送失败，请查看日志")
        return 0 if ok else 1

    if args.session_replay:
        return replay_session(monitor, args.session_replay)

    if not (args.once or args.loop):
        parser.print_help()
        return 0

    def one_pass() -> None:
        try:
            # 每轮都调用一次：跨天时自动把日志切到新文件
            setup_logging(
                monitor.cfg.resolve_path(str(monitor.app.get("log_dir", "logs"))),
                str(monitor.app.get("log_level", "INFO")),
            )
            # 重读本地控制台改动（品种开关 / 免打扰 / 重点提醒 / 持仓）
            monitor.reload_control()
            # 已退出免打扰：先把延迟队列里的消息补发掉，再跑本轮轨道
            if not monitor.control.is_dnd_active(datetime.now(BEIJING)):
                flushed = monitor._flush_pending()
                if flushed:
                    log.info("免打扰已结束，补发延迟消息 %d 条", flushed)
            if not (args.event_only or args.session_only or args.short_only
                    or args.world_only):
                monitor.run_trend(force=args.force, only=args.symbol)
            if not (args.trend_only or args.session_only or args.short_only
                    or args.world_only):
                monitor.run_events(force=args.force,
                                   pre_minutes_override=args.pre_window_minutes,
                                   max_events_override=args.max_events)
            if not (args.trend_only or args.event_only or args.short_only
                    or args.world_only):
                monitor.run_session_watch(force=args.force, ignore_window=args.ignore_window)
            if not (args.trend_only or args.event_only or args.session_only
                    or args.world_only):
                monitor.run_short_watch(force=args.force, ignore_window=args.ignore_window)
            if not (args.trend_only or args.event_only or args.session_only
                    or args.short_only):
                pushed = monitor.run_world_events(
                    force=args.force or args.world_only)
                if pushed:
                    log.info("国际大事本轮推送 %d 条：%s",
                             len(pushed), "；".join(pushed))
        except Exception:
            log.error("本轮执行出现异常（程序继续存活）：\n%s", traceback.format_exc())

    if args.once:
        one_pass()
        return 0

    base_interval = float(monitor.app.get("poll_interval_seconds", 300))
    log.info(
        "常驻模式启动。平时轮询 %.0fs；重点时段轮询 %.0fs。重点时段：%s",
        base_interval, monitor.session_interval,
        "；".join(w.label() for w in monitor.session_windows) or "（未配置）",
    )
    while True:
        started = time.time()
        one_pass()
        elapsed = time.time() - started

        # 重点时段内加密轮询，抓住盘中急动
        in_win = monitor.current_window()
        interval = monitor.session_interval if in_win else base_interval
        sleep_for = max(5.0, interval - elapsed)
        log.info(
            "本轮结束，耗时 %.1fs，%s，休眠 %.0fs",
            elapsed,
            f"处于重点时段「{in_win.name}」→ 加密轮询" if in_win else "非重点时段",
            sleep_for,
        )
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())

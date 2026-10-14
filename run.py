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
    SIGNAL_TREND,
    compute_position_pnl,
    fmt_num,
    now_utc8_str,
    render_position_line,
    short_symbol,
    strip_markdown,
    zone_hint,
)
from src.config import PROJECT_ROOT, Config, load_config
from src.datasource.econ_calendar import BEIJING, EconCalendarClient, EconEvent
from src.datasource.okx import Candle, OkxClient
from src.datasource.us_stock import UsStockClient
from src.llm.deepseek import DeepSeekClient
from src.llm.prompts import (
    SYSTEM_PROMPT,
    build_event_post_user_message,
    build_event_pre_user_message,
    build_session_user_message,
    build_trend_user_message,
)
from src.logutil import get_logger, setup_logging
from src.modules.event import (
    build_branch_facts,
    categorize,
    determine_branch,
    publish_time_text,
    render_fallback_script,
    render_post_fallback,
)
from src.modules.levels import derive_levels, detect_level_breaks
from src.modules.session import (
    UTC8,
    active_window,
    decide_alert,
    detect_moves,
    parse_windows,
    render_fallback_text as render_session_fallback,
)
from src.modules.trend import STATE_LABEL, evaluate_trend
from src.state import StateStore

log = get_logger("run")

# 标题用的极短状态名（微信通知栏只显示标题）
STATE_SHORT = {"bull": "多头", "bear": "空头", "range": "震荡", "unknown": "数据不足"}


class Monitor:
    def __init__(self, cfg: Config, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.app = cfg.get("app", {}) or {}
        self.dry_run = bool(dry_run or self.app.get("dry_run", False))

        log_dir = cfg.resolve_path(str(self.app.get("log_dir", "logs")))
        setup_logging(log_dir, str(self.app.get("log_level", "INFO")))
        self.log_file = log_dir

        self.state = StateStore(cfg.resolve_path(str(self.app.get("state_file", "state/state.json"))))

        market = cfg.get("market", {}) or {}
        self.okx = OkxClient(
            base_url=market.get("base_url", "https://www.okx.com"),
            timeout=float(market.get("timeout_seconds", 20)),
            max_retries=int(market.get("max_retries", 4)),
            backoff=float(market.get("retry_backoff_seconds", 2)),
        )
        self.weekly_bar = market.get("weekly_bar", "1W")
        self.weekly_limit = int(market.get("weekly_candle_limit", 300))

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
        self.calendar = EconCalendarClient(
            days_ahead=int(cal_cfg.get("days_ahead", 7)),
            min_importance=int(cal_cfg.get("min_importance", 2)),
            keywords=list(cal_cfg.get("keywords", []) or []),
            regions=list(cal_cfg.get("regions", []) or []),
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

    # ================= 推送 =================
    def push(self, title: str, body: str, fingerprint: str,
             force: bool = False, cooldown_minutes: float | None = None) -> bool:
        body = strip_markdown(body)
        body = self.safety.ensure_disclaimer(body)

        cooldown = self.cooldown_minutes if cooldown_minutes is None else cooldown_minutes
        if not force:
            cooling, remaining = self.state.is_in_cooldown(fingerprint, cooldown)
            if cooling:
                log.info(
                    "信号 %s 处于冷却期，剩余 %.0f 分钟，跳过推送", fingerprint, remaining / 60
                )
                return False

        result = self.notifier.send(title, body)
        if result.ok:
            self.state.mark_sent(fingerprint)
            self.state.save()
            log.info("已推送：%s", title)
        else:
            log.error("推送失败：%s -> %s", title, result.detail)
        return result.ok

    # ================= 周K来源调度 =================
    def _weekly_candles(self, symbol: dict) -> tuple[list[Candle], str]:
        """按 symbol.history_source 选择周K来源。

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

    # ================= 趋势轨 =================
    def run_trend(self, force: bool = False, only: str | None = None) -> list[str]:
        messages: list[str] = []
        trend_cfg = self.cfg.get("trend", {}) or {}
        rules = trend_cfg.get("rules", {}) or {}
        only_on_change = bool(trend_cfg.get("only_alert_on_state_change", True))

        for symbol in self.cfg.enabled_symbols():
            inst_id = symbol["inst_id"]
            if only and inst_id != only:
                continue
            name = symbol.get("name", inst_id)
            inst_type = symbol.get("inst_type", "SWAP")
            ma_periods = [int(p) for p in (symbol.get("ma_periods") or [20])]
            cross_pairs = symbol.get("cross_pairs") or []

            try:
                if self.okx.get_instrument(inst_id, inst_type) is None:
                    log.error("%s 在 OKX 上不存在，请检查 config 里的 inst_id", inst_id)
                    continue
                ticker = self.okx.get_ticker(inst_id)
                candles, history_desc = self._weekly_candles(symbol)
            except Exception as exc:
                log.error("%s 行情获取失败（本轮跳过，下轮重试）：%s", inst_id, exc)
                continue

            if not candles:
                log.error("%s 没拉到任何周K，跳过", inst_id)
                continue

            divergence = self._divergence_warning(symbol, candles, ticker.last)

            prev = self.state.get_trend_state(inst_id)
            prev_state = (prev or {}).get("state")

            snapshot = evaluate_trend(
                inst_id=inst_id,
                name=name,
                candles=candles,
                last_price=ticker.last,
                ma_periods=ma_periods,
                cross_pairs=cross_pairs,
                primary_ma=str(trend_cfg.get("primary_ma", "fast")),
                tolerance_pct=float(trend_cfg.get("break_tolerance_pct", 0.3)),
                prev_state=prev_state,
                price_vs_ma=bool(rules.get("price_vs_ma", True)),
                ma_cross=bool(rules.get("ma_cross", True)),
            )

            levels_cfg = symbol.get("levels", {}) or {}
            levels = derive_levels(
                inst_id=inst_id,
                candles=[c for c in candles if c.confirmed],
                current_price=ticker.last,
                lookback_weeks=int(levels_cfg.get("lookback_weeks", 12)),
                manual_support=levels_cfg.get("manual_support"),
                manual_resistance=levels_cfg.get("manual_resistance"),
                min_distance_pct=float(levels_cfg.get("min_distance_pct", 1.5)),
            )
            breaks = detect_level_breaks(
                levels=levels,
                current_price=ticker.last,
                prev_close=snapshot.close,
                tolerance_pct=float(trend_cfg.get("break_tolerance_pct", 0.3)),
            )

            # 状态记忆（无论是否告警都要记）
            self.state.set_trend_state(inst_id, {
                "state": snapshot.state,
                "as_of_week": snapshot.as_of_week,
                "close": snapshot.close,
                "ma_values": {str(k): v for k, v in snapshot.ma_values.items()},
                "updated_at": now_utc8_str(),
            })
            self.state.save()

            for note in snapshot.insufficient:
                log.warning("%s 数据不足提示：%s", inst_id, note)

            should_alert = snapshot.state_changed or force
            if only_on_change and not snapshot.state_changed and not force:
                log.info(
                    "%s 趋势状态未变化（%s），按配置不推送", inst_id, snapshot.state
                )
                continue
            if not should_alert:
                continue

            body = self._build_trend_alert(symbol, snapshot, levels, breaks, ticker,
                                           history_desc, divergence)
            fp = f"trend|{inst_id}|{snapshot.state}"
            if self.push(body.title(), body.render(), fp, force=force):
                messages.append(body.render())
        return messages

    def _build_trend_alert(self, symbol: dict, snapshot, levels, breaks, ticker,
                           history_desc: str = "OKX 周K",
                           divergence: dict | None = None) -> Alert:
        inst_id = snapshot.inst_id

        trigger_bits: list[str] = []
        for signal in snapshot.signals:
            trigger_bits.append(signal.text)
        if not trigger_bits:
            if snapshot.prev_state is None:
                trigger_bits.append(
                    f"首次建立趋势基线：当前趋势状态为「{snapshot.state_label}」"
                )
            elif snapshot.prev_state == snapshot.state:
                # 强制/手动查询时状态并未变化，不能写成「由 X 变为 X」
                trigger_bits.append(
                    f"当前趋势状态维持「{snapshot.state_label}」不变（本次为手动查询）"
                )
            else:
                trigger_bits.append(
                    f"趋势状态由「{snapshot.prev_state_label}」变为「{snapshot.state_label}」"
                )
        for brk in breaks:
            trigger_bits.append(brk["text"])
        trigger = "；".join(trigger_bits)

        pnl = compute_position_pnl(symbol.get("position") or {}, ticker.last)
        extra_lines: list[str] = [
            f"【周线均线】{snapshot.render_ma()}",
            f"【历史数据来源】{history_desc}｜已收盘周K {snapshot.weeks_available} 根",
        ]
        if divergence:
            line = (
                f"【跟踪偏差】美股周收盘 {fmt_num(divergence['us_close'])}"
                f"（{divergence['us_week']}）vs OKX 现价 {fmt_num(ticker.last)}"
                f"，偏离 {divergence['divergence_pct']:+.2f}%"
            )
            if divergence["breached"]:
                line += f" ⚠ 已超过你设的 {divergence['threshold']:g}% 阈值，请人工确认两个市场是否仍同步"
            extra_lines.append(line)
        extra_lines.append(
            f"【关键位来源】{'配置指定' if levels.source == 'manual' else '由真实周K算法推导'}"
            f"（近 {levels.lookback_weeks} 周高低点）"
        )
        if snapshot.insufficient:
            extra_lines.append("【数据提示】" + "；".join(snapshot.insufficient))
        if pnl:
            extra_lines.append(render_position_line(pnl))

        fallback = self._trend_fallback_text(snapshot, levels, breaks, pnl)

        facts: dict[str, Any] = {
            "品种": snapshot.name,
            "品种代码": inst_id,
            "信号类型": SIGNAL_TREND,
            "已收盘周K(北京时间周)": snapshot.as_of_week,
            "周线收盘价": snapshot.close,
            "实时最新价": ticker.last,
            "最新价UTC时间": ticker.fetched_at_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "均线": {f"MA{k}": v for k, v in snapshot.ma_values.items()},
            "均线方向": {f"MA{k}": ("向上" if v else "向下" if v is False else "未知")
                     for k, v in snapshot.ma_rising.items()},
            "已收盘周K数量": snapshot.weeks_available,
            "数据不足说明": snapshot.insufficient,
            "趋势状态": snapshot.state_label,
            "上一次趋势状态": snapshot.prev_state_label,
            "触发条件": trigger,
            "支撑位": levels.nearest_support(),
            "压力位": levels.nearest_resistance(),
            "支撑位列表": levels.supports[:6],
            "压力位列表": levels.resistances[:6],
            "破位情况": [b["text"] for b in breaks],
            "历史数据来源": history_desc,
        }
        if divergence:
            facts["与美股跟踪偏差%"] = round(divergence["divergence_pct"], 2)
            facts["美股最新周收盘"] = divergence["us_close"]
        if pnl:
            facts["你的持仓"] = {
                "方向": "多单" if pnl.side == "long" else "空单",
                "开仓价": pnl.entry_price,
                "保证金U": pnl.margin_usdt,
                "杠杆": pnl.leverage,
                "浮动盈亏U": round(pnl.unrealized, 4),
                "保证金收益率%": round(pnl.roe_pct, 2),
            }

        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_trend_user_message(facts), facts, fallback
        )
        if source != "deepseek":
            ai_text = f"{fallback}（说明：本条解读由本地规则模板生成）"

        safe_text, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        support = levels.nearest_support()
        resistance = levels.nearest_resistance()
        happened = trigger
        action = f"{self.safety.enforce_action('')} —— {zone_hint(ticker.last, support, resistance)}"

        state_short = STATE_SHORT.get(snapshot.state, snapshot.state)
        if snapshot.prev_state is None:
            title_hint = f"趋势基线：{state_short}"
        elif snapshot.prev_state == snapshot.state:
            title_hint = f"趋势{state_short}未变"
        else:
            title_hint = (
                f"趋势{STATE_SHORT.get(snapshot.prev_state, snapshot.prev_state)}"
                f"→{state_short}"
            )

        return Alert(
            symbol=f"{snapshot.name}（{inst_id}）",
            short_name=short_symbol(inst_id, snapshot.name),
            title_hint=title_hint,
            what_happened=happened,
            time_text=(
                f"{ticker.fetched_at_utc.astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间"
                f" / {ticker.fetched_at_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                f"（来源 OKX ticker）"
            ),
            price=ticker.last,
            support=support,
            resistance=resistance,
            action_text=action,
            ai_text=safe_text,
            signal_type=SIGNAL_TREND,
            extra_lines=extra_lines,
            disclaimer=self.safety.disclaimer,
        )

    @staticmethod
    def _trend_fallback_text(snapshot, levels, breaks, pnl) -> str:
        break_text = ""
        if breaks:
            break_text = "，".join(b["text"] for b in breaks) + "。"
        if snapshot.prev_state is None:
            change_text = f"本次为首轮基线记录，趋势判定为「{snapshot.state_label}」。"
        elif snapshot.prev_state == snapshot.state:
            change_text = (
                f"趋势判定为「{snapshot.state_label}」，"
                f"与上一次记录的状态一致，尚未发生变化。"
            )
        else:
            change_text = (
                f"趋势判定为「{snapshot.state_label}」，"
                f"较上一次「{snapshot.prev_state_label}」已发生变化。"
            )
        return (
            f"周线收盘 {fmt_num(snapshot.close)}，{snapshot.render_ma()}，"
            f"{change_text}"
            f"{break_text}历史上这类均线状态切换后，通常需要观察价格能否在新位置站稳，而不是立刻下结论。"
        )

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
        max_events = int(
            max_events_override
            if max_events_override is not None
            else event_cfg.get("max_events_per_run", 3)
        )
        watch = event_cfg.get("watch_symbols") or []
        now = datetime.now(BEIJING)

        try:
            events = self.calendar.fetch_range()
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
                self._emit_pre_script(event, now, watch, fp, force)
                self.state.set_event(fp, {**record, "pre_sent": True, "pre_at": now_utc8_str()})
                self.state.save()
                out.append(event.name)
                emitted += 1

            # ---------- 发布后：对号入座 ----------
            minutes_since = -minutes_to_publish
            if 0 <= minutes_since <= max(post_minutes, 240) and event.released:
                record = self.state.get_event(fp) or {}
                if record.get("post_sent") and not force:
                    continue
                self._emit_post_result(event, now, watch, fp, force)
                self.state.set_event(fp, {
                    **record, "post_sent": True, "post_at": now_utc8_str(),
                    "actual": event.actual,
                })
                self.state.save()
                out.append(event.name)
                emitted += 1
        return out

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

    def _target_symbols(self, watch: list[str]) -> list[dict]:
        symbols = self.cfg.enabled_symbols()
        if not watch:
            return symbols
        return [s for s in symbols if s.get("inst_id") in watch]

    def _primary_symbol_for_event(self, event: EconEvent, watch: list[str]) -> dict | None:
        """按事件类别挑最相关的品种。

        原油库存挂到 CL，通胀/就业挂到纳指，避免「EIA 库存」配纳指支撑位的笑话。
        """
        symbols = self._target_symbols(watch)
        if not symbols:
            return None
        event_cfg = self.cfg.get("event", {}) or {}
        mapping = event_cfg.get("symbol_by_category") or {}
        target_id = mapping.get(categorize(event.name)) or event_cfg.get("default_symbol")
        if target_id:
            for symbol in symbols:
                if symbol.get("inst_id") == target_id:
                    return symbol
        return symbols[0]

    def _symbol_levels(self, symbol: dict) -> tuple[dict | None, float | None, str]:
        """取该品种的真实行情 + 支撑压力位（周K来源与趋势轨保持一致）。"""
        inst_id = symbol["inst_id"]
        try:
            ticker = self.okx.get_ticker(inst_id)
            candles, _ = self._weekly_candles(symbol)
        except Exception as exc:
            log.error("事件轨：%s 行情获取失败：%s", inst_id, exc)
            return None, None, ""
        confirmed = [c for c in candles if c.confirmed]
        if not confirmed:
            return None, None, ""
        levels_cfg = symbol.get("levels", {}) or {}
        levels = derive_levels(
            inst_id=inst_id,
            candles=confirmed,
            current_price=ticker.last,
            lookback_weeks=int(levels_cfg.get("lookback_weeks", 12)),
            manual_support=levels_cfg.get("manual_support"),
            manual_resistance=levels_cfg.get("manual_resistance"),
            min_distance_pct=float(levels_cfg.get("min_distance_pct", 1.5)),
        )
        return levels.as_dict(), ticker.last, ticker.fetched_at_utc.strftime("%Y-%m-%d %H:%M:%S")

    def _emit_pre_script(self, event: EconEvent, now: datetime, watch: list[str],
                         fp: str, force: bool) -> None:
        primary = self._primary_symbol_for_event(event, watch)
        if primary is None:
            log.warning("事件 %s 没有可关联的品种，跳过", event.name)
            return
        levels_dict, last_price, ts = self._symbol_levels(primary)
        facts = build_branch_facts(event, primary, levels_dict)
        facts["当前价"] = last_price
        facts["当前价时间"] = ts

        fallback = render_fallback_script(facts, primary["inst_id"], region=event.region)
        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_event_pre_user_message(facts), facts, fallback
        )
        if source != "deepseek":
            ai_text = fallback
        body, blocked = self.safety.sanitize_ai_text(ai_text, fallback)

        alert = EventAlert(
            headline=event.name,
            stage="pre",
            happened=(
                f"{event.name} 将于 {publish_time_text(event, now)} 公布，"
                f"提前给你三分支预案"
            ),
            time_text=f"{event.date} {event.time} 北京时间（公布时间，来源 AKShare 经济日历）",
            body=body,
            symbol=f"{primary.get('name')}（{primary['inst_id']}）",
            short_name=short_symbol(primary["inst_id"], primary.get("name", "")),
            title_hint=f"{event.name[:14]} 即将公布",
            price=last_price,
            support=(levels_dict or {}).get("nearest_support"),
            resistance=(levels_dict or {}).get("nearest_resistance"),
            action_text=(
                f"{self.safety.enforce_action('')} —— "
                f"先定好三个分支各自对应哪个价位，等数据出来对号入座"
            ),
            forecast_text=event.forecast_text(),
            previous_text=event.previous_text(),
            disclaimer=self.safety.disclaimer,
            extra_lines=[f"【重要性】{event.importance_text()}"],
        )
        log.info("事件预警：%s 公布时间 %s", event.name, publish_time_text(event, now))
        self.push(alert.title(), alert.render(), f"{fp}|pre", force=force)

    def _emit_post_result(self, event: EconEvent, now: datetime, watch: list[str],
                          fp: str, force: bool) -> None:
        branch, reason = determine_branch(event.actual, event.forecast, event.previous)
        primary = self._primary_symbol_for_event(event, watch)
        if primary is None:
            log.warning("事件 %s 没有可关联的品种，跳过", event.name)
            return
        targets = [primary]
        for symbol in targets:
            levels_dict, last_price, ts = self._symbol_levels(symbol)
            facts = build_branch_facts(event, symbol, levels_dict)
            facts["当前价"] = last_price
            facts["当前价时间"] = ts
            facts["判定分支"] = branch
            facts["判定依据"] = reason

            fallback = render_post_fallback(branch, reason, event, levels_dict, last_price)
            ai_text, source = self.llm.translate(
                SYSTEM_PROMPT, build_event_post_user_message(facts), facts, fallback
            )
            if source != "deepseek":
                ai_text = fallback
            body, _ = self.safety.sanitize_ai_text(ai_text, fallback)

            alert = EventAlert(
                headline=event.name,
                stage="post",
                happened=reason,
                time_text=(
                    f"{event.date} {event.time} 北京时间公布"
                    f"　数据时间 {ts} UTC（来源 AKShare + OKX）"
                ),
                body=body,
                symbol=f"{symbol.get('name')}（{symbol['inst_id']}）",
                short_name=short_symbol(symbol["inst_id"], symbol.get("name", "")),
                title_hint=f"落在剧本{branch}",
                price=last_price,
                support=(levels_dict or {}).get("nearest_support"),
                resistance=(levels_dict or {}).get("nearest_resistance"),
                action_text=(
                    f"{self.safety.enforce_action('')} —— "
                    + zone_hint(last_price,
                                (levels_dict or {}).get("nearest_support"),
                                (levels_dict or {}).get("nearest_resistance"))
                ),
                forecast_text=event.forecast_text(),
                previous_text=event.previous_text(),
                disclaimer=self.safety.disclaimer,
                extra_lines=[
                    f"【实际值】{event.actual_text()}　【预期值】{event.forecast_text()}　"
                    f"【前值】{event.previous_text()}",
                ],
            )
            log.info("事件结果：%s 判定分支 %s", event.name, branch)
            self.push(alert.title(), alert.render(), f"{fp}|post|{symbol['inst_id']}", force=force)

    # ================= 重点时段盘中波动 =================
    def current_window(self, now_bj: datetime | None = None):
        now_bj = now_bj or datetime.now(BEIJING)
        return active_window(self.session_windows, now_bj)

    def run_session_watch(self, force: bool = False, ignore_window: bool = False,
                          now_bj: datetime | None = None) -> list[str]:
        """重点时段内监测盘中大幅波动。

        force         = 绕过冷却
        ignore_window = 无视时段限制（用于手动测试）
        """
        if not self.session_enabled and not force and not ignore_window:
            return []

        now_bj = now_bj or datetime.now(BEIJING)
        window = self.current_window(now_bj)
        if window is None and not ignore_window:
            return []
        window_label = window.label() if window else "手动触发（不在配置时段内）"

        messages: list[str] = []
        for symbol in self.cfg.enabled_symbols():
            inst_id = symbol["inst_id"]
            try:
                ticker = self.okx.get_ticker(inst_id)
                candles = self.okx.get_candles(inst_id, self.session_bar, self.session_limit)
            except Exception as exc:
                log.error("盘中监控：%s 行情获取失败（跳过）：%s", inst_id, exc)
                continue
            if not candles:
                continue

            since_open_ms = None
            if window is not None:
                since_open_ms = int(window.start_moment(now_bj).timestamp() * 1000)

            hits = detect_moves(
                candles=candles,
                now_ms=ticker.ts_ms,
                rules=self.session_rules,
                since_open_ms=since_open_ms,
                since_open_pct=self.session_since_open_pct,
            )
            if not hits:
                log.info("盘中监控：%s 时段[%s]内无超阈值波动", inst_id, window_label)
                continue

            levels_dict, _, _ = self._symbol_levels(symbol)

            # ---- 防刷屏：幅度分档 + 升级即报 + 冷却（逻辑在 session.decide_alert，可测）----
            biggest = max(abs(h["change_pct"]) for h in hits)
            state_key = f"{inst_id}|{window_label}"
            record = self.state.get_session(state_key) or {}
            decision = decide_alert(
                biggest_pct=biggest,
                last_ts=record.get("last_ts"),
                last_bucket=int(record.get("last_bucket", -1)),
                now_ts=time.time(),
                cooldown_minutes=self.session_cooldown,
                bucket_pct=self.session_bucket_pct,
            )
            if not force and not decision.alert:
                log.info(
                    "盘中监控：%s 时段[%s] 波动 %.2f%%，%s -> 跳过",
                    inst_id, window_label, biggest, decision.reason,
                )
                continue

            body = self._build_session_alert(symbol, ticker, hits, window_label, levels_dict)
            fp = f"session|{inst_id}|{window_label}|b{decision.bucket}"
            log.info(
                "盘中监控：%s 时段[%s] 触发告警（%s），最大幅度 %.2f%%，档位 %s",
                inst_id, window_label, decision.reason, biggest, decision.bucket,
            )
            if self.push(body.title(), body.render(), fp, force=True):
                self.state.set_session(state_key, {
                    "last_ts": time.time(),
                    "last_bucket": max(decision.bucket, int(record.get("last_bucket", -1))),
                    "last_pct": round(biggest, 2),
                    "last_at": now_utc8_str(),
                    "window": window_label,
                })
                self.state.save()
                messages.append(body.render())
        return messages

    def _build_session_alert(self, symbol: dict, ticker, hits: list[dict],
                             window_label: str, levels_dict: dict | None) -> Alert:
        inst_id = symbol["inst_id"]
        name = symbol.get("name", inst_id)
        # 「发生了什么」只放最大那一条，保证一眼看懂；其余明细放到后面
        biggest = max(hits, key=lambda h: abs(h["change_pct"]))
        what = f"{biggest['direction']} {abs(biggest['change_pct']):.2f}%　" \
               f"（{biggest['text']}）"
        trigger = "；".join(h["text"] for h in hits)

        extra_lines = [
            f"【重点时段】{window_label}",
            f"【盘中K线】{self.session_bar} 周期，来源 OKX /api/v5/market/candles",
            f"【关键位来源】由真实周K算法推导（近 {symbol.get('levels', {}).get('lookback_weeks', 12)} 周高低点）",
        ]
        if len(hits) > 1:
            extra_lines.append("【全部触发条件】" + trigger)
        pnl = compute_position_pnl(symbol.get("position") or {}, ticker.last)
        if pnl:
            extra_lines.append(render_position_line(pnl))

        facts: dict[str, Any] = {
            "品种": name,
            "品种代码": inst_id,
            "信号类型": "盘中波动",
            "重点时段": window_label,
            "实时最新价": ticker.last,
            "最新价UTC时间": ticker.fetched_at_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "触发条件": trigger,
            "波动明细": [
                {
                    "类型": h["kind"],
                    "区间分钟": h.get("minutes"),
                    "涨跌幅%": round(h["change_pct"], 2),
                    "起点价": h["ref_price"],
                    "起点时间北京": h["ref_time"],
                    "现价": h["last_price"],
                    "阈值%": h.get("threshold"),
                }
                for h in hits
            ],
            "支撑位": (levels_dict or {}).get("nearest_support"),
            "压力位": (levels_dict or {}).get("nearest_resistance"),
        }
        if pnl:
            facts["你的持仓"] = {
                "方向": "多单" if pnl.side == "long" else "空单",
                "开仓价": pnl.entry_price,
                "杠杆": pnl.leverage,
                "浮动盈亏U": round(pnl.unrealized, 4),
                "保证金收益率%": round(pnl.roe_pct, 2),
            }

        fallback = render_session_fallback(name, window_label, hits, levels_dict)
        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_session_user_message(facts), facts, fallback
        )
        if source != "deepseek":
            ai_text = f"{fallback}（说明：本条解读由本地规则模板生成）"
        safe_text, _ = self.safety.sanitize_ai_text(ai_text, fallback)

        return Alert(
            symbol=f"{name}（{inst_id}）",
            short_name=short_symbol(inst_id, name),
            title_hint=f"盘中{biggest['direction']} {abs(biggest['change_pct']):.2f}%",
            what_happened=what,
            time_text=(
                f"{ticker.fetched_at_utc.astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间"
                f" / {ticker.fetched_at_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                f"（来源 OKX ticker）"
            ),
            price=ticker.last,
            support=(levels_dict or {}).get("nearest_support"),
            resistance=(levels_dict or {}).get("nearest_resistance"),
            action_text=(
                f"{self.safety.enforce_action('')} —— "
                + zone_hint(ticker.last,
                            (levels_dict or {}).get("nearest_support"),
                            (levels_dict or {}).get("nearest_resistance"))
            ),
            ai_text=safe_text,
            signal_type="盘中波动",
            extra_lines=extra_lines,
            disclaimer=self.safety.disclaimer,
        )

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
            print("    SendKey：已从环境变量读取 ✓")
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
        for symbol in self.cfg.enabled_symbols():
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

        print("\n[6] 红线自查")
        print("    · 本程序只调用 OKX market 只读接口，无任何下单/平仓/撤单调用 ✓")
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
    for symbol in monitor.cfg.enabled_symbols():
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
            rec = fired.get(window.name) or {}
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
            fired[window.name] = {
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
        ok = monitor.notifier.send(body.title(), strip_markdown(body.render())).ok
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
            if not (args.event_only or args.session_only):
                monitor.run_trend(force=args.force, only=args.symbol)
            if not (args.trend_only or args.session_only):
                monitor.run_events(force=args.force,
                                   pre_minutes_override=args.pre_window_minutes,
                                   max_events_override=args.max_events)
            if not (args.trend_only or args.event_only):
                monitor.run_session_watch(force=args.force, ignore_window=args.ignore_window)
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

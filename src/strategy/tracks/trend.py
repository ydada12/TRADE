"""趋势轨：周线均线状态切换提醒（多头/空头/震荡）。

规则与文案原样搬自 run.py。以 mixin 挂在 Monitor 上，调用点（main / check）
零改动。改阈值看 config.yaml 的 trend 段，改指纹看 ../fingerprint.py，
改措辞看 ../wording.py。
"""

from __future__ import annotations

import traceback
from typing import Any

from src.alert import (
    Alert,
    SIGNAL_TREND,
    compute_position_pnl,
    fmt_num,
    now_utc8_str,
    render_position_line,
    short_symbol,
    zone_hint,
)
from src.datasource.econ_calendar import BEIJING
from src.llm.prompts import SYSTEM_PROMPT, build_trend_user_message
from src.logutil import get_logger
from src.modules.levels import derive_levels, detect_level_breaks
from src.modules.trend import evaluate_trend

from ..fingerprint import retry_fp, trend_fp
from ..gate import PUSH_FAILED, PUSH_SENT
from ..helpers import gap_pct as _gap_pct
from ..wording import STATE_SHORT

log = get_logger("run")


class TrendTrack:
    """趋势轨 mixin。"""

    def run_trend(self, force: bool = False, only: str | None = None) -> list[str]:
        messages: list[str] = []
        trend_cfg = self.cfg.get("trend", {}) or {}
        rules = trend_cfg.get("rules", {}) or {}
        only_on_change = bool(trend_cfg.get("only_alert_on_state_change", True))

        for symbol in self._active_symbols():
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

            new_record = {
                "state": snapshot.state,
                "as_of_week": snapshot.as_of_week,
                "close": snapshot.close,
                "ma_values": {str(k): v for k, v in snapshot.ma_values.items()},
                "updated_at": now_utc8_str(),
            }

            for note in snapshot.insufficient:
                log.warning("%s 数据不足提示：%s", inst_id, note)

            # only_alert_on_state_change=false 时每轮都进入推送流程（由冷却去重）
            should_alert = snapshot.state_changed or force or not only_on_change
            if not should_alert:
                # 没有要送达的东西，状态直接记下
                self.state.set_trend_state(inst_id, new_record)
                self.state.save()
                log.info(
                    "%s 趋势状态未变化（%s），按配置不推送", inst_id, snapshot.state
                )
                continue

            fp = trend_fp(inst_id, snapshot.state)
            try:
                body = self._build_trend_alert(symbol, snapshot, levels, breaks, ticker,
                                               history_desc, divergence)
                # 面板标注用：钉住品种与「当时看到的价」对应的时间
                body.inst_id = inst_id
                body.ts_ms = ticker.ts_ms
                status = self.push_status(body.title(), body.render(), fp, force=force,
                                          alert=body)
            except Exception:
                log.error("%s 趋势告警生成/推送异常：\n%s", inst_id, traceback.format_exc())
                status = PUSH_FAILED

            # 推送失败时不落新状态：下一轮 state_changed 仍为真，会自动重试，
            # 否则「状态已更新但用户没收到」这次切换就永远丢了。
            if not self._delivery_settled(retry_fp(fp), status, force):
                continue
            self.state.set_trend_state(inst_id, new_record)
            self.state.save()
            if status == PUSH_SENT:
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

        pnl = compute_position_pnl(self._effective_position(symbol), ticker.last)
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
            "距支撑位%": _gap_pct(levels.nearest_support(), ticker.last),
            "距压力位%": _gap_pct(levels.nearest_resistance(), ticker.last),
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
        ai_used = source == "deepseek"
        if not ai_used:
            ai_text = f"{fallback}（说明：本条解读由本地规则模板生成）"

        safe_text, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        if blocked:
            ai_used = False
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
            rule_text=not ai_used,
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

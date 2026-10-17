"""做空观察位轨：日内压力位 + 盘口卖压 → 合成「观察区 / 失效位」判断（不给开仓止损指令）。

规则与文案原样搬自 run.py。以 mixin 挂在 Monitor 上，调用点（main / check）零改动。
改阈值看 config.yaml 的 short_watch 段，改指纹看 ../fingerprint.py，改措辞看 ../wording.py。

方案A：观察位生命周期（假突破回落）
  观察位只在价格贴近压力位时才说话，价格一旦离开必然静默；
  而「上破失效位后又跌回」恰好落在那段静默里。这里用锚点状态机把它补回来：
  watching --收盘站上失效位--> broken --跌回失效位下方+卖压确认--> 补发一条回落观察
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from typing import Any

from src.alert import (
    Alert,
    SIGNAL_SHORT,
    compute_position_pnl,
    fmt_num,
    now_utc8_str,
    render_position_line,
    short_symbol,
)
from src.datasource.econ_calendar import BEIJING
from src.llm.prompts import SYSTEM_PROMPT, build_short_user_message
from src.logutil import get_logger
from src.modules.levels import derive_intraday_levels
from src.modules.session import active_window
from src.modules.short_signal import compute_orderflow_metrics, score_short_signal

from ..fingerprint import retry_fp, short_fake_break_fp, short_fp
from ..gate import PUSH_FAILED, PUSH_SENT
from ..helpers import bar_minutes
from ..wording import STATE_SHORT

log = get_logger("run")


class ShortTrack:
    """做空观察位轨 mixin。"""

    # ---------- 方案A：观察位生命周期（假突破回落）----------
    ANCHOR_STALE_HOURS = 12.0

    @staticmethod
    def _anchor_key(inst_id: str) -> str:
        return f"shortwatch|{inst_id}"

    def _open_short_anchor(self, inst_id: str, decision, now_text: str) -> None:
        """推送出观察位后登记锚点：压力位/失效位就此冻结，不再随日内新高上移。"""
        if self.dry_run:
            return
        self.state.set_session(self._anchor_key(inst_id), {
            "resistance": float(decision.observation_level),
            "invalidation": float(decision.invalidation_level),
            "peak": float(decision.invalidation_level),
            "state": "watching",
            "opened_ts": time.time(),
            "opened_at": now_text,
            "broken_at": None,
            "pushed": False,
            "at": now_text,
        })
        self.state.save()

    def _fake_break_followup(self, symbol: dict, ticker, intraday, metrics, last_close: float,
                             window_label: str, weekly_state: str | None, decision,
                             *, suppress: bool, force: bool) -> list[str]:
        """推进锚点状态；上破失效位后跌回且卖压确认时，补发一次「假突破回落观察位」。

        每个锚点只补发一次；措辞仍是观察区/失效位口径，不给任何开仓或止损指令。
        """
        inst_id = symbol["inst_id"]
        key = self._anchor_key(inst_id)
        anchor = self.state.get_session(key)
        if not anchor or not anchor.get("resistance") or not anchor.get("invalidation"):
            return []
        if time.time() - float(anchor.get("opened_ts") or 0.0) > self.ANCHOR_STALE_HOURS * 3600:
            if not self.dry_run:
                self.state.set_session(key, {})
                self.state.save()
            log.info("做空观察：%s 锚点超过 %.0f 小时未走完生命周期，已清除",
                     inst_id, self.ANCHOR_STALE_HOURS)
            return []
        if suppress:
            return []

        now_text = now_utc8_str()
        invalidation = float(anchor["invalidation"])
        resistance = float(anchor["resistance"])
        peak = float(anchor.get("peak") or invalidation)
        messages: list[str] = []

        if anchor.get("state") == "watching":
            if last_close >= invalidation:
                anchor["state"] = "broken"
                anchor["broken_at"] = now_text
                anchor["peak"] = max(peak, last_close, float(ticker.last or 0.0))
                log.info("做空观察：%s 收盘 %.4g 站上失效位 %.4g，锚点转入「已上破」",
                         inst_id, last_close, invalidation)
        elif ticker.last and float(ticker.last) > peak:
            anchor["peak"] = float(ticker.last)
        anchor["at"] = now_text

        if anchor.get("state") == "broken" and not anchor.get("pushed"):
            peak = float(anchor.get("peak") or invalidation)
            pullback = ((peak - last_close) / last_close * 100.0) if last_close else 0.0
            sell_ok = (
                metrics.imbalance is not None
                and metrics.imbalance <= self.short_params["imbalance_threshold"]
            ) or (
                metrics.taker_sell_ratio is not None
                and metrics.taker_sell_ratio >= self.short_params["taker_sell_ratio_threshold"]
            )
            if last_close < invalidation and pullback >= self.short_params["pullback_pct"] and sell_ok:
                fp = short_fake_break_fp(inst_id, resistance)
                try:
                    body = self._build_short_alert(
                        symbol, ticker, intraday, metrics, decision, window_label,
                        weekly_state, variant="fake_break", anchor=dict(anchor),
                    )
                    body.inst_id = inst_id
                    body.ts_ms = ticker.ts_ms
                    status = self.push_status(body.title(), body.render(), fp, force=force,
                                              cooldown_minutes=self.short_cooldown, alert=body)
                except Exception:
                    log.error("%s 假突破回落告警生成/推送异常：\n%s", inst_id,
                              traceback.format_exc())
                    status = PUSH_FAILED
                if self._delivery_settled(retry_fp(fp), status, force) and status == PUSH_SENT:
                    anchor["pushed"] = True
                    self._note_direction(inst_id, "偏空", SIGNAL_SHORT)
                    messages.append(body.render())
                    log.info("做空观察：%s 推送「假突破回落观察位」（破位高点 %.4g）",
                             inst_id, peak)
        if not self.dry_run:
            self.state.set_session(key, anchor)
            self.state.save()
        return messages

    def run_short_watch(self, force: bool = False, ignore_window: bool = False,
                        now_bj: datetime | None = None) -> list[str]:
        """重点时段内观察「做空观察位」：日内压力位 + 盘口卖压 → 合成一条判断。

        只产出「观察区 / 失效位」这类客观描述；不下单、不给开仓或止损指令。
        force         = 绕过冷却（成交额达标即推）
        ignore_window = 无视时段限制（手动测试用）
        """
        if not self.short_enabled and not (force or ignore_window):
            return []

        now_bj = now_bj or datetime.now(BEIJING)
        window = active_window(self.short_windows, now_bj)
        if window is None and not ignore_window:
            return []
        window_label = window.label() if window else "手动触发（不在配置时段内）"

        symbols = self._active_symbols()
        if self.short_symbols:
            symbols = [s for s in symbols if s["inst_id"] in self.short_symbols]

        messages: list[str] = []
        ib_thr = self.short_params["imbalance_threshold"]
        for symbol in symbols:
            inst_id = symbol["inst_id"]
            try:
                ticker = self.okx.get_ticker(inst_id)
                candles = self.okx.get_candles(inst_id, self.short_bar, self.short_limit)
                book = self.okx.get_orderbook(inst_id, self.short_book_depth)
                trades = self.okx.get_trades(inst_id, self.short_trades_limit)
            except Exception as exc:
                log.error("做空观察：%s 盘口/行情获取失败（本轮跳过）：%s", inst_id, exc)
                continue
            if not candles:
                continue

            intraday = derive_intraday_levels(
                inst_id, candles, ticker.last, bar_minutes=bar_minutes(self.short_bar)
            )
            resistance = intraday.nearest_resistance(max_pct=self.short_resistance_max_pct)
            metrics = compute_orderflow_metrics(
                book, trades, ticker.last, resistance,
                top_levels=self.short_top_levels, now_ms=ticker.ts_ms,
            )

            # 卖压连续性：与上一轮采样对照（无论是否告警都记下本轮，供下轮判连续）
            pkey = f"short|{inst_id}"
            prev = self.state.get_session(pkey) or {}
            prev_ib = prev.get("imbalance")
            pressure_persist = (
                isinstance(prev_ib, (int, float))
                and metrics.imbalance is not None
                and float(prev_ib) <= ib_thr
                and metrics.imbalance <= ib_thr
            )
            self.state.set_session(pkey, {"imbalance": metrics.imbalance, "at": now_utc8_str()})
            self.state.save()

            weekly_state = (self.state.get_trend_state(inst_id) or {}).get("state")
            decision = score_short_signal(
                current_price=ticker.last,
                resistance=resistance,
                recent_high=intraday.day_high,
                orderflow=metrics,
                weekly_state=weekly_state,
                pressure_persist=pressure_persist,
                **self.short_params,
            )

            # 方案A：正常观察位在价格离开压力位后必然静默，先推进锚点生命周期，
            # 把「上破失效位后跌回」那段沉默期补上（本轮已有贴近压力位的推送则不重复补）
            last_close = candles[-2].close if len(candles) >= 2 else candles[-1].close
            messages.extend(self._fake_break_followup(
                symbol, ticker, intraday, metrics, last_close, window_label, weekly_state,
                decision, suppress=decision.alert, force=force,
            ))

            if not decision.alert:
                # --force 只绕过阈值入流程用于演示；数据不足则绝不强推
                if not (force and decision.data_ok):
                    log.info("做空观察：%s 未达阈值（score=%s，%s）", inst_id, decision.score,
                             "；".join(decision.reasons) or "数据不足")
                    continue
                log.info("做空观察：%s 未达阈值，按 --force 强制进入推送流程", inst_id)
            if decision.observation_level is None:
                log.warning("做空观察：%s 缺少日内压力位，无法给出观察位，跳过", inst_id)
                continue

            fp = short_fp(inst_id, decision.observation_level)
            try:
                body = self._build_short_alert(symbol, ticker, intraday, metrics, decision,
                                               window_label, weekly_state)
                body.inst_id = inst_id
                body.ts_ms = ticker.ts_ms
                status = self.push_status(body.title(), body.render(), fp, force=force,
                                          cooldown_minutes=self.short_cooldown, alert=body)
            except Exception:
                log.error("%s 做空观察告警生成/推送异常：\n%s", inst_id, traceback.format_exc())
                status = PUSH_FAILED
            if not self._delivery_settled(retry_fp(fp), status, force):
                continue
            if status == PUSH_SENT:
                self._note_direction(inst_id, "偏空", SIGNAL_SHORT)
                if decision.observation_level and decision.invalidation_level:
                    self._open_short_anchor(inst_id, decision, now_utc8_str())
                messages.append(body.render())
        return messages

    @staticmethod
    def _short_fallback_text(name: str, intraday, metrics, decision, window_label: str,
                             anchor: dict | None = None) -> str:
        bits: list[str] = []
        if anchor:
            peak = float(anchor["peak"])
            bits.append(
                f"价格此前收盘站上失效位 {fmt_num(float(anchor['invalidation']))}"
                f"（破位高点 {fmt_num(peak)}），现已回落至该位下方，属假突破回落形态"
            )
        elif decision.rejected:
            bits.append(f"价格先触及压力位 {fmt_num(decision.observation_level)} 后回落，上冲被拒绝")
        else:
            bits.append(f"价格贴近压力位 {fmt_num(decision.observation_level)}")
        if metrics.imbalance is not None and metrics.imbalance < 0:
            bits.append(f"盘口买卖量失衡 {metrics.imbalance:+.2f}（负=卖压偏强）")
        if metrics.taker_sell_ratio is not None:
            bits.append(f"近端主动卖出占比 {metrics.taker_sell_ratio * 100:.0f}%")
        if metrics.sell_wall_price is not None and metrics.sell_wall_ratio is not None:
            bits.append(f"压力位附近挂单放大 {metrics.sell_wall_ratio:.1f} 倍")
        if "周线多头" in "；".join(decision.reasons):
            bits.append("周线仍处多头，本次属于逆势观察，风险更高")
        tail_ref = fmt_num(float(anchor["peak"])) if anchor else fmt_num(decision.invalidation_level)
        return (
            f"{name}：" + "；".join(bits) + "。"
            f"这只是观察条件：价格站上失效位 {tail_ref} 则本次观察不再成立。"
            "方向与买卖仍由你自行判断，本系统只做提醒。"
        )

    def _build_short_alert(self, symbol: dict, ticker, intraday, metrics,
                           decision, window_label: str, weekly_state: str | None,
                           variant: str | None = None, anchor: dict | None = None) -> Alert:
        inst_id = symbol["inst_id"]
        name = symbol.get("name", inst_id)
        # 方案A：假突破回落变体沿用同一套模板，但压力位/失效位改用冻结的锚点值，
        # 并且标题与首行事件句完全另写一套，保证「贴近压力位」与「冲高回落」一眼可辨
        fake_break = variant == "fake_break" and bool(anchor)
        if fake_break:
            zone = float(anchor["resistance"])
            invalidation = float(anchor["peak"])
            break_level = float(anchor["invalidation"])
            distance = ((zone - ticker.last) / ticker.last * 100.0) if ticker.last else None
            inv_dist = ((invalidation - ticker.last) / ticker.last * 100.0) if ticker.last else None
            title_hint = f"假突破回落·跌回 {fmt_num(break_level)} 下方"
            kind_label = "假突破回落观察位"
            headline = f"⬇️ 冲高回落｜跌回失效位 {fmt_num(break_level)} 下方"
        else:
            zone = decision.observation_level
            invalidation = decision.invalidation_level
            break_level = decision.invalidation_level
            distance = decision.distance_pct
            inv_dist = decision.invalidation_distance_pct
            title_hint = f"做空观察位·贴近压力位 {fmt_num(zone)}"
            kind_label = ""
            headline = (
                "⚡ 贴近压力位｜触及后上冲被拒绝" if decision.rejected
                else "⚡ 贴近压力位｜卖压失衡待确认"
            )
        imbalance_text = f"{metrics.imbalance:+.2f}" if metrics.imbalance is not None else "未提供"
        # 标注跟着真实符号走：负值才是卖压，正值是买盘挂单偏重，避免文案自相矛盾
        imbalance_annot = "（正=买盘挂单偏重）" if (metrics.imbalance or 0) > 0 else "（负=卖压）"
        sell_text = (
            f"{metrics.taker_sell_ratio * 100:.0f}%"
            if metrics.taker_sell_ratio is not None else "未提供"
        )

        extra_lines = [
            f"【重点时段】{window_label}",
            f"【观察区】{fmt_num(zone)}（现价上方 {distance:+.2f}%）" if distance is not None
            else f"【观察区】{fmt_num(zone)}",
            (f"【失效位·破位高点】{fmt_num(invalidation)}" if fake_break
             else f"【失效位】{fmt_num(invalidation)}")
            + (f"（若价格站上该位，本次观察条件不再成立，距现价 {inv_dist:+.2f}%）"
               if inv_dist is not None else "（若价格站上该位，本次观察条件不再成立）"),
            f"【盘口】买卖失衡 {imbalance_text}{imbalance_annot}｜主动卖出占比 {sell_text}"
            f"｜逐笔样本 {metrics.trade_count} 笔",
            f"【日内关键位】前日高 {fmt_num(intraday.prev_day_high)}｜当日高 "
            f"{fmt_num(intraday.day_high)}｜VWAP {fmt_num(intraday.vwap)}",
            f"【数据来源】OKX /market/books 盘口 + /market/trades 逐笔 + {self.short_bar} K线",
        ]
        if fake_break:
            extra_lines.append(
                f"【与上一条的关系】本条是 {anchor.get('opened_at') or '此前'} 那条「贴近压力位」观察的后续："
                "价格上破失效位后回落，属同一次观察的后续确认，不是新的点位推荐")
            extra_lines.append(f"【锚点压力位】{fmt_num(zone)}（推送当时冻结，不随日内新高上移）")
            extra_lines.append(
                f"【破位高点】{fmt_num(float(anchor['peak']))}"
                "（价格上破后创出的最高价，作为本次回落观察的失效位）")
            if anchor.get("broken_at"):
                extra_lines.append(f"【上破时间】{anchor['broken_at']}（北京时间）")
            extra_lines.append(
                "【触发口径】价格上破失效位后收盘重回其下方，且盘口卖压确认，属假突破回落形态")
        if metrics.sell_wall_price is not None and metrics.sell_wall_ratio is not None:
            extra_lines.append(
                f"【压力位附近卖墙】{fmt_num(metrics.sell_wall_price)}"
                f"（挂单量为同侧中位的 {metrics.sell_wall_ratio:.1f} 倍）"
            )
        weekly_label = STATE_SHORT.get(weekly_state, weekly_state) if weekly_state else "未记录"
        if weekly_state == "bull":
            extra_lines.append("【周线对照】周线多头，本次为逆势观察（打分已扣分）")
        else:
            extra_lines.append(f"【周线对照】周线状态 {weekly_label}")
        # 做空观察位隐含「偏空（日内）」；若同品种近期国际大事/事件轨给的是偏多，显式提示口径不同
        note = self._cross_track_note(inst_id, SIGNAL_SHORT, "偏空")
        if note:
            extra_lines.append(note)
        pnl = compute_position_pnl(self._effective_position(symbol), ticker.last)
        if pnl:
            extra_lines.append(render_position_line(pnl))

        facts: dict[str, Any] = {
            "品种": name,
            "品种代码": inst_id,
            "信号类型": SIGNAL_SHORT,
            "重点时段": window_label,
            "实时最新价": ticker.last,
            "最新价UTC时间": ticker.fetched_at_utc.strftime("%Y-%m-%d %H:%M:%S"),
            "日内观察区(压力位)": zone,
            "观察区距现价%": round(distance, 2) if distance is not None else None,
            "失效位": invalidation,
            "失效位距现价%": round(inv_dist, 2) if inv_dist is not None else None,
            "前日高点": intraday.prev_day_high,
            "当日高点": intraday.day_high,
            "当日低点": intraday.day_low,
            "当日VWAP": intraday.vwap,
            "盘口买卖失衡": metrics.imbalance,
            "主动卖出占比%": (
                round(metrics.taker_sell_ratio * 100, 1)
                if metrics.taker_sell_ratio is not None else None
            ),
            "压力位附近卖墙": metrics.sell_wall_price,
            "卖墙倍数": round(metrics.sell_wall_ratio, 2) if metrics.sell_wall_ratio is not None else None,
            "逐笔样本数": metrics.trade_count,
            "盘口档位数": metrics.depth_levels,
            "周线状态": weekly_label,
            "触发理由": decision.reasons,
            "打分": decision.score,
        }
        if fake_break:
            facts["信号类型"] = f"{SIGNAL_SHORT}（假突破回落）"
            facts["锚点压力位(已冻结)"] = zone
            facts["破位高点(失效位)"] = float(anchor["peak"])
            facts["上破时间"] = anchor.get("broken_at")
            facts["破位后回落%"] = round(
                (float(anchor["peak"]) - ticker.last) / ticker.last * 100.0, 2
            ) if ticker.last else None
        if pnl:
            facts["你的持仓"] = {
                "方向": "多单" if pnl.side == "long" else "空单",
                "开仓价": pnl.entry_price,
                "杠杆": pnl.leverage,
                "浮动盈亏U": round(pnl.unrealized, 4),
                "保证金收益率%": round(pnl.roe_pct, 2),
            }

        fallback = self._short_fallback_text(name, intraday, metrics, decision, window_label,
                                            anchor=anchor if fake_break else None)
        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_short_user_message(facts), facts, fallback
        )
        ai_used = source == "deepseek"
        if not ai_used:
            ai_text = f"{fallback}（说明：本条解读由本地规则模板生成）"
        safe_text, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        if blocked:
            ai_used = False

        rejection = "，触及压力位后回落（上冲被拒绝）" if decision.rejected else "，贴近压力位"
        # 卖压措辞必须由真实数据支撑：没命中就直说，不写死「盘口呈现卖压」
        sell_bits: list[str] = []
        if (metrics.taker_sell_ratio is not None
                and metrics.taker_sell_ratio >= self.short_params["taker_sell_ratio_threshold"]):
            sell_bits.append(f"主动卖出占比 {metrics.taker_sell_ratio * 100:.0f}%")
        if (metrics.imbalance is not None
                and metrics.imbalance <= self.short_params["imbalance_threshold"]):
            sell_bits.append(f"盘口买卖失衡 {metrics.imbalance:+.2f}")
        if (metrics.sell_wall_ratio is not None
                and metrics.sell_wall_ratio >= self.short_params["wall_ratio_threshold"]):
            sell_bits.append(f"压力位附近 {metrics.sell_wall_ratio:.1f} 倍卖墙")
        pressure_text = ("盘口卖压信号：" + "、".join(sell_bits)) if sell_bits else "盘口暂无明显卖压确认"
        if fake_break:
            happened = (
                f"价格此前收盘站上失效位 {fmt_num(break_level)}，现已回落至该位下方"
                f"（假突破回落，破位高点 {fmt_num(float(anchor['peak']))}），"
                f"{pressure_text}（观察位，非入场指令）"
            )
        else:
            happened = (f"价格逼近日内压力位 {fmt_num(zone)}{rejection}，"
                        f"{pressure_text}（观察位，非入场指令）")
        if fake_break:
            action = (
                f"{self.safety.enforce_action('')} —— "
                f"看能否持续压回并跌破观察区 {fmt_num(zone)}；若价格重新站上破位高点 "
                f"{fmt_num(float(anchor['peak']))}，本次回落观察条件作废"
            )
        else:
            action = (
                f"{self.safety.enforce_action('')} —— "
                f"到观察区先看能否被压回；若价格站上失效位 {fmt_num(invalidation)}，"
                "本次观察条件作废"
            )
        return Alert(
            symbol=f"{name}（{inst_id}）",
            short_name=short_symbol(inst_id, name),
            title_hint=title_hint,
            kind_label=kind_label,
            headline=headline,
            what_happened=happened,
            time_text=(
                f"{ticker.fetched_at_utc.astimezone(BEIJING):%Y-%m-%d %H:%M:%S} 北京时间"
                f" / {ticker.fetched_at_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                f"（来源 OKX ticker + 盘口）"
            ),
            price=ticker.last,
            support=None,
            resistance=zone,
            action_text=action,
            ai_text=safe_text,
            rule_text=not ai_used,
            signal_type=SIGNAL_SHORT,
            extra_lines=extra_lines,
            disclaimer=self.safety.disclaimer,
        )

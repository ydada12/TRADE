"""重点时段盘中波动轨：在配置的重点时段内监测大幅波动并按分档推送。

规则与文案原样搬自 run.py。以 mixin 挂在 Monitor 上，调用点（main / check）零改动。
`current_window` 属编排层的小助手，留在 run.py，本模块通过 self 调用它。
改阈值看 config.yaml 的 session_watch 段，改指纹看 ../fingerprint.py。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.alert import (
    Alert,
    compute_position_pnl,
    now_utc8_str,
    render_position_line,
    short_symbol,
    zone_hint,
)
from src.datasource.econ_calendar import BEIJING
from src.llm.prompts import SYSTEM_PROMPT, build_session_user_message
from src.logutil import get_logger
from src.modules.session import (
    decide_alert,
    detect_moves,
    record_for_window,
    render_fallback_text as render_session_fallback,
)

from ..fingerprint import session_fp
from ..helpers import gap_pct as _gap_pct

log = get_logger("run")


class SessionTrack:
    """重点时段盘中波动轨 mixin。"""

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
        for symbol in self._active_symbols():
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
            window_start_ts: int | None = None
            if window is not None:
                since_open_ms = int(window.start_moment(now_bj).timestamp() * 1000)
                window_start_ts = since_open_ms // 1000

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

            levels_dict, _, _, _ = self._symbol_levels(symbol)

            # ---- 防刷屏：幅度分档 + 升级即报 + 冷却（逻辑在 session.decide_alert，可测）----
            biggest = max(abs(h["change_pct"]) for h in hits)
            state_key = f"{inst_id}|{window_label}"
            # 记录只在同一次时段开启内有效，跨天/换时段自动清零（见 record_for_window）
            record = record_for_window(self.state.get_session(state_key), window_start_ts)
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
            body.inst_id = inst_id
            body.ts_ms = ticker.ts_ms
            fp = session_fp(inst_id, window_label, decision.bucket)
            log.info(
                "盘中监控：%s 时段[%s] 触发告警（%s），最大幅度 %.2f%%，档位 %s",
                inst_id, window_label, decision.reason, biggest, decision.bucket,
            )
            if self.push(body.title(), body.render(), fp, force=True, alert=body):
                self.state.set_session(state_key, {
                    "last_ts": time.time(),
                    "last_bucket": max(decision.bucket, int(record.get("last_bucket", -1))),
                    "last_pct": round(biggest, 2),
                    "last_at": now_utc8_str(),
                    "window": window_label,
                    "window_start_ts": window_start_ts,
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
        pnl = compute_position_pnl(self._effective_position(symbol), ticker.last)
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
            "距支撑位%": _gap_pct((levels_dict or {}).get("nearest_support"), ticker.last),
            "距压力位%": _gap_pct((levels_dict or {}).get("nearest_resistance"), ticker.last),
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
        ai_used = source == "deepseek"
        if not ai_used:
            ai_text = f"{fallback}（说明：本条解读由本地规则模板生成）"
        safe_text, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        if blocked:
            ai_used = False

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
            rule_text=not ai_used,
            signal_type="盘中波动",
            extra_lines=extra_lines,
            disclaimer=self.safety.disclaimer,
        )

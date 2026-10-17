"""事件轨：公布前三分支剧本 + 公布后结果（偏多/偏空由真实数据推导）。

规则与文案原样搬自 run.py。以 mixin 挂在 Monitor 上，调用点（run_events /
main / check）零改动。改触发窗口看 config.yaml 的 event 段。

注：`_target_symbols` / `_primary_symbol_for_event` / `_symbol_levels` 是事件轨
挑选关联品种、取关键位的直接助手（其中两个也被 world/short 轨复用），一并随
事件轨搬入本模块；它们作为 Monitor 方法仍可被其它轨通过 self 调用。
"""

from __future__ import annotations

from datetime import datetime

from src.alert import EventAlert, fmt_num, short_symbol, zone_hint
from src.datasource.econ_calendar import EconEvent
from src.llm.prompts import (
    SYSTEM_PROMPT,
    build_event_post_user_message,
    build_event_pre_user_message,
)
from src.logutil import get_logger
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
from src.modules.levels import derive_levels

from ..fingerprint import event_post_fp, event_pre_fp
from ..gate import PUSH_SENT, PUSH_SKIPPED
from ..helpers import gap_pct as _gap_pct

log = get_logger("run")


class EventTrack:
    """事件轨 mixin。"""

    def _target_symbols(self, watch: list[str]) -> list[dict]:
        symbols = self._active_symbols()
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

    def _symbol_levels(self, symbol: dict) -> tuple[dict | None, float | None, str, int | None]:
        """取该品种的真实行情 + 支撑压力位（周K来源与趋势轨保持一致）。

        返回 (关键位字典, 最新价, 数据时间文本, 数据时间 epoch ms)。
        最后一个值是给面板标注 K 线用的，行情拿不到时为 None。
        """
        inst_id = symbol["inst_id"]
        try:
            ticker = self.okx.get_ticker(inst_id)
            candles, _ = self._weekly_candles(symbol)
        except Exception as exc:
            log.error("事件轨：%s 行情获取失败：%s", inst_id, exc)
            return None, None, "", None
        confirmed = [c for c in candles if c.confirmed]
        if not confirmed:
            return None, None, "", None
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
        return (levels.as_dict(), ticker.last,
                ticker.fetched_at_utc.strftime("%Y-%m-%d %H:%M:%S"), ticker.ts_ms)

    def _emit_pre_script(self, event: EconEvent, now: datetime, watch: list[str],
                         fp: str, force: bool) -> str:
        """生成并推送公布前剧本，返回 PUSH_* 状态。"""
        primary = self._primary_symbol_for_event(event, watch)
        if primary is None:
            log.warning("事件 %s 没有可关联的品种，跳过", event.name)
            return PUSH_SKIPPED
        levels_dict, last_price, ts, ts_ms = self._symbol_levels(primary)
        facts = build_branch_facts(event, primary, levels_dict)
        facts["当前价"] = last_price
        facts["当前价时间"] = ts
        facts["距支撑位%"] = _gap_pct((levels_dict or {}).get("nearest_support"), last_price)
        facts["距压力位%"] = _gap_pct((levels_dict or {}).get("nearest_resistance"), last_price)

        fallback = render_fallback_script(facts, primary["inst_id"], region=event.region)
        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_event_pre_user_message(facts), facts, fallback
        )
        ai_used = source == "deepseek"
        if not ai_used:
            ai_text = fallback
        body, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        if blocked:
            ai_used = False

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
            inst_id=primary["inst_id"],
            ts_ms=ts_ms,
            rule_text=not ai_used,
        )
        log.info("事件预警：%s 公布时间 %s", event.name, publish_time_text(event, now))
        return self.push_status(alert.title(), alert.render(), event_pre_fp(fp), force=force,
                                alert=alert)

    def _emit_post_result(self, event: EconEvent, now: datetime, watch: list[str],
                          fp: str, force: bool) -> str:
        """生成并推送公布后结果，返回 PUSH_* 状态。

        硬规矩：每条结果必须带明确的偏多/偏空，且方向只能由真实数据推导。
        C 分支先用前值再归并一次；连前值都没有、或事件类别认不出来 -> 不推送。
        """
        branch, reason = determine_branch(event.actual, event.forecast, event.previous)
        # 数据源没给预期值、只能与前值比较时，reason 里会带「预期值数据源未提供」。
        # 这种占位不刷屏：直接改写成「高于/低于前值」。
        if event.forecast is None and branch in ("A", "B") and event.previous is not None:
            word = "高于" if branch == "A" else "低于"
            reason = f"实际值 {event.actual_text()} {word}前值 {event.previous_text()}"
        primary = self._primary_symbol_for_event(event, watch)
        if primary is None:
            log.warning("事件 %s 没有可关联的品种，跳过", event.name)
            return PUSH_SKIPPED

        inst_id = primary["inst_id"]
        dir_branch = branch
        if branch == "C":
            # 与预期持平时，再拿真实前值归并一次；归并不了就没有数据依据，不推
            dir_branch = branch_from_previous(event)
            if dir_branch is None:
                log.info("事件 %s 与预期、前值均持平（或缺前值），无数据依据给方向，不推送",
                         event.name)
                return PUSH_SKIPPED
        verdict = verdict_for_event(event.name, inst_id, dir_branch, event.region)
        if verdict is None:
            log.info("事件 %s 无法依据真实数据得出偏多/偏空方向，不推送", event.name)
            return PUSH_SKIPPED

        levels_dict, last_price, ts, ts_ms = self._symbol_levels(primary)
        facts = build_branch_facts(event, primary, levels_dict)
        facts["当前价"] = last_price
        facts["当前价时间"] = ts
        facts["距支撑位%"] = _gap_pct((levels_dict or {}).get("nearest_support"), last_price)
        facts["距压力位%"] = _gap_pct((levels_dict or {}).get("nearest_resistance"), last_price)
        facts["判定分支"] = branch
        facts["判定依据"] = reason
        facts["方向使用分支"] = dir_branch
        # 方向结论由确定性逻辑算好，大模型只能复述、不许翻转
        facts["确定性方向结论"] = verdict["verdict_text"]
        facts["方向传导链"] = verdict["chain"]

        fallback = render_post_fallback(branch, reason, event, levels_dict,
                                        last_price, verdict=verdict)
        ai_text, source = self.llm.translate(
            SYSTEM_PROMPT, build_event_post_user_message(facts), facts, fallback
        )
        ai_used = source == "deepseek"
        if not ai_used:
            ai_text = fallback
        body, blocked = self.safety.sanitize_ai_text(ai_text, fallback)
        if blocked:
            ai_used = False
        # 双保险：大模型文案里若把方向写反，整条丢弃用兜底
        opposite = "偏空" if verdict["verdict_text"] == "偏多" else "偏多"
        if opposite in body and verdict["verdict_text"] not in body:
            log.warning("事件 %s 的 AI 文案方向与确定性结论相反，已替换兜底", event.name)
            body = fallback
            ai_used = False

        reference = "前值" if event.forecast is None else "预期"
        branch_short = {
            "A": f"强于{reference}", "B": f"弱于{reference}", "C": "符合预期",
        }.get(branch, "")
        # 数据源没给的字段直接不展示，不拿「数据源未提供」占位
        number_parts = [f"【实际值】{event.actual_text()}"]
        if event.forecast is not None:
            number_parts.append(f"【预期值】{fmt_num(event.forecast)}")
        if event.previous is not None:
            number_parts.append(f"【前值】{fmt_num(event.previous)}")

        alert = EventAlert(
            headline=event.name,
            stage="post",
            happened=reason,
            time_text=(
                f"{event.date} {event.time} 北京时间公布"
                f"　数据时间 {ts} UTC（来源 AKShare + OKX）"
            ),
            body=body,
            symbol=f"{primary.get('name')}（{inst_id}）",
            short_name=short_symbol(inst_id, primary.get("name", "")),
            title_hint=f"{verdict['verdict_text']}｜{branch_short}".rstrip("｜"),
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
            extra_lines=["　".join(number_parts)],
            verdict=verdict["verdict_text"],
            verdict_chain=verdict["chain"],
            inst_id=inst_id,
            ts_ms=ts_ms,
            rule_text=not ai_used,
        )
        log.info("事件结果：%s 判定分支 %s 方向 %s", event.name, branch,
                 verdict["verdict_text"])
        status = self.push_status(
            alert.title(), alert.render(), event_post_fp(fp, inst_id), force=force,
            alert=alert,
        )
        if status == PUSH_SENT:
            self._note_direction(inst_id, verdict["verdict_text"], "事件结果")
        return status

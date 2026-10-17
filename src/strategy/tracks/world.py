"""国际大事轨：GDELT/RSS 新闻候选 -> DeepSeek 研判（失败走确定性兜底）-> 推送。

规则与文案原样搬自 run.py。以 mixin 挂在 Monitor 上，调用点（main / check）零改动。
取数与缓存（`_world_items`）留在 run.py（编排层的「取数 + 缓存」职责），本模块只放
「规则与文案」。改阈值看 config.yaml 的 world_events 段，改指纹看 ../fingerprint.py。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.alert import SIGNAL_WORLD, WorldAlert, short_symbol
from src.datasource.econ_calendar import BEIJING
from src.datasource.world_news import normalize_url
from src.llm.prompts import WORLD_SYSTEM_PROMPT, build_world_judge_user_message
from src.logutil import get_logger
from src.modules import world as world_mod

from ..fingerprint import retry_fp, world_fp
from ..gate import PUSH_SENT

log = get_logger("run")


class WorldTrack:
    """国际大事轨 mixin。"""

    def _world_actions(self, item: world_mod.ScoredItem,
                       decision: dict[str, Any] | None,
                       symbols: list[dict]) -> list[dict[str, Any]]:
        """把一条候选新闻展开成「按品种的推送动作」。无动作返回空列表（=不推）。"""
        actions: list[dict[str, Any]] = []
        use_llm = bool(decision and decision.get("major"))
        if use_llm:
            category = str(decision.get("category") or item.category)
            happened = str(decision.get("summary_zh") or item.title)
            chain = str(decision.get("chain") or "")
            thinking = str(decision.get("thinking") or "")
            bias_map = decision.get("bias") or {}
        else:
            category = item.category
            happened = item.title  # 兜底不翻译：原样引用真实标题，避免翻译出错
            chain = world_mod.fallback_chain(category)
            thinking = ""
            bias_map = None

        for symbol in symbols:
            inst_id = symbol["inst_id"]
            if use_llm:
                bias = bias_map.get(inst_id)
                if bias not in ("偏多", "偏空"):
                    continue  # 中性 / 模型没给 -> 该品种不推
            else:
                bias = world_mod.fallback_bias(category, inst_id)
                if bias is None:
                    continue
            body = (
                f"{chain}。据此给出【{bias}】的历史方向参考。"
                "历史规律不代表必然结果，请人工查看事件进展与价格确认。"
            ) if chain else (
                f"据真实报道整理的{world_mod.CATEGORY_LABELS.get(category, '国际')}题材事件，"
                f"给出【{bias}】的历史方向参考，请人工查看事件进展与价格确认。"
            )
            actions.append({
                "symbol": symbol, "inst_id": inst_id, "bias": bias,
                "category": category, "happened": happened, "chain": chain,
                "body": body, "llm": use_llm, "thinking": thinking,
            })
        return actions

    def run_world_events(self, force: bool = False) -> list[str]:
        """抓取 -> 关键词初筛 -> DeepSeek 研判（失败走确定性兜底）-> 推送。

        每条推送必须带明确偏多/偏空；给不出方向的题材（选举、无法识别）一律不推。
        """
        if not self.world_enabled and not force:
            return []
        now_ts = time.time()
        if not force and self._world_last_judge and \
                now_ts - self._world_last_judge < self.world_interval:
            return []
        self._world_last_judge = now_ts

        try:
            items = self._world_items(force=force)
        except Exception as exc:
            log.error("国际大事取数失败（本轮跳过）：%s", exc)
            return []

        symbols = self._target_symbols(self.world_watch)
        if not symbols:
            return []
        symbol_ids = {s["inst_id"] for s in symbols}

        candidates = world_mod.filter_candidates(
            items, seen_urls=set(), groups=self.world_keyword_groups,
            min_score=self.world_min_score,
            require_title_hit=self.world_require_title_hit,
            limit=self.world_judge_batch,
        )
        if not candidates:
            return []

        verdicts: dict[str, dict[str, Any]] = {}
        if self.llm.available:
            try:
                raw = self.llm.chat(
                    WORLD_SYSTEM_PROMPT,
                    build_world_judge_user_message(
                        [c.to_llm_dict() for c in candidates], symbols),
                    max_tokens=self.world_judge_max_tokens,
                )
                verdicts = world_mod.parse_world_verdicts(raw, candidates, symbol_ids)
            except Exception as exc:
                log.warning("国际大事研判调用失败，本轮全部走确定性兜底：%s", exc)

        out: list[str] = []
        pushed_this_run = 0
        today = datetime.now(BEIJING).strftime("%Y-%m-%d")
        for item in candidates:
            if pushed_this_run >= self.world_per_run:
                log.info("国际大事本轮已达上限 %d 条，剩余下轮再处理", self.world_per_run)
                break
            if self.state.world_pushed_today(today) >= self.world_per_day:
                log.info("国际大事今日已达上限 %d 条", self.world_per_day)
                break
            url_key = normalize_url(item.url)
            if self.state.is_world_evaluated(url_key):
                continue

            decision = verdicts.get(item.url)
            if decision is not None and not decision.get("major"):
                if not self.dry_run:
                    self.state.mark_world_evaluated(url_key)
                    self.state.save()
                continue

            actions = self._world_actions(item, decision, symbols)
            if not actions:
                # 模型判中性、或兜底题材给不出任何品种方向：评估过，不再重复研判
                if not self.dry_run:
                    self.state.mark_world_evaluated(url_key)
                    self.state.save()
                continue

            # 假设所有动作都能收尾；遇到「发送失败需下轮重试」或上限中断再置 False。
            # 最终是否标记 evaluated 还要过下面 all(...) 一关，防止把没推的品种吞掉。
            closed = True
            for act in actions:
                if pushed_this_run >= self.world_per_run:
                    closed = False
                    break
                if self.state.world_pushed_today(today) >= self.world_per_day:
                    closed = False
                    break
                inst_id = act["inst_id"]
                if self.state.world_symbol_done(url_key, inst_id):
                    continue

                # 大模型文案过禁用词红线；命中就整条退回确定性兜底文案
                happened, chain, body = act["happened"], act["chain"], act["body"]
                thinking = act.get("thinking") or ""
                _, blocked = self.safety.sanitize_ai_text(
                    f"{happened}\n{chain}\n{body}\n{thinking}",
                    world_mod.fallback_body(item, inst_id, act["bias"]),
                )
                if blocked:
                    happened = item.title
                    chain = world_mod.fallback_chain(act["category"])
                    body = world_mod.fallback_body(item, inst_id, act["bias"])
                    thinking = ""
                    act["llm"] = False

                # 同品种近期若有做空观察位（日内压力位口径），显式提示两条轨口径不同
                note = self._cross_track_note(inst_id, SIGNAL_WORLD, act["bias"])
                if note:
                    body = f"{body}\n\n{note}"

                price = None
                ts_ms = None
                try:
                    ticker = self.okx.get_ticker(inst_id)
                    price, ts_ms = ticker.last, ticker.ts_ms
                except Exception as exc:
                    log.warning("国际大事：%s 现价取不到（仍推送，不做K线标注）：%s",
                                inst_id, exc)

                symbol = act["symbol"]
                alert = WorldAlert(
                    happened=happened,
                    category=act["category"],
                    verdict=act["bias"],
                    verdict_chain=chain or "依据真实报道与历史一般规律研判",
                    body=body,
                    thinking=thinking,
                    source=item.source,
                    url=item.url,
                    published_text=item.published_text,
                    symbol=f"{symbol.get('name')}（{inst_id}）",
                    short_name=short_symbol(inst_id, symbol.get("name", "")),
                    price=price,
                    action_text=self.safety.enforce_action(""),
                    disclaimer=self.safety.disclaimer,
                    inst_id=inst_id,
                    ts_ms=ts_ms,
                    rule_text=not act["llm"],
                )
                fp = world_fp(url_key, inst_id)
                status = self.push_status(alert.title(), alert.render(), fp,
                                          force=force, alert=alert)
                settled = self._delivery_settled(retry_fp(fp), status, force)
                if status == PUSH_SENT:
                    if not self.dry_run:
                        self.state.mark_world_symbol_sent(url_key, inst_id)
                        self.state.bump_world_pushed(today)
                        self.state.save()
                    self._note_direction(inst_id, act["bias"], SIGNAL_WORLD)
                    pushed_this_run += 1
                    out.append(happened[:30])
                elif settled:
                    # 冷却/长期失败放弃：关闭该品种，避免无限重试
                    if not self.dry_run:
                        self.state.mark_world_symbol_sent(url_key, inst_id)
                        self.state.save()
                    closed = True
                else:
                    # 发送失败：URL 不标记，下一轮重判重发
                    closed = False
                    break
            else:
                closed = True

            if closed and not self.dry_run and all(
                self.state.world_symbol_done(url_key, a["inst_id"]) for a in actions
            ):
                self.state.mark_world_evaluated(url_key)
                self.state.save()
        return out

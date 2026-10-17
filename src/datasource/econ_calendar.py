"""经济日历数据源（真实 API）。

主源：AKShare 的百度经济日历（免费、无需 key）
  实际返回的列：日期 / 时间 / 地区 / 事件 / 公布 / 预期 / 前值 / 重要性
  —— 「公布」（实际值）在数据公布后才会填上，「预期」是市场一致预期。
  个别事件源里没给预期值（如部分能源/分项 CPI），系统如实报告「数据源未提供」。

红线要求：公布时间、预期值、实际值【必须】来自这里或其它真实 API。
数据源没给的字段，系统就如实报告「数据源未提供」，绝不由大模型补一个数字。

可选补充源：Polymarket 预测市场概率（只作为「市场怎么看」的旁证，不替代预期值）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..http_util import HttpClient

log = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")

# akshare 返回的中文列名（可能出现轻微差异，做兼容映射）
_COLUMN_ALIASES = {
    "日期": "date",
    "时间": "time",
    "地区": "region",
    "事件": "name",
    # 实际值：akshare 的百度日历实际返回的列名是「公布」（文档与旧版写作「今值」）。
    # 曾因为只映射「今值」，导致公布后的实际值永远取不到、对号入座提醒从未触发。
    "公布": "actual",
    "今值": "actual",
    "公布值": "actual",
    "预期": "forecast",
    "预测": "forecast",
    "预期值": "forecast",
    "前值": "previous",
    "重要性": "importance",
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if text in {"", "-", "--", "None", "nan", "NaN"}:
            return None
        # 处理 "18.5万"、"3.2%" 这类带单位/符号的写法
        text = text.replace(",", "").replace("%", "")
        multiplier = 1.0
        if text.endswith("万"):
            multiplier, text = 10000.0, text[:-1]
        elif text.endswith("亿"):
            multiplier, text = 100000000.0, text[:-1]
        return float(text) * multiplier
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


@dataclass
class EconEvent:
    date: str                    # YYYY-MM-DD
    time: str                    # HH:MM（北京时间）
    region: str
    name: str
    actual: float | None = None
    forecast: float | None = None
    previous: float | None = None
    importance: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def publish_dt(self) -> datetime | None:
        """公布时间（北京时间）。"""
        if not self.time:
            return None
        try:
            hh, _, mm = self.time.partition(":")
            return datetime(
                int(self.date[0:4]), int(self.date[5:7]), int(self.date[8:10]),
                int(hh), int(mm or 0), tzinfo=BEIJING,
            )
        except (ValueError, IndexError):
            return None

    @property
    def fingerprint(self) -> str:
        return f"{self.date}|{self.time}|{self.region}|{self.name}"

    @property
    def released(self) -> bool:
        return self.actual is not None

    def forecast_text(self) -> str:
        if self.forecast is None:
            return "数据源未提供预期值"
        return f"{self.forecast:g}"

    def actual_text(self) -> str:
        if self.actual is None:
            return "数据源未提供实际值"
        return f"{self.actual:g}"

    def previous_text(self) -> str:
        if self.previous is None:
            return "数据源未提供前值"
        return f"{self.previous:g}"

    def importance_text(self) -> str:
        return {3: "高", 2: "中", 1: "低"}.get(self.importance or 0, "未知")


class EconCalendarClient:
    """经济日历客户端。

    akshare 是重依赖，因此延迟导入：即使没装 akshare，
    趋势轨依然能正常跑。
    """

    def __init__(self, days_ahead: int = 7, min_importance: int = 2,
                 keywords: list[str] | None = None,
                 regions: list[str] | None = None,
                 lookback_days: int = 1,
                 day_retries: int = 2,
                 day_interval_seconds: float = 0.4) -> None:
        self.days_ahead = days_ahead
        self.min_importance = min_importance
        self.keywords = [k for k in (keywords or []) if k]
        self.regions = [r for r in (regions or []) if r]
        # 要往前回看几天：数据源的「公布」（实际值）列不是公布瞬间就填上的，
        # 实测常滞后几十分钟到几小时；晚间的美国数据（20:30/22:30 公布）经常要
        # 等跨过北京时间零点才填上，若只从「今天」开始抓，隔夜的实际值就永远取不到。
        self.lookback_days = max(0, int(lookback_days))
        # 百度经济日历会限流（实测出现过 HTTP 403），所以按天抓取 + 重试 + 间隔
        self.day_retries = max(1, int(day_retries))
        self.day_interval_seconds = max(0.0, float(day_interval_seconds))

    # ---------- 抓取 ----------
    def fetch_range(self, start: date_cls | None = None,
                    days: int | None = None) -> list[EconEvent]:
        """抓取 [start, start+days) 的经济日历。单个日期失败不影响其它日期。"""
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 akshare，无法读取经济日历。请执行：python -m pip install akshare"
            ) from exc

        start = start or (datetime.now(BEIJING).date() - timedelta(days=self.lookback_days))
        days = (self.days_ahead + self.lookback_days) if days is None else days
        events: list[EconEvent] = []
        failed_days: list[str] = []

        for index, offset in enumerate(range(days)):
            day = start + timedelta(days=offset)
            if index > 0 and self.day_interval_seconds:
                time.sleep(self.day_interval_seconds)

            df = None
            for attempt in range(1, self.day_retries + 1):
                try:
                    df = ak.news_economic_baidu(date=day.strftime("%Y%m%d"))
                    break
                except Exception as exc:  # akshare 内部异常类型不可控
                    if attempt < self.day_retries:
                        time.sleep(0.8 * attempt)
                    else:
                        log.warning(
                            "经济日历 %s 拉取失败（已重试 %s 次，跳过该日）：%s",
                            day, self.day_retries, exc,
                        )
                        failed_days.append(day.strftime("%Y-%m-%d"))

            if df is None or getattr(df, "empty", True):
                continue
            for record in df.to_dict("records"):
                event = self._build_event(record)
                if event:
                    events.append(event)

        if failed_days:
            log.warning(
                "本轮有 %s 天经济日历未取到（下轮会自动重试）：%s",
                len(failed_days), "、".join(failed_days),
            )

        events.sort(key=lambda e: (e.date, e.time))
        return events

    @staticmethod
    def _build_event(record: dict[str, Any]) -> EconEvent | None:
        mapped: dict[str, Any] = {}
        for key, value in record.items():
            target = _COLUMN_ALIASES.get(str(key).strip())
            if target:
                mapped[target] = value
        if not mapped.get("name"):
            return None
        return EconEvent(
            date=str(mapped.get("date") or "").strip()[:10],
            time=str(mapped.get("time") or "").strip()[:5],
            region=str(mapped.get("region") or "").strip(),
            name=str(mapped.get("name") or "").strip(),
            actual=_to_float(mapped.get("actual")),
            forecast=_to_float(mapped.get("forecast")),
            previous=_to_float(mapped.get("previous")),
            importance=_to_int(mapped.get("importance")),
            raw={str(k): str(v) for k, v in mapped.items()},
        )

    # ---------- 过滤 ----------
    def is_watched(self, event: EconEvent) -> bool:
        if not self.keywords:
            return True
        blob = f"{event.name}"
        return any(keyword in blob for keyword in self.keywords)

    def region_ok(self, event: EconEvent) -> bool:
        if not self.regions:
            return True
        return any(region in event.region for region in self.regions)

    def important_events(self, start: date_cls | None = None,
                         days: int | None = None) -> list[EconEvent]:
        """重要性达标 且 地区符合 且 命中关键词的事件。"""
        result = []
        for event in self.fetch_range(start=start, days=days):
            if (event.importance or 0) < self.min_importance:
                continue
            if not self.region_ok(event):
                continue
            if not self.is_watched(event):
                continue
            result.append(event)
        return result

    def find_event(self, fingerprint: str, start: date_cls | None = None,
                   days: int | None = None) -> EconEvent | None:
        """按指纹回查事件（用于公布后拿实际值）。"""
        for event in self.fetch_range(start=start, days=days):
            if event.fingerprint == fingerprint:
                return event
        return None


class PolymarketClient:
    """Polymarket 预测市场（可选，默认关闭）。

    用途：给大模型一个「市场当前怎么定价这件事」的旁证。
    它不是经济数据本身，因此绝不能拿来替代「市场一致预期值」。
    """

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com",
                 timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = HttpClient(timeout=timeout, max_retries=2)

    def search_markets(self, keywords: list[str], limit: int = 200) -> list[dict[str, Any]]:
        try:
            payload = self.http.get_json(
                f"{self.base_url}/markets",
                {"limit": limit, "active": "true", "closed": "false"},
            )
        except Exception as exc:
            log.warning("Polymarket 拉取失败（忽略）：%s", exc)
            return []
        if not isinstance(payload, list):
            return []

        matched: list[dict[str, Any]] = []
        lowered = [k.lower() for k in keywords if k]
        for market in payload:
            question = str(market.get("question") or "")
            if not lowered or any(k in question.lower() for k in lowered):
                matched.append({
                    "question": question,
                    "outcomePrices": market.get("outcomePrices"),
                    "volume": market.get("volume"),
                    "endDate": market.get("endDate"),
                })
        return matched

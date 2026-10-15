"""告警历史落盘（append-only JSONL）—— 面板在 K 线上标注告警位置的唯一数据来源。

为什么不用 state.json：`state.json` 只记「时间戳 + 布尔」（冷却与去重），
没有价格、没有类型、没有支撑压力位，因此**无法**在 K 线上还原「当时标在哪」。

为什么用 JSONL 而不是 SQLite：可 tail、可 diff、可手改；`state/` 已在
`.gitignore` 里被忽略，落盘内容天然不进公开仓库。文件损坏时最多丢一行，不会整库打不开。

落盘时机（由 run.py 控制）：**只在推送真的成功之后**写。冷却中跳过的不写
（早写过），推送失败的不写（用户根本没收到）。

红线：本模块只读写本地文件，不联网、不发推送、不做任何交易动作。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..alert import SIGNAL_EVENT

log = logging.getLogger(__name__)

UTC8 = timezone(timedelta(hours=8))

DEFAULT_PATH = "state/alerts.jsonl"
KEEP_DAYS = 120
MAX_LINES = 20000

SOURCE_LIVE = "live"
SOURCE_BACKFILL = "log_backfill"

# ------------------------------------------------------------
# K 线对齐（口径全部用真实 OKX 样本核对过，不是拍脑袋）
# ------------------------------------------------------------
MINUTE_MS = 60 * 1000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS

# OKX 日K 的开盘时间是「UTC+8 的 0 点」，不是 UTC 0 点（实测 2026-10-15 的日K
# ts=1791993600000 → 2026-10-14 16:00 UTC → 北京时间 2026-10-15 00:00）。
_DAY_ANCHOR_MS = 16 * HOUR_MS
# 周K 的开盘时间是「北京时间周一 0 点」= UTC 周日 16 点（实测 ts=1791734400000
# → 2026-10-11 16:00 UTC → 北京时间 2026-10-12 00:00 周一）。
_WEEK_ANCHOR_MS = 316800000  # 1970-01-04 16:00 UTC，即第一个北京时间周一 0 点

_BAR_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def bar_ms(bar: str) -> int | None:
    """把 OKX 的 bar 字符串（5m / 1H / 4H / 1D / 1W）换算成毫秒。无法识别返回 None。"""
    text = (bar or "").strip().lower()
    if not text:
        return None
    unit = text[-1]
    minutes = _BAR_MINUTES.get(unit)
    if minutes is None:
        return None
    try:
        value = int(float(text[:-1]))
    except ValueError:
        return None
    if value <= 0:
        return None
    return value * minutes * MINUTE_MS


def bar_start_ms(ts_ms: int | None, bar: str) -> int | None:
    """把某个时间点 floor 到它所在 K 线的起始毫秒（UTC epoch ms）。

    周K 单独走锚点算法：直接按 7 天取模会落到「UTC 周一」，与 OKX 实际口径
    （北京时间周一）差 8 小时，标到图上就是错的一根。
    """
    if ts_ms is None:
        return None
    try:
        value = int(ts_ms)
    except (TypeError, ValueError):
        return None

    duration = bar_ms(bar)
    if duration is None or duration <= 0:
        return value  # 认不出的周期就不对齐，宁可标原时间也不标错位置
    if duration >= WEEK_MS:
        return (value - _WEEK_ANCHOR_MS) // WEEK_MS * WEEK_MS + _WEEK_ANCHOR_MS
    if duration >= DAY_MS:
        return (value - _DAY_ANCHOR_MS) // DAY_MS * DAY_MS + _DAY_ANCHOR_MS
    return (value // duration) * duration


def alert_index_for_ts(candle_ts: Iterable[int], ts_ms: int | None) -> int:
    """二分找出「告警落在哪一根 K 线」的下标；早于全部 K 线则返回 -1。

    用「最后一根起始时间 <= 告警时间」的口径，比要求两者完全相等更稳：
    告警时间取的是成交时刻，未必正好压在 bar 边界上。
    """
    if ts_ms is None:
        return -1
    starts = list(candle_ts)
    if not starts:
        return -1
    try:
        value = int(ts_ms)
    except (TypeError, ValueError):
        return -1
    index = bisect_right(starts, value) - 1
    return index if index >= 0 else -1


def ms_to_bj(value: int | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """epoch ms → 北京时间字符串（本机时区是 Etc/GMT-8，必须显式 +8 才能对上北京时间）。"""
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC8).strftime(fmt)
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


# ------------------------------------------------------------
# 记录
# ------------------------------------------------------------
@dataclass(frozen=True)
class AlertRecord:
    """一条已经送达用户的告警。字段设计目标：足够在 K 线上还原一个「点」。"""

    ts_ms: int | None = None          # 数据时间（K 线对齐的唯一依据）
    recorded_at: str = ""             # 落盘时间（北京时间）
    inst_id: str = ""                 # OKX instId / 品种标识
    symbol: str = ""                  # 完整品种名
    short_name: str = ""              # 标题短名
    signal_type: str = ""             # 趋势 / 事件 / 做空观察位 / 盘中波动
    title: str = ""                   # 推送标题（原文）
    what_happened: str = ""
    price: float | None = None
    support: float | None = None
    resistance: float | None = None
    fingerprint: str = ""
    pushed_at: str = ""               # 推送时间（北京时间）
    source: str = SOURCE_LIVE

    def to_line(self) -> str:
        """序列化成一行 JSON（中文不转义，方便直接肉眼 tail）。"""
        payload = {
            "ts_ms": self.ts_ms,
            "recorded_at": self.recorded_at,
            "inst_id": self.inst_id,
            "symbol": self.symbol,
            "short_name": self.short_name,
            "signal_type": self.signal_type,
            "title": self.title,
            "what_happened": self.what_happened,
            "price": self.price,
            "support": self.support,
            "resistance": self.resistance,
            "fingerprint": self.fingerprint,
            "pushed_at": self.pushed_at,
            "source": self.source,
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def from_line(line: str) -> "AlertRecord | None":
        """解析一行；坏行/空行返回 None，绝不抛异常（历史文件允许被手改坏）。"""
        text = (line or "").strip().lstrip("\ufeff")
        if not text:
            return None
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(raw, dict):
            return None
        return AlertRecord(
            ts_ms=_opt_int(raw.get("ts_ms")),
            recorded_at=_text(raw.get("recorded_at")),
            inst_id=_text(raw.get("inst_id")),
            symbol=_text(raw.get("symbol")),
            short_name=_text(raw.get("short_name")),
            signal_type=_text(raw.get("signal_type")),
            title=_text(raw.get("title")),
            what_happened=_text(raw.get("what_happened")),
            price=_opt_float(raw.get("price")),
            support=_opt_float(raw.get("support")),
            resistance=_opt_float(raw.get("resistance")),
            fingerprint=_text(raw.get("fingerprint")),
            pushed_at=_text(raw.get("pushed_at")),
            source=_text(raw.get("source")) or SOURCE_LIVE,
        )


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _opt_float(value: Any) -> float | None:
    """None / "" / 非数字 → None（缺失就是缺失，绝不当成 0）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def record_from_alert(alert: Any, title: str, fingerprint: str,
                      source: str = SOURCE_LIVE) -> AlertRecord:
    """把 Alert / EventAlert 映射成落盘记录（纯函数，好测）。

    两者字段名不完全一样：EventAlert 没有 signal_type，也没有 what_happened 以外
    的时间字段，所以这里全部用 getattr 兜底，缺什么就给空值。
    """
    signal_type = _text(getattr(alert, "signal_type", "")) or SIGNAL_EVENT
    return AlertRecord(
        ts_ms=_opt_int(getattr(alert, "ts_ms", None)),
        recorded_at=_text(getattr(alert, "recorded_at", "")) or _now_bj(),
        inst_id=_text(getattr(alert, "inst_id", "")),
        symbol=_text(getattr(alert, "symbol", "")),
        short_name=_text(getattr(alert, "short_name", "")),
        signal_type=signal_type,
        title=_text(title),
        what_happened=_text(getattr(alert, "what_happened", "")) or _text(getattr(alert, "happened", "")),
        price=_opt_float(getattr(alert, "price", None)),
        support=_opt_float(getattr(alert, "support", None)),
        resistance=_opt_float(getattr(alert, "resistance", None)),
        fingerprint=_text(fingerprint),
        pushed_at=_text(getattr(alert, "pushed_at", "")) or _now_bj(),
        source=source or SOURCE_LIVE,
    )


def _now_bj() -> str:
    return datetime.now(UTC8).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# 读写
# ------------------------------------------------------------
class AlertHistory:
    """追加式告警历史文件。

    写用线程锁 + 单行 flush；**内部吃掉所有异常**——历史记录写不进去是小事，
    绝不能因此把「推送成功」误判成失败，那会让用户重复收到同一条告警。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # ---------- 写 ----------
    def append(self, record: AlertRecord) -> bool:
        """写一条记录。成功 True / 失败 False（失败只记日志，不往上抛）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(record.to_line() + "\n")
                    fh.flush()
            return True
        except OSError as exc:
            log.error("告警历史写入失败（忽略，不影响推送判定）：%s", exc)
            return False

    # ---------- 读 ----------
    def load(self, inst_id: str | None = None, limit: int | None = None,
             since_ms: int | None = None, signal_type: str | None = None) -> list[AlertRecord]:
        """按时间【升序】读回记录，并可按品种/类型/起始时间过滤。

        文件不存在、含 BOM、夹着坏行都只是「少几条」，绝不抛异常。
        """
        if not self.path.is_file():
            return []
        records: list[AlertRecord] = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("告警历史读取失败：%s", exc)
            return []
        for line in text.splitlines():
            record = AlertRecord.from_line(line)
            if record is None:
                continue
            if inst_id and record.inst_id != inst_id:
                continue
            if signal_type and record.signal_type != signal_type:
                continue
            if since_ms is not None and (record.ts_ms is None or record.ts_ms < since_ms):
                continue
            records.append(record)

        records.sort(key=lambda r: (r.ts_ms is None, r.ts_ms or 0, r.pushed_at))
        if limit is not None and limit > 0 and len(records) > limit:
            records = records[-limit:]
        return records

    def prune(self, keep_days: int = KEEP_DAYS, max_lines: int = MAX_LINES) -> int:
        """裁掉过期与超量的记录，返回删除条数。

        读不出时间戳的记录按「保留」处理：宁可多留，也不要把用户的记录误删。
        """
        records = self.load()
        if not records:
            return 0
        cutoff = time.time() * 1000 - keep_days * DAY_MS
        kept = [r for r in records
                if r.ts_ms is None or r.ts_ms >= cutoff]
        if max_lines > 0 and len(kept) > max_lines:
            kept = kept[-max_lines:]
        removed = len(records) - len(kept)
        if removed <= 0:
            return 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text("".join(r.to_line() + "\n" for r in kept), encoding="utf-8")
                tmp.replace(self.path)
        except OSError as exc:
            log.error("告警历史裁剪失败：%s", exc)
            return 0
        return removed

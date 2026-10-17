"""运行期控制项：逐品种开关 / 消息免打扰 / 重点提醒 / 持仓覆盖。

定位：这是「本地控制台」的唯一写入口，只改**运行期的监控与推送偏好**，
既不回写 config.yaml（真实持仓与本地配置留在原处），也不接任何交易接口、
不下单、不调大模型。

落盘位置：state/control.json。`state/` 已在 .gitignore 里被忽略，
跟随推送状态一起留在本地，不会进公开仓库。

四类控制项：
  symbols   —— 逐品种开关覆盖。{"inst_id": true/false}；没记录的品种跟随 config。
  dnd       —— 消息免打扰。{"enabled": bool, "windows": [时段, ...]}；
               时段支持跨零点与限定星期（复用重点时段同一套 TimeWindow 口径）。
  priority  —— 重点提醒。{"symbols": [...], "tracks": [...]}；
               重点信号在免打扰期间照常推送，并在标题加【重点】标记。
  positions —— 持仓覆盖。{"inst_id": {side, entry_price, margin_usdt, leverage, note}}；
               没记录就用 config 里的真实持仓。

红线：
  - 字段白名单 + inst_id 白名单校验，拒绝任何未知的品种与轨道；
  - 数值必须来自调用方（面板输入），本模块不推导、不编造任何数字；
  - 写文件走「临时文件 + 原子替换」，避免写坏。
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .modules.session import TimeWindow, active_window, parse_windows

log = logging.getLogger(__name__)

# 允许被设为重点的推送轨道（与 run.py 里的指纹前缀一致）
VALID_TRACKS: tuple[str, ...] = ("trend", "event", "world", "short", "session")

TRACK_LABELS: dict[str, str] = {
    "trend": "趋势轨（周线状态变化）",
    "event": "事件轨（经济日历）",
    "world": "国际大事轨",
    "short": "做空观察位",
    "session": "盘中波动",
}

VALID_SIDES: tuple[str, ...] = ("long", "short")


class ControlError(RuntimeError):
    """控制项不合法时抛出。宁可明确报错，也不猜用户想要什么。"""


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ControlError(f"{field} 必须是布尔值，收到：{value!r}")


def _opt_positive(value: Any, field: str) -> float | None:
    """空值 -> None；否则必须是正数。0/负数/非数字一律报错，不静默吞掉。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ControlError(f"{field} 必须是数字，收到：{value!r}")
    if number <= 0:
        raise ControlError(f"{field} 必须大于 0，收到：{value!r}")
    return number


def _normalize_windows(raw: Any) -> list[dict[str, Any]]:
    """把面板传来的时段规范化成 TimeWindow 认识的形状（保留原始文本便于回显）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ControlError("免打扰时段必须是列表")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ControlError("免打扰时段每一项必须是对象")
        name = str(item.get("name") or "免打扰").strip()[:20] or "免打扰"
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        weekdays: list[int] = []
        for day in (item.get("weekdays") or []):
            try:
                value = int(day)
            except (TypeError, ValueError):
                raise ControlError(f"星期取值必须是 0-6 的整数，收到：{day!r}")
            if value < 0 or value > 6:
                raise ControlError(f"星期取值必须是 0-6（0=周一），收到：{value}")
            if value not in weekdays:
                weekdays.append(value)
        out.append({"name": name, "start": start, "end": end, "weekdays": weekdays})
    # 交给同一套解析器校验：解析不出来的时段直接报错，不放进配置
    parsed = parse_windows(out) if out else []
    if out and len(parsed) != len(out):
        raise ControlError("免打扰时段格式不合法（start/end 需为 HH:MM）")
    return out


def _parse_windows_or_empty(raw: Any) -> list[TimeWindow]:
    """解析已规范化的时段。空列表表示「永不免打扰」，不能回退到默认时段。"""
    if not raw:
        return []
    return parse_windows(raw)


def inst_from_fingerprint(fingerprint: str) -> str:
    """从信号指纹里猜测品种代码（兜底用；首选 alert.inst_id）。

    trend / session / short 是 `轨道|inst_id|...`，world 是 `轨道|url_key|inst_id`，
    event 的公布后是 `event|fp|post|inst_id`。统一扫描「含短横线的大写代码」即可。
    """
    for part in str(fingerprint or "").split("|"):
        text = part.strip()
        if "-" in text and text.upper() == text and any(ch.isalpha() for ch in text):
            return text
    return ""


class ControlStore:
    """control.json 的读写与语义判定。线程安全由「原子写」与「每次重读」保证。"""

    def __init__(self, path: Path | str, valid_inst_ids: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self._valid: set[str] = {str(i) for i in valid_inst_ids if i}
        self._data: dict[str, Any] = self._defaults()
        self._windows: list[TimeWindow] = []
        self.load()

    # ---------- 结构 ----------
    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "symbols": {},                                   # inst_id -> bool
            "dnd": {"enabled": False, "windows": []},
            "priority": {"symbols": [], "tracks": []},
            "positions": {},                                 # inst_id -> dict
        }

    def set_valid_inst_ids(self, inst_ids: Iterable[str]) -> None:
        self._valid = {str(i) for i in inst_ids if i}

    # ---------- 读写 ----------
    def load(self) -> None:
        if not self.path.is_file():
            self._windows = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 控制文件坏了不该让监控进程挂掉：退回默认（全部跟随 config）
            log.error("控制文件解析失败，已按默认值继续运行：%s", exc)
            self._data = self._defaults()
            self._windows = []
            return
        self._data = self._merge(raw if isinstance(raw, dict) else {})
        self._windows = _parse_windows_or_empty(self._data["dnd"]["windows"])

    def reload(self) -> None:
        """监控进程每轮重读一次，面板另一进程改了控制项即刻生效。"""
        self.load()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp"
        ) as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
            tmp_name = fh.name
        Path(tmp_name).replace(self.path)

    def _merge(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把外部数据收拢成已知结构；未知键直接丢弃，避免脏数据混进来。"""
        data = self._defaults()

        symbols = raw.get("symbols")
        if isinstance(symbols, dict):
            for inst_id, enabled in symbols.items():
                if isinstance(enabled, bool):
                    data["symbols"][str(inst_id)] = enabled

        dnd = raw.get("dnd")
        if isinstance(dnd, dict):
            data["dnd"]["enabled"] = bool(dnd.get("enabled", False))
            try:
                data["dnd"]["windows"] = _normalize_windows(dnd.get("windows"))
            except ControlError as exc:
                log.error("控制文件里的免打扰时段不合法，已按空处理：%s", exc)
                data["dnd"]["windows"] = []

        priority = raw.get("priority")
        if isinstance(priority, dict):
            data["priority"]["symbols"] = [
                str(s) for s in (priority.get("symbols") or []) if s
            ]
            data["priority"]["tracks"] = [
                str(t) for t in (priority.get("tracks") or []) if t in VALID_TRACKS
            ]

        positions = raw.get("positions")
        if isinstance(positions, dict):
            for inst_id, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                side = str(pos.get("side") or "").strip().lower()
                data["positions"][str(inst_id)] = {
                    "side": side if side in VALID_SIDES else None,
                    "entry_price": _safe_float(pos.get("entry_price")),
                    "margin_usdt": _safe_float(pos.get("margin_usdt")),
                    "leverage": _safe_float(pos.get("leverage")),
                    "note": str(pos.get("note") or "").strip()[:60],
                }
        return data

    # ---------- 快照 ----------
    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    def valid_tracks(self) -> list[dict[str, str]]:
        return [{"key": key, "label": TRACK_LABELS[key]} for key in VALID_TRACKS]

    # ---------- 逐品种开关 ----------
    def symbol_override(self, inst_id: str) -> bool | None:
        value = self._data["symbols"].get(str(inst_id))
        return value if isinstance(value, bool) else None

    def set_symbol_enabled(self, inst_id: str, enabled: bool | None) -> None:
        self._require_inst(inst_id)
        if enabled is None:
            # 清除覆盖 = 回到 config 里的设定
            self._data["symbols"].pop(str(inst_id), None)
        else:
            self._data["symbols"][str(inst_id)] = _as_bool(enabled, "enabled")
        self.save()

    # ---------- 免打扰 ----------
    def dnd_enabled(self) -> bool:
        return bool(self._data["dnd"]["enabled"])

    def dnd_windows(self) -> list[dict[str, Any]]:
        return [dict(w) for w in self._data["dnd"]["windows"]]

    def set_dnd(self, enabled: Any, windows: Any) -> None:
        normalized = _normalize_windows(windows)
        self._data["dnd"] = {
            "enabled": _as_bool(enabled, "dnd.enabled"),
            "windows": normalized,
        }
        self._windows = _parse_windows_or_empty(normalized)
        self.save()

    def is_dnd_active(self, now_bj: datetime) -> bool:
        """当前（北京时间）是否处于免打扰。总开关关掉时永远为 False。"""
        if not self.dnd_enabled():
            return False
        return active_window(self._windows, now_bj) is not None

    # ---------- 重点提醒 ----------
    def priority_symbols(self) -> list[str]:
        return list(self._data["priority"]["symbols"])

    def priority_tracks(self) -> list[str]:
        return list(self._data["priority"]["tracks"])

    def set_priority(self, symbols: Any, tracks: Any) -> None:
        if symbols is None:
            raise ControlError("priority.symbols 不能为空（要清空请传 []）")
        if not isinstance(symbols, list):
            raise ControlError("priority.symbols 必须是列表")
        clean_symbols: list[str] = []
        for inst_id in symbols:
            self._require_inst(inst_id)
            text = str(inst_id)
            if text not in clean_symbols:
                clean_symbols.append(text)
        clean_tracks: list[str] = []
        for track in (tracks or []):
            text = str(track)
            if text not in VALID_TRACKS:
                raise ControlError(f"未知的推送轨道：{text}")
            if text not in clean_tracks:
                clean_tracks.append(text)
        self._data["priority"] = {"symbols": clean_symbols, "tracks": clean_tracks}
        self.save()

    def is_priority(self, track: str | None, inst_id: str | None) -> bool:
        priority = self._data["priority"]
        if track and track in priority["tracks"]:
            return True
        if inst_id and inst_id in priority["symbols"]:
            return True
        return False

    # ---------- 持仓覆盖 ----------
    def position(self, inst_id: str) -> dict[str, Any] | None:
        value = self._data["positions"].get(str(inst_id))
        return dict(value) if isinstance(value, dict) else None

    def set_position(self, inst_id: str, payload: Any) -> None:
        self._require_inst(inst_id)
        if not isinstance(payload, dict):
            raise ControlError("持仓必须是一个对象")
        side_raw = str(payload.get("side") or "").strip().lower()
        if side_raw and side_raw not in VALID_SIDES:
            raise ControlError(f"持仓方向只能是 long / short / 空，收到：{side_raw!r}")
        side = side_raw if side_raw in VALID_SIDES else None
        entry = _opt_positive(payload.get("entry_price"), "开仓价")
        margin = _opt_positive(payload.get("margin_usdt"), "保证金")
        leverage = _opt_positive(payload.get("leverage"), "杠杆")
        note = str(payload.get("note") or "").strip()[:60]

        key = str(inst_id)
        # 完全留空 = 清除该品种的持仓覆盖，回到 config 里的真实持仓
        if side is None and entry is None and margin is None and leverage is None and not note:
            self._data["positions"].pop(key, None)
        else:
            if entry is None:
                raise ControlError("填了持仓就必须给出开仓价")
            self._data["positions"][key] = {
                "side": side or "long",
                "entry_price": entry,
                "margin_usdt": margin,
                "leverage": leverage,
                "note": note,
            }
        self.save()

    # ---------- 内部 ----------
    def _require_inst(self, inst_id: Any) -> None:
        text = str(inst_id or "")
        if self._valid and text not in self._valid:
            raise ControlError(f"未知的品种代码：{text!r}")


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PendingQueue:
    """免打扰期间被「延迟补发」的信号队列（落盘 state/pending.json）。

    为什么单独落盘：免打扰要能跨进程重启——控制项改动、守护进程崩溃重启之后，
    被延迟的消息不能凭空消失，否则「延迟补发」就变成「悄悄丢弃」。
    只存已经渲染好的标题/正文与一条历史记录，补发时直接发送，不重算、不重调大模型。
    """

    MAX_ITEMS = 200

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("延迟队列解析失败，已按空处理：%s", exc)
            self._items = []
            return
        items = raw.get("items") if isinstance(raw, dict) else None
        self._items = [i for i in (items or []) if isinstance(i, dict)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp"
        ) as fh:
            json.dump({"items": self._items}, fh, ensure_ascii=False, indent=2)
            tmp_name = fh.name
        Path(tmp_name).replace(self.path)

    def items(self) -> list[dict[str, Any]]:
        return [dict(i) for i in self._items]

    def fingerprints(self) -> set[str]:
        return {str(i.get("fingerprint") or "") for i in self._items}

    def enqueue(self, item: dict[str, Any]) -> bool:
        """入队；同一指纹已在队里则不再重复（返回 False）。"""
        fp = str(item.get("fingerprint") or "")
        if not fp or fp in self.fingerprints():
            return False
        self._items.append(item)
        # 超过上限丢最旧的，避免控制项被误设成全天免打扰时无限膨胀
        if len(self._items) > self.MAX_ITEMS:
            self._items = self._items[-self.MAX_ITEMS:]
        self.save()
        return True

    def remove(self, fingerprint: str) -> None:
        fp = str(fingerprint or "")
        self._items = [i for i in self._items if str(i.get("fingerprint") or "") != fp]
        self.save()

    def bump_attempt(self, fingerprint: str) -> int:
        fp = str(fingerprint or "")
        for item in self._items:
            if str(item.get("fingerprint") or "") == fp:
                item["attempts"] = int(item.get("attempts") or 0) + 1
                self.save()
                return item["attempts"]
        return 0

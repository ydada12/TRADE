"""状态持久化：趋势状态记忆 + 推送去重冷却。

state.json 结构：
{
  "trend_state": {"TQQQ-USDT-SWAP": {"state": "bull", "ma_fast": 78.1, "as_of": "..."}},
  "sent": {"信号指纹": 1712345678.0},
  "events": {"事件指纹": {"stage": "post", "as_of": "..."}}
}
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {
            "trend_state": {},
            "sent": {},
            "events": {},
            "session": {},
            "retries": {},
        }
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in self._data:
                    if isinstance(raw.get(key), dict):
                        self._data[key] = raw[key]
        except (json.JSONDecodeError, OSError):
            # 状态文件坏了不该让程序挂掉，重建即可
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再替换，避免断电写坏
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.path.parent), delete=False, suffix=".tmp"
        ) as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
            tmp_name = fh.name
        Path(tmp_name).replace(self.path)

    # ---------- 趋势状态 ----------
    def get_trend_state(self, inst_id: str) -> dict[str, Any] | None:
        value = self._data["trend_state"].get(inst_id)
        return value if isinstance(value, dict) else None

    def set_trend_state(self, inst_id: str, state: dict[str, Any]) -> None:
        self._data["trend_state"][inst_id] = state

    # ---------- 推送冷却 ----------
    def is_in_cooldown(self, fingerprint: str, cooldown_minutes: float) -> tuple[bool, float]:
        """返回 (是否在冷却中, 剩余秒数)。"""
        last = self._data["sent"].get(fingerprint)
        if not isinstance(last, (int, float)):
            return False, 0.0
        elapsed = time.time() - float(last)
        remaining = cooldown_minutes * 60 - elapsed
        if remaining > 0:
            return True, remaining
        return False, 0.0

    def mark_sent(self, fingerprint: str) -> None:
        self._data["sent"][fingerprint] = time.time()
        # 顺手清理超过 30 天的记录，防止文件无限膨胀
        cutoff = time.time() - 30 * 86400
        self._data["sent"] = {
            k: v for k, v in self._data["sent"].items() if isinstance(v, (int, float)) and v >= cutoff
        }

    # ---------- 事件阶段记忆 ----------
    def get_event(self, fingerprint: str) -> dict[str, Any] | None:
        value = self._data["events"].get(fingerprint)
        return value if isinstance(value, dict) else None

    def set_event(self, fingerprint: str, info: dict[str, Any]) -> None:
        self._data["events"][fingerprint] = info

    # ---------- 推送失败重试计数 ----------
    def bump_retry(self, key: str) -> int:
        """记一次推送失败，返回该 key 累计失败次数。顺手清理 7 天前的旧记录。"""
        retries = self._data.setdefault("retries", {})
        now = time.time()
        cutoff = now - 7 * 86400
        for stale in [k for k, v in retries.items()
                      if not isinstance(v, dict) or float(v.get("at", 0)) < cutoff]:
            retries.pop(stale, None)
        count = int((retries.get(key) or {}).get("n", 0)) + 1
        retries[key] = {"n": count, "at": now}
        return count

    def clear_retry(self, key: str) -> None:
        self._data.setdefault("retries", {}).pop(key, None)

    # ---------- 盘中监控记忆（防刷屏用）----------
    def get_session(self, key: str) -> dict[str, Any] | None:
        value = self._data.get("session", {}).get(key)
        return value if isinstance(value, dict) else None

    def set_session(self, key: str, info: dict[str, Any]) -> None:
        self._data.setdefault("session", {})[key] = info

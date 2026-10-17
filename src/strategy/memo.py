"""跨轨方向备忘（同品种多轨口径差异提示）。

国际大事/事件结果是「题材历史规律」口径，做空观察位是「日内压力位+盘口」口径，
两者本就可能方向相反。这里不压制任何一条轨，只记下最近一次明确方向，
好在文案里显式提示「口径不同」，避免用户把两条推送读成同一个声音自相矛盾。

仅内存，不落盘。以 mixin 形式挂在 Monitor 上：`self._dir_memo` 由 Monitor
在 __init__ 里初始化为空 dict。
"""

from __future__ import annotations

import time
from datetime import datetime

from src.datasource.econ_calendar import BEIJING


class DirectionMemo:
    """方向备忘 mixin：只读 `self._dir_memo`，只写 `self._dir_memo`。"""

    # 备忘有效期（小时）：超过这个窗口就不再做跨轨提示
    _dir_memo_hours = 6.0

    def _note_direction(self, inst_id: str, bias: str, kind: str,
                        ts: float | None = None) -> None:
        """记下某品种最近一次明确方向（事件结果 / 国际大事 / 做空观察位）。"""
        if not inst_id or not bias:
            return
        self._dir_memo[inst_id] = {
            "bias": bias,
            "kind": kind,
            "ts": time.time() if ts is None else float(ts),
        }

    def _cross_track_note(self, inst_id: str, kind: str, bias: str) -> str | None:
        """同品种近期另一条轨给了相反方向时，返回一行「口径不同」提示，否则 None。

        只解释两条消息为何方向相反，不下任何结论、不给买卖建议。
        """
        rec = self._dir_memo.get(inst_id) or {}
        if not rec or rec.get("kind") == kind or not rec.get("bias"):
            return None
        if rec["bias"] == bias:
            return None
        if time.time() - float(rec.get("ts") or 0.0) > self._dir_memo_hours * 3600:
            return None
        when = datetime.fromtimestamp(float(rec["ts"]), BEIJING).strftime("%m-%d %H:%M")
        return (f"⚠️ 跨轨口径提示：本品种 {when} 的【{rec['kind']}】给的是【{rec['bias']}】，"
                f"与本条【{kind}】【{bias}】口径不同，请人工综合判断")

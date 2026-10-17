"""免打扰延迟补发队列：入队 / 补发 / 放弃。

免打扰期间非重点信号不是「丢弃」，而是转入延迟队列（落盘 state/pending.json，
跨进程重启也不丢）；退出免打扰后再补发，补发成功那一刻才落历史。

边界：严禁反向依赖 run.py。state / notifier / history / pending 由 Monitor 注入
【同一个实例】；把 Alert 映射成落盘记录的函数由 Monitor 注入（见 make_record），
以免本模块反向依赖面板层。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from src.logutil import get_logger
from .gate import MAX_PUSH_RETRIES

log = get_logger("run")


class DndDeferral:
    """免打扰延迟队列。持有 Monitor 注入的同一批实例。"""

    def __init__(self, *, state: Any, notifier: Any, history: Any, pending: Any,
                 make_record: Callable[..., Any],
                 record_from_line: Callable[[str], Any]) -> None:
        self.state = state
        self.notifier = notifier
        self.history = history
        self.pending = pending
        self._make_record = make_record
        self._record_from_line = record_from_line

    def defer_push(self, track: str | None, inst_id: str | None, title: str, body: str,
                   fingerprint: str, alert: Any) -> None:
        """免打扰期间的信号：不发、也不丢，进延迟队列等退出免打扰后补发。"""
        record_line = None
        if alert is not None:
            record_line = self._make_record(alert, title, fingerprint, body=body).to_line()
        queued = self.pending.enqueue({
            "title": title,
            "body": body,
            "fingerprint": fingerprint,
            "track": track or "",
            "inst_id": inst_id or "",
            "record_line": record_line,
            "queued_at": time.time(),
            "attempts": 0,
        })
        # 记冷却：避免同一信号在免打扰期间每轮都重新入队、重新调大模型
        self.state.mark_sent(fingerprint)
        self.state.save()
        if queued:
            log.info("免打扰生效：%s（轨道 %s）转入延迟补发队列", fingerprint, track)

    def flush_pending(self) -> int:
        """退出免打扰后补发延迟队列。返回本轮补发成功条数。

        补发失败不丢：留在队列里下轮再试，累计失败 MAX_PUSH_RETRIES 次后丢弃。
        """
        self.pending.load()
        items = self.pending.items()
        for item in items:
            fp = str(item.get("fingerprint") or "")
            result = self.notifier.send(str(item.get("title") or ""), str(item.get("body") or ""))
            if result.ok:
                log.info("免打扰结束，补发延迟消息：%s", item.get("title"))
                record_line = item.get("record_line")
                if record_line:
                    record = self._record_from_line(str(record_line))
                    if record is not None:
                        self.history.append(record)
                self.pending.remove(fp)
                continue
            attempts = self.pending.bump_attempt(fp)
            if attempts >= MAX_PUSH_RETRIES:
                log.error("延迟补发 %s 连续失败 %s 次，丢弃", fp, attempts)
                self.pending.remove(fp)
            else:
                log.warning("延迟补发 %s 失败（第 %s/%s 次），下一轮重试",
                            fp, attempts, MAX_PUSH_RETRIES)
        return len(items) - len(self.pending.items())

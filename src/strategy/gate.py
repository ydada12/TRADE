"""推送单点闸口：去重 / 冷却 / 免打扰 / 重点标记 / 失败重试 / 落历史。

所有推送的唯一出口。轨道的规则与文案在 src/strategy/tracks/，本模块只回答
「这条信号该不该发、发没发出去、要不要重试」。

顺序（与线上完全一致）：
  指纹冷却跳过 -> 算轨道/品种 -> 重点加前缀 -> 免打扰（非重点转延迟补发）
  -> 真发送 -> 成功才记冷却 + 落历史；失败返回 PUSH_FAILED 由调用方决定重试。

边界：严禁反向依赖 run.py（避免循环依赖）。state / control / notifier / history /
safety / pending 全部由 Monitor 注入【同一个实例】——尤其是 control：
测试里会 monkeypatch `m.control.is_dnd_active`，克隆新对象会让它失效。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from src.alert import (
    Alert,
    EventAlert,
    WorldAlert,
    sanitize_for_push,
)
from src.control import inst_from_fingerprint
from src.datasource.econ_calendar import BEIJING
from src.logutil import get_logger
from .wording import PRIORITY_PREFIX

log = get_logger("run")

# push_status 的返回值
PUSH_SENT = "sent"          # 已发送
PUSH_COOLDOWN = "cooldown"  # 冷却中，按配置跳过（不是失败）
PUSH_FAILED = "failed"      # 发送失败，用户没收到
PUSH_SKIPPED = "skipped"    # 没有可推送的内容（例如事件没有关联品种）
# 处于消息免打扰时段：不发、但也不丢——入延迟队列，退出免打扰后补发
PUSH_DEFERRED = "deferred"
# 同一条信号推送连续失败多少次后放弃重试
MAX_PUSH_RETRIES = 5


class PushGate:
    """单点闸口。持有 Monitor 注入的同一批实例与一个 dry_run 取值器。

    dry_run 不预先捕获，而是每次现取（`dry_run()`）：它在运行期可能被改，
    捕获布尔值会让「事后切 dry_run」失效。
    """

    def __init__(self, *, state: Any, control: Any, notifier: Any, history: Any,
                 safety: Any, deferral: Any, dry_run: Callable[[], bool],
                 cooldown_minutes: float, make_record: Callable[..., Any]) -> None:
        self.state = state
        self.control = control
        self.notifier = notifier
        self.history = history
        self.safety = safety
        self._deferral = deferral
        self._dry_run = dry_run
        self.cooldown_minutes = cooldown_minutes
        self._make_record = make_record

    def push_status(self, title: str, body: str, fingerprint: str,
                    force: bool = False, cooldown_minutes: float | None = None,
                    alert: Alert | EventAlert | WorldAlert | None = None,
                    track: str | None = None, inst_id: str | None = None) -> str:
        """推送并返回结果：PUSH_SENT / PUSH_COOLDOWN / PUSH_FAILED / PUSH_DEFERRED。

        调用方必须区分「冷却中跳过」和「真的发送失败」：
        前者说明这条信号刚推过，可以放心记为已处理；
        后者说明用户根本没收到，不能把状态记成已处理，否则这条信号永远丢了。

        免打扰（延迟补发）：处于免打扰时段时，非重点信号不进入发送链路，而是转入
        延迟队列并返回 PUSH_DEFERRED —— 它同样算「处理完了」（记录去重状态、下轮不重算），
        消息留到退出免打扰后再补发；重点提醒（命中重点品种或重点轨道）不受免打扰拦截。
        dry_run 与 --force 都不受免打扰影响，避免调试时被静音。

        alert：可选。传了就在**推送成功之后**把它结构化落盘（面板标注 K 线用）。
        冷却跳过不写（早写过），发送失败不写（用户没收到）；延迟补发时先入队，
        补发成功那一刻再写历史。
        """
        body = sanitize_for_push(body)
        body = self.safety.ensure_disclaimer(body)

        cooldown = self.cooldown_minutes if cooldown_minutes is None else cooldown_minutes
        if not force:
            cooling, remaining = self.state.is_in_cooldown(fingerprint, cooldown)
            if cooling:
                log.info(
                    "信号 %s 处于冷却期，剩余 %.0f 分钟，跳过推送", fingerprint, remaining / 60
                )
                return PUSH_COOLDOWN

        # 轨道取自指纹前缀（trend/event/world/short/session），品种优先取告警自带的
        track = track or (str(fingerprint).split("|", 1)[0] or None)
        if not inst_id:
            inst_id = str(getattr(alert, "inst_id", "") or "") or inst_from_fingerprint(fingerprint)
        priority = self.control.is_priority(track, inst_id)
        if priority:
            title = f"{PRIORITY_PREFIX}{title}"

        if not force and not self._dry_run() and self.control.is_dnd_active(datetime.now(BEIJING)):
            if not priority:
                self._deferral.defer_push(track, inst_id, title, body, fingerprint, alert)
                return PUSH_DEFERRED
            log.info("免打扰生效，但 %s 属重点提醒（轨道 %s），照常推送", fingerprint, track)

        result = self.notifier.send(title, body)
        if result.ok:
            log.info("已推送：%s", title)
            if self._dry_run():
                # 演练不写任何真实状态：否则演练会压制同名信号的真实告警，
                # 还会把「从没真发出去」的条目写进面板历史。
                return PUSH_SENT
            self.state.mark_sent(fingerprint)
            self.state.save()
            if alert is not None:
                self.history.append(self._make_record(alert, title, fingerprint, body=body))
            return PUSH_SENT
        log.error("推送失败：%s -> %s", title, result.detail)
        return PUSH_FAILED

    def push(self, title: str, body: str, fingerprint: str,
             force: bool = False, cooldown_minutes: float | None = None,
             alert: Alert | EventAlert | None = None) -> bool:
        """兼容旧调用：只在真的发出去时返回 True。"""
        return self.push_status(title, body, fingerprint, force, cooldown_minutes,
                                alert) == PUSH_SENT

    def _delivery_settled(self, retry_key: str, status: str, force: bool) -> bool:
        """这条信号算不算「处理完了」（可以落状态/标记已发）。

        已发送、冷却中、跳过：处理完了。
        发送失败：不算，下一轮重试；累计失败 MAX_PUSH_RETRIES 次后放弃，
        避免 Server酱长时间故障时每一轮都白白重算、重调大模型。
        手动 --force 的失败不重试（用户在场，自己看得到）。
        """
        if status != PUSH_FAILED or force:
            self.state.clear_retry(retry_key)
            return True
        attempts = self.state.bump_retry(retry_key)
        if attempts >= MAX_PUSH_RETRIES:
            log.error("信号 %s 连续推送失败 %s 次，放弃重试", retry_key, attempts)
            self.state.clear_retry(retry_key)
            return True
        log.warning("信号 %s 推送失败（第 %s/%s 次），状态未落盘，下一轮重试",
                    retry_key, attempts, MAX_PUSH_RETRIES)
        self.state.save()
        return False

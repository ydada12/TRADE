"""模块4：推送 —— Server酱 Turbo。

接口：POST https://sctapi.ftqq.com/<SendKey>.send
参数：title=标题, desp=告警内容

支持一次推给多个微信用户：把多个 SendKey 用逗号（中英文）/ 分号 / 空格分隔，
填进同一个环境变量即可，例如：

    SERVERCHAN_SENDKEY=SCTaaa...bbb,SCTccc...ddd

旧的单个 key 写法完全兼容。只要有一个目标没送达，整体就记为失败 ——
这样上层的重试逻辑（失败不标记已发、下一轮重发）继续生效，
不会因为「另一个用户收不到」而把告警当成已送达丢掉。

失败重发 2 次（可通过 config 调整），每个目标的每次结果都打日志。
dry_run 模式下不发网络请求，只把内容写进日志。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from ..http_util import HttpClient, HttpError

log = logging.getLogger(__name__)

# 支持中英文逗号、分号以及空白（换行/空格/Tab）分隔
_KEY_SPLIT_RE = re.compile(r"[,，;；\s]+")

# 模板里的示例值，出现这些就当作没配置
_PLACEHOLDERS = ("sk-在这里", "sct在这里")


@dataclass
class PushResult:
    ok: bool
    attempts: int
    detail: str


def parse_send_keys(raw: str | None) -> list[str]:
    """把环境变量里的值拆成多个 SendKey（去空、去重、剔除模板占位符）。

    兼容旧的单个 key 写法；同一个 key 写重复了只发一次。
    """
    if not raw:
        return []
    keys: list[str] = []
    for chunk in _KEY_SPLIT_RE.split(str(raw)):
        key = chunk.strip()
        if not key:
            continue
        if key.lower().startswith(_PLACEHOLDERS):
            continue
        if key not in keys:
            keys.append(key)
    return keys


def mask_key(key: str) -> str:
    """日志里只留头尾，避免完整 SendKey 落进日志或仓库。"""
    return f"{key[:8]}…{key[-4:]}" if len(key) > 12 else "…"


class ServerChanNotifier:
    name = "serverchan"

    def __init__(
        self,
        send_key: str | None,
        endpoint: str = "https://sctapi.ftqq.com",
        max_retries: int = 2,
        retry_backoff_seconds: float = 3.0,
        timeout: float = 20.0,
        dry_run: bool = False,
    ) -> None:
        self.send_keys = parse_send_keys(send_key)
        self.endpoint = endpoint.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = retry_backoff_seconds
        self.dry_run = dry_run
        self.http = HttpClient(timeout=timeout, max_retries=1, backoff=1.0)

    @property
    def configured(self) -> bool:
        return bool(self.send_keys)

    def send(self, title: str, desp: str) -> PushResult:
        if self.dry_run:
            for key in self.send_keys or [""]:
                log.info(
                    "[DRY-RUN] 不实际推送（目标 %s）。标题=%s\n%s",
                    mask_key(key) if key else "未配置", title, desp,
                )
            return PushResult(True, 0, "dry_run")

        if not self.configured:
            log.error("未配置 Server酱 SendKey（环境变量 SERVERCHAN_SENDKEY 为空），无法推送")
            return PushResult(False, 0, "missing_send_key")

        results: list[tuple[str, bool, str]] = []
        total_attempts = 0
        for key in self.send_keys:
            ok, attempts, detail = self._send_one(key, title, desp)
            total_attempts += attempts
            results.append((key, ok, detail))

        all_ok = all(ok for _, ok, _ in results)
        detail = " | ".join(
            f"{mask_key(k)} {'成功' if ok else '失败(' + d + ')'}" for k, ok, d in results
        )
        if all_ok:
            log.info("推送成功（%s 个目标）：%s | %s", len(results), title, detail)
            # 正文也落日志：推送失败或不一致时能回溯到底发了什么
            log.info("推送正文：\n%s", desp)
        else:
            failed = sum(1 for _, ok, _ in results if not ok)
            log.error("推送未全部送达（%s/%s 个目标失败）：%s | %s",
                      failed, len(results), title, detail)
        return PushResult(all_ok, total_attempts, detail)

    def _send_one(self, key: str, title: str, desp: str) -> tuple[bool, int, str]:
        """给单个 SendKey 发送（含重发），返回 (是否成功, 尝试次数, 详情)。"""
        url = f"{self.endpoint}/{key}.send"
        attempts = 0
        last_detail = ""
        # 首次 + 重发 max_retries 次
        for attempt in range(1, self.max_retries + 2):
            attempts = attempt
            try:
                resp = self.http.request("POST", url, data={"title": title, "desp": desp})
                body = resp.json()
                if resp.status_code == 200 and int(body.get("code", -1)) == 0:
                    push_id = (body.get("data") or {}).get("pushid", "")
                    last_detail = f"HTTP 200 code=0 pushid={push_id}"
                    log.info("推送成功（%s，第 %s 次尝试）：%s", mask_key(key), attempt, last_detail)
                    return True, attempts, last_detail
                last_detail = f"HTTP {resp.status_code} body={str(body)[:200]}"
                log.warning("推送被拒（%s，第 %s 次）：%s", mask_key(key), attempt, last_detail)
            except (HttpError, ValueError) as exc:
                last_detail = str(exc)
                log.warning("推送异常（%s，第 %s 次）：%s", mask_key(key), attempt, exc)

            if attempt <= self.max_retries:
                time.sleep(self.retry_backoff_seconds)

        log.error("推送最终失败（%s），已尝试 %s 次：%s", mask_key(key), attempts, last_detail)
        return False, attempts, last_detail

"""模块4：推送 —— Server酱 Turbo。

接口：POST https://sctapi.ftqq.com/<SendKey>.send
参数：title=标题, desp=告警内容
失败重发 2 次（可通过 config 调整），每次结果都打日志。
dry_run 模式下不发网络请求，只把内容写进日志。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..http_util import HttpClient, HttpError

log = logging.getLogger(__name__)


@dataclass
class PushResult:
    ok: bool
    attempts: int
    detail: str


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
        self.send_key = send_key
        self.endpoint = endpoint.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = retry_backoff_seconds
        self.dry_run = dry_run
        self.http = HttpClient(timeout=timeout, max_retries=1, backoff=1.0)

    @property
    def configured(self) -> bool:
        return bool(self.send_key)

    def send(self, title: str, desp: str) -> PushResult:
        if self.dry_run:
            log.info("[DRY-RUN] 不实际推送。标题=%s\n%s", title, desp)
            return PushResult(True, 0, "dry_run")

        if not self.configured:
            log.error("未配置 Server酱 SendKey（环境变量 SERVERCHAN_SENDKEY 为空），无法推送")
            return PushResult(False, 0, "missing_send_key")

        url = f"{self.endpoint}/{self.send_key}.send"
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
                    log.info("推送成功（第 %s 次尝试）：%s | %s", attempt, title, last_detail)
                    # 正文也落日志：推送失败或不一致时能回溯到底发了什么
                    log.info("推送正文：\n%s", desp)
                    return PushResult(True, attempts, last_detail)
                last_detail = f"HTTP {resp.status_code} body={str(body)[:200]}"
                log.warning("推送被拒（第 %s 次）：%s", attempt, last_detail)
            except (HttpError, ValueError) as exc:
                last_detail = str(exc)
                log.warning("推送异常（第 %s 次）：%s", attempt, exc)

            if attempt <= self.max_retries:
                time.sleep(self.retry_backoff_seconds)

        log.error("推送最终失败，已尝试 %s 次：%s", attempts, last_detail)
        return PushResult(False, attempts, last_detail)

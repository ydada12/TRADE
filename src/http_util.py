"""带重试的 HTTP 客户端。

断网 / 拉取失败要能自动重试，别整个程序挂掉 —— 这是硬要求。
只做 GET/POST，且【只用于行情读取与微信推送】，不接任何交易接口。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 4,
        backoff: float = 2.0,
        user_agent: str = "trade-signal-monitor/1.0 (read-only)",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.backoff = backoff
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _delay(self, attempt: int) -> float:
        # 指数退避 + 抖动，避免被风控打成高频
        delay = self.backoff * (2 ** (attempt - 1))
        return min(delay, 30.0) * (0.7 + random.random() * 0.6)

    def _sleep(self, attempt: int) -> None:
        time.sleep(self._delay(attempt))

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                # 5xx / 429 值得重试；4xx 一般是请求本身有问题，不重试
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise HttpError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return resp
            except (requests.RequestException, HttpError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self._delay(attempt)
                    log.warning(
                        "请求失败(%s/%s) %s %s -> %s，%.1fs 后重试",
                        attempt, self.max_retries, method, url, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    log.error("请求最终失败(%s/%s) %s %s -> %s", attempt, self.max_retries, method, url, exc)
        raise HttpError(f"请求失败，已重试 {self.max_retries} 次：{method} {url} -> {last_exc}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.request("GET", url, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"返回不是合法 JSON：{url} -> {resp.text[:200]}") from exc

    def post_json(self, url: str, data: dict[str, Any] | None = None) -> Any:
        resp = self.request("POST", url, data=data)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(f"返回不是合法 JSON：{url} -> {resp.text[:200]}") from exc

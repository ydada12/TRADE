"""DeepSeek 客户端 + 防编造数字校验。

红线 2 的技术兜底：大模型返回的文本里出现的每一个数字，
都必须能在我喂给它的真实数据里找到。找不到就判定为「疑似编造」，
直接丢弃大模型输出，改用确定性模板（宁可读起来朴素，也不许出现假数字）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..http_util import HttpClient, HttpError

log = logging.getLogger(__name__)

# 提取数字：支持 2550.17 / 3.7 / -210 / 1,234.5 / 3.2%
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

# 结构性用词里允许出现的裸数字（剧本A/B/C、三分支、2~3句 之类）
_STRUCTURAL_ALLOWED = {str(i) for i in range(0, 13)}


def _collect_numbers(node: Any, sink: set[str]) -> None:
    """递归收集 facts 里的所有数字，生成多种等价写法。"""
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        sink.add(str(node))
        sink.add(f"{node:g}")
        sink.add(f"{float(node):.2f}")
        sink.add(f"{float(node):.4f}")
        sink.add(f"{abs(float(node)):g}")
    elif isinstance(node, str):
        for match in _NUMBER_RE.findall(node):
            sink.add(match)
            sink.add(match.replace(",", ""))
            try:
                sink.add(f"{float(match.replace(',', '')):g}")
            except ValueError:
                pass
    elif isinstance(node, dict):
        for key, value in node.items():
            # 键名里也可能带数字：均线字段名就是 "MA20"/"MA30"，
            # 模型写「MA20=74.426」时那个 20 必须算合法，否则整段解读被误拦。
            _collect_numbers(str(key), sink)
            _collect_numbers(value, sink)
    elif isinstance(node, (list, tuple, set)):
        for value in node:
            _collect_numbers(value, sink)


def allowed_numbers(facts: Any) -> set[str]:
    sink: set[str] = set()
    _collect_numbers(facts, sink)
    # 常见等价表达：带千分位、四舍五入到 1~4 位小数
    expanded = set(sink)
    for token in sink:
        cleaned = token.replace(",", "")
        try:
            value = float(cleaned)
        except ValueError:
            continue
        for digits in (1, 2, 3, 4):
            expanded.add(f"{value:.{digits}f}")
            expanded.add(f"{value:,.{digits}f}")
        expanded.add(f"{value:g}")
        # 百分比口径的数字（如 0.3 与 0.3% 并存）
        expanded.add(f"{value * 100:g}")
    return expanded


def find_fabricated_numbers(text: str, facts: Any, extra_allowed: set[str] | None = None) -> list[str]:
    """返回文本里「数据源没有」的数字列表。空列表 = 通过。"""
    allowed = allowed_numbers(facts)
    if extra_allowed:
        allowed |= extra_allowed
    allowed |= _STRUCTURAL_ALLOWED

    bad: list[str] = []
    for token in _NUMBER_RE.findall(text or ""):
        cleaned = token.replace(",", "")
        if token in allowed or cleaned in allowed:
            continue
        try:
            value = float(cleaned)
        except ValueError:
            bad.append(token)
            continue
        # 再放宽一点等价形式
        candidates = {
            f"{value:g}", f"{value:.1f}", f"{value:.2f}", f"{value:.3f}", f"{value:.4f}",
            f"{abs(value):g}", f"{abs(value):.1f}", f"{abs(value):.2f}",
        }
        if candidates & allowed:
            continue
        bad.append(token)
    return bad


class DeepSeekClient:
    """只调用 chat/completions。没有任何交易相关能力。"""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.2,
        max_tokens: int = 800,
        timeout: float = 60.0,
        max_retries: int = 3,
        guard: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.guard = guard
        self.http = HttpClient(timeout=timeout, max_retries=max_retries, backoff=2.0)
        if api_key:
            self.http.session.headers.update({
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            })

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, system_prompt: str, user_message: str,
             max_tokens: int | None = None) -> str:
        if not self.available:
            raise HttpError("未配置 DeepSeek API key（环境变量 DEEPSEEK_API_KEY 为空）")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        resp = self.http.request("POST", f"{self.base_url}/chat/completions", json=payload)
        try:
            body = resp.json()
        except ValueError as exc:
            raise HttpError(f"DeepSeek 返回非 JSON：{resp.text[:200]}") from exc
        if "choices" not in body:
            raise HttpError(f"DeepSeek 返回异常：{json.dumps(body, ensure_ascii=False)[:300]}")
        content = body["choices"][0]["message"]["content"]
        return str(content).strip()

    def translate(self, system_prompt: str, user_message: str, facts: Any,
                  fallback: str) -> tuple[str, str]:
        """翻译成人话，返回 (文本, 来源标记)。

        来源标记：deepseek = 用了大模型；template = 退回确定性模板。
        """
        if not self.available:
            log.info("未配置 DeepSeek key，使用确定性模板")
            return fallback, "template"

        try:
            text = self.chat(system_prompt, user_message)
        except Exception as exc:
            log.warning("DeepSeek 调用失败，退回确定性模板：%s", exc)
            return fallback, "template"

        if self.guard:
            fabricated = find_fabricated_numbers(text, facts)
            if fabricated:
                log.warning(
                    "大模型输出中检测到数据源没有的数字 %s —— 已丢弃，改用确定性模板", fabricated
                )
                return fallback, "template_guard"

        return text, "deepseek"

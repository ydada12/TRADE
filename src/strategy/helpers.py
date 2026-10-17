"""轨道共用的纯计算助手（原在 run.py 顶部，随轨道一起搬来）。

这些函数不碰网络、不读配置，只是把「关键位 / bar 字符串」换算成数字，
五条轨道都要用。搬到策略层是为了让 src/strategy/tracks/** 不必依赖 run.py。
"""

from __future__ import annotations


def bar_minutes(bar: str) -> int:
    """把 OKX 的 bar 字符串（如 5m / 15m / 1H / 1D）换算成分钟数。"""
    text = (bar or "").strip().lower()
    if not text:
        return 5
    unit = text[-1]
    try:
        value = int(float(text[:-1]))
    except ValueError:
        return 5
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    if unit == "w":
        return value * 7 * 1440
    return 5


def gap_pct(level: float | None, price: float | None) -> float | None:
    """关键位相对现价的百分比（正=在现价上方）。

    必须预先算好喂给大模型：正文里本来就会展示这个距离，如果留给模型自己算，
    它算出来的数字会被防编造数字的校验拦掉，结果整段 AI 文案退回模板。
    """
    if level is None or not price:
        return None
    return round((level - price) / price * 100.0, 2)

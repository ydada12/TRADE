"""模块4：国际大事轨的纯逻辑（不联网、不推送，好测）。

三件事：
  1. 关键词初筛 + 分类：把抓回来的标题/摘要按题材分组打分。
  2. 确定性方向兜底：大模型不可用 / 返回不可信时，用固定的历史冲击口径给方向；
     口径覆盖不到的题材（如选举）宁可不推，也不硬猜。
  3. 校验大模型回传的 JSON：URL 必须对得上输入，方向只能取 偏多/偏空/中性，
     摘要与传导链里出现的每个数字都必须能在原标题/摘要里找到（防编造数字红线）。

措辞红线与事件轨一致：只描述历史一般规律，不写买卖指令，不做必然断言。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..llm.deepseek import find_fabricated_numbers
from .event import INSTRUMENT_SENSITIVITY

log = logging.getLogger(__name__)

VERDICT_LONG_ZH = "偏多"
VERDICT_SHORT_ZH = "偏空"
VERDICT_NEUTRAL_ZH = "中性"
VALID_BIAS = {VERDICT_LONG_ZH, VERDICT_SHORT_ZH, VERDICT_NEUTRAL_ZH}

# 题材 -> 关键词（中英双语；英文按子串、不区分大小写匹配）。
# 命中标题权重高，命中摘要权重低；只要题材口径不明确的词（如 election 单走）
# 在兜底表里就不给方向，必须等大模型研判。
DEFAULT_KEYWORD_GROUPS: dict[str, list[str]] = {
    "war": [
        "war", "airstrike", "air strike", "invasion", "missile", "ceasefire",
        "military", "strike on", "conflict", "troops", "nuclear attack",
        "战争", "冲突", "空袭", "导弹", "入侵", "停火", "军事", "袭击",
    ],
    "oil_supply": [
        "opec", "strait of hormuz", "hormuz", "oil embargo", "crude oil",
        "oil pipeline", "pipeline attack", "oil supply", "red sea shipping",
        "原油", "石油", "欧佩克", "霍尔木兹", "输油管道", "石油供应",
    ],
    "policy_trade": [
        "tariff", "sanction", "sanctions", "export control", "trade war",
        "trade deal", "subsidy", "import ban", "embargo on",
        "关税", "制裁", "出口管制", "贸易战", "法案", "补贴", "禁令",
    ],
    "cb_ease": [
        "rate cut", "cuts rates", "dovish", "easing", "stimulus",
        "降息", "宽松", "刺激政策", "放水",
    ],
    "cb_tighten": [
        "rate hike", "raises rates", "hawkish", "tightening",
        "加息", "紧缩", "缩表",
    ],
    "tech_breakthrough": [
        "ai breakthrough", "breakthrough in ai", "semiconductor", "chip export",
        "quantum computing", "nuclear fusion", "fusion breakthrough",
        "芯片", "半导体", "量子计算", "核聚变", "技术突破", "人工智能突破",
    ],
    "election": [
        "election", "referendum", "coup", "impeachment", "impeached",
        "选举", "公投", "政变", "弹劾",
    ],
}

# 题材的中文展示名
CATEGORY_LABELS: dict[str, str] = {
    "war": "地缘冲突",
    "oil_supply": "原油供应",
    "policy_trade": "贸易/政策",
    "cb_ease": "央行宽松",
    "cb_tighten": "央行收紧",
    "tech_breakthrough": "技术突破",
    "election": "选举/政局",
    "other": "其他",
}

# 题材 -> 「事件发生/升级」时对两类资产的历史冲击方向。0 = 没有可靠的固定口径。
CATEGORY_IMPACT: dict[str, dict[str, float]] = {
    "war":              {"risk": -1.0, "oil": +1.0},   # 避险升温、供应担忧
    "oil_supply":       {"risk": -0.5, "oil": +1.0},
    "policy_trade":     {"risk": -1.0, "oil": -0.5},   # 关税/制裁压制增长与需求
    "cb_ease":          {"risk": +1.0, "oil": +0.5},   # 流动性宽松
    "cb_tighten":       {"risk": -1.0, "oil": 0.0},
    "tech_breakthrough": {"risk": +1.0, "oil": 0.0},
    "election":         {"risk": 0.0, "oil": 0.0},     # 方向取决于人选，不硬猜
    "other":            {"risk": 0.0, "oil": 0.0},
}

# 兜底方向要求的最小敏感度：避免给「与题材弱相关」的品种硬派方向
MIN_SENSITIVITY = 0.5

_CHAIN_ZH: dict[str, str] = {
    "war": "地缘冲突升级 → 避险情绪升温 → 历史上通常压制风险资产、利多原油",
    "oil_supply": "供应中断担忧 → 历史上通常利多油价、压制风险偏好",
    "policy_trade": "关税/制裁升级 → 增长预期受压 → 历史上通常压制风险资产与原油需求",
    "cb_ease": "宽松信号 → 流动性预期改善 → 历史上通常利多风险资产",
    "cb_tighten": "收紧信号 → 利率预期上行 → 历史上通常压制风险资产",
    "tech_breakthrough": "技术突破 → 产业与风险偏好提振 → 历史上通常利多科技风险资产",
}

# 英文标题里常见的 HTML/转义残留
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int = 280) -> str:
    """剥 HTML 标签、压空白。纯字符串处理，不联网。"""
    text = _TAG_RE.sub(" ", raw or "")
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
        .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    )
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


@dataclass
class ScoredItem:
    """初筛后的候选新闻。"""

    title: str
    url: str
    source: str
    summary: str
    via: str
    published_ts: float | None = None
    category: str = "other"
    score: int = 0
    matched: list[str] = field(default_factory=list)
    published_text: str = ""

    def to_llm_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "summary": self.summary,
            "published_ts": self.published_ts,
            "via": self.via,
        }


def score_news(title: str, summary: str,
               groups: dict[str, list[str]] | None = None
               ) -> tuple[str, int, list[str], bool]:
    """按关键词给新闻打分，返回 (类别, 分数, 命中词, 标题是否至少命中一个)。

    标题命中权重 3、摘要命中权重 1；类别取总分最高者（并列时按 groups 顺序）。
    注意不能用「分数>=3」代替 title_hit：摘要里命中三个词也能凑出 3 分。
    """
    groups = groups or DEFAULT_KEYWORD_GROUPS
    hay_title = (title or "").lower()
    hay_summary = (summary or "").lower()
    best_cat = "other"
    best_score = 0
    best_matched: list[str] = []
    best_title_hit = False
    for cat, words in groups.items():
        score = 0
        matched: list[str] = []
        title_hit = False
        for word in words:
            needle = word.lower()
            if needle and needle in hay_title:
                score += 3
                title_hit = True
                matched.append(word)
            elif needle and needle in hay_summary:
                score += 1
                matched.append(word)
        if score > best_score:
            best_cat, best_score, best_matched = cat, score, matched
            best_title_hit = title_hit
    return best_cat, best_score, best_matched, best_title_hit


def filter_candidates(items: list[Any], *, seen_urls: set[str],
                      groups: dict[str, list[str]] | None = None,
                      min_score: int = 2,
                      require_title_hit: bool = True,
                      limit: int = 12) -> list[ScoredItem]:
    """关键词初筛 + 去重 + 限量。按分数降序、同分按时间新者优先。

    items 需有 title/url/source/summary/via/published_ts 属性（见 datasource.NewsItem）。
    """
    out: list[ScoredItem] = []
    dedup: set[str] = set()
    for item in items:
        url_key = getattr(item, "url", "")
        if not url_key or url_key in seen_urls or url_key in dedup:
            continue
        cat, score, matched, title_hit = score_news(item.title, item.summary, groups)
        if score < min_score:
            continue
        if require_title_hit and not title_hit:
            # 分数全部来自摘要 -> 多半是正文蹭词，不要
            continue
        dedup.add(url_key)
        out.append(ScoredItem(
            title=item.title, url=url_key, source=item.source,
            summary=item.summary, via=item.via,
            published_ts=item.published_ts, category=cat, score=score,
            matched=matched, published_text=getattr(item, "published_text", ""),
        ))
    out.sort(key=lambda x: (x.score, x.published_ts or 0.0), reverse=True)
    return out[:limit]


def fallback_bias(category: str, inst_id: str) -> str | None:
    """大模型不可用时的确定性方向。给不出方向返回 None（调用方不推该品种）。"""
    impact = CATEGORY_IMPACT.get(category)
    if not impact:
        return None
    sens = INSTRUMENT_SENSITIVITY.get(inst_id)
    if not sens:
        return None
    score = impact["risk"] * float(sens.get("risk", 0.0)) \
        + impact["oil"] * float(sens.get("oil", 0.0))
    if abs(score) < MIN_SENSITIVITY:
        return None
    return VERDICT_LONG_ZH if score > 0 else VERDICT_SHORT_ZH


def fallback_chain(category: str) -> str:
    return _CHAIN_ZH.get(category, "")


def fallback_body(item: ScoredItem, inst_id: str, bias: str) -> str:
    """兜底正文：事实部分原样引用英文/中文标题（真实抓取文本，不翻译就不犯错）。"""
    chain = fallback_chain(item.category)
    label = CATEGORY_LABELS.get(item.category, item.category)
    return (
        f"据 {item.source} 报道（{label}题材）：{item.title}。"
        f"{chain}，据此对相关品种给出【{bias}】的历史方向参考。"
        "历史规律不代表必然结果，请人工查看事件进展与价格确认。"
    )


_FENCE_RE = re.compile(r"^\s*```(?:json)?|```\s*$", re.MULTILINE)


def parse_world_verdicts(raw: str, items: list[ScoredItem],
                         symbol_ids: set[str]) -> dict[str, dict[str, Any]]:
    """解析并校验大模型回传的 JSON。

    返回 {url: verdict_dict}；解析整体失败返回 {}（调用方对全部候选走兜底）。
    单条不合规的条目直接缺席（同样按兜底处理），绝不带着脏数据推送。
    """
    if not raw or not raw.strip().startswith(("[", "```")):
        return {}
    try:
        data = json.loads(_FENCE_RE.sub("", raw).strip())
    except (json.JSONDecodeError, ValueError):
        log.warning("国际大事研判返回不是合法 JSON，整批改用确定性兜底")
        return {}
    if not isinstance(data, list):
        return {}

    by_url = {item.url: item for item in items}
    verdicts: dict[str, dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        item = by_url.get(url) if isinstance(url, str) else None
        if item is None:
            continue
        major = bool(entry.get("major"))
        category = str(entry.get("category") or "other")
        summary_zh = clean_text(str(entry.get("summary_zh") or ""), limit=80)
        chain = clean_text(str(entry.get("chain") or ""), limit=120)
        thinking = clean_text(str(entry.get("thinking") or ""), limit=240)
        bias_raw = entry.get("bias")
        confidence = entry.get("confidence")
        try:
            confidence = int(confidence)
        except (TypeError, ValueError):
            confidence = 0
        bias = _valid_bias(bias_raw, symbol_ids)
        if not major or category not in CATEGORY_LABELS or confidence < 1:
            verdicts[url] = {"major": False}
            continue
        if not summary_zh or not chain or not bias:
            continue
        # 防编造数字：摘要与传导链里的数字必须出自原标题/摘要
        facts = {"title": item.title, "summary": item.summary, "category": category}
        fabricated = find_fabricated_numbers(f"{summary_zh} {chain}", facts)
        if fabricated:
            log.warning("国际大事摘要中出现原文没有的数字 %s（%s），该条改用兜底",
                        fabricated, url)
            continue
        # 思考段落单独校验：脏了只丢这一段，不拖累整条（事实与方向仍然是合规的）
        if thinking and find_fabricated_numbers(thinking, facts):
            log.warning("国际大事思考段落出现原文没有的数字（%s），仅丢弃该段落", url)
            thinking = ""
        verdicts[url] = {
            "major": True,
            "category": category,
            "summary_zh": summary_zh,
            "chain": chain,
            "thinking": thinking,
            "bias": bias,
            "confidence": confidence,
        }
    return verdicts


def _valid_bias(bias_raw: Any, symbol_ids: set[str]) -> dict[str, str]:
    if not isinstance(bias_raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in bias_raw.items():
        key = str(key)
        if key not in symbol_ids:
            continue
        if value in VALID_BIAS:
            out[key] = value
    return out

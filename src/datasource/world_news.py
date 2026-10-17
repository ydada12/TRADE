"""国际大事数据源：GDELT DOC 2.0 为主，权威媒体 RSS 为辅。

两个源都免 key：
  - GDELT DOC API：聚合全球英文媒体，支持关键词检索，按时间倒序；
    不挂 key 时有频率限制（429），本客户端只做有限重试，拿不到就退到 RSS。
  - RSS：BBC World / BBC Business / 半岛电视台等，作为 GDELT 失败时的兜底。

只读取公开发布的新闻标题与摘要，不登录、不写任何东西。
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

UTC = timezone.utc
_BEIJING_OFFSET = 8 * 3600
_TRACK_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                 "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid"}

DEFAULT_GDELT_QUERY = (
    "(war OR airstrike OR invasion OR missile OR ceasefire OR sanctions OR tariff "
    "OR \"interest rate\" OR \"rate cut\" OR \"rate hike\" OR OPEC OR embargo "
    "OR blockade OR pipeline OR coup OR referendum OR \"trade war\" "
    "OR \"trade deal\" OR semiconductor OR \"export control\" OR quantum "
    "OR \"nuclear fusion\" OR \"AI breakthrough\") sourcelang:eng"
)
DEFAULT_RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_ts: float | None      # epoch 秒（UTC）；解析失败为 None
    summary: str
    via: str                        # gdelt / rss:<feed>

    @property
    def published_text(self) -> str:
        if self.published_ts is None:
            return "发布时间数据源未提供"
        bj = datetime.fromtimestamp(self.published_ts, tz=timezone.utc)
        # epoch 是绝对时刻，直接加 8 小时格式化成北京时间字符串
        return f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(self.published_ts + _BEIJING_OFFSET))} 北京时间"


def normalize_url(url: str) -> str:
    """去掉 fragment / 跟踪参数后规范化，用来识别同一篇文章的不同链接。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACK_PARAMS
    ))
    path = parts.path.rstrip("/") or parts.path
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(), path, query, ""
    ))


class WorldNewsClient:
    def __init__(self,
                 gdelt_query: str = DEFAULT_GDELT_QUERY,
                 rss_feeds: list[str] | None = None,
                 timespan_hours: int = 12,
                 timeout: float = 20.0,
                 gdelt_maxrecords: int = 75,
                 gdelt_retries: int = 2,
                 rss_retries: int = 1) -> None:
        self.gdelt_query = gdelt_query
        self.rss_feeds = list(rss_feeds if rss_feeds is not None else DEFAULT_RSS_FEEDS)
        self.timespan_hours = max(1, int(timespan_hours))
        self.timeout = timeout
        self.gdelt_maxrecords = int(gdelt_maxrecords)
        self.gdelt_retries = max(0, int(gdelt_retries))
        self.rss_retries = max(0, int(rss_retries))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (TRADE monitor; readonly)"})

    # ---------------- GDELT ----------------
    def fetch_gdelt(self) -> list[NewsItem]:
        params = {
            "query": self.gdelt_query,
            "mode": "artlist",
            "maxrecords": str(self.gdelt_maxrecords),
            "format": "json",
            "sort": "datedesc",
            "timespan": f"{self.timespan_hours}h",
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        last_exc: Exception | None = None
        for attempt in range(self.gdelt_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                break  # 网络不通就别等了，直接走 RSS
            if resp.status_code == 429:
                last_exc = RuntimeError(f"GDELT 429 限流（第 {attempt + 1} 次）")
                if attempt < self.gdelt_retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            resp.raise_for_status()
            try:
                body = resp.json()
            except ValueError as exc:
                raise RuntimeError("GDELT 返回非 JSON") from exc
            return [
                item for item in (self._parse_gdelt_article(a) for a in body.get("articles", []) or [])
                if item is not None
            ]
        raise RuntimeError(f"GDELT 取数失败：{last_exc}")

    def _parse_gdelt_article(self, article: dict) -> NewsItem | None:
        title = str(article.get("title") or "").strip()
        url = str(article.get("url") or "").strip()
        if not title or not url:
            return None
        return NewsItem(
            title=title,
            url=url,
            source=str(article.get("domain") or urlsplit(url).netloc),
            published_ts=_parse_gdelt_time(article.get("seendate")),
            summary="",  # DOC artlist 模式不提供正文摘要，避免拿图片字段冒充
            via="gdelt",
        )

    # ---------------- RSS ----------------
    def fetch_rss(self, feed_url: str) -> list[NewsItem]:
        last_exc: Exception | None = None
        for _ in range(self.rss_retries + 1):
            try:
                resp = self.session.get(feed_url, timeout=self.timeout)
                resp.raise_for_status()
                return self.parse_feed_bytes(resp.content, feed_url)
            except (requests.RequestException, ET.ParseError, RuntimeError) as exc:
                last_exc = exc
        log.warning("RSS 取数失败 %s：%s", feed_url, last_exc)
        return []

    def parse_feed_bytes(self, content: bytes, feed_url: str = "") -> list[NewsItem]:
        """解析 RSS 2.0 / Atom。纯函数式解析，坏条目跳过、绝不抛异常给调用方。"""
        root = ET.fromstring(content)
        items: list[NewsItem] = []
        channel_title = feed_url

        # RSS 2.0
        for channel in root.findall("channel"):
            label = channel.findtext("title")
            if label:
                channel_title = label.strip()
            for node in channel.findall("item"):
                parsed = self._parse_rss_item(node, channel_title)
                if parsed is not None:
                    items.append(parsed)

        # Atom（少见，兜底支持）
        if not items:
            entries = root.findall(f"{_ATOM_NS}entry")
            feed_label = root.findtext(f"{_ATOM_NS}title") or channel_title
            for node in entries:
                parsed = self._parse_atom_entry(node, str(feed_label))
                if parsed is not None:
                    items.append(parsed)
        return items

    def _parse_rss_item(self, node: ET.Element, source: str) -> NewsItem | None:
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        if not title or not url:
            return None
        return NewsItem(
            title=title,
            url=url,
            source=(node.findtext("source") or source or urlsplit(url).netloc).strip(),
            published_ts=_parse_rfc822(node.findtext("pubDate")),
            summary=_clean_html(node.findtext("description") or ""),
            via=f"rss:{source[:20]}",
        )

    def _parse_atom_entry(self, node: ET.Element, source: str) -> NewsItem | None:
        title = (node.findtext(f"{_ATOM_NS}title") or "").strip()
        link_node = node.find(f"{_ATOM_NS}link")
        url = link_node.get("href", "") if link_node is not None else ""
        if not title or not url:
            return None
        summary = node.findtext(f"{_ATOM_NS}summary") or node.findtext(f"{_ATOM_NS}content") or ""
        ts = _parse_iso8601(
            node.findtext(f"{_ATOM_NS}published")
            or node.findtext(f"{_ATOM_NS}updated")
        )
        return NewsItem(
            title=title, url=url, source=source,
            published_ts=ts, summary=_clean_html(summary), via="rss:atom",
        )

    # ---------------- 合并 ----------------
    def fetch_all(self) -> tuple[list[NewsItem], str]:
        """GDELT 主，RSS 辅。返回 (合并去重后的新闻, 使用了哪些源说明)。"""
        merged: list[NewsItem] = []
        sources: list[str] = []
        try:
            gdelt_items = self.fetch_gdelt()
            if gdelt_items:
                merged.extend(gdelt_items)
                sources.append("gdelt")
        except Exception as exc:
            log.warning("GDELT 不可用，改用 RSS 兜底：%s", exc)

        for feed in self.rss_feeds:
            feed_items = self.fetch_rss(feed)
            if feed_items:
                merged.extend(feed_items)
                sources.append(feed)

        if not merged:
            raise RuntimeError("GDELT 与全部 RSS 源均未取到新闻")

        cutoff = time.time() - self.timespan_hours * 3600
        dedup: dict[str, NewsItem] = {}
        for item in merged:
            if item.published_ts is not None and item.published_ts < cutoff:
                continue
            key = normalize_url(item.url)
            # GDELT 优先（已按时间倒序），RSS 只补 GDELT 没有的
            if key not in dedup:
                dedup[key] = item
        items = sorted(dedup.values(),
                       key=lambda x: x.published_ts or 0.0, reverse=True)
        return items, "+".join(sources) or "none"


def _parse_gdelt_time(value: Any) -> float | None:
    """GDELT seendate 形如 20261015T123000Z。"""
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _parse_rfc822(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def _parse_iso8601(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _clean_html(raw: str, limit: int = 280) -> str:
    """RSS description 去标签。world 模块有同名纯函数，但数据源层不依赖业务模块。"""
    import re
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]

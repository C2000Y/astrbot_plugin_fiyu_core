"""Fixed public research sources, bounded parsing and safe diagnostic labels."""
from __future__ import annotations

import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

from .util import clean, html_text, now, stamp

STEAM_NEWS = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_RSS = {app: f"https://store.steampowered.com/feeds/news/app/{app}/" for app in (570, 730)}
PRO_LIST = "https://api.opendota.com/api/proMatches"
PRO_MATCH = "https://api.opendota.com/api/matches/"
ENDPOINTS = {
    "steam_rss": "Steam 官方 RSS",
    "steam_api": "Steam 新闻 API",
    "opendota_pro": "OpenDota 职业比赛",
    "local_meta": "本地胜率快照",
}
SOURCE_NAMES = {
    "dota_news": "Dota 2 官方公告与版本动态",
    "cs2_news": "CS2 官方公告与版本/赛事动态",
    "dota_pro": "OpenDota 近期记录的职业比赛，最多两场阵容样本",
    "dota_meta": "现有已校验 OpenDota 英雄公开比赛胜率快照",
}
NEWS_SOURCES = {"dota_news": 570, "cs2_news": 730}
HEALTH_KEY = "research_source_health_v1"
GID = re.compile(r"[1-9]\d{0,22}")


class SourceUnavailable(RuntimeError):
    """A bounded, user-readable data acquisition failure, not a model failure."""

    def __init__(self, reason, *, empty=False):
        self.reason = reason
        self.empty = empty
        super().__init__(reason)


class HTTPFailure(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class RequestBudget:
    def __init__(self, limit):
        self.limit = int(limit)
        self.used = 0
        self.guard = None

    @property
    def left(self):
        return max(0, self.limit - self.used)

    def take(self):
        if not self.left:
            raise SourceUnavailable("公开接口请求预算已用完")
        self.used += 1
        return self.used


def error_info(exc):
    """Never serialize raw exception strings (URLs, secrets or response body)."""
    if isinstance(exc, HTTPFailure):
        return f"HTTP {exc.status}", exc.status in (408, 500, 502, 503, 504)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "TimeoutError（请求超时）", True
    if isinstance(exc, ConnectionResetError):
        return "ConnectionResetError（连接被重置）", True
    if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, OSError)):
        return type(exc).__name__ + "（网络连接或传输失败）", True
    if isinstance(exc, (json.JSONDecodeError, ET.ParseError, UnicodeError, ValueError, KeyError, TypeError, AttributeError)):
        return type(exc).__name__ + "（资料格式或内容无效）", False
    return type(exc).__name__, False


def text_excerpt(value, limit):
    # RSS XML already unescapes the XML layer; the description contains HTML.
    text = html_text(html.unescape(str(value or "")))
    text = re.sub(r"\[[/?a-zA-Z][^\]\n]{0,250}\]", " ", text)
    return clean(" ".join(text.split()), limit)


def _record(appid, gid, ts, title, description, transport):
    if not GID.fullmatch(gid) or type(ts) is not int or not (0 < ts <= now() + 300):
        return None
    if now() - ts > 90 * 86400:
        return None
    title = text_excerpt(title, 150)
    body = text_excerpt(description, 650)
    if not title or not body:
        return None
    return {
        # The same announcement has the same identity on RSS and JSON.
        "key": f"steam:{appid}:{gid}",
        "title": title,
        "date": stamp(ts),
        "url": f"https://store.steampowered.com/news/app/{appid}/view/{gid}",
        "scope": ("Steam 官方公告 RSS（摘录）" if transport == "steam_rss" else
                  "Steam 游戏官方公告频道（标题和摘要）"),
        "text": body,
    }


def _recent_unique(rows):
    out, seen = [], set()
    for row in sorted(rows, key=lambda r: r["date"], reverse=True):
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        out.append(row)
    return out[:4]


def parse_news_json(data, appid):
    news = data.get("appnews", {}) if isinstance(data, dict) else {}
    if news.get("appid") != appid or not isinstance(news.get("newsitems"), list):
        raise ValueError("公告数据不匹配")
    rows = []
    for row in news["newsitems"][:8]:
        if not isinstance(row, dict) or row.get("feedname") != "steam_community_announcements":
            continue
        result = _record(appid, str(row.get("gid", "")), row.get("date"),
                         row.get("title"), row.get("contents"), "steam_api")
        if result:
            rows.append(result)
    return _recent_unique(rows)


def _store_path(value):
    """The only accepted article origin is the requested official Steam app."""
    try:
        p = urlsplit(str(value or "").strip())
        if (p.scheme == "https" and p.netloc.lower() == "store.steampowered.com"
                and not p.fragment):
            return p.path.rstrip("/")
    except ValueError:
        pass
    return ""


def parse_news_rss(data, appid):
    if not isinstance(data, (str, bytes)):
        raise ValueError("RSS 响应不是 XML")
    if isinstance(data, bytes):
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("RSS 数据超限")
        data = data.decode("utf-8-sig")
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("RSS 数据超限")
    # Do not allow DTDs/entities in an external feed. No external resources
    # or server-provided URLs are followed when parsing a news item.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("RSS 包含不允许的声明")
    root = ET.fromstring(data)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None or _store_path(channel.findtext("link")) != f"/news/app/{appid}":
        raise ValueError("RSS 公告频道与游戏不匹配")
    articles = channel.findall("item")[:40]
    rows = []
    identified = 0
    for item in articles:
        ids = set()
        for name in ("link", "guid"):
            match = re.fullmatch(r"/news/app/"+str(appid)+r"/view/([1-9]\d{0,22})",
                                 _store_path(item.findtext(name)))
            if match:
                ids.add(match[1])
        if len(ids) != 1:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                continue
            ts = int(published.timestamp())
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        identified += 1
        result = _record(appid, next(iter(ids)), ts, item.findtext("title"),
                         item.findtext("description"), "steam_rss")
        if result:
            rows.append(result)
    if articles and not identified:
        raise ValueError("RSS 公告缺少可验证的文章链接或时间")
    return _recent_unique(rows)

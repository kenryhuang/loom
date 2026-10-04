"""Source requirements for tasks that explicitly ask to read a URL."""

import re
from urllib.parse import urlsplit, urlunsplit


def source_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def requested_source_urls(objective):
    if not re.search(r"\b(?:read|fetch|retrieve|visit|browse|summarize|summarise)\b|读取|抓取|获取|阅读|访问|浏览|根据", objective, re.I):
        return ()
    return tuple(dict.fromkeys(source_url(url.rstrip(".,;:!?)]}、")) for url in re.findall(r"https?://[^\s<>\"'，。；]+", objective)))

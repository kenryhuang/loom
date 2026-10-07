"""Bounded local HTTP crawler with pinned public addresses and provenance."""

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from collections import deque
from contextlib import suppress
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from loom.service.contracts import ServiceError

USER_AGENT = "LoomKnowledge/1.0"
MAX_BYTES = 4_000_000


def normalize(url):
    if not isinstance(url, str) or len(url) > 4000:
        raise ServiceError("Website URL must be text of at most 4000 characters")
    try:
        parts = urlsplit(urldefrag(url)[0])
    except ValueError as exc:
        raise ServiceError("Invalid website URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ServiceError("Website URL must be HTTP(S), without credentials")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ServiceError("Invalid website port") from exc
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ServiceError("Invalid website hostname") from exc
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc,
                       quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                       quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"), ""))


def public_addresses(host, port):
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise ServiceError("Website hostname could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ServiceError("Website crawling requires public addresses; local/private networks are not allowed")
    return addresses


def redirect_target(url, location):
    # http.client exposes header bytes as Latin-1; some sites send raw UTF-8 in Location.
    with suppress(UnicodeEncodeError, UnicodeDecodeError):
        location = location.encode("latin-1").decode("utf-8")
    return normalize(urljoin(url, location))


def _fetch(url, *, scope=None, address_index=0, deadline=None):
    for _ in range(6):
        url = normalize(url)
        if scope and not scope(url):
            raise ServiceError("Redirect leaves the configured website scope")
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = public_addresses(parts.hostname, port)
        address = addresses[address_index % len(addresses)]
        timeout = min(20, deadline - time.monotonic()) if deadline else 20
        if timeout <= 0:
            raise ServiceError("Website request deadline reached") from TimeoutError()
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
            conn.sock = sock
            if parts.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
            conn.sock = sock  # Connect to the validated address, preserving Host and TLS hostname.
            conn.request("GET", urlunsplit(("", "", parts.path or "/", parts.query, "")),
                         headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ServiceError("Website redirect has no destination")
                url = redirect_target(url, location)
                continue
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ServiceError("Website page exceeds 4 MB")
            charset = response.headers.get_content_charset() or "utf-8"
            return {"url": url, "status": response.status, "type": response.getheader("Content-Type", ""),
                    "text": body.decode(charset, errors="replace")}
        except (OSError, LookupError, http.client.HTTPException) as exc:
            raise ServiceError(f"Website request failed ({type(exc).__name__})") from exc
        finally:
            conn.close()
    raise ServiceError("Too many website redirects")


def fetch(url, *, scope=None, cancelled=lambda: False, progress=lambda **_: None):
    deadline = time.monotonic() + 45
    for attempt in range(3):
        if cancelled():
            raise ServiceError("Knowledge job cancelled", 409)
        try:
            reply = _fetch(url, scope=scope, address_index=attempt, deadline=deadline)
            if reply["status"] not in {429, 500, 502, 503, 504} or attempt == 2:
                return reply
        except ServiceError as exc:
            if not isinstance(exc.__cause__, (TimeoutError, ConnectionError, http.client.RemoteDisconnected)) or attempt == 2:
                raise
        if time.monotonic() >= deadline:
            raise ServiceError("Website request deadline reached") from TimeoutError()
        progress(stage="crawl_retry", current_url=url, retry=attempt + 1)
        until = min(deadline, time.monotonic() + 2 ** attempt)
        while time.monotonic() < until:
            if cancelled():
                raise ServiceError("Knowledge job cancelled", 409)
            time.sleep(0.05)


def extract(html, url):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    links = [urljoin(url, a["href"]) for a in soup.select("a[href]")]
    for node in soup.select("script,style,noscript,nav,header,footer,aside,form,svg"):
        node.decompose()
    root = soup.select_one("main,article,[role=main]") or soup.body or soup
    for heading in root.select("h1,h2,h3,h4,h5,h6"):
        heading.insert_before("\n" + "#" * int(heading.name[1]) + " ")
    for pre in root.select("pre"):
        pre.insert_before("\n```\n")
        pre.insert_after("\n```\n")
    for a in root.select("a[href]"):
        a.replace_with(f"[{a.get_text(' ', strip=True)}]({urljoin(url, a['href'])})")
    content = root.get_text("\n", strip=True)
    return title, content, links


def crawl(source, progress=lambda **_: None, cancelled=lambda: False, *, fetcher=fetch, resume=None, checkpoint=lambda _: None):
    start = normalize(source["url"])
    origin = urlsplit(start)
    prefix = quote(source["path_prefix"].rstrip("/"), safe="/%:@!$&'()*+,;=-._~")

    def scope(url):
        p = urlsplit(url)
        return (p.scheme, p.netloc) == (origin.scheme, origin.netloc) and (not prefix or p.path == prefix or p.path.startswith(prefix + "/"))

    robots_url = urlunsplit((origin.scheme, origin.netloc, "/robots.txt", "", ""))
    robots_reply = fetcher(robots_url, scope=lambda u: urlsplit(u).netloc == origin.netloc, cancelled=cancelled, progress=progress)
    robot = RobotFileParser()
    if robots_reply["status"] == 404:
        robot.parse([])
    elif robots_reply["status"] == 200:
        robot.parse(robots_reply["text"].splitlines())
    else:
        raise ServiceError("Could not read robots.txt; website sync was not started")
    delay = max(source.get("delay_seconds", 0.3), robot.crawl_delay(USER_AGENT) or robot.crawl_delay("*") or 0)
    if delay > 60:
        raise ServiceError("Website crawl delay exceeds 60 seconds")
    pending, seen, pages, errors, skipped = deque([(start, 0)]), set(), [], [], []
    complete = True
    total_bytes = 0
    missing, unsupported, duplicates, attempted, reasons = [], [], 0, 0, set()
    if resume:
        pending, seen = deque(resume["pending"]), set(resume["seen"])
        pages, errors, skipped = resume["pages"], resume["errors"], resume["skipped"]
        complete, total_bytes = resume["complete"], resume["total_bytes"]
        missing, unsupported = resume["missing"], resume["unsupported"]
        duplicates, attempted, reasons = resume["duplicates"], resume["attempted"], set(resume["reasons"])
        progress(stage="resuming_crawl", pages=len(pages), visited=attempted)
    last = time.monotonic()
    while pending:
        checkpoint({"pending": list(pending), "seen": sorted(seen), "pages": pages, "errors": errors,
                    "skipped": skipped, "complete": complete, "total_bytes": total_bytes, "missing": missing,
                    "unsupported": unsupported, "duplicates": duplicates, "attempted": attempted, "reasons": sorted(reasons)})
        if cancelled():
            raise ServiceError("Knowledge job cancelled", 409)
        url, depth = pending.popleft()
        if url in seen:
            continue
        if attempted >= source["max_pages"]:
            pending.appendleft((url, depth))
            complete = False
            reasons.add("page_limit")
            break
        seen.add(url)
        attempted += 1
        if not robot.can_fetch(USER_AGENT, url):
            skipped.append(url)
            reasons.add("robots")
            complete = False
            continue
        while time.monotonic() - last < delay:
            if cancelled():
                raise ServiceError("Knowledge job cancelled", 409)
            time.sleep(0.05)
        last = time.monotonic()
        progress(stage="crawling", visited=attempted, discovered=len(seen) + len(pending), pages=len(pages), current_url=url,
                 missing_count=len(missing), request_errors=len(errors))
        try:
            reply = fetcher(url, scope=scope, cancelled=cancelled, progress=progress)
            if reply["status"] in {404, 410}:
                missing.append({"url": url, "status": reply["status"]})
                continue
            if reply["status"] != 200:
                raise ServiceError(f"HTTP {reply['status']}")
            if "html" in reply["type"]:
                title, content, links = extract(reply["text"], reply["url"])
            elif any(t in reply["type"] for t in ("text/plain", "text/markdown")):
                title, content, links = url, reply["text"], []
            else:
                unsupported.append(url)
                continue
            if not content.strip():
                raise ServiceError("No readable content; client-rendered sites need exported HTML or Markdown")
            if len(content) > 500000:
                raise ServiceError("Extracted page exceeds 500,000 characters")
            total_bytes += len(content.encode())
            if total_bytes > 50_000_000:
                reasons.add("snapshot_limit")
                errors.append({"url": url, "error": "Website snapshot exceeds 50 MB"})
                complete = False
                break
            canonical_url = reply["url"]
            seen.add(canonical_url)
            if not any(p["url"] == canonical_url for p in pages):
                pages.append({"url": canonical_url, "title": title, "content": content, "digest": hashlib.sha256(content.encode()).hexdigest()})
            else:
                duplicates += 1
            for link in links:
                try:
                    link = normalize(link)
                except (ServiceError, ValueError):
                    continue
                if not scope(link) or link in seen or any(link == u for u, _ in pending):
                    continue
                if urlsplit(link).query:  # Avoid unbounded filter/search/calendar URL spaces.
                    continue
                if depth < source["max_depth"] and len(pending) < 2000:
                    pending.append((link, depth + 1))
                else:
                    reasons.add("depth_limit" if depth >= source["max_depth"] else "queue_limit")
                    complete = False
        except ServiceError as exc:
            if cancelled():
                raise
            reasons.add("request_errors")
            errors.append({"url": url, "error": str(exc)})
            complete = False
    if not pages:
        raise ServiceError("No readable pages found. " + (errors[0]["error"] if errors else "Check scope and robots.txt rules."))
    return {"pages": pages, "complete": complete, "errors": errors, "skipped": skipped, "visited": attempted,
            "missing": missing, "unsupported": unsupported, "duplicates": duplicates, "pending": len(pending), "incomplete_reasons": sorted(reasons)}

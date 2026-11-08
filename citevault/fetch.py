"""抓取远程页面或读取本地文件。"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import ipaddress
import random
import re
import socket
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "citevault/0.9"
MAX_BYTES = 5_000_000

ALLOWED_SCHEMES = {"http", "https"}
RETRY_STATUSES = {502, 503, 504}

# 这些格式无法直接当文本比对，明确拒绝而不是静默产生垃圾记录
UNSUPPORTED_SUFFIXES = {".doc", ".docx", ".epub", ".zip"}

# 云厂商元数据地址，任何情况下都不访问
METADATA_HOSTS = {"169.254.169.254"}

_ROBOTS_CACHE: dict = {}
_ROBOTS_LOCK = threading.Lock()


class FetchError(Exception):
    """网络、URL 校验或文件读取失败。"""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str
    content_hash: str
    content_type: str = ""
    raw: bytes = b""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.IGNORECASE)


def _sniff_charset(raw: bytes) -> Optional[str]:
    """从页面开头的 meta 标签里猜编码，兼容没有在响应头声明编码的旧站点。"""
    match = _META_CHARSET_RE.search(raw[:4096])
    if not match:
        return None
    return match.group(1).decode("ascii", "ignore")


def _decode(raw: bytes, charset: Optional[str]) -> str:
    """尝试多个编码，选替换字符最少的结果。"""
    candidates = []
    for candidate in (charset, _sniff_charset(raw), "utf-8"):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    best_text: Optional[str] = None
    best_bad: Optional[int] = None
    for candidate in candidates:
        try:
            text = raw.decode(candidate, errors="replace")
        except LookupError:
            continue
        bad = text.count("\ufffd")
        if best_bad is None or bad < best_bad:
            best_text, best_bad = text, bad
    if best_text is not None:
        return best_text
    return raw.decode("utf-8", errors="replace")


def _is_private_ip(address: ipaddress._BaseAddress) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    )


def _is_private_host(host: str) -> bool:
    if not host:
        return True
    if host.lower() in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    try:
        address = ipaddress.ip_address(host)
        return _is_private_ip(address)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return any(_is_private_ip(address) for address in addresses)


def validate_url(url: str, block_private: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError(f"只允许 http/https，拒绝: {url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise FetchError(f"URL 缺少主机: {url}")
    if host in METADATA_HOSTS:
        raise FetchError("拒绝访问云元数据地址")
    if parsed.username or parsed.password:
        raise FetchError("URL 不允许包含用户名或密码")
    if block_private and _is_private_host(host):
        raise FetchError(f"--block-private 拒绝本地/私有地址: {host}")


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _parse_robots(text: str) -> tuple:
    rules = []
    crawl_delay = 0.0
    applies = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            applies = value == "*" or value.lower() in USER_AGENT.lower()
        elif applies and key == "crawl-delay":
            try:
                crawl_delay = max(0.0, min(60.0, float(value)))
            except ValueError:
                pass
        elif applies and key in {"allow", "disallow"} and value:
            rules.append((key == "allow", value))
    return rules, crawl_delay


def _fetch_robots(base: str, timeout: float) -> tuple:
    url = base + "/robots.txt"
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=min(timeout, 5.0)) as response:
            if response.status >= 400:
                return [], 0.0
            text = response.read(MAX_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return [], 0.0
    return _parse_robots(text)


def _pattern_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("$"):
        return fnmatch.fnmatchcase(path, pattern[:-1])
    if "*" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return path.startswith(pattern)


def _match_robots(rules: list, path: str) -> bool:
    best_length = -1
    allowed = True
    for allow, pattern in rules:
        if _pattern_matches(pattern, path) and len(pattern) > best_length:
            best_length = len(pattern)
            allowed = allow
    return allowed


def _robots_allowed(url: str, timeout: float) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    # 本地/内网地址通常没有 robots.txt，也避免测试环境多一次请求
    if not host or _is_private_host(host):
        return True
    base = f"{parsed.scheme}://{parsed.netloc}"
    with _ROBOTS_LOCK:
        entry = _ROBOTS_CACHE.get(base)
    if entry is None:
        rules, crawl_delay = _fetch_robots(base, timeout)
        with _ROBOTS_LOCK:
            entry = _ROBOTS_CACHE.setdefault(
                base, {"rules": rules, "delay": crawl_delay, "next": 0.0}
            )

    path = parsed.path or "/"
    allowed = _match_robots(entry["rules"], path)

    delay = float(entry.get("delay") or 0.0)
    if allowed and delay > 0:
        with _ROBOTS_LOCK:
            now = time.monotonic()
            start = max(now, entry.get("next", 0.0))
            entry["next"] = start + delay
        pause = start - time.monotonic()
        if pause > 0:
            time.sleep(pause)
    return allowed


def _decompress(raw: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _sleep_backoff(attempt: int) -> None:
    time.sleep(min(4.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.2))


def fetch_url(
    url: str,
    timeout: float = 15.0,
    block_private: bool = False,
    retries: int = 2,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    respect_robots: bool = True,
) -> FetchResult:
    """抓取 URL。HTTP 4xx/5xx 不抛异常，交给调用方判断状态。"""
    validate_url(url, block_private)
    if respect_robots and not _robots_allowed(url, timeout):
        raise FetchError(f"robots.txt 不允许抓取: {url}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(url, headers=headers)

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise FetchError(f"页面超过 {MAX_BYTES} 字节上限")
                try:
                    raw = _decompress(
                        raw,
                        (response.headers.get("Content-Encoding", "") or "").lower(),
                    )
                except (OSError, EOFError, zlib.error) as exc:
                    raise FetchError(f"内容解压失败: {exc}") from exc
                if len(raw) > MAX_BYTES:
                    raise FetchError(f"解压后超过 {MAX_BYTES} 字节上限")
                return FetchResult(
                    url=url,
                    final_url=response.geturl(),
                    status=getattr(response, "status", 200),
                    html=_decode(raw, response.headers.get_content_charset()),
                    content_hash=_sha256_bytes(raw),
                    content_type=response.headers.get_content_type(),
                    raw=raw,
                    etag=response.headers.get("ETag", "") or "",
                    last_modified=response.headers.get("Last-Modified", "") or "",
                )
        except HTTPError as exc:
            code = exc.code
            final_url = exc.geturl() or url
            response_etag = exc.headers.get("ETag", "") if exc.headers else ""
            response_last_modified = (
                exc.headers.get("Last-Modified", "") if exc.headers else ""
            )
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            if code == 304:
                return FetchResult(
                    url=url,
                    final_url=final_url,
                    status=304,
                    html="",
                    content_hash="",
                    content_type="",
                    raw=b"",
                    etag=response_etag or (etag or ""),
                    last_modified=response_last_modified or (last_modified or ""),
                    not_modified=True,
                )
            if code == 429 and attempt < retries:
                wait = _parse_retry_after(retry_after)
                if wait is None:
                    _sleep_backoff(attempt)
                else:
                    time.sleep(min(wait, 30.0))
                continue
            if code in RETRY_STATUSES and attempt < retries:
                _sleep_backoff(attempt)
                continue
            return FetchResult(
                url=url,
                final_url=final_url,
                status=code,
                html="",
                content_hash="",
                content_type="",
                raw=b"",
            )
        except (URLError, socket.timeout, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            raise FetchError(f"抓取失败: {exc}") from exc

    raise FetchError(f"抓取失败: {last_error}")


def read_local_file(path: str) -> FetchResult:
    file_path = Path(path)
    if not file_path.is_file():
        raise FetchError(f"文件不存在: {path}")
    suffix = file_path.suffix.lower()
    if suffix in UNSUPPORTED_SUFFIXES:
        raise FetchError(f"暂不支持 {suffix} 文件，请先转换为 HTML")
    raw = file_path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise FetchError(f"文件超过 {MAX_BYTES} 字节上限")
    if suffix == ".pdf":
        return FetchResult(
            url=str(file_path.resolve()),
            final_url=file_path.resolve().as_uri(),
            status=200,
            html="",
            content_hash=_sha256_bytes(raw),
            content_type="application/pdf",
            raw=raw,
        )
    return FetchResult(
        url=str(file_path.resolve()),
        final_url=file_path.resolve().as_uri(),
        status=200,
        html=_decode(raw, None),
        content_hash=_sha256_bytes(raw),
        content_type="text/plain" if suffix == ".txt" else "text/html",
        raw=raw,
    )

"""存证与再核验的核心逻辑。"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .extract import contains_quote, extract_title, html_to_text, normalize_quote
from .fetch import FetchError, FetchResult, fetch_url, read_local_file
from .pdf import PdfError, extract_pdf_text
from .store import CheckRecord, Evidence, Vault

STATUS_OK = "ok"
STATUS_CHANGED = "changed"
STATUS_QUOTE_MISSING = "quote_missing"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_ERROR = "error"

FAILING_STATUSES = {
    STATUS_QUOTE_MISSING,
    STATUS_UNAVAILABLE,
    STATUS_INCONCLUSIVE,
    STATUS_ERROR,
}

HTML_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "text/plain",
}

PDF_CONTENT_TYPES = {"application/pdf"}

PAYWALL_MARKERS = (
    "enable javascript",
    "sign in to continue",
    "subscribe to continue",
    "log in to continue",
    "请先登录",
    "登录后继续",
    "访问受限",
    "该内容仅限",
)


class AlreadyExists(Exception):
    """同一 claim + quote + 来源已经存证过。"""


class QuoteNotFound(Exception):
    """引用片段没有出现在页面正文里。"""


class EvidenceNotFound(Exception):
    """存证 ID 不存在。"""


@dataclass
class ScanResult:
    index: int
    claim: str
    evidence_id: str
    action: str
    status: str
    detail: str
    diff: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_id(url: str, claim: str, quote: str) -> str:
    payload = f"{url}\n{claim}\n{normalize_quote(quote)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _looks_unfetchable(text: str, expected_length: int) -> bool:
    """只在明确登录墙或极端缩水时判为“没抓到”，避免把真实删除误报。"""
    lowered = text.lower()
    if any(marker in lowered for marker in PAYWALL_MARKERS):
        return True
    if expected_length >= 1000 and len(text) < int(expected_length * 0.1):
        return True
    return False


def _host_changed(old_url: str, new_url: str) -> bool:
    old_host = urlparse(old_url).netloc.lower()
    new_host = urlparse(new_url).netloc.lower()
    return bool(old_host and new_host and old_host != new_host)


def _split_sentences(text: str) -> List[str]:
    # 小数点后不跟空格时不切，避免把 3.5 拆成 3. / 5
    return [
        part.strip()
        for part in re.split(r"(?<=[。！？])\s*|(?<=[.!?])(?=\s)", text)
        if part.strip()
    ]


def _text_diff(old_text: str, new_text: str, limit: int = 400) -> str:
    lines = list(difflib.unified_diff(
        _split_sentences(old_text),
        _split_sentences(new_text),
        lineterm="",
        n=1,
    ))
    body = lines[2:] if len(lines) > 2 else lines
    text = "\n".join(body)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _diff_from_snapshot(item: Evidence, new_text: str) -> str:
    snapshot = Path(item.snapshot)
    text_path = snapshot.with_suffix(".txt")
    if text_path.is_file():
        old_text = text_path.read_text(encoding="utf-8")
    elif snapshot.is_file():
        old_text = html_to_text(snapshot.read_text(encoding="utf-8"))
    else:
        return ""
    return _text_diff(old_text, new_text)


def _result_text(result: FetchResult) -> str:
    if result.content_type == "application/pdf":
        return extract_pdf_text(result.raw)
    return html_to_text(result.html)


class DomainLimiter:
    """按域名控制最小请求间隔，避免并发时把同一站点打爆。"""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._next = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.delay <= 0:
            return
        host = urlparse(url).netloc.lower()
        if not host:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + self.delay
        pause = start - time.monotonic()
        if pause > 0:
            time.sleep(pause)


def _unlink(paths: List[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def capture(
    vault: Vault,
    claim: str,
    quote: str,
    url: Optional[str] = None,
    file: Optional[str] = None,
    note: str = "",
    timeout: float = 15.0,
    allow_missing_quote: bool = False,
    block_private: bool = False,
    limiter: Optional[DomainLimiter] = None,
    update_id: Optional[str] = None,
) -> Evidence:
    """抓取来源、校验引用片段、保存快照与哈希。

    update_id 用于更新已有记录：保留 ID 与核查历史，覆盖证据内容。
    """
    if url and file:
        raise ValueError("--url 与 --file 只能选一个")
    if not url and not file:
        raise ValueError("需要 --url 或 --file")

    if file:
        result = read_local_file(file)
        source_type = "file"
        target = result.url
    else:
        if limiter:
            limiter.wait(url)
        result = fetch_url(url, timeout=timeout, block_private=block_private)
        source_type = "url"
        target = url

    if result.status >= 400:
        raise FetchError(f"HTTP {result.status}: {target}")
    if result.content_type and result.content_type not in (
        HTML_CONTENT_TYPES | PDF_CONTENT_TYPES
    ):
        raise FetchError(f"来源类型 {result.content_type} 暂不支持")

    try:
        text = _result_text(result)
    except PdfError as exc:
        raise FetchError(str(exc)) from exc

    if not contains_quote(text, quote) and not allow_missing_quote:
        raise QuoteNotFound(
            "引用片段没有出现在页面正文里；"
            "如果页面是 JS 动态渲染，可先用 --allow-missing-quote 存证"
        )

    if update_id:
        old_item = vault.get(update_id)
        if not old_item:
            raise EvidenceNotFound(f"找不到 {update_id}")
        evidence_id = update_id
    else:
        old_item = None
        evidence_id = make_id(target, claim, quote)
        if vault.get(evidence_id):
            raise AlreadyExists(f"{evidence_id} 已存在")

    is_pdf = result.content_type == "application/pdf"
    suffix = ".pdf" if is_pdf else ".html"
    snapshot_path = vault.root / "snapshots" / f"{evidence_id}{suffix}"
    text_path = vault.root / "snapshots" / f"{evidence_id}.txt"
    raw_file = vault.root / "snapshots" / f"{evidence_id}.raw"
    final_paths = [snapshot_path, text_path] if is_pdf else [snapshot_path, text_path, raw_file]

    backups: dict = {}
    if old_item:
        for path in final_paths:
            if path.exists():
                backup = path.with_name(path.name + ".bak")
                shutil.copy2(str(path), str(backup))
                backups[path] = backup

    temporary: List[Path] = []
    snapshot_tmp = snapshot_path.with_name(snapshot_path.name + ".tmp")
    text_tmp = text_path.with_name(text_path.name + ".tmp")
    temporary.append(snapshot_tmp)
    temporary.append(text_tmp)

    try:
        if is_pdf:
            snapshot_tmp.write_bytes(result.raw)
            raw_path_value = ""
        else:
            snapshot_tmp.write_text(result.html, encoding="utf-8")
            raw_tmp = raw_file.with_name(raw_file.name + ".tmp")
            temporary.append(raw_tmp)
            raw_tmp.write_bytes(result.raw)
            raw_path_value = str(raw_file)
        text_tmp.write_text(text, encoding="utf-8")

        item = Evidence(
            id=evidence_id,
            url=target,
            final_url=result.final_url,
            source_type=source_type,
            claim=claim.strip(),
            quote=quote.strip(),
            title=extract_title(result.html) or target,
            note=note.strip(),
            captured_at=_now(),
            status_code=result.status,
            content_hash=result.content_hash,
            text_hash=_sha256_text(text),
            text_length=len(text),
            snapshot=str(snapshot_path),
            raw_snapshot=raw_path_value,
            etag=result.etag,
            last_modified=result.last_modified,
            allow_missing=int(allow_missing_quote),
        )
        try:
            if update_id:
                vault.update_evidence(item)
            else:
                vault.insert_evidence(item)
        except sqlite3.IntegrityError as exc:
            raise AlreadyExists(f"{evidence_id} 已存在") from exc

        try:
            os.replace(str(snapshot_tmp), str(snapshot_path))
            os.replace(str(text_tmp), str(text_path))
            if not is_pdf:
                os.replace(str(raw_file.with_name(raw_file.name + ".tmp")), str(raw_file))
        except Exception:
            for path, backup in backups.items():
                os.replace(str(backup), str(path))
            if old_item:
                vault.update_evidence(old_item)
            else:
                vault.delete_evidence(evidence_id)
                _unlink(final_paths)
            raise

        for backup in backups.values():
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        if old_item:
            old_paths = {Path(old_item.snapshot), Path(old_item.snapshot).with_suffix(".txt")}
            if old_item.raw_snapshot:
                old_paths.add(Path(old_item.raw_snapshot))
            for path in old_paths - set(final_paths):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
    except Exception:
        _unlink(temporary)
        for backup in backups.values():
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        raise

    return item


def remove_evidence(vault: Vault, evidence_id: str) -> Evidence:
    item = vault.get(evidence_id)
    if not item:
        raise EvidenceNotFound(f"找不到 {evidence_id}")
    paths = [Path(item.snapshot), Path(item.snapshot).with_suffix(".txt")]
    if item.raw_snapshot:
        paths.append(Path(item.raw_snapshot))
    _unlink(paths)
    vault.delete_evidence(evidence_id)
    return item


def _error_record(vault: Vault, evidence_id: str, detail: str) -> CheckRecord:
    record = CheckRecord(
        evidence_id=evidence_id,
        checked_at=_now(),
        status=STATUS_ERROR,
        http_status=0,
        content_hash="",
        quote_found=False,
        detail=detail,
    )
    vault.insert_check(record)
    return record


def check_one(
    vault: Vault,
    evidence_id: str,
    timeout: float = 15.0,
    block_private: bool = False,
    limiter: Optional[DomainLimiter] = None,
) -> CheckRecord:
    """重新抓取并判断：页面是否还在、引用片段是否还在、正文是否变化。"""
    item = vault.get(evidence_id)
    if not item:
        raise EvidenceNotFound(f"找不到 {evidence_id}")

    try:
        if item.source_type == "file":
            result = read_local_file(item.url)
        else:
            if limiter:
                limiter.wait(item.url)
            result = fetch_url(
                item.url,
                timeout=timeout,
                block_private=block_private,
                etag=item.etag or None,
                last_modified=item.last_modified or None,
            )
    except FetchError as exc:
        return _error_record(vault, evidence_id, str(exc))

    if result.not_modified:
        text_path = Path(item.snapshot).with_suffix(".txt")
        if text_path.is_file():
            found = contains_quote(text_path.read_text(encoding="utf-8"), item.quote)
        else:
            found = True
        if found:
            status = STATUS_OK
            detail = "服务器返回 304，正文未变化"
        elif item.allow_missing:
            status = STATUS_OK
            detail = "服务器返回 304；引用片段缺失，但存证时已允许"
        else:
            status = STATUS_QUOTE_MISSING
            detail = "服务器返回 304，但存证文本中本来就没有该引用片段"
        vault.update_metadata(
            item.id,
            item.final_url,
            result.etag or item.etag,
            result.last_modified or item.last_modified,
        )
        record = CheckRecord(
            evidence_id=evidence_id,
            checked_at=_now(),
            status=status,
            http_status=304,
            content_hash=item.content_hash,
            quote_found=found,
            detail=detail,
        )
        vault.insert_check(record)
        return record

    found = False
    diff = ""
    host_changed = item.source_type == "url" and _host_changed(
        item.final_url, result.final_url
    )
    if result.status in {401, 403, 429} or result.status >= 500:
        status = STATUS_INCONCLUSIVE
        detail = f"HTTP {result.status}，可能是访问受限或服务异常"
    elif result.status >= 400:
        status = STATUS_UNAVAILABLE
        detail = f"HTTP {result.status}"
    elif host_changed:
        status = STATUS_INCONCLUSIVE
        detail = (
            f"重定向到不同主机: {urlparse(item.final_url).netloc}"
            f" → {urlparse(result.final_url).netloc}"
        )
    elif result.content_type and result.content_type not in (
        HTML_CONTENT_TYPES | PDF_CONTENT_TYPES
    ):
        status = STATUS_INCONCLUSIVE
        detail = f"返回类型 {result.content_type}，无法比对正文"
    else:
        try:
            text = _result_text(result)
        except PdfError as exc:
            status = STATUS_INCONCLUSIVE
            detail = f"PDF 文本抽取失败: {exc}"
        else:
            found = contains_quote(text, item.quote)
            text_hash = _sha256_text(text)

            if not found:
                if _looks_unfetchable(text, item.text_length):
                    status = STATUS_INCONCLUSIVE
                    detail = "正文明显变短或出现登录提示，可能没有抓到真实内容"
                elif item.allow_missing:
                    status = STATUS_OK
                    detail = "引用片段缺失（存证时已允许）"
                else:
                    status = STATUS_QUOTE_MISSING
                    detail = "引用片段已不在页面正文中"
            elif text_hash != item.text_hash:
                status = STATUS_CHANGED
                detail = "页面正文已变化，但引用片段仍在"
                diff = _diff_from_snapshot(item, text)
            else:
                status = STATUS_OK
                detail = "页面正文未变化"

    if result.status == 200 and not host_changed:
        vault.update_metadata(
            item.id,
            result.final_url,
            result.etag,
            result.last_modified,
        )

    record = CheckRecord(
        evidence_id=evidence_id,
        checked_at=_now(),
        status=status,
        http_status=result.status,
        content_hash=result.content_hash,
        quote_found=found,
        detail=detail,
        diff=diff,
    )
    vault.insert_check(record)
    return record


def _safe_check(
    vault: Vault,
    item: Evidence,
    timeout: float,
    block_private: bool,
    limiter: DomainLimiter,
) -> CheckRecord:
    try:
        return check_one(
            vault,
            item.id,
            timeout=timeout,
            block_private=block_private,
            limiter=limiter,
        )
    except Exception as exc:
        try:
            return _error_record(vault, item.id, f"核查异常: {exc}")
        except Exception:
            return CheckRecord(
                evidence_id=item.id,
                checked_at=_now(),
                status=STATUS_ERROR,
                http_status=0,
                content_hash="",
                quote_found=False,
                detail=f"核查异常（且无法写库）: {exc}",
            )


def check_all(
    vault: Vault,
    timeout: float = 15.0,
    delay: float = 1.0,
    block_private: bool = False,
    workers: int = 1,
) -> List[CheckRecord]:
    """逐条核查；workers > 1 时按域名限速并行，单条异常不会中断整批。"""
    items = vault.all()
    if not items:
        return []
    limiter = DomainLimiter(delay)
    if workers <= 1:
        return [
            _safe_check(vault, item, timeout, block_private, limiter)
            for item in items
        ]

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_safe_check, vault, item, timeout, block_private, limiter): item
            for item in items
        }
        for future, item in futures.items():
            results[item.id] = future.result()
    return [results[item.id] for item in items if item.id in results]


def _scan_entry(
    vault: Vault,
    index: int,
    entry: dict,
    timeout: float,
    allow_missing_quote: bool,
    block_private: bool,
    limiter: DomainLimiter,
) -> ScanResult:
    claim = entry["claim"]
    quote = entry["quote"]
    url = entry.get("url") or None
    file = entry.get("file") or None
    target = str(Path(file).resolve()) if file else url
    try:
        existing = vault.get(make_id(target or "", claim, quote))
        if existing:
            record = check_one(
                vault,
                existing.id,
                timeout=timeout,
                block_private=block_private,
                limiter=limiter,
            )
            return ScanResult(
                index=index,
                claim=claim,
                evidence_id=existing.id,
                action="checked",
                status=record.status,
                detail=record.detail,
                diff=record.diff,
            )
        item = capture(
            vault,
            claim=claim,
            quote=quote,
            url=url,
            file=file,
            note=entry.get("note") or "",
            timeout=timeout,
            allow_missing_quote=allow_missing_quote,
            block_private=block_private,
            limiter=limiter,
        )
        return ScanResult(
            index=index,
            claim=claim,
            evidence_id=item.id,
            action="captured",
            status="captured",
            detail="已存证",
        )
    except (FetchError, QuoteNotFound, AlreadyExists, ValueError) as exc:
        return ScanResult(
            index=index,
            claim=claim,
            evidence_id="",
            action="failed",
            status="failed",
            detail=str(exc),
        )


def _safe_scan(
    vault: Vault,
    index: int,
    entry: dict,
    timeout: float,
    allow_missing_quote: bool,
    block_private: bool,
    limiter: DomainLimiter,
) -> ScanResult:
    try:
        return _scan_entry(
            vault,
            index,
            entry,
            timeout,
            allow_missing_quote,
            block_private,
            limiter,
        )
    except Exception as exc:
        return ScanResult(
            index=index,
            claim=entry.get("claim", ""),
            evidence_id="",
            action="failed",
            status="failed",
            detail=f"扫描异常: {exc}",
        )


def scan(
    vault: Vault,
    entries: List[dict],
    timeout: float = 15.0,
    delay: float = 0.0,
    allow_missing_quote: bool = False,
    block_private: bool = False,
    workers: int = 1,
) -> List[ScanResult]:
    """按清单执行：新条目存证，已有条目重新核查；重复条目提前去重。"""
    if not entries:
        return []

    prepared = []
    duplicates: List[ScanResult] = []
    seen = set()
    for index, entry in enumerate(entries, start=1):
        file = entry.get("file") or None
        target = str(Path(file).resolve()) if file else (entry.get("url") or "")
        evidence_id = make_id(target, entry["claim"], entry["quote"])
        if evidence_id in seen:
            duplicates.append(ScanResult(
                index=index,
                claim=entry["claim"],
                evidence_id=evidence_id,
                action="skipped",
                status="duplicate",
                detail="清单中存在重复条目",
            ))
            continue
        seen.add(evidence_id)
        prepared.append((index, entry))

    limiter = DomainLimiter(delay)
    if workers <= 1:
        results = [
            _safe_scan(
                vault,
                index,
                entry,
                timeout,
                allow_missing_quote,
                block_private,
                limiter,
            )
            for index, entry in prepared
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _safe_scan,
                    vault,
                    index,
                    entry,
                    timeout,
                    allow_missing_quote,
                    block_private,
                    limiter,
                ): index
                for index, entry in prepared
            }
            for future, _ in futures.items():
                results.append(future.result())

    results.extend(duplicates)
    return sorted(results, key=lambda result: result.index)

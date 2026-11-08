"""可独立验证的证据包（bundle）。"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from . import __version__
from .extract import contains_quote, html_to_text
from .store import Evidence, Vault

BUNDLE_FORMAT = "citevault-bundle"
BUNDLE_VERSION = 2
MANIFEST_NAME = "bundle.json"

MAX_BUNDLE_CHECKS = 50
MAX_BUNDLE_TOTAL_BYTES = 50_000_000


class BundleError(Exception):
    """证据包创建或读取失败。"""


@dataclass
class BundleResult:
    path: str
    count: int
    entries_hash: str


@dataclass
class EntryVerification:
    evidence_id: str
    ok: bool
    errors: List[str] = field(default_factory=list)


@dataclass
class BundleReport:
    path: str
    valid: bool
    entries_hash: str
    entries: List[EntryVerification] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_snapshot_text(item: Evidence) -> str:
    snapshot = Path(item.snapshot)
    text_path = snapshot.with_suffix(".txt")
    if text_path.is_file():
        return text_path.read_text(encoding="utf-8")
    if snapshot.is_file():
        return html_to_text(snapshot.read_text(encoding="utf-8"))
    raise BundleError(f"快照不存在: {item.snapshot}")


def _checks_for_entry(vault: Vault, evidence_id: str):
    history = vault.checks(evidence_id)
    checks = [
        {
            "checked_at": record.checked_at,
            "status": record.status,
            "http_status": record.http_status,
            "content_hash": record.content_hash,
            "quote_found": record.quote_found,
            "detail": record.detail,
            "diff": record.diff,
        }
        for record in history[:MAX_BUNDLE_CHECKS]
    ]
    return checks, len(history)


def _evidence_fingerprint(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把可变的核查历史排除在证据哈希之外，保证锚点身份稳定。"""
    return {
        key: value
        for key, value in entry.items()
        if key not in {"checks", "checks_total"}
    }


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def build_bundle(
    vault: Vault,
    evidence_ids: List[str],
    output: str,
    force: bool = False,
) -> BundleResult:
    """把指定证据导出成 zip；先写临时文件，成功后再原子替换。"""
    if not evidence_ids:
        raise BundleError("没有可导出的证据")

    output_path = Path(output)
    if output_path.exists() and not force:
        raise BundleError(f"输出文件已存在: {output}（使用 --force 覆盖）")

    entries: List[Dict[str, Any]] = []
    files: Dict[str, bytes] = {}
    seen = set()

    for evidence_id in evidence_ids:
        item = vault.get(evidence_id)
        if not item:
            raise BundleError(f"找不到 {evidence_id}")
        if item.id in seen:
            continue
        seen.add(item.id)

        snapshot_path = Path(item.snapshot)
        if not snapshot_path.is_file():
            raise BundleError(f"快照不存在: {item.snapshot}")
        snapshot_bytes = snapshot_path.read_bytes()
        text_bytes = _load_snapshot_text(item).encode("utf-8")

        snapshot_suffix = snapshot_path.suffix or ".html"
        snapshot_name = f"evidence/{item.id}{snapshot_suffix}"
        text_name = f"evidence/{item.id}.txt"
        files[snapshot_name] = snapshot_bytes
        files[text_name] = text_bytes

        checks, checks_total = _checks_for_entry(vault, item.id)
        entry: Dict[str, Any] = {
            "id": item.id,
            "url": item.url,
            "final_url": item.final_url,
            "source_type": item.source_type,
            "claim": item.claim,
            "quote": item.quote,
            "title": item.title,
            "note": item.note,
            "captured_at": item.captured_at,
            "status_code": item.status_code,
            "content_hash": item.content_hash,
            "etag": item.etag,
            "last_modified": item.last_modified,
            "allow_missing": bool(item.allow_missing),
            "text_hash": _sha256_bytes(text_bytes),
            "text_length": len(text_bytes.decode("utf-8")),
            "snapshot": snapshot_name,
            "snapshot_sha256": _sha256_bytes(snapshot_bytes),
            "snapshot_size": len(snapshot_bytes),
            "text": text_name,
            "checks_total": checks_total,
            "checks": checks,
        }

        raw_path = Path(item.raw_snapshot) if item.raw_snapshot else None
        if raw_path and raw_path.is_file():
            raw_bytes = raw_path.read_bytes()
            if _sha256_bytes(raw_bytes) != item.content_hash:
                raise BundleError(f"{item.id} 的原始字节哈希与记录不一致")
            raw_name = f"evidence/{item.id}.raw"
            files[raw_name] = raw_bytes
            entry["raw"] = raw_name
            entry["raw_sha256"] = _sha256_bytes(raw_bytes)
            entry["raw_size"] = len(raw_bytes)

        entries.append(entry)

    created_at = _now()
    fingerprint = [_evidence_fingerprint(entry) for entry in entries]
    entries_hash = _sha256_bytes(_canonical(fingerprint))
    checks_hash = _sha256_bytes(_canonical({
        entry["id"]: entry.get("checks", []) for entry in entries
    }))
    manifest_core = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": created_at,
        "tool_version": __version__,
        "entries_hash": entries_hash,
        "checks_hash": checks_hash,
    }
    payload = {
        **manifest_core,
        "manifest_hash": _sha256_bytes(_canonical(manifest_core)),
        "entries": entries,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            _write_member(
                archive,
                MANIFEST_NAME,
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for name, data in files.items():
                _write_member(archive, name, data)
        os.replace(str(temporary), str(output_path))
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise

    return BundleResult(path=str(output_path), count=len(entries), entries_hash=entries_hash)


def _read_manifest(archive: zipfile.ZipFile) -> Dict[str, Any]:
    try:
        raw = archive.read(MANIFEST_NAME)
    except KeyError as exc:
        raise BundleError("证据包里缺少 bundle.json") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"bundle.json 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise BundleError("bundle.json 格式错误")
    if payload.get("format") != BUNDLE_FORMAT:
        raise BundleError("不是 citevault 证据包")
    if payload.get("version") not in {1, BUNDLE_VERSION}:
        raise BundleError(f"不支持的版本: {payload.get('version')}")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BundleError("证据包里没有条目")
    return payload


def _verify_entry(entry: Dict[str, Any], archive: zipfile.ZipFile, names: set) -> EntryVerification:
    errors: List[str] = []
    evidence_id = str(entry.get("id") or "?")

    for key in (
        "claim", "quote", "captured_at", "snapshot", "text",
        "snapshot_sha256", "text_hash", "text_length",
    ):
        if key not in entry:
            errors.append(f"缺少字段 {key}")

    snapshot_name = entry.get("snapshot")
    if isinstance(snapshot_name, str):
        if snapshot_name not in names:
            errors.append(f"缺少快照文件 {snapshot_name}")
        else:
            snapshot_bytes = archive.read(snapshot_name)
            if _sha256_bytes(snapshot_bytes) != entry.get("snapshot_sha256"):
                errors.append("快照哈希不匹配")

    text_name = entry.get("text")
    if isinstance(text_name, str):
        if text_name not in names:
            errors.append(f"缺少文本文件 {text_name}")
        else:
            text_bytes = archive.read(text_name)
            if _sha256_bytes(text_bytes) != entry.get("text_hash"):
                errors.append("文本哈希不匹配")
            text = text_bytes.decode("utf-8", errors="replace")
            quote = entry.get("quote") or ""
            if not contains_quote(text, quote):
                errors.append("引用片段不在文本中")
            if entry.get("text_length") != len(text):
                errors.append("文本长度不匹配")

    raw_name = entry.get("raw")
    if isinstance(raw_name, str) and raw_name:
        if raw_name not in names:
            errors.append(f"缺少原始文件 {raw_name}")
        else:
            raw_bytes = archive.read(raw_name)
            raw_hash = _sha256_bytes(raw_bytes)
            if raw_hash != entry.get("raw_sha256"):
                errors.append("原始字节哈希不匹配")
            if entry.get("content_hash") and raw_hash != entry.get("content_hash"):
                errors.append("原始字节与 content_hash 不一致")

    return EntryVerification(evidence_id=evidence_id, ok=not errors, errors=errors)


def verify_bundle(path: str) -> BundleReport:
    """离线复核证据包：只依赖包内文件，不依赖本地存证库或网络。"""
    bundle_path = Path(path)
    if not bundle_path.is_file():
        raise BundleError(f"证据包不存在: {path}")

    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise BundleError(f"无法读取证据包: {exc}") from exc

    with archive:
        total_size = sum(info.file_size for info in archive.infolist())
        if total_size > MAX_BUNDLE_TOTAL_BYTES:
            return BundleReport(
                path=str(bundle_path),
                valid=False,
                entries_hash="",
                errors=[f"证据包解压后超过 {MAX_BUNDLE_TOTAL_BYTES} 字节上限"],
            )

        try:
            payload = _read_manifest(archive)
        except BundleError as exc:
            return BundleReport(
                path=str(bundle_path),
                valid=False,
                entries_hash="",
                errors=[str(exc)],
            )

        entries = payload["entries"]
        version = payload.get("version")
        global_errors: List[str] = []

        if version == 1:
            # v1: entries_hash 覆盖包含 checks 的完整条目
            actual_hash = _sha256_bytes(_canonical(entries))
            if payload.get("entries_hash") != actual_hash:
                global_errors.append("entries_hash 不匹配，证据字段可能被修改")
            if payload.get("manifest_hash"):
                legacy_core = {
                    "format": payload.get("format"),
                    "version": payload.get("version"),
                    "created_at": payload.get("created_at"),
                    "tool_version": payload.get("tool_version"),
                    "entries_hash": payload.get("entries_hash"),
                }
                if payload.get("manifest_hash") != _sha256_bytes(_canonical(legacy_core)):
                    global_errors.append("manifest_hash 不匹配")
        else:
            fingerprint = [_evidence_fingerprint(entry) for entry in entries]
            actual_hash = _sha256_bytes(_canonical(fingerprint))
            actual_checks_hash = _sha256_bytes(_canonical({
                entry.get("id", "?"): entry.get("checks", []) for entry in entries
            }))
            if payload.get("entries_hash") != actual_hash:
                global_errors.append("entries_hash 不匹配，证据字段可能被修改")
            if payload.get("checks_hash") != actual_checks_hash:
                global_errors.append("checks_hash 不匹配，核查历史可能被修改")

            manifest_core = {
                "format": payload.get("format"),
                "version": payload.get("version"),
                "created_at": payload.get("created_at"),
                "tool_version": payload.get("tool_version"),
                "entries_hash": payload.get("entries_hash"),
                "checks_hash": payload.get("checks_hash"),
            }
            if payload.get("manifest_hash") != _sha256_bytes(_canonical(manifest_core)):
                global_errors.append("manifest_hash 不匹配，bundle.json 元数据可能被修改")

        names = set(archive.namelist())
        verifications: List[EntryVerification] = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                verifications.append(EntryVerification("?", False, ["条目不是对象"]))
                continue
            evidence_id = str(entry.get("id") or "?")
            if evidence_id in seen:
                verifications.append(EntryVerification(evidence_id, False, ["重复的 ID"]))
                continue
            seen.add(evidence_id)
            verifications.append(_verify_entry(entry, archive, names))

        valid = not global_errors and all(item.ok for item in verifications)
        return BundleReport(
            path=str(bundle_path),
            valid=valid,
            entries_hash=actual_hash,
            entries=verifications,
            errors=global_errors,
        )

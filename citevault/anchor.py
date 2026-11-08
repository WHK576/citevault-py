"""把证据包的哈希锚定到外部位置，可选 GPG 签名。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .bundle import BUNDLE_FORMAT, BundleError, verify_bundle

ANCHOR_FORMAT = "citevault-anchor"
ANCHOR_VERSION = 1


class AnchorError(Exception):
    """锚点创建或验证失败。"""


@dataclass
class AnchorResult:
    path: str
    signature: str
    bundle_sha256: str
    entries_hash: str


@dataclass
class AnchorReport:
    path: str
    bundle: str
    valid: bool
    signed: bool
    signature_valid: Optional[bool]
    bundle_sha256: str
    entries_hash: str
    errors: List[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bundle_manifest(bundle_path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(bundle_path, "r") as archive:
            raw = archive.read("bundle.json")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise AnchorError(f"无法读取证据包: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnchorError(f"bundle.json 解析失败: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("format") != BUNDLE_FORMAT:
        raise AnchorError("不是 citevault 证据包")
    return payload


def _run_gpg(args: List[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gpg"] + args, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise AnchorError("未找到 gpg，请先安装 GnuPG") from exc


def create_anchor(
    bundle_path: str,
    output: Optional[str] = None,
    sign: bool = False,
    gpg_key: Optional[str] = None,
) -> AnchorResult:
    """生成锚点文件；--sign 时额外生成 .asc 分离签名。"""
    source = Path(bundle_path)
    if not source.is_file():
        raise AnchorError(f"证据包不存在: {bundle_path}")

    payload = _read_bundle_manifest(source)
    entries_hash = payload.get("entries_hash", "")
    if not entries_hash:
        raise AnchorError("bundle.json 缺少 entries_hash")

    report = verify_bundle(str(source))
    if not report.valid:
        raise AnchorError("证据包未通过内部校验，拒绝生成锚点")

    bundle_bytes = source.read_bytes()
    anchor = {
        "format": ANCHOR_FORMAT,
        "version": ANCHOR_VERSION,
        "anchored_at": _now(),
        "tool_version": __version__,
        "bundle": source.name,
        "bundle_created_at": payload.get("created_at", ""),
        "bundle_sha256": _sha256_bytes(bundle_bytes),
        "entries_hash": entries_hash,
        "entry_count": len(payload.get("entries") or []),
    }

    target = Path(output) if output else source.with_suffix(source.suffix + ".anchor.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(anchor, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    signature = ""
    try:
        if sign:
            signature = str(target) + ".asc"
            command = ["--armor", "--detach-sign", "--output", signature]
            if gpg_key:
                command.extend(["--local-user", gpg_key])
            command.append(str(temporary))
            result = _run_gpg(command)
            if result.returncode != 0:
                raise AnchorError(f"GPG 签名失败: {result.stderr.strip()}")
        os.replace(str(temporary), str(target))
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if signature:
            try:
                Path(signature).unlink()
            except FileNotFoundError:
                pass
        raise

    return AnchorResult(
        path=str(target),
        signature=signature,
        bundle_sha256=anchor["bundle_sha256"],
        entries_hash=entries_hash,
    )


def verify_anchor(
    anchor_path: str,
    bundle_path: str,
    signature: Optional[str] = None,
) -> AnchorReport:
    """离线验证锚点、证据包字节、entries_hash 与可选 GPG 签名。"""
    anchor_file = Path(anchor_path)
    bundle_file = Path(bundle_path)
    if not anchor_file.is_file():
        raise AnchorError(f"锚点不存在: {anchor_path}")
    if not bundle_file.is_file():
        raise AnchorError(f"证据包不存在: {bundle_path}")

    try:
        payload = json.loads(anchor_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnchorError(f"锚点解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnchorError("锚点格式错误")
    if payload.get("format") != ANCHOR_FORMAT:
        raise AnchorError("不是 citevault 锚点")
    if payload.get("version") != ANCHOR_VERSION:
        raise AnchorError(f"不支持的锚点版本: {payload.get('version')}")

    errors: List[str] = []
    bundle_sha256 = _sha256_bytes(bundle_file.read_bytes())
    if bundle_sha256 != payload.get("bundle_sha256"):
        errors.append("证据包字节与锚点记录不一致")

    try:
        manifest = _read_bundle_manifest(bundle_file)
    except AnchorError as exc:
        errors.append(str(exc))
        manifest = {}
    entries_hash = manifest.get("entries_hash", "")
    if entries_hash != payload.get("entries_hash"):
        errors.append("entries_hash 与锚点记录不一致")

    try:
        bundle_report = verify_bundle(str(bundle_file))
        if not bundle_report.valid:
            errors.append("证据包内部校验失败")
    except BundleError as exc:
        errors.append(str(exc))

    signature_file = signature or (str(anchor_file) + ".asc")
    if signature and not os.path.isfile(signature):
        raise AnchorError(f"签名文件不存在: {signature}")
    signed = os.path.isfile(signature_file)
    signature_valid: Optional[bool] = None
    if signed:
        result = _run_gpg(["--verify", signature_file, str(anchor_file)])
        signature_valid = result.returncode == 0
        if not signature_valid:
            errors.append(f"GPG 签名无效: {result.stderr.strip()}")

    return AnchorReport(
        path=str(anchor_file),
        bundle=str(bundle_file),
        valid=not errors and signature_valid is not False,
        signed=signed,
        signature_valid=signature_valid,
        bundle_sha256=bundle_sha256,
        entries_hash=entries_hash,
        errors=errors,
    )

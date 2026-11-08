"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import Counter
from typing import List, Optional

from . import __version__
from .anchor import AnchorError, AnchorReport, create_anchor, verify_anchor
from .bundle import BundleError, BundleReport, build_bundle, verify_bundle
from .core import (
    AlreadyExists,
    EvidenceNotFound,
    FAILING_STATUSES,
    QuoteNotFound,
    ScanResult,
    capture,
    check_all,
    check_one,
    remove_evidence,
    scan,
)
from .fetch import FetchError, validate_url
from .manifest import ManifestError, load_manifest
from .store import CheckRecord, Vault


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _short(text: str, limit: int = 36) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _worker_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers 必须是整数") from exc
    if not 1 <= number <= 32:
        raise argparse.ArgumentTypeError("workers 必须在 1 到 32 之间")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return number


def _list(vault: Vault, as_json: bool) -> None:
    items = vault.all()
    if as_json:
        payload = []
        for item in items:
            last = vault.last_check(item.id)
            payload.append({**item.__dict__, "last_check": last.__dict__ if last else None})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not items:
        print("存证库还是空的，先用 `citevault add` 或 `citevault scan` 加一条。")
        return
    print(_pad("ID", 14) + _pad("最近状态", 16) + "claim")
    for item in items:
        last = vault.last_check(item.id)
        status = last.status if last else "未核查"
        print(_pad(item.id, 14) + _pad(status, 16) + _short(item.claim))


def _check(records: List[CheckRecord], as_json: bool) -> None:
    if not records:
        if as_json:
            print("[]")
        else:
            print("存证库为空，没有可核查的记录。")
        return
    if as_json:
        print(json.dumps([record.__dict__ for record in records], ensure_ascii=False, indent=2))
        return
    for record in records:
        print(f"{record.status:<16}{record.evidence_id:<14}{record.checked_at}  {record.detail}")
        if record.diff:
            for line in record.diff.splitlines():
                print(f"    {line}")
    counts = Counter(record.status for record in records)
    print("汇总: " + " / ".join(f"{status} {count}" for status, count in counts.items()))


def _print_scan(results: List[ScanResult], as_json: bool) -> None:
    if as_json:
        print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2))
        return
    for result in results:
        print(
            _pad(result.action, 10) + _pad(result.status, 16)
            + _pad(result.evidence_id, 14) + _short(result.claim) + "  " + result.detail
        )
        if result.diff:
            for line in result.diff.splitlines():
                print(f"    {line}")
    counts = Counter(result.status for result in results)
    print("汇总: " + " / ".join(f"{status} {count}" for status, count in counts.items()))


def _print_bundle(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return
    print(f"已导出 {result.count} 条证据 → {result.path}")
    print(f"entries_hash: {result.entries_hash}")


def _print_verification(report: BundleReport, as_json: bool) -> None:
    if as_json:
        payload = {
            "path": report.path,
            "valid": report.valid,
            "entries_hash": report.entries_hash,
            "errors": report.errors,
            "entries": [
                {"id": item.evidence_id, "ok": item.ok, "errors": item.errors}
                for item in report.entries
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"{'valid' if report.valid else 'invalid'}  {report.path}")
    print(f"entries_hash: {report.entries_hash}")
    for error in report.errors:
        print(f"  错误: {error}")
    for item in report.entries:
        print(f"  {item.evidence_id}  {'ok' if item.ok else 'FAILED'}")
        for error in item.errors:
            print(f"    - {error}")


def _print_anchor(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return
    print(f"锚点: {result.path}")
    print(f"bundle_sha256: {result.bundle_sha256}")
    print(f"entries_hash: {result.entries_hash}")
    if result.signature:
        print(f"签名: {result.signature}")


def _print_anchor_report(report: AnchorReport, as_json: bool) -> None:
    if as_json:
        payload = {
            "path": report.path,
            "bundle": report.bundle,
            "valid": report.valid,
            "signed": report.signed,
            "signature_valid": report.signature_valid,
            "bundle_sha256": report.bundle_sha256,
            "entries_hash": report.entries_hash,
            "errors": report.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"{'valid' if report.valid else 'invalid'}  {report.path}")
    print(f"证据包: {report.bundle}")
    if report.signed:
        print(f"签名: {'有效' if report.signature_valid else '无效'}")
    else:
        print("签名: 未签名")
    print(f"bundle_sha256: {report.bundle_sha256}")
    print(f"entries_hash: {report.entries_hash}")
    for error in report.errors:
        print(f"  错误: {error}")


def _show(vault: Vault, evidence_id: str, limit: int) -> int:
    item = vault.get(evidence_id)
    if not item:
        raise EvidenceNotFound(f"找不到 {evidence_id}")
    print(f"ID        {item.id}")
    print(f"标题      {item.title}")
    print(f"来源      {item.url}")
    print(f"最终地址  {item.final_url}")
    print(f"存证时间  {item.captured_at}")
    print(f"claim     {item.claim}")
    print(f"quote     {item.quote}")
    if item.note:
        print(f"备注      {item.note}")
    print(f"快照      {item.snapshot}")
    history = vault.checks(item.id)
    print(f"\n核查历史（{len(history)} 次）")
    for record in history[:limit]:
        print(f"  {record.checked_at}  {record.status:<16}{record.detail}")
        if record.diff:
            for line in record.diff.splitlines():
                print(f"      {line}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    store_default = os.environ.get("CITEVAULT_DIR", ".citevault")
    parser = argparse.ArgumentParser(
        prog="citevault",
        description="给 AI 引用做本地存证：保存来源快照，之后核查引用是否还在。",
    )
    parser.add_argument("--store", default=store_default, help="存证库目录（默认 .citevault）")
    parser.add_argument("--version", action="version", version=f"citevault {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", parents=[common], help="存证一条引用")
    add.add_argument("--url", help="来源 URL")
    add.add_argument("--file", help="本地 HTML / PDF 文件，与 --url 二选一")
    add.add_argument("--claim", required=True, help="这条引用支撑的结论")
    add.add_argument("--quote", required=True, help="来源里的原文片段")
    add.add_argument("--note", default="", help="备注")
    add.add_argument("--timeout", type=float, default=15.0, help="抓取超时秒数")
    add.add_argument("--allow-missing-quote", action="store_true", help="正文里找不到片段时仍然存证")
    add.add_argument("--block-private", action="store_true", help="拒绝本地/私有地址")
    add.add_argument("--update", help="更新已有存证 ID，保留核查历史")

    scan_cmd = sub.add_parser("scan", parents=[common], help="按清单批量存证并核查")
    scan_cmd.add_argument("manifest", help="citations.json / citations.yml")
    scan_cmd.add_argument("--timeout", type=float, default=15.0, help="抓取超时秒数")
    scan_cmd.add_argument("--delay", type=float, default=1.0, help="同域名两次请求的最小间隔")
    scan_cmd.add_argument("--workers", type=_worker_count, default=1, help="并发数 1-32")
    scan_cmd.add_argument("--allow-missing-quote", action="store_true", help="找不到片段时仍然存证")
    scan_cmd.add_argument("--block-private", action="store_true", help="拒绝本地/私有地址")
    scan_cmd.add_argument("--json", action="store_true", help="输出 JSON")

    bundle_cmd = sub.add_parser("bundle", parents=[common], help="导出可独立验证的证据包")
    bundle_cmd.add_argument("ids", nargs="*", help="存证 ID；不填则配合 --all")
    bundle_cmd.add_argument("--all", action="store_true", help="导出全部")
    bundle_cmd.add_argument("-o", "--output", default="citevault-evidence.cvbundle", help="输出文件")
    bundle_cmd.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    bundle_cmd.add_argument("--json", action="store_true", help="输出 JSON")

    verify = sub.add_parser("verify-bundle", help="离线验证证据包")
    verify.add_argument("path", help="证据包路径")
    verify.add_argument("--json", action="store_true", help="输出 JSON")

    anchor_cmd = sub.add_parser("anchor", help="为证据包生成外部锚点")
    anchor_cmd.add_argument("bundle", help="证据包路径")
    anchor_cmd.add_argument("-o", "--output", help="锚点输出路径（默认 <bundle>.anchor.json）")
    anchor_cmd.add_argument("--sign", action="store_true", help="用 GPG 生成分离签名")
    anchor_cmd.add_argument("--gpg-key", help="GPG 公钥 ID")
    anchor_cmd.add_argument("--json", action="store_true", help="输出 JSON")

    verify_anchor_cmd = sub.add_parser("verify-anchor", help="离线验证锚点与证据包")
    verify_anchor_cmd.add_argument("anchor", help="锚点文件")
    verify_anchor_cmd.add_argument("bundle", help="证据包路径")
    verify_anchor_cmd.add_argument("--signature", help="签名文件（默认查找 <anchor>.asc）")
    verify_anchor_cmd.add_argument("--json", action="store_true", help="输出 JSON")

    listing = sub.add_parser("list", parents=[common], help="列出所有存证")
    listing.add_argument("--json", action="store_true", help="输出 JSON")

    check = sub.add_parser("check", parents=[common], help="重新核查")
    check.add_argument("id", nargs="?", help="存证 ID；不填则配合 --all")
    check.add_argument("--all", action="store_true", help="核查全部")
    check.add_argument("--json", action="store_true", help="输出 JSON")
    check.add_argument("--timeout", type=float, default=15.0, help="抓取超时秒数")
    check.add_argument("--delay", type=float, default=1.0, help="同域名两次请求的最小间隔")
    check.add_argument("--workers", type=_worker_count, default=1, help="并发数 1-32")
    check.add_argument("--block-private", action="store_true", help="拒绝本地/私有地址")

    show = sub.add_parser("show", parents=[common], help="查看单条存证与历史")
    show.add_argument("id", help="存证 ID")
    show.add_argument("--limit", type=_positive_int, default=10, help="显示最近 N 次核查历史")

    remove = sub.add_parser("remove", parents=[common], help="删除一条存证及其快照")
    remove.add_argument("id", help="存证 ID")
    remove.add_argument("--yes", action="store_true", help="确认删除")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "verify-bundle":
        try:
            report = verify_bundle(args.path)
        except BundleError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        _print_verification(report, args.json)
        return 0 if report.valid else 2

    if args.cmd == "anchor":
        try:
            result = create_anchor(args.bundle, args.output, args.sign, args.gpg_key)
        except AnchorError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        _print_anchor(result, args.json)
        return 0

    if args.cmd == "verify-anchor":
        try:
            report = verify_anchor(args.anchor, args.bundle, args.signature)
        except AnchorError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        _print_anchor_report(report, args.json)
        return 0 if report.valid else 2

    # 先校验 URL 和清单，避免无效输入仍然创建 .citevault
    if args.cmd == "add" and args.url:
        try:
            validate_url(args.url, args.block_private)
        except FetchError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1

    entries = None
    if args.cmd == "scan":
        try:
            entries = load_manifest(args.manifest)
        except ManifestError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        for entry in entries:
            if entry.get("url"):
                try:
                    validate_url(entry["url"], args.block_private)
                except FetchError as exc:
                    print(f"错误: {exc}", file=sys.stderr)
                    return 1

    vault = Vault(args.store)
    try:
        if args.cmd == "add":
            item = capture(
                vault,
                claim=args.claim,
                quote=args.quote,
                url=args.url,
                file=args.file,
                note=args.note,
                timeout=args.timeout,
                allow_missing_quote=args.allow_missing_quote,
                block_private=args.block_private,
                update_id=args.update,
            )
            print(f"已存证 {item.id}  {item.title}")
            print(f"快照: {item.snapshot}")
            return 0

        if args.cmd == "scan":
            results = scan(
                vault,
                entries or [],
                timeout=args.timeout,
                delay=args.delay,
                allow_missing_quote=args.allow_missing_quote,
                block_private=args.block_private,
                workers=args.workers,
            )
            _print_scan(results, args.json)
            failed = any(
                result.action == "failed" or result.status in FAILING_STATUSES
                for result in results
            )
            return 2 if failed else 0

        if args.cmd == "bundle":
            if args.all:
                ids = [item.id for item in vault.all()]
            elif args.ids:
                ids = args.ids
            else:
                print("需要指定 ID 或使用 --all", file=sys.stderr)
                return 1
            result = build_bundle(vault, ids, args.output, force=args.force)
            _print_bundle(result, args.json)
            return 0

        if args.cmd == "list":
            _list(vault, args.json)
            return 0

        if args.cmd == "check":
            if args.all:
                records = check_all(
                    vault,
                    timeout=args.timeout,
                    delay=args.delay,
                    block_private=args.block_private,
                    workers=args.workers,
                )
            elif args.id:
                records = [
                    check_one(
                        vault,
                        args.id,
                        timeout=args.timeout,
                        block_private=args.block_private,
                    )
                ]
            else:
                print("需要指定 ID 或使用 --all", file=sys.stderr)
                return 1
            _check(records, args.json)
            return 2 if any(record.status in FAILING_STATUSES for record in records) else 0

        if args.cmd == "show":
            return _show(vault, args.id, args.limit)

        if args.cmd == "remove":
            if not args.yes:
                print("需要 --yes 确认删除", file=sys.stderr)
                return 1
            item = remove_evidence(vault, args.id)
            print(f"已删除 {item.id}")
            return 0
    except (
        FetchError,
        QuoteNotFound,
        AlreadyExists,
        EvidenceNotFound,
        ManifestError,
        BundleError,
        ValueError,
    ) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        vault.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())

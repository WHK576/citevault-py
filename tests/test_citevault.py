"""citevault 的核心与 CLI 测试。"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from citevault.anchor import AnchorError, create_anchor, verify_anchor
from citevault.bundle import BundleError, build_bundle, verify_bundle
from citevault.cli import main
from citevault.core import (
    AlreadyExists,
    STATUS_CHANGED,
    STATUS_INCONCLUSIVE,
    STATUS_OK,
    STATUS_QUOTE_MISSING,
    STATUS_UNAVAILABLE,
    _split_sentences,
    capture,
    check_all,
    check_one,
    remove_evidence,
    scan,
)
from citevault.extract import extract_title, html_to_text, normalize_text
from citevault.fetch import FetchError, _match_robots, _parse_robots
from citevault.manifest import ManifestError, load_manifest
from citevault.store import Vault

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import pypdf  # noqa: F401
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

PAGE = {
    "status": 200,
    "html": "",
    "redirect": "",
    "fail_times": 0,
    "etag": "",
    "content_type": "text/html; charset=utf-8",
    "body_bytes": b"",
    "gzip": False,
    "retry_after_times": 0,
    "retry_after": "0",
}


def _minimal_pdf(text: str) -> bytes:
    """生成一个最小可解析的 PDF，用来测试文本抽取。"""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = ("BT /F1 12 Tf 72 720 Td (" + escaped + ") Tj ET").encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend("{} 0 obj\n".format(index).encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend("xref\n0 {}\n".format(len(objects) + 1).encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend("{:010d} 00000 n \n".format(offset).encode())
    output.extend(
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n".format(
            len(objects) + 1, xref_offset
        ).encode()
    )
    return bytes(output)


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if PAGE.get("retry_after_times", 0) > 0:
            PAGE["retry_after_times"] -= 1
            self.send_response(429)
            self.send_header("Retry-After", PAGE.get("retry_after", "0"))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if PAGE.get("fail_times", 0) > 0:
            PAGE["fail_times"] -= 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if PAGE.get("redirect"):
            self.send_response(302)
            self.send_header("Location", PAGE["redirect"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = PAGE.get("etag", "")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = PAGE["body_bytes"] if PAGE["body_bytes"] else PAGE["html"].encode("utf-8")
        if PAGE.get("gzip") and body:
            body = gzip.compress(body)
        self.send_response(PAGE["status"])
        self.send_header("Content-Type", PAGE["content_type"])
        if PAGE.get("gzip"):
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:
        pass


class CiteVaultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:{}".format(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        PAGE["status"] = 200
        PAGE["redirect"] = ""
        PAGE["fail_times"] = 0
        PAGE["etag"] = ""
        PAGE["content_type"] = "text/html; charset=utf-8"
        PAGE["body_bytes"] = b""
        PAGE["gzip"] = False
        PAGE["retry_after_times"] = 0
        PAGE["retry_after"] = "0"
        PAGE["html"] = (
            "<html><head><title>测试页</title></head>"
            "<body><p>这是正文。引用片段在这里。</p></body></html>"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(self.tmp.name)

    def tearDown(self) -> None:
        self.vault.close()
        self.tmp.cleanup()

    def _write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _rewrite_zip(self, path: str, name: str, data: bytes) -> None:
        with zipfile.ZipFile(path, "r") as archive:
            items = {item: archive.read(item) for item in archive.namelist()}
        items[name] = data
        with zipfile.ZipFile(path, "w") as archive:
            for key, value in items.items():
                archive.writestr(key, value)

    def _make_bundle(self, name: str = "anchor-source.html"):
        path = self._write(
            name,
            "<html><body><p>The market reached 12 billion dollars in 2026.</p></body></html>",
        )
        item = capture(
            self.vault,
            claim="市场规模",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        bundle_path = os.path.join(self.tmp.name, name + ".cvbundle")
        build_bundle(self.vault, [item.id], bundle_path)
        return item, bundle_path

    def test_capture_then_check_ok(self) -> None:
        item = capture(
            self.vault,
            claim="这是一条结论",
            quote="引用片段在这里",
            url=self.base + "/a",
        )
        self.assertEqual(len(item.id), 12)
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)
        self.assertTrue(record.quote_found)

    def test_page_changed_keeps_quote(self) -> None:
        item = capture(
            self.vault,
            claim="结论",
            quote="引用片段在这里",
            url=self.base + "/b",
        )
        PAGE["html"] = (
            "<html><body><p>新增了别的内容。</p>"
            "<p>引用片段在这里。</p></body></html>"
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_CHANGED)
        self.assertIn("新增了别的内容", record.diff)
        self.assertIn("-这是正文", record.diff)

    def test_diff_persisted(self) -> None:
        item = capture(
            self.vault,
            claim="持久化差异",
            quote="引用片段在这里",
            url=self.base + "/diff",
        )
        PAGE["html"] = (
            "<html><body><p>新增了别的内容。</p>"
            "<p>引用片段在这里。</p></body></html>"
        )
        check_one(self.vault, item.id)
        saved = self.vault.checks(item.id)[0]
        self.assertIn("新增了别的内容", saved.diff)

    def test_quote_removed(self) -> None:
        item = capture(
            self.vault,
            claim="结论",
            quote="引用片段在这里",
            url=self.base + "/c",
        )
        PAGE["html"] = "<html><body><p>引用片段消失了。</p></body></html>"
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_QUOTE_MISSING)
        self.assertFalse(record.quote_found)

    def test_unavailable(self) -> None:
        item = capture(
            self.vault,
            claim="结论",
            quote="引用片段在这里",
            url=self.base + "/d",
        )
        PAGE["status"] = 404
        PAGE["html"] = ""
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_UNAVAILABLE)
        self.assertEqual(record.http_status, 404)

    def test_inconclusive_on_403(self) -> None:
        item = capture(
            self.vault,
            claim="结论",
            quote="引用片段在这里",
            url=self.base + "/e",
        )
        PAGE["status"] = 403
        PAGE["html"] = ""
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_INCONCLUSIVE)

    def test_inconclusive_on_short_page(self) -> None:
        PAGE["html"] = (
            "<html><body><p>"
            + ("这是很长的正文内容。" * 150)
            + "引用片段在这里。</p></body></html>"
        )
        item = capture(
            self.vault,
            claim="长文",
            quote="引用片段在这里",
            url=self.base + "/f",
        )
        PAGE["html"] = "<html><body><p>打不开。</p></body></html>"
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_INCONCLUSIVE)

    def test_duplicate_capture(self) -> None:
        capture(
            self.vault,
            claim="结论",
            quote="引用片段在这里",
            url=self.base + "/g",
        )
        with self.assertRaises(AlreadyExists):
            capture(
                self.vault,
                claim="结论",
                quote="引用片段在这里",
                url=self.base + "/g",
            )

    def test_quote_normalization(self) -> None:
        PAGE["html"] = "<html><body><p>他说：“这是‘引用’片段”。</p></body></html>"
        item = capture(
            self.vault,
            claim="引号归一化",
            quote="\"这是'引用'片段\"",
            url=self.base + "/h",
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

    def test_extract_skips_script(self) -> None:
        text = html_to_text(
            "<html><body><p>正文</p><script>alert(1)</script>"
            "<style>.x{}</style></body></html>"
        )
        self.assertIn("正文", text)
        self.assertNotIn("alert", text)
        self.assertNotIn(".x", text)
        self.assertEqual(
            extract_title("<html><head><title>A &amp; B</title></head></html>"),
            "A & B",
        )

    def test_extract_prefers_main(self) -> None:
        text = html_to_text(
            "<html><body><nav>导航</nav><main><p>正文引用片段</p></main>"
            "<footer>页脚</footer></body></html>"
        )
        self.assertIn("正文引用片段", text)
        self.assertNotIn("导航", text)
        self.assertNotIn("页脚", text)

    def test_extract_role_main(self) -> None:
        text = html_to_text(
            '<html><body><div role="main"><p>正文</p></div>'
            "<nav>导航</nav></body></html>"
        )
        self.assertIn("正文", text)
        self.assertNotIn("导航", text)

    def test_extract_article(self) -> None:
        text = html_to_text(
            "<html><body><nav>导航</nav><article><p>正文</p></article></body></html>"
        )
        self.assertIn("正文", text)
        self.assertNotIn("导航", text)

    def test_extract_falls_back_to_body(self) -> None:
        text = html_to_text(
            "<html><body><div><p>正文</p></div><nav>导航</nav></body></html>"
        )
        self.assertIn("正文", text)

    def test_local_file(self) -> None:
        path = self._write("local.html", "<html><body><p>本地引用片段。</p></body></html>")
        item = capture(self.vault, claim="本地", quote="本地引用片段", file=path)
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

    def test_gbk_local_file(self) -> None:
        path = os.path.join(self.tmp.name, "gbk.html")
        with open(path, "wb") as handle:
            handle.write(
                '<html><head><meta charset="gbk"></head>'
                "<body><p>中文引用片段。</p></body></html>".encode("gbk")
            )
        item = capture(self.vault, claim="GBK 编码", quote="中文引用片段", file=path)
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

    def test_docx_rejected(self) -> None:
        path = os.path.join(self.tmp.name, "paper.docx")
        with open(path, "wb") as handle:
            handle.write(b"PK\x03\x04")
        with self.assertRaises(FetchError):
            capture(self.vault, claim="DOCX", quote="片段", file=path)

    @unittest.skipUnless(HAS_PYPDF, "pypdf 未安装")
    def test_pdf_local_file(self) -> None:
        path = os.path.join(self.tmp.name, "paper.pdf")
        with open(path, "wb") as handle:
            handle.write(_minimal_pdf("The market reached 12 billion dollars in 2026."))
        item = capture(
            self.vault,
            claim="PDF 引用",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        self.assertTrue(item.snapshot.endswith(".pdf"))
        self.assertEqual(item.raw_snapshot, "")

        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

        bundle_path = os.path.join(self.tmp.name, "pdf.cvbundle")
        build_bundle(self.vault, [item.id], bundle_path)
        with zipfile.ZipFile(bundle_path, "r") as archive:
            self.assertIn("evidence/{}.pdf".format(item.id), archive.namelist())
        self.assertTrue(verify_bundle(bundle_path).valid)

    @unittest.skipUnless(HAS_PYPDF, "pypdf 未安装")
    def test_pdf_url(self) -> None:
        PAGE["content_type"] = "application/pdf"
        PAGE["body_bytes"] = _minimal_pdf(
            "The market reached 12 billion dollars in 2026."
        )
        item = capture(
            self.vault,
            claim="PDF URL",
            quote="The market reached 12 billion dollars in 2026.",
            url=self.base + "/paper.pdf",
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

    def test_pdf_requires_dependency(self) -> None:
        path = os.path.join(self.tmp.name, "requires.pdf")
        with open(path, "wb") as handle:
            handle.write(_minimal_pdf("The market reached 12 billion dollars in 2026."))
        with mock.patch("citevault.pdf._load_pypdf", return_value=None), mock.patch(
            "citevault.pdf.shutil.which", return_value=None
        ):
            with self.assertRaises(FetchError):
                capture(
                    self.vault,
                    claim="缺少依赖",
                    quote="The market reached 12 billion dollars in 2026.",
                    file=path,
                )

    def test_url_scheme_rejected(self) -> None:
        with self.assertRaises(FetchError):
            capture(
                self.vault,
                claim="scheme",
                quote="片段",
                url="file:///C:/Windows/win.ini",
            )

    def test_block_private_rejected(self) -> None:
        with self.assertRaises(FetchError):
            capture(
                self.vault,
                claim="private",
                quote="引用片段在这里",
                url=self.base + "/private",
                block_private=True,
            )

    def test_retry_on_503(self) -> None:
        PAGE["fail_times"] = 2
        with mock.patch("citevault.fetch.time.sleep"):
            item = capture(
                self.vault,
                claim="retry",
                quote="引用片段在这里",
                url=self.base + "/retry",
            )
        self.assertEqual(item.status_code, 200)

    def test_redirect_host_change_is_inconclusive(self) -> None:
        item = capture(
            self.vault,
            claim="redirect",
            quote="引用片段在这里",
            url=self.base + "/redirect",
        )
        self.vault.conn.execute(
            "UPDATE evidence SET final_url = ? WHERE id = ?",
            ("http://other.example/x", item.id),
        )
        self.vault.conn.commit()
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_INCONCLUSIVE)

    def test_vault_uses_wal(self) -> None:
        mode = self.vault.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_manifest_json_and_scan_idempotent(self) -> None:
        path = self._write(
            "citations.json",
            json.dumps(
                {
                    "citations": [
                        {
                            "claim": "清单结论",
                            "quote": "引用片段在这里",
                            "url": self.base + "/scan",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )
        entries = load_manifest(path)
        first = scan(self.vault, entries, delay=0)
        self.assertEqual(first[0].action, "captured")
        second = scan(self.vault, entries, delay=0)
        self.assertEqual(second[0].action, "checked")
        self.assertEqual(second[0].status, STATUS_OK)

    @unittest.skipUnless(HAS_YAML, "PyYAML 未安装")
    def test_manifest_yaml(self) -> None:
        path = self._write(
            "citations.yml",
            "citations:\n"
            "  - claim: 清单结论\n"
            "    quote: 引用片段在这里\n"
            "    url: " + self.base + "/yaml\n",
        )
        entries = load_manifest(path)
        self.assertEqual(len(entries), 1)
        results = scan(self.vault, entries, delay=0)
        self.assertEqual(results[0].status, "captured")

    def test_manifest_error(self) -> None:
        path = self._write("bad.json", "{not-json")
        with self.assertRaises(ManifestError):
            load_manifest(path)

    def test_manifest_accepts_utf8_bom(self) -> None:
        path = os.path.join(self.tmp.name, "bom.json")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write(
                '{"citations":[{"claim":"x","quote":"y","url":"https://example.com"}]}'
            )
        entries = load_manifest(path)
        self.assertEqual(entries[0]["claim"], "x")

    def test_scan_reports_missing_quote(self) -> None:
        path = self._write(
            "citations.json",
            json.dumps(
                {
                    "citations": [
                        {
                            "claim": "清单结论",
                            "quote": "引用片段在这里",
                            "url": self.base + "/scan2",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )
        entries = load_manifest(path)
        scan(self.vault, entries, delay=0)
        PAGE["html"] = "<html><body><p>引用片段消失了。</p></body></html>"
        results = scan(self.vault, entries, delay=0)
        self.assertEqual(results[0].status, STATUS_QUOTE_MISSING)

    def test_scan_includes_diff(self) -> None:
        path = self._write(
            "diff.json",
            json.dumps(
                {
                    "citations": [
                        {
                            "claim": "清单差异",
                            "quote": "引用片段在这里",
                            "url": self.base + "/scan-diff",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )
        entries = load_manifest(path)
        scan(self.vault, entries, delay=0)
        PAGE["html"] = (
            "<html><body><p>新增了别的内容。</p>"
            "<p>引用片段在这里。</p></body></html>"
        )
        results = scan(self.vault, entries, delay=0)
        self.assertEqual(results[0].status, STATUS_CHANGED)
        self.assertIn("新增了别的内容", results[0].diff)

    def test_bundle_and_verify_ok(self) -> None:
        path = self._write(
            "bundle.html",
            "<html><body><p>The market reached 12 billion dollars in 2026.</p></body></html>",
        )
        item = capture(
            self.vault,
            claim="市场规模",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        bundle_path = os.path.join(self.tmp.name, "evidence.cvbundle")
        result = build_bundle(self.vault, [item.id], bundle_path)
        self.assertEqual(result.count, 1)

        report = verify_bundle(bundle_path)
        self.assertTrue(report.valid)
        self.assertEqual(len(report.entries), 1)
        self.assertTrue(report.entries[0].ok)

    def test_bundle_tamper_text(self) -> None:
        path = self._write(
            "tamper.html",
            "<html><body><p>The market reached 12 billion dollars in 2026.</p></body></html>",
        )
        item = capture(
            self.vault,
            claim="市场规模",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        bundle_path = os.path.join(self.tmp.name, "evidence.cvbundle")
        build_bundle(self.vault, [item.id], bundle_path)

        self._rewrite_zip(
            bundle_path,
            "evidence/{}.txt".format(item.id),
            b"The market reached 9 billion dollars in 2026.",
        )
        report = verify_bundle(bundle_path)
        self.assertFalse(report.valid)
        self.assertIn("文本哈希不匹配", report.entries[0].errors)

    def test_bundle_tamper_manifest(self) -> None:
        path = self._write(
            "manifest.html",
            "<html><body><p>The market reached 12 billion dollars in 2026.</p></body></html>",
        )
        item = capture(
            self.vault,
            claim="市场规模",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        bundle_path = os.path.join(self.tmp.name, "evidence.cvbundle")
        build_bundle(self.vault, [item.id], bundle_path)

        with zipfile.ZipFile(bundle_path, "r") as archive:
            items = {name: archive.read(name) for name in archive.namelist()}
        payload = json.loads(items["bundle.json"].decode("utf-8"))
        payload["entries"][0]["claim"] = "被改过的结论"
        items["bundle.json"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with zipfile.ZipFile(bundle_path, "w") as archive:
            for name, data in items.items():
                archive.writestr(name, data)

        report = verify_bundle(bundle_path)
        self.assertFalse(report.valid)
        self.assertTrue(any("entries_hash" in error for error in report.errors))

    def test_bundle_detects_metadata_tamper(self) -> None:
        _, bundle_path = self._make_bundle("metadata.html")

        with zipfile.ZipFile(bundle_path, "r") as archive:
            items = {name: archive.read(name) for name in archive.namelist()}
        payload = json.loads(items["bundle.json"].decode("utf-8"))
        payload["created_at"] = "2000-01-01T00:00:00+00:00"
        items["bundle.json"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with zipfile.ZipFile(bundle_path, "w") as archive:
            for name, data in items.items():
                archive.writestr(name, data)

        report = verify_bundle(bundle_path)
        self.assertFalse(report.valid)
        self.assertTrue(any("manifest_hash" in error for error in report.errors))

    def test_bundle_detects_raw_tamper(self) -> None:
        item, bundle_path = self._make_bundle("raw.html")
        with zipfile.ZipFile(bundle_path, "r") as archive:
            self.assertIn("evidence/{}.raw".format(item.id), archive.namelist())

        self._rewrite_zip(
            bundle_path,
            "evidence/{}.raw".format(item.id),
            b"tampered",
        )
        report = verify_bundle(bundle_path)
        self.assertFalse(report.valid)
        self.assertTrue(any("原始字节哈希" in error for error in report.entries[0].errors))

    def test_bundle_missing_id(self) -> None:
        with self.assertRaises(BundleError):
            build_bundle(
                self.vault,
                ["not-found"],
                os.path.join(self.tmp.name, "missing.cvbundle"),
            )

    def test_cli_bundle_and_verify(self) -> None:
        path = self._write(
            "cli-bundle.html",
            "<html><body><p>The market reached 12 billion dollars in 2026.</p></body></html>",
        )
        item = capture(
            self.vault,
            claim="市场规模",
            quote="The market reached 12 billion dollars in 2026.",
            file=path,
        )
        bundle_path = os.path.join(self.tmp.name, "cli.cvbundle")

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--store", self.tmp.name, "bundle", item.id, "-o", bundle_path])
        self.assertEqual(code, 0)

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["verify-bundle", bundle_path])
        self.assertEqual(code, 0)

        self._rewrite_zip(
            bundle_path,
            "evidence/{}.html".format(item.id),
            b"<html><body><p>tampered</p></body></html>",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["verify-bundle", bundle_path])
        self.assertEqual(code, 2)

    def test_anchor_and_verify_ok(self) -> None:
        item, bundle_path = self._make_bundle()
        anchor_path = os.path.join(self.tmp.name, "evidence.anchor.json")
        result = create_anchor(bundle_path, anchor_path)
        self.assertTrue(os.path.isfile(result.path))
        self.assertEqual(result.entries_hash, build_bundle(
            self.vault, [item.id], os.path.join(self.tmp.name, "again.cvbundle")
        ).entries_hash)

        report = verify_anchor(anchor_path, bundle_path)
        self.assertTrue(report.valid)
        self.assertFalse(report.signed)
        self.assertIsNone(report.signature_valid)

    def test_anchor_detects_bundle_tamper(self) -> None:
        _, bundle_path = self._make_bundle("tamper-source.html")
        anchor_path = os.path.join(self.tmp.name, "tamper.anchor.json")
        create_anchor(bundle_path, anchor_path)

        with open(bundle_path, "ab") as handle:
            handle.write(b"tampered")

        report = verify_anchor(anchor_path, bundle_path)
        self.assertFalse(report.valid)
        self.assertTrue(any("证据包字节" in error for error in report.errors))

    def test_anchor_detects_manifest_tamper(self) -> None:
        _, bundle_path = self._make_bundle("manifest-source.html")
        anchor_path = os.path.join(self.tmp.name, "manifest.anchor.json")
        create_anchor(bundle_path, anchor_path)

        with open(anchor_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["entries_hash"] = "0" * 64
        with open(anchor_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

        report = verify_anchor(anchor_path, bundle_path)
        self.assertFalse(report.valid)
        self.assertTrue(any("entries_hash" in error for error in report.errors))

    def test_anchor_missing_bundle(self) -> None:
        with self.assertRaises(AnchorError):
            create_anchor(os.path.join(self.tmp.name, "not-found.cvbundle"))

    def test_anchor_missing_signature(self) -> None:
        _, bundle_path = self._make_bundle("signature-source.html")
        anchor_path = os.path.join(self.tmp.name, "signature.anchor.json")
        create_anchor(bundle_path, anchor_path)
        with self.assertRaises(AnchorError):
            verify_anchor(
                anchor_path,
                bundle_path,
                signature=os.path.join(self.tmp.name, "missing.asc"),
            )

    def test_cli_anchor_and_verify(self) -> None:
        _, bundle_path = self._make_bundle("cli-anchor.html")
        anchor_path = os.path.join(self.tmp.name, "cli.anchor.json")

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["anchor", bundle_path, "-o", anchor_path])
        self.assertEqual(code, 0)

        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["verify-anchor", anchor_path, bundle_path])
        self.assertEqual(code, 0)

        with open(bundle_path, "ab") as handle:
            handle.write(b"tampered")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["verify-anchor", anchor_path, bundle_path])
        self.assertEqual(code, 2)

    def test_check_uses_etag_304(self) -> None:
        PAGE["etag"] = '"v1"'
        item = capture(
            self.vault,
            claim="ETag",
            quote="引用片段在这里",
            url=self.base + "/etag",
        )
        self.assertEqual(item.etag, '"v1"')

        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)
        self.assertEqual(record.http_status, 304)
        self.assertIn("304", record.detail)

    def test_validators_update_on_change(self) -> None:
        PAGE["etag"] = '"v1"'
        item = capture(
            self.vault,
            claim="ETag 更新",
            quote="引用片段在这里",
            url=self.base + "/etag-change",
        )
        PAGE["etag"] = '"v2"'
        PAGE["html"] = (
            "<html><body><p>新增了别的内容。</p>"
            "<p>引用片段在这里。</p></body></html>"
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_CHANGED)
        updated = self.vault.get(item.id)
        self.assertEqual(updated.etag, '"v2"')

    def test_concurrent_checks(self) -> None:
        for index in range(3):
            capture(
                self.vault,
                claim="并发{}".format(index),
                quote="引用片段在这里",
                url=self.base + "/concurrent-{}".format(index),
            )
        records = check_all(self.vault, delay=0, workers=4)
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record.status == STATUS_OK for record in records))

    def test_cli_workers(self) -> None:
        capture(
            self.vault,
            claim="命令行并发",
            quote="引用片段在这里",
            url=self.base + "/cli-workers",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--store", self.tmp.name,
                "check", "--all", "--workers", "4", "--delay", "0",
            ])
        self.assertEqual(code, 0)

    def test_zero_width_normalized(self) -> None:
        self.assertEqual(normalize_text("\ufeffA\u200bB"), "AB")

    def test_sentence_split_decimal(self) -> None:
        parts = _split_sentences("Revenue was 3.5 million. Next sentence.")
        self.assertEqual(parts, ["Revenue was 3.5 million.", "Next sentence."])

    def test_gzip_response(self) -> None:
        PAGE["gzip"] = True
        item = capture(
            self.vault,
            claim="gzip",
            quote="引用片段在这里",
            url=self.base + "/gzip",
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)

    def test_concurrent_duplicate_scan(self) -> None:
        path = self._write(
            "duplicates.json",
            json.dumps(
                {
                    "citations": [
                        {"claim": "重复", "quote": "引用片段在这里", "file": self._write(
                            "dup.html", "<html><body><main><p>引用片段在这里。</p></main></body></html>"
                        )},
                        {"claim": "重复", "quote": "引用片段在这里", "file": self._write(
                            "dup.html", "<html><body><main><p>引用片段在这里。</p></main></body></html>"
                        )},
                    ]
                },
                ensure_ascii=False,
            ),
        )
        entries = load_manifest(path)
        results = scan(self.vault, entries, delay=0, workers=2)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].action, "captured")
        self.assertEqual(results[1].action, "skipped")
        self.assertEqual(results[1].status, "duplicate")
        self.assertIn("重复", results[1].detail)

    def test_304_allowed_missing_is_ok(self) -> None:
        PAGE["etag"] = '"v1"'
        item = capture(
            self.vault,
            claim="304 允许缺失",
            quote="不存在的片段",
            url=self.base + "/etag-missing",
            allow_missing_quote=True,
        )
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_OK)
        self.assertIn("已允许", record.detail)

    def test_304_missing_quote_not_ok(self) -> None:
        PAGE["etag"] = '"v1"'
        item = capture(
            self.vault,
            claim="304 缺片段",
            quote="引用片段在这里",
            url=self.base + "/etag-missing-strict",
        )
        text_path = Path(item.snapshot).with_suffix(".txt")
        text_path.write_text("没有引用片段", encoding="utf-8")
        record = check_one(self.vault, item.id)
        self.assertEqual(record.status, STATUS_QUOTE_MISSING)
        self.assertFalse(record.quote_found)

    def test_bundle_entries_hash_stable_across_checks(self) -> None:
        path = self._write(
            "stable.html",
            "<html><body><main><p>引用片段在这里。</p></main></body></html>",
        )
        item = capture(self.vault, claim="稳定哈希", quote="引用片段在这里", file=path)
        first = build_bundle(
            self.vault, [item.id], os.path.join(self.tmp.name, "first.cvbundle")
        )
        check_one(self.vault, item.id)
        second = build_bundle(
            self.vault, [item.id], os.path.join(self.tmp.name, "second.cvbundle")
        )
        self.assertEqual(first.entries_hash, second.entries_hash)

    def test_anchor_rejects_tampered_bundle(self) -> None:
        item, bundle_path = self._make_bundle("anchor-tamper.html")
        self._rewrite_zip(
            bundle_path,
            "evidence/{}.txt".format(item.id),
            b"tampered",
        )
        with self.assertRaises(AnchorError):
            create_anchor(
                bundle_path,
                os.path.join(self.tmp.name, "tampered.anchor.json"),
            )

    def test_bundle_overwrite_rejected(self) -> None:
        item, bundle_path = self._make_bundle("overwrite.html")
        with self.assertRaises(BundleError):
            build_bundle(self.vault, [item.id], bundle_path)
        result = build_bundle(self.vault, [item.id], bundle_path, force=True)
        self.assertTrue(os.path.isfile(result.path))

    def test_store_option_after_subcommand(self) -> None:
        code = main([
            "add",
            "--store", self.tmp.name,
            "--url", self.base + "/after-store",
            "--claim", "后置 store",
            "--quote", "引用片段在这里",
        ])
        self.assertEqual(code, 0)

    def test_update_and_remove(self) -> None:
        item = capture(
            self.vault,
            claim="旧结论",
            quote="引用片段在这里",
            url=self.base + "/update",
        )
        updated = capture(
            self.vault,
            claim="新结论",
            quote="引用片段在这里",
            url=self.base + "/update",
            update_id=item.id,
        )
        self.assertEqual(updated.id, item.id)
        self.assertEqual(self.vault.get(item.id).claim, "新结论")

        removed = remove_evidence(self.vault, item.id)
        self.assertEqual(removed.id, item.id)
        self.assertIsNone(self.vault.get(item.id))

    def test_workers_validation(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--store", self.tmp.name, "check", "--all", "--workers", "0"])

    def test_retry_after_429(self) -> None:
        PAGE["retry_after_times"] = 1
        with mock.patch("citevault.fetch.time.sleep"):
            item = capture(
                self.vault,
                claim="429",
                quote="引用片段在这里",
                url=self.base + "/retry-429",
            )
        self.assertEqual(item.status_code, 200)

    def test_robots_wildcard(self) -> None:
        rules = [(False, "/private/*"), (True, "/private/public")]
        self.assertFalse(_match_robots(rules, "/private/a"))
        self.assertTrue(_match_robots(rules, "/private/public"))

    def test_parse_robots_crawl_delay(self) -> None:
        rules, delay = _parse_robots(
            "User-agent: *\nCrawl-delay: 4\nDisallow: /private/\n"
        )
        self.assertEqual(delay, 4.0)
        self.assertFalse(_match_robots(rules, "/private/x"))

    def test_empty_check_message(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--store", self.tmp.name, "check", "--all"])
        self.assertEqual(code, 0)
        self.assertIn("存证库为空", output.getvalue())

    def test_userinfo_rejected(self) -> None:
        url = self.base.replace("http://", "http://user:pass@") + "/userinfo"
        with self.assertRaises(FetchError):
            capture(self.vault, claim="userinfo", quote="引用片段在这里", url=url)

    def test_zip_bomb_limit(self) -> None:
        item, bundle_path = self._make_bundle("zipbomb.html")
        with mock.patch("citevault.bundle.MAX_BUNDLE_TOTAL_BYTES", 100):
            report = verify_bundle(bundle_path)
        self.assertFalse(report.valid)
        self.assertIn("解压后超过", report.errors[0])

    def test_bundle_v1_legacy(self) -> None:
        item, bundle_path = self._make_bundle("legacy.html")
        with zipfile.ZipFile(bundle_path, "r") as archive:
            items = {name: archive.read(name) for name in archive.namelist()}
        payload = json.loads(items["bundle.json"].decode("utf-8"))
        payload["version"] = 1
        payload.pop("checks_hash", None)
        payload["entries_hash"] = hashlib.sha256(
            _canonical(payload["entries"])
        ).hexdigest()
        manifest_core = {
            "format": payload["format"],
            "version": 1,
            "created_at": payload["created_at"],
            "tool_version": payload["tool_version"],
            "entries_hash": payload["entries_hash"],
        }
        payload["manifest_hash"] = hashlib.sha256(
            _canonical(manifest_core)
        ).hexdigest()
        items["bundle.json"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with zipfile.ZipFile(bundle_path, "w") as archive:
            for name, data in items.items():
                archive.writestr(name, data)
        self.assertTrue(verify_bundle(bundle_path).valid)

    def test_show_limit_negative(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--store", self.tmp.name, "show", "missing", "--limit", "-1"])

    def test_duplicate_scan_is_warning(self) -> None:
        path = self._write(
            "duplicate-warning.json",
            json.dumps(
                {
                    "citations": [
                        {"claim": "重复", "quote": "引用片段在这里", "file": self._write(
                            "warning.html",
                            "<html><body><main><p>引用片段在这里。</p></main></body></html>",
                        )},
                        {"claim": "重复", "quote": "引用片段在这里", "file": self._write(
                            "warning.html",
                            "<html><body><main><p>引用片段在这里。</p></main></body></html>",
                        )},
                    ]
                },
                ensure_ascii=False,
            ),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--store", self.tmp.name,
                "scan", path, "--workers", "2", "--delay", "0",
            ])
        self.assertEqual(code, 0)
        self.assertIn("duplicate", output.getvalue())

    def test_cli_check_exit_code(self) -> None:
        capture(
            self.vault,
            claim="命令行核查",
            quote="引用片段在这里",
            url=self.base + "/h",
        )
        PAGE["html"] = "<html><body><p>没了。</p></body></html>"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--store", self.tmp.name,
                "check", "--all", "--delay", "0",
            ])
        self.assertEqual(code, 2)
        self.assertIn("quote_missing", output.getvalue())

    def test_cli_check_prints_diff(self) -> None:
        capture(
            self.vault,
            claim="命令行差异",
            quote="引用片段在这里",
            url=self.base + "/cli-diff",
        )
        PAGE["html"] = (
            "<html><body><p>新增了别的内容。</p>"
            "<p>引用片段在这里。</p></body></html>"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--store", self.tmp.name,
                "check", "--all", "--delay", "0",
            ])
        self.assertEqual(code, 0)
        self.assertIn("新增了别的内容", output.getvalue())

    def test_cli_scan_exit_code(self) -> None:
        path = self._write(
            "citations.json",
            json.dumps(
                {
                    "citations": [
                        {
                            "claim": "命令行清单",
                            "quote": "引用片段在这里",
                            "url": self.base + "/cli-scan",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            first = main(["--store", self.tmp.name, "scan", path, "--delay", "0"])
        self.assertEqual(first, 0)

        PAGE["html"] = "<html><body><p>引用片段消失了。</p></body></html>"
        output = io.StringIO()
        with redirect_stdout(output):
            second = main(["--store", self.tmp.name, "scan", path, "--delay", "0"])
        self.assertEqual(second, 2)
        self.assertIn("quote_missing", output.getvalue())


if __name__ == "__main__":
    unittest.main()

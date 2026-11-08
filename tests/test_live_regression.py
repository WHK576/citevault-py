"""真实站点回归；默认跳过。

运行：
    CITEVAULT_LIVE=1 python -m unittest tests.test_live_regression -v
"""

from __future__ import annotations

import os
import tempfile
import unittest

from citevault.anchor import create_anchor, verify_anchor
from citevault.bundle import build_bundle, verify_bundle
from citevault.core import STATUS_CHANGED, STATUS_OK, capture, check_one
from citevault.fetch import FetchError
from citevault.store import Vault

SITES = [
    ("https://www.rfc-editor.org/rfc/rfc2606.txt", "Reserved Top Level DNS Names"),
    ("https://www.gnu.org/philosophy/free-sw.en.html", "a matter of liberty, not price"),
    ("https://www.iana.org/help/example-domains", "maintained for documentation purposes"),
    ("https://www.python.org/about/", "experienced with other languages"),
]

ROBOTS_DENIED = "https://www.gnu.org/private/"
NOT_FOUND = "https://www.rfc-editor.org/definitely-not-a-real-path"
REDIRECT = "https://python.org/about/"


@unittest.skipUnless(
    os.environ.get("CITEVAULT_LIVE") == "1",
    "设置 CITEVAULT_LIVE=1 才跑真实站点",
)
class LiveRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Vault(self.tmp.name)

    def tearDown(self) -> None:
        self.vault.close()
        self.tmp.cleanup()

    def test_happy_path_bundle_and_anchor(self) -> None:
        items = []
        for url, quote in SITES:
            item = capture(
                self.vault,
                claim="live " + url,
                quote=quote,
                url=url,
                timeout=30,
            )
            items.append(item)

        for item in items:
            record = check_one(self.vault, item.id, timeout=30)
            self.assertIn(record.status, {STATUS_OK, STATUS_CHANGED})

        bundle = os.path.join(self.tmp.name, "live.cvbundle")
        build_bundle(self.vault, [item.id for item in items], bundle)
        self.assertTrue(verify_bundle(bundle).valid)

        anchor = os.path.join(self.tmp.name, "live.anchor.json")
        create_anchor(bundle, anchor)
        self.assertTrue(verify_anchor(anchor, bundle).valid)

    def test_robots_denied(self) -> None:
        with self.assertRaises(FetchError):
            capture(
                self.vault,
                claim="robots",
                quote="denied",
                url=ROBOTS_DENIED,
                timeout=30,
            )

    def test_not_found(self) -> None:
        with self.assertRaises(FetchError):
            capture(
                self.vault,
                claim="404",
                quote="not found",
                url=NOT_FOUND,
                timeout=30,
            )

    def test_cross_host_redirect(self) -> None:
        item = capture(
            self.vault,
            claim="redirect",
            quote="experienced with other languages",
            url=REDIRECT,
            timeout=30,
        )
        self.assertIn("www.python.org", item.final_url)
        record = check_one(self.vault, item.id, timeout=30)
        self.assertIn(record.status, {STATUS_OK, STATUS_CHANGED})


if __name__ == "__main__":
    unittest.main()

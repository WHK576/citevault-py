"""SQLite 存储层（线程安全）。"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    claim TEXT NOT NULL,
    quote TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    text_length INTEGER NOT NULL DEFAULT 0,
    snapshot TEXT NOT NULL,
    raw_snapshot TEXT NOT NULL DEFAULT '',
    etag TEXT NOT NULL DEFAULT '',
    last_modified TEXT NOT NULL DEFAULT '',
    allow_missing INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    quote_found INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (evidence_id) REFERENCES evidence(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checks_evidence ON checks(evidence_id);
"""


@dataclass
class Evidence:
    id: str
    url: str
    final_url: str
    source_type: str
    claim: str
    quote: str
    title: str
    note: str
    captured_at: str
    status_code: int
    content_hash: str
    text_hash: str
    text_length: int
    snapshot: str
    raw_snapshot: str
    etag: str
    last_modified: str
    allow_missing: int


@dataclass
class CheckRecord:
    evidence_id: str
    checked_at: str
    status: str
    http_status: int
    content_hash: str
    quote_found: bool
    detail: str
    diff: str = ""


class Vault:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "snapshots").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.root / "vault.db"),
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        # 并发 CI 下减少 database is locked
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """兼容旧库：补上后来新增的列。"""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(evidence)")}
        if "text_length" not in columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN text_length INTEGER NOT NULL DEFAULT 0"
            )
        if "raw_snapshot" not in columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN raw_snapshot TEXT NOT NULL DEFAULT ''"
            )
        if "etag" not in columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN etag TEXT NOT NULL DEFAULT ''"
            )
        if "last_modified" not in columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN last_modified TEXT NOT NULL DEFAULT ''"
            )
        if "allow_missing" not in columns:
            self.conn.execute(
                "ALTER TABLE evidence ADD COLUMN allow_missing INTEGER NOT NULL DEFAULT 0"
            )
        check_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(checks)")
        }
        if "diff" not in check_columns:
            self.conn.execute(
                "ALTER TABLE checks ADD COLUMN diff TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def insert_evidence(self, item: Evidence) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO evidence (id, url, final_url, source_type, claim, quote, title, note, "
                "captured_at, status_code, content_hash, text_hash, text_length, snapshot, "
                "raw_snapshot, etag, last_modified, allow_missing) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id, item.url, item.final_url, item.source_type, item.claim,
                    item.quote, item.title, item.note, item.captured_at, item.status_code,
                    item.content_hash, item.text_hash, item.text_length, item.snapshot,
                    item.raw_snapshot, item.etag, item.last_modified, item.allow_missing,
                ),
            )
            self.conn.commit()

    def update_evidence(self, item: Evidence) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE evidence SET url = ?, final_url = ?, source_type = ?, claim = ?, "
                "quote = ?, title = ?, note = ?, captured_at = ?, status_code = ?, "
                "content_hash = ?, text_hash = ?, text_length = ?, snapshot = ?, "
                "raw_snapshot = ?, etag = ?, last_modified = ?, allow_missing = ? WHERE id = ?",
                (
                    item.url, item.final_url, item.source_type, item.claim, item.quote,
                    item.title, item.note, item.captured_at, item.status_code,
                    item.content_hash, item.text_hash, item.text_length, item.snapshot,
                    item.raw_snapshot, item.etag, item.last_modified, item.allow_missing,
                    item.id,
                ),
            )
            self.conn.commit()

    def update_metadata(
        self,
        evidence_id: str,
        final_url: str,
        etag: str,
        last_modified: str,
    ) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE evidence SET final_url = ?, etag = ?, last_modified = ? WHERE id = ?",
                (final_url, etag, last_modified, evidence_id),
            )
            self.conn.commit()

    def delete_evidence(self, evidence_id: str) -> bool:
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM evidence WHERE id = ?", (evidence_id,)
            )
            self.conn.execute(
                "DELETE FROM checks WHERE evidence_id = ?", (evidence_id,)
            )
            self.conn.commit()
            return cursor.rowcount > 0

    def get(self, evidence_id: str) -> Optional[Evidence]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            return _row_to_evidence(row) if row else None

    def all(self) -> List[Evidence]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM evidence ORDER BY captured_at DESC"
            ).fetchall()
            return [_row_to_evidence(row) for row in rows]

    def insert_check(self, record: CheckRecord) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO checks (evidence_id, checked_at, status, http_status, content_hash, "
                "quote_found, detail, diff) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.evidence_id, record.checked_at, record.status, record.http_status,
                    record.content_hash, int(record.quote_found), record.detail, record.diff,
                ),
            )
            self.conn.commit()

    def checks(self, evidence_id: str) -> List[CheckRecord]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM checks WHERE evidence_id = ? ORDER BY id DESC",
                (evidence_id,),
            ).fetchall()
            return [_row_to_check(row) for row in rows]

    def last_check(self, evidence_id: str) -> Optional[CheckRecord]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM checks WHERE evidence_id = ? ORDER BY id DESC LIMIT 1",
                (evidence_id,),
            ).fetchone()
            return _row_to_check(row) if row else None


def _row_to_evidence(row: sqlite3.Row) -> Evidence:
    return Evidence(
        id=row["id"],
        url=row["url"],
        final_url=row["final_url"],
        source_type=row["source_type"],
        claim=row["claim"],
        quote=row["quote"],
        title=row["title"],
        note=row["note"],
        captured_at=row["captured_at"],
        status_code=row["status_code"],
        content_hash=row["content_hash"],
        text_hash=row["text_hash"],
        text_length=row["text_length"],
        snapshot=row["snapshot"],
        raw_snapshot=row["raw_snapshot"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        allow_missing=row["allow_missing"],
    )


def _row_to_check(row: sqlite3.Row) -> CheckRecord:
    return CheckRecord(
        evidence_id=row["evidence_id"],
        checked_at=row["checked_at"],
        status=row["status"],
        http_status=row["http_status"],
        content_hash=row["content_hash"],
        quote_found=bool(row["quote_found"]),
        detail=row["detail"],
        diff=row["diff"],
    )

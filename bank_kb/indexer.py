"""SQLite (FTS5) index over the downloaded corpus.

Two tables:
    documents     – one row per downloaded PDF, with metadata (bank, type, dates, path).
    doc_fts       – FTS5 virtual table mirroring `documents.title` and `body`.

Plus one helper table:
    manifest      – the set of (bank, url) we've already downloaded, with sha1 of the
                    bytes for change-detection. Lets the daily run skip known URLs fast.

We use FTS5 with `tokenize='porter unicode61 remove_diacritics 1'` for decent recall
on banker-ese. Topic columns (ai_ml, digital_banking, …) are stored as JSON for
filtering by `json_extract`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional


# FTS5 reserved operators: AND OR NOT NEAR ( ) " column:term
# To make the query CLI feel natural, we auto-quote any token that contains
# characters FTS5 would otherwise interpret structurally (like '-' or ':' or
# digits-followed-by-letters in some edge cases). Pass --raw to keep the query
# verbatim if you want explicit boolean syntax.
_FTS_OPS = {"AND", "OR", "NOT", "NEAR"}
_TOKEN_SAFE = re.compile(r"^[A-Za-z0-9_]+$")


def _sanitize_fts_query(q: str) -> str:
    q = (q or "").strip()
    if not q:
        return q
    out_tokens = []
    for tok in q.split():
        if tok in _FTS_OPS or (tok.startswith('"') and tok.endswith('"')):
            out_tokens.append(tok)
        elif _TOKEN_SAFE.match(tok):
            out_tokens.append(tok)
        else:
            # Quote-wrap any tok with hyphens/punctuation/etc. Strip embedded quotes.
            out_tokens.append('"' + tok.replace('"', '') + '"')
    return " ".join(out_tokens)


SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
    bank TEXT NOT NULL,
    url  TEXT NOT NULL,
    sha1 TEXT,
    downloaded_at TEXT,
    file_path TEXT,
    PRIMARY KEY (bank, url)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank TEXT NOT NULL,
    bank_category TEXT,
    doc_type TEXT,
    title TEXT,
    source_url TEXT UNIQUE,
    file_path TEXT,
    fiscal_year INTEGER,
    fiscal_quarter INTEGER,
    calendar_date TEXT,
    page_count INTEGER,
    char_count INTEGER,
    topic_hits TEXT,         -- JSON {topic: count}
    extracted_at TEXT,
    sha1 TEXT
);

CREATE INDEX IF NOT EXISTS idx_doc_bank ON documents (bank);
CREATE INDEX IF NOT EXISTS idx_doc_type ON documents (doc_type);
CREATE INDEX IF NOT EXISTS idx_doc_fy ON documents (fiscal_year, fiscal_quarter);

-- Standalone FTS5 table (not external-content). We INSERT rows explicitly with the
-- same rowid as `documents.id`, so JOINs on rowid work without external-content overhead.
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
    title, body,
    tokenize='porter unicode61 remove_diacritics 1'
);
"""


class Index:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ---------------- manifest (dedupe) ----------------

    def already_downloaded(self, bank: str, url: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM manifest WHERE bank = ? AND url = ?", (bank, url))
        return cur.fetchone() is not None

    def record_download(self, bank: str, url: str, sha1: str, file_path: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO manifest (bank, url, sha1, downloaded_at, file_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (bank, url, sha1, datetime.utcnow().isoformat(timespec='seconds'), file_path),
        )
        self._conn.commit()

    # ---------------- documents + FTS ----------------

    def upsert_document(self, *, bank: str, bank_category: str, doc_type: str,
                        title: str, source_url: str, file_path: str,
                        fiscal_year: Optional[int], fiscal_quarter: Optional[int],
                        calendar_date: Optional[str], page_count: int, char_count: int,
                        topic_hits: dict, sha1: str, body: str) -> int:
        topic_json = json.dumps(topic_hits, sort_keys=True)
        # Upsert by source_url.
        cur = self._conn.execute(
            "SELECT id FROM documents WHERE source_url = ?", (source_url,))
        row = cur.fetchone()
        if row:
            doc_id = row[0]
            self._conn.execute(
                "UPDATE documents SET bank=?, bank_category=?, doc_type=?, title=?, file_path=?, "
                "fiscal_year=?, fiscal_quarter=?, calendar_date=?, page_count=?, char_count=?, "
                "topic_hits=?, extracted_at=?, sha1=? WHERE id=?",
                (bank, bank_category, doc_type, title, file_path,
                 fiscal_year, fiscal_quarter, calendar_date, page_count, char_count,
                 topic_json, datetime.utcnow().isoformat(timespec='seconds'), sha1, doc_id),
            )
            self._conn.execute("DELETE FROM doc_fts WHERE rowid = ?", (doc_id,))
        else:
            cur = self._conn.execute(
                "INSERT INTO documents (bank, bank_category, doc_type, title, source_url, file_path, "
                "fiscal_year, fiscal_quarter, calendar_date, page_count, char_count, topic_hits, "
                "extracted_at, sha1) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bank, bank_category, doc_type, title, source_url, file_path,
                 fiscal_year, fiscal_quarter, calendar_date, page_count, char_count,
                 topic_json, datetime.utcnow().isoformat(timespec='seconds'), sha1),
            )
            doc_id = cur.lastrowid
        self._conn.execute(
            "INSERT INTO doc_fts (rowid, title, body) VALUES (?,?,?)",
            (doc_id, title, body or ""),
        )
        self._conn.commit()
        return doc_id

    # ---------------- search ----------------

    def search(self, query: str, *, bank: Optional[str] = None,
               doc_type: Optional[str] = None, topic: Optional[str] = None,
               fy: Optional[int] = None, limit: int = 25,
               raw: bool = False) -> list[dict]:
        """Full-text search with optional filters. Returns dicts with snippet.

        By default the query is sanitized to be hyphen-/punctuation-safe. Pass
        `raw=True` to send the FTS5 query verbatim (useful for boolean logic
        like `(AI OR genai) AND -KYC`).
        """
        q = query if raw else _sanitize_fts_query(query)
        sql = (
            "SELECT d.id, d.bank, d.doc_type, d.title, d.fiscal_year, d.fiscal_quarter, "
            "       d.calendar_date, d.file_path, d.source_url, d.topic_hits, "
            "       snippet(doc_fts, 1, '<<', '>>', ' … ', 18) AS snippet "
            "FROM doc_fts JOIN documents d ON d.id = doc_fts.rowid "
            "WHERE doc_fts MATCH ? "
        )
        params: list = [q]
        if bank:
            sql += " AND d.bank = ?"; params.append(bank)
        if doc_type:
            sql += " AND d.doc_type = ?"; params.append(doc_type)
        if topic:
            sql += " AND json_extract(d.topic_hits, '$.' || ?) IS NOT NULL"
            params.append(topic)
        if fy:
            sql += " AND d.fiscal_year = ?"; params.append(fy)
        sql += " ORDER BY rank LIMIT ?"; params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        keys = ["id", "bank", "doc_type", "title", "fiscal_year", "fiscal_quarter",
                "calendar_date", "file_path", "source_url", "topic_hits", "snippet"]
        return [dict(zip(keys, r)) for r in rows]

    def stats(self) -> dict:
        c = self._conn.cursor()
        out = {}
        out["total_documents"] = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        out["by_bank"] = dict(c.execute(
            "SELECT bank, COUNT(*) FROM documents GROUP BY bank ORDER BY 2 DESC").fetchall())
        out["by_type"] = dict(c.execute(
            "SELECT doc_type, COUNT(*) FROM documents GROUP BY doc_type ORDER BY 2 DESC").fetchall())
        return out


@contextmanager
def open_index(db_path: Path) -> Iterator[Index]:
    idx = Index(db_path)
    try:
        yield idx
    finally:
        idx.close()

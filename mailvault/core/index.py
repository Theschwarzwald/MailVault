"""Per-file SQLite cache: folder list summaries + full-text search (FTS5).

The cache key is a fingerprint of the file's content (size + first/last MiB),
so re-opening the same PST, even after re-uploading it through Nextcloud,
reuses the finished index instantly.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

CACHE_DIR = ((os.path.join(os.environ["APP_PERSISTENT_STORAGE"], "cache") if os.environ.get("APP_PERSISTENT_STORAGE") else "")
             or os.environ.get("MAILVAULT_CACHE")
             or os.path.join(os.path.expanduser("~"), ".cache", "mailvault"))
SCHEMA = 5


_BUILD_SLOTS = threading.Semaphore(max(1, int(os.environ.get("MAILVAULT_INDEX_THREADS", "2"))))


def fingerprint(path: str, name: str = "") -> str:
    h = hashlib.sha1(f"{SCHEMA}|{name}|".encode())
    try:
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
            if size > 2 << 20:
                fh.seek(-(1 << 20), 2)
                h.update(fh.read())
    except OSError:
        h.update(path.encode())
    return h.hexdigest()


class Index:
    def __init__(self, key: str):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, key + ".db")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS items(fid INT, idx INT, summary TEXT, PRIMARY KEY(fid, idx));
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                subject, sender, recipients, body, attachments, fid UNINDEXED, idx UNINDEXED,
                tokenize='unicode61 remove_diacritics 2');
        """)
        self.state = self.get_meta("state") or "empty"   # empty | building | ready
        self.done = int(self.get_meta("done") or 0)
        self.total = int(self.get_meta("total") or 0)
        self.error = ""
        self.cancel = False

    def get_meta(self, k):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def set_meta(self, **kw):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)", [(k, str(v)) for k, v in kw.items()])
            self.db.commit()

    def summaries(self, fid: int) -> list[dict] | None:
        if self.state != "ready":
            return None
        rows = self.db.execute("SELECT idx, summary FROM items WHERE fid=? ORDER BY idx", (fid,)).fetchall()
        return [dict(json.loads(s), idx=i) for i, s in rows]

    def partial_summaries(self, fid: int) -> list[dict]:
        """Rows indexed so far, while a build is still running."""
        with self.lock:
            rows = self.db.execute("SELECT idx, summary FROM items WHERE fid=? ORDER BY idx", (fid,)).fetchall()
        return [dict(json.loads(s), idx=i) for i, s in rows]

    def put(self, fid, idx, summary: dict, body: str, recipients: str, attachments: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO items VALUES(?,?,?)", (fid, idx, json.dumps(summary)))
            self.db.execute("DELETE FROM fts WHERE fid=? AND idx=?", (fid, idx))
            self.db.execute("INSERT INTO fts VALUES(?,?,?,?,?,?,?)",
                            (summary.get("subject", ""), summary.get("from", ""), recipients, body[:200_000],
                             attachments, fid, idx))

    def search(self, q: str, limit: int = 1000) -> list[tuple[int, int]]:
        terms = [t.replace('"', "") for t in q.split() if t.strip('"')]
        if not terms:
            return []
        # field filters: from:dana subject:invoice to:you body:… has:attachment is unsupported here
        parts = []
        for t in terms:
            col = None
            if ":" in t:
                c, _, v = t.partition(":")
                if c in ("from", "subject", "to", "body", "attachment"):
                    col = {"from": "sender", "to": "recipients", "attachment": "attachments"}.get(c, c)
                    t = v
            if not t:
                continue
            parts.append((f"{col}:" if col else "") + f'"{t}"*')
        if not parts:
            return []
        try:
            return self.db.execute("SELECT fid, idx FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
                                   (" AND ".join(parts), limit)).fetchall()
        except sqlite3.OperationalError:
            return []

    def build(self, source, on_change=None):
        """Index every item (runs in a background thread; at most a few builds run at once)."""
        self.state = "building"
        with _BUILD_SLOTS:
            self._build(source, on_change)

    def _build(self, source, on_change=None):
        try:
            self.state = "building"
            folders = list(source.folders.items())
            self.total = sum(len(f.items) for _, f in folders)
            self.done = 0
            self.set_meta(state="building", total=self.total, done=0)
            last = time.time()
            for fid, f in folders:
                for i in range(len(f.items)):
                    if self.cancel:
                        return
                    m = source.message(fid, i, cache=False)
                    s = source.summary_of(fid, i, m)
                    body = m.body_text or ""
                    if not body and m.body_html:
                        from .mime import html_to_text
                        body = html_to_text(m.body_html)
                    if m.item:
                        body += "\n" + " ".join(str(v) for v in m.item.values() if isinstance(v, str))
                    self.put(fid, i, s, body, " ".join(map(str, m.to + m.cc + m.bcc)),
                             " ".join(a.filename for a in m.attachments))
                    self.done += 1
                    if time.time() - last > 1:
                        with self.lock:
                            self.db.commit()
                        self.set_meta(done=self.done)
                        last = time.time()
                        if on_change:
                            on_change()
            with self.lock:
                self.db.commit()
            self.state = "ready"
            self.set_meta(state="ready", done=self.done, built=time.time())
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", str(e)
        finally:
            if on_change:
                on_change()

    def status(self) -> dict:
        return {"state": self.state, "done": self.done, "total": self.total, "error": self.error}

    def close(self):
        self.cancel = True
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass

"""The factory's memory (one SQLite file): what it has seen, checked, posted, and how you reacted."""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .models import Candidate

COLUMNS = {
    "id": "TEXT PRIMARY KEY", "title": "TEXT", "url": "TEXT", "source": "TEXT", "signal": "TEXT",
    "possibly_pitched": "TEXT", "first_seen": "REAL", "status": "TEXT", "score": "INTEGER", "data": "TEXT",
    "updated": "REAL", "lane": "TEXT", "archive": "INTEGER DEFAULT 0", "channel_id": "TEXT",
    "message_id": "TEXT", "posted_at": "REAL", "up": "INTEGER DEFAULT 0", "down": "INTEGER DEFAULT 0",
    "stars": "INTEGER DEFAULT 0", "picked_posted": "INTEGER DEFAULT 0",
}


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS items ({', '.join(f'{k} {v}' for k, v in COLUMNS.items())})")
        have = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        for col, kind in COLUMNS.items():  # upgrade older files
            if col not in have:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} {kind.replace(' PRIMARY KEY', '')}")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_status ON items(status)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_first_seen ON items(first_seen)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (at REAL, report TEXT)")
        # The search pool: every search phrase the factory knows (lane searches, autocomplete finds, trends)
        # and how well each one has worked, so good ones are kept and dead ones are dropped.
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS queries (q TEXT PRIMARY KEY, lane TEXT, origin TEXT, added REAL,
                   expires REAL, last_news REAL DEFAULT 0, last_yt REAL DEFAULT 0, searches INTEGER DEFAULT 0,
                   finds INTEGER DEFAULT 0, kept INTEGER DEFAULT 0, dead INTEGER DEFAULT 0)"""
        )
        self.db.commit()

    # -------------------------------------------------- items
    def seen(self, item_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone() is not None

    def add(self, c: Candidate, status: str, score: int | None = None, data: dict | None = None,
            lane: str | None = None, light: bool = False, commit: bool = True) -> None:
        """light=True only remembers that the headline was seen (used for the thousands of filtered ones)."""
        now = time.time()
        title, url, source = (None, None, None) if light else (c.title, c.url, c.source)
        self.db.execute(
            """INSERT INTO items (id, title, url, source, signal, possibly_pitched, first_seen, status, score, data,
                                  updated, lane, archive)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, score=COALESCE(excluded.score, items.score),
               data=COALESCE(excluded.data, items.data), lane=COALESCE(excluded.lane, items.lane),
               updated=excluded.updated""",
            (c.id, title, url, source, None if light else c.signal, None if light else c.possibly_pitched, now,
             status, score, json.dumps(data) if data is not None else None, now, lane, int(c.archive)),
        )
        if commit:
            self.db.commit()

    def commit(self) -> None:
        self.db.commit()

    def mark_posted(self, item_id: str, channel_id: str, message_id: str) -> None:
        self.db.execute(
            "UPDATE items SET status='posted', channel_id=?, message_id=?, posted_at=?, updated=? WHERE id=?",
            (channel_id, message_id, time.time(), time.time(), item_id),
        )
        self.db.commit()

    def deep_pending(self, limit: int, max_age_days: int = 10) -> list[dict]:
        """Stories that passed the quick screen and are waiting for the deep check (survives AI outages)."""
        return self._rows(
            "SELECT * FROM items WHERE status='deep_pending' AND updated>=? "
            "ORDER BY score DESC, archive ASC, updated DESC LIMIT ?",
            (time.time() - max_age_days * 86400, limit),
        )

    def screen_pending(self, limit: int, max_age_days: int = 5) -> list[dict]:
        """Headlines that passed the keyword filter but didn't fit in an earlier run's quick screen."""
        return self._rows(
            "SELECT * FROM items WHERE status='screen_pending' AND first_seen>=? ORDER BY score DESC, first_seen DESC "
            "LIMIT ?",
            (time.time() - max_age_days * 86400, limit),
        )

    def restore_posted(self, item_id: str, title: str, url: str, lane: str, channel_id: str, message_id: str,
                       story: str, up: int, down: int, stars: int, posted_at: float) -> bool:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO items (id, title, url, source, first_seen, status, data, updated, lane,
                   channel_id, message_id, posted_at, up, down, stars, picked_posted)
               VALUES (?,?,?,?,?,'posted',?,?,?,?,?,?,?,?,?,1)""",
            (item_id, title, url, "restored from Discord", posted_at, json.dumps({"story": story}), posted_at,
             lane, channel_id, message_id, posted_at, up, down, stars),
        )
        self.db.commit()
        return cur.rowcount > 0

    def recent_titles(self, days: int = 14) -> list[str]:
        rows = self.db.execute("SELECT title FROM items WHERE first_seen>=? AND title IS NOT NULL",
                               (time.time() - days * 86400,))
        return [r[0] for r in rows]

    def _rows(self, sql: str, params=()) -> list[dict]:
        cur = self.db.execute(sql, params)
        keys = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            item = dict(zip(keys, row))
            if "data" in item:
                item["data"] = json.loads(item["data"] or "{}")
            out.append(item)
        return out

    def ready_to_post(self, limit: int, max_age_days: int = 3) -> list[dict]:
        return self._rows(
            "SELECT * FROM items WHERE status='ready' AND updated>=? ORDER BY score DESC, updated DESC LIMIT ?",
            (time.time() - max_age_days * 86400, limit),
        )

    def set_status(self, item_id: str, status: str) -> None:
        self.db.execute("UPDATE items SET status=?, updated=? WHERE id=?", (status, time.time(), item_id))
        self.db.commit()

    def count(self, status: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM items WHERE status=?", (status,)).fetchone()[0]

    def posted_since(self, since: float) -> list[dict]:
        return self._rows("SELECT * FROM items WHERE status='posted' AND posted_at>=?", (since,))

    def posted_today_in_lane(self, lane: str, since: float) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE status='posted' AND lane=? AND posted_at>=?", (lane, since)
        ).fetchone()[0]

    def set_feedback(self, channel_id: str, message_id: str, up: int, down: int, stars: int) -> None:
        self.db.execute(
            "UPDATE items SET up=?, down=?, stars=? WHERE channel_id=? AND message_id=?",
            (up, down, stars, channel_id, message_id),
        )
        self.db.commit()

    def taste_examples(self, n: int = 10) -> tuple[list[dict], list[dict]]:
        liked = self._rows(
            "SELECT title, data, up, down, stars FROM items WHERE status='posted' AND (stars>0 OR up>down) "
            "ORDER BY stars DESC, (up-down) DESC, posted_at DESC LIMIT ?", (n,))
        disliked = self._rows(
            "SELECT title, data, up, down, stars FROM items WHERE status='posted' AND down>up AND stars=0 "
            "ORDER BY (down-up) DESC, posted_at DESC LIMIT ?", (n,))
        return liked, disliked

    def to_repost_as_picked(self) -> list[dict]:
        return self._rows("SELECT * FROM items WHERE status='posted' AND stars>0 AND picked_posted=0")

    def mark_picked_posted(self, item_id: str) -> None:
        self.db.execute("UPDATE items SET picked_posted=1 WHERE id=?", (item_id,))
        self.db.commit()

    # -------------------------------------------------- meta + run log
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def bump_run(self) -> int:
        run_no = int(self.get_meta("run_no", "0")) + 1
        self.set_meta("run_no", str(run_no))
        return run_no

    def log_run(self, report: dict) -> None:
        self.db.execute("INSERT INTO runs (at, report) VALUES (?, ?)", (time.time(), json.dumps(report)))
        self.db.commit()

    def runs_since(self, since: float) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT report FROM runs WHERE at>=?", (since,))]

    def prune(self, keep_days: int = 180, light_days: int = 21) -> None:
        cutoff = time.time() - keep_days * 86400
        self.db.execute("DELETE FROM items WHERE first_seen<? AND status!='posted'", (cutoff,))
        # filtered headlines are only "seen" markers; old ones are dropped to keep the memory file small
        self.db.execute("DELETE FROM items WHERE first_seen<? AND title IS NULL AND status!='posted'",
                        (time.time() - light_days * 86400,))
        self.db.execute("DELETE FROM runs WHERE at<?", (cutoff,))
        self.db.execute("DELETE FROM queries WHERE expires IS NOT NULL AND expires<?", (time.time(),))
        self.db.commit()

    # -------------------------------------------------- the search pool
    def pool_add(self, rows: list[tuple[str, str, str]], expires: float | None = None, cap: int = 0) -> int:
        """rows: (search phrase, lane, origin). Returns how many were new. cap: stop adding past this pool size."""
        now, added = time.time(), 0
        size = self.pool_size() if cap else 0
        for q, lane, origin in rows:
            q = " ".join((q or "").split())[:150]
            if not q or (cap and size >= cap):
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO queries (q, lane, origin, added, expires) VALUES (?,?,?,?,?)",
                (q, lane or "", origin, now, expires),
            )
            if cur.rowcount:
                added += 1
                size += 1
            elif expires:  # a trend seen again stays alive longer
                self.db.execute("UPDATE queries SET expires=MAX(COALESCE(expires,0), ?) WHERE q=?", (expires, q))
        self.db.commit()
        return added

    def pool_sync(self, origin: str, current: list[str]) -> None:
        """Searches you removed from config.yaml lose their special place (they can then retire like any other)."""
        keep = {" ".join(q.split())[:150] for q in current}
        stale = [r[0] for r in self.db.execute("SELECT q FROM queries WHERE origin=?", (origin,)) if r[0] not in keep]
        self.db.executemany("UPDATE queries SET origin='old-' || origin WHERE q=?", [(q,) for q in stale])
        self.db.commit()

    def pool_size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM queries WHERE dead=0").fetchone()[0]

    def pool_due(self, n: int, engine: str = "news", origins: tuple[str, ...] | None = None,
                 min_gap_hours: float = 0) -> list[tuple[str, str]]:
        """The n searches most worth running now: trends, then your lane searches (twice a day), then the
        longest-waiting ones, best performers first."""
        col = "last_yt" if engine == "yt" else "last_news"
        where = ["dead=0", "(expires IS NULL OR expires>?)", f"{col}<=?"]
        params: list = [time.time(), time.time() - min_gap_hours * 3600]
        if origins:
            where.append(f"origin IN ({','.join('?' * len(origins))})")
            params += list(origins)
        rows = self.db.execute(
            f"SELECT q, lane FROM queries WHERE {' AND '.join(where)} "
            f"ORDER BY (origin='trend') DESC, (origin='lane' AND {col}<?) DESC, {col} ASC, "
            f"(kept*5+finds+1.0)/(searches+1) DESC LIMIT ?",
            (*params, time.time() - 12 * 3600, n),
        )
        return [(r[0], r[1]) for r in rows]

    def pool_searched(self, queries, engine: str = "news") -> None:
        col = "last_yt" if engine == "yt" else "last_news"
        now = time.time()
        self.db.executemany(f"UPDATE queries SET {col}=?, searches=searches+1 WHERE q=?", [(now, q) for q in queries])
        self.db.commit()

    def pool_credit(self, finds: dict[str, int], kept: dict[str, int]) -> None:
        self.db.executemany("UPDATE queries SET finds=finds+? WHERE q=?", [(n, q) for q, n in finds.items()])
        self.db.executemany("UPDATE queries SET kept=kept+? WHERE q=?", [(n, q) for q, n in kept.items()])
        self.db.commit()

    def pool_retire(self, after_searches: int = 6) -> int:
        """Searches that never found a single kid story after several tries are switched off."""
        cur = self.db.execute(
            "UPDATE queries SET dead=1 WHERE dead=0 AND origin NOT IN ('lane','seed') AND searches>=? AND finds=0",
            (after_searches,),
        )
        self.db.commit()
        return cur.rowcount

    def pool_best(self, n: int) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT q, lane FROM queries WHERE dead=0 AND kept>0 AND origin!='trend' "
            "ORDER BY (kept*5.0+finds)/(searches+1) DESC LIMIT ?", (n,))
        return [(r[0], r[1]) for r in rows]

    def pool_stats(self) -> dict:
        rows = self.db.execute("SELECT origin, COUNT(*) FROM queries WHERE dead=0 GROUP BY origin").fetchall()
        dead = self.db.execute("SELECT COUNT(*) FROM queries WHERE dead=1").fetchone()[0]
        return {**{r[0]: r[1] for r in rows}, "retired": dead}

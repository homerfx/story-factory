"""The archive digger: walks back month by month through old stories, a few searches per run."""
from __future__ import annotations

import time
from datetime import date

from .collect import HTTP, google_news, lane_queries, youtube, yt_allowance, yt_spend
from .models import Candidate


def month_windows(oldest_year: int, today: date | None = None) -> list[tuple[str, str]]:
    """Newest first, starting two months back (fresh searches cover the recent weeks)."""
    today = today or date.today()
    y, m = today.year, today.month - 2
    while m <= 0:
        y, m = y - 1, m + 12
    out = []
    while y >= oldest_year:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def dig(http: HTTP, cfg: dict, store, run_no: int, log) -> tuple[list[Candidate], dict]:
    acfg = cfg.get("archive") or {}
    if not acfg.get("enabled"):
        return [], {}
    windows = month_windows(int(acfg.get("oldest_year", 2008)))
    queries = [(q, lane) for q, lane in lane_queries(cfg) if not q.startswith("site:")] or lane_queries(cfg)
    # plus the pool's proven performers: the searches that have already found stories the AI liked
    queries += [p for p in store.pool_best(int(acfg.get("best_pool_queries", 100))) if p not in queries]
    if not windows or not queries:
        return [], {}

    w_i = int(store.get_meta("archive_window", "0")) % len(windows)
    q_i = int(store.get_meta("archive_query", "0")) % len(queries)
    per_run = int(acfg.get("queries_per_run", 8))
    batch = queries[q_i : q_i + per_run]
    window = windows[w_i]

    gcfg = {**cfg.get("sources", {}).get("google_news", {}), "per_query": 40, "_deadline": cfg.get("_deadline_collect")}
    found = google_news(http, batch, gcfg, log, window)
    counts = {f"archive {window[0][:7]}": len(found)}

    ycfg = cfg.get("sources", {}).get("youtube") or {}
    if ycfg.get("enabled") and run_no % int(ycfg.get("every_n_runs", 1) or 1) == 0:
        n = yt_allowance(store, ycfg, int(acfg.get("youtube_queries_per_run", 1)))
        yq = [q for q, _ in batch][:n]
        if yq:
            yt_spend(store, len(yq))
            yt = youtube(http, yq, ycfg, log, window)
            counts[f"archive youtube {window[0][:7]}"] = len(yt)
            found += yt

    # advance the cursor: next queries, or the next (older) month when all queries are done
    q_next = q_i + per_run
    if q_next >= len(queries):
        q_next = 0
        w_i = (w_i + 1) % len(windows)  # after the oldest month it starts over from the newest
    store.set_meta("archive_query", str(q_next))
    store.set_meta("archive_window", str(w_i))
    store.set_meta("archive_last", f"{window[0][:7]} at {time.strftime('%Y-%m-%d %H:%M')}")
    return found, counts

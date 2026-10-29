"""Factory 1: collectors. Each returns a list of Candidates and never crashes the run."""
from __future__ import annotations

import calendar
import html
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import requests

from .models import Candidate, fmt_num
from .world import LOCALES, lang_of, split_loc

# An ordinary browser signature: websites see a normal visitor, nothing that names this tool.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class HTTP:
    """Thin wrapper around requests (one connection pool per thread) so tests can swap in fake responses."""

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            s.headers["Accept-Language"] = "en-US,en;q=0.9"
            self._local.session = s
        return s

    def get(self, url: str, params: dict | None = None, timeout: int = 20, headers: dict | None = None,
            stream: bool = False, allow_redirects: bool = True):
        return self.session.get(url, params=params, timeout=timeout, headers=headers, stream=stream,
                                allow_redirects=allow_redirects)


def clean(text: str, limit: int = 500) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def entry_time(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return float(calendar.timegm(value))
    return None


def iso_time(value: str) -> float | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            pass
    return None


# ---------------------------------------------------------------- running many searches at once

def parallel(fn, items, workers: int = 1, deadline: float | None = None, max_errors: int = 25):
    """Runs fn(item) for every item on a few threads and returns (all results in one list, stats).

    Stops starting new searches after the deadline (the run's time budget) or once a site has refused
    max_errors times, so one blocked site can't waste the whole run."""
    stats: dict = {"done": 0, "skipped": 0, "errors": 0, "first_error": "", "done_items": []}
    lock = threading.Lock()

    def wrap(item):
        with lock:
            if (deadline is not None and time.time() > deadline) or stats["errors"] >= max_errors:
                stats["skipped"] += 1
                return []
        try:
            result = fn(item) or []
        except Exception as ex:  # noqa: BLE001
            with lock:
                stats["errors"] += 1
                stats["first_error"] = stats["first_error"] or quiet_error(ex)
            return []
        with lock:
            stats["done"] += 1
            stats["done_items"].append(item)
        return result

    items = list(items)
    if workers <= 1 or len(items) <= 1:
        results = [wrap(i) for i in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(wrap, items))
    return [x for r in results for x in r], stats


def quiet_error(ex) -> str:
    """Error text without web addresses, so the logs never show what was searched."""
    return re.sub(r"https?://\S+", "<link>", str(ex))[:160]


def report_stats(log, name: str, stats: dict) -> None:
    msg = f"  {name}: {stats['done']} done"
    if stats["skipped"]:
        msg += f", {stats['skipped']} skipped (time budget used up or the site kept refusing)"
    if stats["errors"]:
        msg += f", {stats['errors']} failed (first problem: {stats['first_error']})"
    log(msg)


def _refused(r) -> None:
    if r.status_code in (429, 503):
        raise RuntimeError(f"HTTP {r.status_code}: the site is slowing the factory down")
    r.raise_for_status()


# ---------------------------------------------------------------- Google News (fresh + archive)

def google_news(http: HTTP, queries: list[tuple[str, str]], cfg: dict, log, window: tuple[str, str] | None = None,
                stats_out: dict | None = None):
    """queries: (search words, lane). window: ('2019-03-01', '2019-04-01') for archive digging."""
    when, per_query = cfg.get("when", "1d"), int(cfg.get("per_query", 30))
    pause = float(cfg.get("pause_seconds", 1.5))

    def one(pair):
        query, lane = pair
        loc, text = split_loc(query)  # "[pt-BR] menino salvou" searches Brazil's edition in Portuguese
        hl, gl, ceid = LOCALES.get(loc, ("en-US", "US", "US:en"))
        q = f"{text} after:{window[0]} before:{window[1]}" if window else f"{text} when:{when}"
        r = http.get(f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={ceid}")
        _refused(r)
        feed = feedparser.parse(r.content)
        out = []
        for e in feed.entries[:per_query]:
            title = clean(e.get("title", ""), 300)
            src = e.get("source") or {}
            publisher = src.get("title", "") if hasattr(src, "get") else ""
            if publisher and title.endswith(" - " + publisher):
                title = title[: -(len(publisher) + 3)]
            out.append(
                Candidate(
                    title=title,
                    url=e.get("link", ""),
                    source=f"Google News{' ' + gl if loc else ''} · {publisher or 'news'}"
                    + (f" ({window[0][:7]})" if window else ""),
                    published=entry_time(e),
                    lane_hint=lane,
                    archive=bool(window),
                    extra={"q": query},
                )
            )
        if pause:
            time.sleep(pause)
        return out

    out, stats = parallel(one, queries, int(cfg.get("workers", 1)), cfg.get("_deadline"), int(cfg.get("max_errors", 25)))
    report_stats(log, "google news" + (f" {window[0][:7]}" if window else ""), stats)
    if stats_out is not None:
        stats_out.update(stats)
    return out


# ---------------------------------------------------------------- Bing News (different results, real article links)

def bing_link(link: str) -> str:
    """Bing's RSS links go through a Bing redirect; the real article address is in its url= part."""
    try:
        qs = parse_qs(urlparse(link).query)
        for key in ("url", "u"):
            if qs.get(key) and qs[key][0].startswith("http"):
                return qs[key][0]
    except ValueError:
        pass
    return link


def bing_news(http: HTTP, queries: list[tuple[str, str]], cfg: dict, log, stats_out: dict | None = None):
    per_query, pause = int(cfg.get("per_query", 20)), float(cfg.get("pause_seconds", 0.3))

    def one(pair):
        query, lane = pair
        loc, text = split_loc(query)
        params = {"q": text, "format": "rss"}
        if loc:
            params["mkt"] = loc  # that country's Bing News
        r = http.get("https://www.bing.com/news/search", params=params)
        _refused(r)
        feed = feedparser.parse(r.content)
        out = []
        for e in feed.entries[:per_query]:
            url = bing_link(e.get("link", ""))
            publisher = e.get("news_source") or urlparse(url).netloc.replace("www.", "")
            out.append(
                Candidate(
                    title=clean(e.get("title", ""), 300),
                    url=url,
                    source=f"Bing News{' ' + loc.split('-')[1] if loc else ''} · {publisher or 'news'}",
                    summary=clean(e.get("summary", ""), 500),
                    published=entry_time(e),
                    lane_hint=lane,
                    extra={"q": query},
                )
            )
        if pause:
            time.sleep(pause)
        return out

    out, stats = parallel(one, queries, int(cfg.get("workers", 1)), cfg.get("_deadline"), int(cfg.get("max_errors", 25)))
    report_stats(log, "bing news", stats)
    if stats_out is not None:
        stats_out.update(stats)
    return out


# ---------------------------------------------------------------- GDELT (worldwide news, free)

def gdelt(http: HTTP, cfg: dict, log) -> list[Candidate]:
    out: list[Candidate] = []
    failed = 0
    for query in cfg.get("queries", []):
        if cfg.get("_deadline") and time.time() > cfg["_deadline"]:
            break
        try:
            r = http.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": cfg.get("max_records", 75),
                    "timespan": cfg.get("timespan", "1d"),
                    "sort": "hybridrel",
                },
                timeout=30,
            )
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError(clean(r.text, 150)) from None
            for a in data.get("articles", []):
                language = (a.get("language") or "").strip()
                country = (a.get("sourcecountry") or "").strip()
                foreign = bool(language) and language.lower() != "english"
                where = " · ".join(x for x in (country, language if foreign else "") if x)
                out.append(
                    Candidate(
                        title=clean(a.get("title", ""), 300),
                        url=a.get("url", ""),
                        source=f"GDELT · {where + ' · ' if where else ''}{a.get('domain', 'news')}",
                        published=iso_time(a.get("seendate", "")),
                        # GDELT matched our English words in its translation of this article
                        extra={"translated_match": foreign, "q": query},
                    )
                )
        except Exception as ex:  # noqa: BLE001
            failed += 1
            if failed == 1:
                log(f"  gdelt: {quiet_error(ex)}")
        time.sleep(cfg.get("pause_seconds", 6))  # GDELT asks for one request every 5 seconds
    return out


# ---------------------------------------------------------------- Reddit

def reddit(http: HTTP, cfg: dict, log) -> list[Candidate]:
    out: list[Candidate] = []
    failed = []
    for sub in cfg.get("subreddits", []):
        try:
            out.extend(_reddit_json(http, sub, cfg))
        except Exception as ex:  # noqa: BLE001
            try:  # JSON is often blocked from cloud servers; RSS sometimes still works
                out.extend(_reddit_rss(http, sub, cfg))
            except Exception as ex2:  # noqa: BLE001
                failed.append(f"{quiet_error(ex)} / rss: {quiet_error(ex2)}")
        time.sleep(cfg.get("pause_seconds", 2))
    if failed:
        log(f"  reddit: {len(failed)} subreddits blocked or failed (first: {failed[0]})")
    return out


def _reddit_json(http: HTTP, sub: str, cfg: dict) -> list[Candidate]:
    r = http.get(
        f"https://www.reddit.com/r/{sub}/top.json",
        params={"t": cfg.get("time", "day"), "limit": cfg.get("limit", 25)},
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    out = []
    for child in r.json()["data"]["children"]:
        d = child["data"]
        if d.get("stickied") or d.get("over_18"):
            continue
        ups = int(d.get("ups", 0))
        if ups < cfg.get("min_upvotes", 0):
            continue
        out.append(
            Candidate(
                title=clean(d.get("title", ""), 300),
                url="https://www.reddit.com" + d.get("permalink", ""),
                source=f"Reddit r/{sub}",
                summary=clean(d.get("selftext", ""), 500),
                published=d.get("created_utc"),
                signal=f"{fmt_num(ups)} upvotes, {fmt_num(int(d.get('num_comments', 0)))} comments",
                signal_value=ups,
            )
        )
    return out


def _reddit_rss(http: HTTP, sub: str, cfg: dict) -> list[Candidate]:
    r = http.get(f"https://www.reddit.com/r/{sub}/top/.rss", params={"t": cfg.get("time", "day")})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    feed = feedparser.parse(r.content)
    return [
        Candidate(
            title=clean(e.get("title", ""), 300),
            url=e.get("link", ""),
            source=f"Reddit r/{sub}",
            published=entry_time(e),
        )
        for e in feed.entries[: cfg.get("limit", 25)]
    ]


# ---------------------------------------------------------------- plain RSS feeds

def rss(http: HTTP, cfg: dict, log) -> list[Candidate]:
    out: list[Candidate] = []
    for feed_url in cfg.get("feeds", []):
        try:
            r = http.get(feed_url)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            name = clean(feed.feed.get("title", "") or feed_url, 60)
            for e in feed.entries[: cfg.get("per_feed", 40)]:
                out.append(
                    Candidate(
                        title=clean(e.get("title", ""), 300),
                        url=e.get("link", ""),
                        source=f"RSS · {name}",
                        summary=clean(e.get("summary", ""), 500),
                        published=entry_time(e),
                    )
                )
        except Exception as ex:  # noqa: BLE001
            log(f"  rss feed failed: {quiet_error(ex)}")
    return out


# ---------------------------------------------------------------- YouTube (free key: 10,000 units a day)

YT_COST = 101  # one search (100 units) + one view-count lookup (1 unit)


def _yt_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))  # YouTube's day starts at midnight Pacific


def yt_allowance(store, cfg: dict, wanted: int) -> int:
    """How many YouTube searches are still allowed today (the free key gives 10,000 units a day)."""
    if not os.environ.get("YOUTUBE_API_KEY"):
        return 0
    if store is None:
        return wanted
    used = int(store.get_meta(f"yt_units_{_yt_day()}", "0") or 0)
    return max(0, min(wanted, (int(cfg.get("daily_units", 9000)) - used) // YT_COST))


def yt_spend(store, searches: int) -> None:
    if store is None or searches <= 0:
        return
    key = f"yt_units_{_yt_day()}"
    store.set_meta(key, str(int(store.get_meta(key, "0") or 0) + searches * YT_COST))


def youtube(http: HTTP, queries: list[str], cfg: dict, log, window: tuple[str, str] | None = None):
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        log("  youtube: no YOUTUBE_API_KEY, skipped")
        return []
    out: list[Candidate] = []
    failed = 0
    if window:
        after, before = window[0] + "T00:00:00Z", window[1] + "T00:00:00Z"
    else:
        after = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - cfg.get("published_within_hours", 72) * 3600)
        )
        before = None
    for query in queries:
        try:
            loc, text = split_loc(query)
            params = {
                "part": "snippet", "q": text, "type": "video", "order": "viewCount",
                "publishedAfter": after, "maxResults": cfg.get("per_query", 15),
                "relevanceLanguage": lang_of(loc),
            }
            if before:
                params["publishedBefore"] = before
            r = http.get("https://www.googleapis.com/youtube/v3/search", params=params, headers={"X-Goog-Api-Key": key})
            r.raise_for_status()
            items = r.json().get("items", [])
            ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
            if not ids:
                continue
            s = http.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "statistics", "id": ",".join(ids)},
                headers={"X-Goog-Api-Key": key},
            )
            s.raise_for_status()
            views = {v["id"]: int(v.get("statistics", {}).get("viewCount", 0)) for v in s.json().get("items", [])}
            for i in items:
                vid = i.get("id", {}).get("videoId")
                if not vid or views.get(vid, 0) < cfg.get("min_views", 0):
                    continue
                sn = i.get("snippet", {})
                out.append(
                    Candidate(
                        title=clean(sn.get("title", ""), 300),
                        url=f"https://www.youtube.com/watch?v={vid}",
                        source=f"YouTube · {sn.get('channelTitle', '')}",
                        summary=clean(sn.get("description", ""), 500),
                        published=iso_time(sn.get("publishedAt", "")),
                        signal=f"{fmt_num(views[vid])} views",
                        signal_value=views[vid],
                        archive=bool(window),
                        extra={"q": query},
                    )
                )
        except Exception as ex:  # noqa: BLE001
            failed += 1
            if failed == 1:
                log(f"  youtube: {quiet_error(str(ex).replace(key, '***'))}")
    return out


# ---------------------------------------------------------------- everything fresh

def lane_queries(cfg: dict) -> list[tuple[str, str]]:
    pairs = [(q, lane) for lane, spec in (cfg.get("lanes") or {}).items() for q in spec.get("queries", [])]
    pairs += [(q, "") for q in cfg.get("general_queries", [])]
    return pairs


def collect_fresh(http: HTTP, cfg: dict, run_no: int, log, store=None) -> tuple[list[Candidate], dict, dict]:
    """Runs every source at the same time. With a store, the news searches come from the big search pool.
    Returns (stories found, stories per source, searches per source)."""
    src = cfg.get("sources", {})
    deadline = cfg.get("_deadline_collect")
    pool_cfg = cfg.get("search_pool") or {}
    if store is not None:
        news_queries = store.pool_due(int(pool_cfg.get("queries_per_run", 600)), "news")
    else:
        news_queries = lane_queries(cfg)

    # YouTube: the store is only touched here in the main thread (SQLite doesn't like being shared by threads)
    ycfg = src.get("youtube") or {}
    yt_queries: list[str] = []
    if ycfg.get("enabled") and run_no % int(ycfg.get("every_n_runs", 1) or 1) == 0:
        n = yt_allowance(store, ycfg, int(ycfg.get("searches_per_run", 20)))
        if store is None:
            yt_queries = list(ycfg.get("queries", []))[:n]
        elif n:
            picks = store.pool_due(n, "yt", origins=("yt-seed",), min_gap_hours=20)
            picks += store.pool_due(n - len(picks), "yt", origins=("yt-suggest", "trend")) if n > len(picks) else []
            yt_queries = [q for q, _ in picks]
        yt_spend(store, len(yt_queries))
        if not os.environ.get("YOUTUBE_API_KEY"):
            log("  youtube: no YOUTUBE_API_KEY, skipped")
        elif not yt_queries:
            log("  youtube: today's free searches are used up")

    news_stats: dict[str, dict] = {"google_news": {}, "bing": {}}
    jobs = {
        "google_news": lambda s: google_news(http, news_queries, {**s, "_deadline": deadline}, log,
                                             stats_out=news_stats["google_news"]),
        "bing": lambda s: bing_news(http, news_queries, {**s, "_deadline": deadline}, log, stats_out=news_stats["bing"]),
        "gdelt": lambda s: gdelt(http, {**s, "_deadline": deadline}, log),
        "reddit": lambda s: reddit(http, s, log),
        "rss": lambda s: rss(http, s, log),
        "youtube": lambda s: youtube(http, yt_queries, s, log) if yt_queries else [],
    }
    active = {}
    for name, job in jobs.items():
        scfg = src.get(name) or {}
        if not scfg.get("enabled"):
            continue
        if name == "youtube" and not yt_queries:
            continue
        if name != "youtube" and run_no % int(scfg.get("every_n_runs", 1) or 1) != 0:
            continue
        active[name] = (job, scfg)

    def run_job(name):
        job, scfg = active[name]
        try:
            return name, job(scfg)
        except Exception as ex:  # noqa: BLE001
            log(f"  {name} failed: {quiet_error(ex)}")
            return name, []

    found: list[Candidate] = []
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(active))) as pool:
        for name, items in pool.map(run_job, list(active)):
            counts[name] = len(items)
            found.extend(items)

    searched = {q for st in news_stats.values() for q, _ in st.get("done_items", [])}
    if store is not None:
        store.pool_searched(searched, "news")
        store.pool_searched(yt_queries, "yt")
    searches = {name: news_stats[name].get("done", 0) for name in news_stats if name in active}
    if "youtube" in active:
        searches["youtube"] = len(yt_queries)
    return found, counts, searches

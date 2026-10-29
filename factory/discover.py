"""The wide net: finds out what people are searching for right now and turns it into thousands of searches.

- Google Trends "trending now" in many countries: the trending stories themselves are checked, and any trend
  about a kid becomes a search of its own.
- Google and YouTube autocomplete: what people actually type after "boy saved…", "kid arrested for…" and so on.
  Every seed phrase is also tried with a-z after it ("boy saved a…", "boy saved b…"), which brings up
  hundreds of real, popular searches per seed.

Everything found goes into the search pool (see store.py). The news searches work through the pool run by run,
the best phrases get searched more often, and phrases that never find a kid story are dropped.
"""
from __future__ import annotations

import email.utils
import json
import re
import string
import time
import xml.etree.ElementTree as ET

from .collect import HTTP, clean, parallel, report_stats
from .models import Candidate
from .prefilter import BLOCK, KID, PARADOX
from .world import (ALL_GEOS, BLOCK_INTL, ENGLISH_GEOS, LOCALES, intl_counts, is_spaced, locale_for_geo,
                    split_loc, tag, world_queries)

# autocomplete phrases about entertainment, not real events
JUNK = re.compile(
    r"\b(lyrics?|songs?|movies?|film|trailer|episodes?|season|cast|books?|quotes?|memes?|meaning|games?|wiki|imdb|"
    r"cartoons?|anime|minecraft|fortnite|gta|reaction|edits?|asmr|compilation|poem|drawings?|coloring|costume|"
    r"chords|instrumental|remix|karaoke|sims|simulator|story ?time animated|fanfic\w*|ai generated)\b",
    re.I,
)
HOOKY = re.compile(r"\b(bully|bullied|bullies|karen|karma|entitled)\b", re.I)


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _rfc822(text: str | None) -> float | None:
    try:
        return email.utils.parsedate_to_datetime(text).timestamp() if text else None
    except (TypeError, ValueError):
        return None


def useful(phrase: str) -> bool:
    """Keep autocomplete phrases that are about a kid (or a karma/'nobody believed' twist) and not entertainment.
    Works for tagged phrases in other languages too ("[pt-BR] menino salvou a irmã")."""
    loc, text = split_loc(phrase)
    if len(text) > 120 or BLOCK.search(text) or BLOCK_INTL.search(text) or JUNK.search(text):
        return False
    if not loc:
        return len(text.split()) >= 3 and bool(KID.search(text) or PARADOX.search(text) or HOOKY.search(text))
    long_enough = len(text.split()) >= 2 if is_spaced(text) else len(text) >= 4
    return long_enough and bool(intl_counts(text)[0] or KID.search(text))


# ---------------------------------------------------------------- Google Trends

def trends(http: HTTP, cfg: dict, log) -> tuple[list[Candidate], list[str]]:
    """Returns (trending stories to check, trending searches about kids to add to the pool)."""

    def one(geo):
        r = http.get("https://trends.google.com/trending/rss", params={"geo": geo})
        r.raise_for_status()
        out = []
        for item in (el for el in ET.fromstring(r.content).iter() if _tag(el) == "item"):
            term, traffic, published, news = "", "", None, []
            for child in item:
                t = _tag(child)
                if t == "title":
                    term = clean(child.text or "", 120)
                elif t == "approx_traffic":
                    traffic = (child.text or "").strip()
                elif t == "pubdate":
                    published = _rfc822(child.text)
                elif t == "news_item":
                    news.append({_tag(x): (x.text or "").strip() for x in child})
            out.append((geo, term, traffic, published, news))
        return out

    geos = cfg.get("trends_geos") or ["US"]
    geos = ALL_GEOS if geos == "all" else geos
    # countries Trends doesn't cover just fail quietly, so errors never stop the rest
    rows, stats = parallel(one, geos, 8, cfg.get("_deadline"), max_errors=len(geos) + 1)
    if stats["errors"]:
        stats = {**stats, "errors": 0}  # not a problem worth reporting: those countries simply have no Trends feed
    report_stats(log, f"google trends ({stats['done']} countries)", stats)
    cands: list[Candidate] = []
    kid_terms: list[str] = []
    for geo, term, traffic, published, news in rows:
        titles = []
        for n in news:
            title, url = clean(n.get("news_item_title", ""), 300), n.get("news_item_url", "")
            if not title or not url:
                continue
            titles.append(title)
            cands.append(
                Candidate(
                    title=title,
                    url=url,
                    source=f"Google Trends {geo} · {n.get('news_item_source') or 'news'}",
                    summary=clean(n.get("news_item_snippet", ""), 500),
                    published=published,
                    signal=f"trending in {geo}: {term} ({traffic} searches)" if term else "",
                    extra={"q": term},
                )
            )
        about_kid = any(KID.search(t) or PARADOX.search(t) or intl_counts(t)[0] for t in [term, *titles])
        if term and about_kid and not BLOCK.search(term) and not BLOCK_INTL.search(term):
            loc = "" if geo in ENGLISH_GEOS else locale_for_geo(geo)
            if loc or geo in ENGLISH_GEOS:  # searchable in that country's own news edition
                kid_terms.append(tag(loc, term))
    return cands, list(dict.fromkeys(kid_terms))


# ---------------------------------------------------------------- autocomplete

def suggestions(http: HTTP, text: str, youtube: bool = False, loc: str = "") -> list[str]:
    params = {"client": "firefox", "hl": LOCALES[loc][0] if loc else "en",
              "gl": LOCALES[loc][1].lower() if loc else "us", "q": text}
    if youtube:
        params["ds"] = "yt"
    r = http.get("https://suggestqueries.google.com/complete/search", params=params, timeout=10)
    if r.status_code in (429, 503):
        raise RuntimeError(f"HTTP {r.status_code}: Google is slowing the factory down")
    r.raise_for_status()
    data = json.loads(r.text)
    found = data[1] if isinstance(data, list) and len(data) > 1 and isinstance(data[1], list) else []
    return [s for s in found if isinstance(s, str)]


def _next_batch(store, key: str, items: list, n: int, refresh_hours: float) -> list:
    """The next n items, round and round. After a full round it waits refresh_hours before the next round,
    because autocomplete answers barely change within a day."""
    if not items:
        return []
    start = int(store.get_meta(f"{key}_cursor", "0") or 0) % len(items)
    done_at = float(store.get_meta(f"{key}_round_done", "0") or 0)
    if start == 0 and refresh_hours and time.time() - done_at < refresh_hours * 3600:
        return []
    end = min(start + max(1, n), len(items))
    if end == len(items):
        store.set_meta(f"{key}_round_done", str(time.time()))
    store.set_meta(f"{key}_cursor", str(end % len(items)))
    return items[start:end]


def autocomplete(http: HTTP, store, cfg: dict, log) -> dict:
    """Expands the next few seed phrases into everything people type after them."""
    dcfg = cfg.get("discover") or {}
    seeds = [(s, lane) for lane, items in (dcfg.get("seeds") or {}).items() for s in (items or [])]
    if not seeds or not dcfg.get("autocomplete", True):
        return {}
    store.pool_add([(s, lane, "seed") for s, lane in seeds])
    store.pool_sync("seed", [s for s, _ in seeds])
    refresh = float(dcfg.get("refresh_hours", 0) or 0)
    batch = _next_batch(store, "seed", seeds, int(dcfg.get("seeds_per_run", 12)), refresh)

    letters = [""] + (list(string.ascii_lowercase) if dcfg.get("alphabet", True) else [])
    engines = [False] + ([True] if dcfg.get("youtube_suggest", True) else [])
    jobs = [(seed, lane, letter, yt) for seed, lane in batch for letter in letters for yt in engines]

    # the world: the translated searches are typed into autocomplete in their own language and country
    world = [q for q, _ in world_queries(cfg)]
    wbatch = _next_batch(store, "world_seed", world, int((cfg.get("world") or {}).get("seeds_per_run", 60)), refresh)
    jobs += [(q, "", "", yt) for q in wbatch for yt in engines]
    if not jobs:
        log(f"  autocomplete: every seed was looked up in the last {refresh:g} hours, skipped this run")
        return {"autocomplete": 0, "pool_new": 0}
    pause = float(dcfg.get("pause_seconds", 0.2))

    def one(job):
        seed, lane, letter, yt = job
        loc, text = split_loc(seed)
        found = suggestions(http, f"{text} {letter}", yt, loc)
        if pause:
            time.sleep(pause)
        return [(tag(loc, s), lane, "yt-suggest" if yt else "g-suggest") for s in found]

    deadline = dcfg.get("_deadline") or cfg.get("_deadline_collect")
    rows, stats = parallel(one, jobs, int(dcfg.get("workers", 6)), deadline, int(dcfg.get("max_errors", 40)))
    report_stats(log, "autocomplete lookups", stats)
    good: dict[str, tuple[str, str]] = {}
    for phrase, lane, origin in rows:
        loc, text = split_loc(phrase)
        p = tag(loc, " ".join(text.lower().split()))
        if p not in good and useful(p):
            good[p] = (lane, origin)
    added = store.pool_add([(p, lane, origin) for p, (lane, origin) in good.items()], cap=int(dcfg.get("max_pool", 8000)))
    log(f"  autocomplete: {len(good)} useful phrases, {added} new in the search pool")
    return {"autocomplete": stats["done"], "pool_new": added}


# ---------------------------------------------------------------- both together

def discover(http: HTTP, store, cfg: dict, log) -> tuple[list[Candidate], dict, dict]:
    """Returns (stories found, stories per source, lookups per source)."""
    dcfg = cfg.get("discover") or {}
    if not dcfg.get("enabled"):
        return [], {}, {}
    dcfg = {**dcfg, "_deadline": cfg.get("_deadline_collect")}
    cands: list[Candidate] = []
    counts: dict[str, int] = {}
    lookups: dict[str, int] = {}
    if dcfg.get("trends", True):
        try:
            cands, terms = trends(http, dcfg, log)
            counts["trends"] = len(cands)
            geos = dcfg.get("trends_geos") or ["US"]
            lookups["trends"] = len(ALL_GEOS) if geos == "all" else len(geos)
            if terms:
                expires = time.time() + float(dcfg.get("trend_days", 3)) * 86400
                store.pool_add([(t, "", "trend") for t in terms], expires=expires)
                log(f"  google trends: {len(terms)} trending searches are about kids, added to the pool")
        except Exception as ex:  # noqa: BLE001
            log(f"  google trends failed: {str(ex)[:150]}")
    try:
        result = autocomplete(http, store, {**cfg, "discover": dcfg}, log)
        if result:
            lookups["autocomplete"] = result["autocomplete"]
    except Exception as ex:  # noqa: BLE001
        log(f"  autocomplete failed: {str(ex)[:150]}")
    return cands, counts, lookups

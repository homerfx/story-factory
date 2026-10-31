"""Story Factory: find stories (factory 1), check them with AI (factory 2), post the best to Discord.

mode: viral (config.yaml) = only kid stories that already went viral on YouTube, TikTok and Instagram, deeply
researched and hand-picked against your channel's top 50. mode: news = the old wide net (news, trends, Reddit).

    python run.py --setup-discord   create the Discord channels (run once)
    python run.py                   one run
    python run.py --loop            keep running forever (every loop_minutes from config.yaml)
    python run.py --dry-run         do everything but print the cards instead of posting
    python run.py --report          post the daily report now
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from factory.archive import dig
from factory.collect import HTTP, collect_fresh, lane_queries, parallel
from factory.discover import discover
from factory.world import world_queries
from factory.config import load_config, load_env
from factory.discord import (
    DISCORD_EPOCH_MS, DOWN, STAR, UP, DiscordAPI, DiscordError, build_card, card_info, channel_plan,
    count_reactions, lane_from_label, print_card, setup_channels,
)
from factory.fetch import article_text
from factory.judge import deep, lanes_text, passes, screen, taste_text
from factory.llm import LLM, NoAIAvailable
from factory.models import Candidate, TitleIndex
from factory.prefilter import BLOCK, Exclusion, prefilter
from factory import research, viral
from factory.store import Store


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- Discord helpers

def discord_api() -> DiscordAPI | None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    return DiscordAPI(token, log) if token else None


def ensure_channels(api: DiscordAPI | None, store: Store, cfg: dict) -> dict[str, str]:
    channels = json.loads(store.get_meta("discord_channels", "{}") or "{}")
    if api is None:
        return channels
    if set(channel_plan(cfg)) - set(channels):
        guild = os.environ.get("DISCORD_GUILD_ID", "")
        if not guild:
            log("  DISCORD_GUILD_ID is missing, can't create channels")
            return channels
        try:
            channels = setup_channels(api, guild, cfg, log)
            store.set_meta("discord_channels", json.dumps(channels))
        except DiscordError as ex:
            log(f"  Discord setup problem (will retry next run): {ex}")
    return channels


def rebuild_memory_from_discord(api: DiscordAPI, store: Store, channels: dict, cfg: dict) -> int:
    """If the memory file was ever lost, re-learn what was already posted from the cards in Discord."""
    if store.get_meta("rebuilt") or store.count("posted") > 0:
        return 0
    restored = 0
    since = time.time() - 60 * 86400
    with open(cfg["posted_path"], "a", encoding="utf-8") as posted_file:
        for key, cid in channels.items():
            if key in ("report", "picked"):
                continue
            try:
                messages = api.messages_since(cid, since, max_pages=80)
            except DiscordError as ex:
                log(f"  rebuild: {ex}")
                return restored
            for m in messages:
                info = card_info(m)
                if not info:
                    continue
                item_id = Candidate(title=info["title"], url=info["url"], source="").id
                up, down, stars = count_reactions(m)
                posted_at = ((int(m["id"]) >> 22) + DISCORD_EPOCH_MS) / 1000
                lane = key if key in (cfg.get("lanes") or {}) else lane_from_label(cfg, info.get("lane", ""))
                if store.restore_posted(item_id, info["title"], info["url"], lane, cid, m["id"], info["story"],
                                        up, down, stars, posted_at):
                    posted_file.write(f"{info['title']} | {info['story']}\n")
                    restored += 1
    store.set_meta("rebuilt", "1")
    if restored:
        log(f"  memory was empty: restored {restored} already-posted stories from Discord")
    return restored


def read_feedback(api: DiscordAPI, store: Store, channels: dict, cfg: dict) -> int:
    since = time.time() - cfg.get("discord", {}).get("feedback_days", 7) * 86400
    pages = int(cfg.get("discord", {}).get("feedback_pages", 30))  # 100 cards per page
    n = 0
    for key, cid in channels.items():
        if key in ("report", "picked"):
            continue
        try:
            for m in api.messages_since(cid, since, max_pages=pages):
                up, down, stars = count_reactions(m)
                store.set_feedback(cid, m["id"], up, down, stars)
                n += up + down + stars
        except DiscordError as ex:
            log(f"  reading reactions in {key}: {ex}")
    return n


def post_item(api, channels: dict, cfg: dict, item: dict, dry_run: bool, note: str = "", key: str | None = None):
    """Sends the card. Returns (channel id, message id); (None, None) on a dry run."""
    score = (item.get("data") or {}).get("score", 0)
    if key is None:
        lane = item.get("lane") or "other"
        key = "top" if score >= cfg.get("top_threshold", 9) else (lane if lane in channels or dry_run else "other")
    payload = build_card(item, cfg, note)
    plan = channel_plan(cfg)
    name = plan.get(key, plan.get("stories", (key,)))[0]
    if dry_run or api is None:
        print_card(payload, name, log)
        return None, None
    cid = channels.get(key) or channels.get("other") or channels.get("stories")  # "stories" = the one-channel setup
    if not cid:
        raise DiscordError("no Discord channel to post in yet", 404)
    return cid, api.send(cid, payload)


def add_buttons(api, cid: str, mid: str) -> None:
    """👍 👎 ⭐ under each card. A failure here never causes a re-post."""
    try:
        for emoji in (UP, DOWN, STAR):
            api.react(cid, mid, emoji)
    except DiscordError as ex:
        log(f"  couldn't add reactions (check the bot's Add Reactions permission): {ex}")


def daily_report(api, store: Store, channels: dict, cfg: dict, dry_run: bool, force: bool = False) -> None:
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now_local = datetime.now(tz)
    today = now_local.date().isoformat()
    if not force and (now_local.hour < cfg.get("discord", {}).get("report_hour", 10) or store.get_meta("last_report") == today):
        return
    since = time.time() - 86400
    runs = store.runs_since(since)
    posted = store.posted_since(since)
    found = sum(sum(r.get("found", {}).values()) for r in runs)
    source_totals: Counter = Counter()
    for r in runs:
        source_totals.update(r.get("found", {}))
    empty = [s for s in ("google_news", "bing", "trends", "gdelt", "reddit", "rss", "youtube", "tiktok", "instagram")
             if s in source_totals and source_totals[s] == 0]
    search_totals: Counter = Counter()
    for r in runs:
        search_totals.update(r.get("searches", {}))
    pool = runs[-1].get("pool", {}) if runs else {}
    ai_problems: Counter = Counter()
    for r in runs:
        ai_problems.update(r.get("ai_problems", {}).keys())
    by_lane = Counter(p.get("lane") or "other" for p in posted)

    week = store.posted_since(time.time() - 7 * 86400)
    ups, downs, stars = sum(p["up"] for p in week), sum(p["down"] for p in week), sum(p["stars"] for p in week)
    lane_votes: dict[str, list[int]] = {}
    for p in week:
        v = lane_votes.setdefault(p.get("lane") or "other", [0, 0])
        v[0] += p["up"] + 2 * p["stars"]
        v[1] += p["down"]
    liked_lanes = sorted(lane_votes.items(), key=lambda kv: kv[1][0] - kv[1][1], reverse=True)
    best = sorted(week, key=lambda p: (p["stars"], p["up"] - p["down"]), reverse=True)[:3]

    names = {"google_news": "Google News", "bing": "Bing News", "autocomplete": "autocomplete lookups",
             "trends": "Trends countries", "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
    lines = [
        "**Searches run:** " + (" · ".join(f"{names.get(k, k)} {v:,}" for k, v in search_totals.most_common()) or "none")
        + (f" · **Search pool:** {sum(v for k, v in pool.items() if k != 'retired'):,} phrases "
           f"({pool.get('retired', 0):,} retired)" if pool else ""),
        f"**Runs:** {len(runs)} · **Headlines found:** {found:,} · **New:** {sum(r.get('new', 0) for r in runs):,} "
        f"· **Passed keyword filter:** {sum(r.get('passed_filter', 0) for r in runs):,}",
        f"**AI quick-screened:** {sum(r.get('screened', 0) for r in runs):,} · "
        f"**Deep-checked:** {sum(r.get('deep_checked', 0) for r in runs):,} · **Posted:** {len(posted)}",
        "**Posted per lane:** " + (", ".join(f"{k} {v}" for k, v in by_lane.most_common()) or "none"),
        f"**Your reactions (7 days):** {UP} {ups} · {DOWN} {downs} · {STAR} {stars}",
    ]
    if liked_lanes and (ups or downs or stars):
        lines.append("**Lanes you like most:** " + ", ".join(f"{k} (+{v[0]}/-{v[1]})" for k, v in liked_lanes[:3]))
    if best and (best[0]["stars"] or best[0]["up"]):
        lines.append("**Best of the week:** " + " · ".join(p["title"][:80] for p in best))
    if store.get_meta("archive_last"):
        lines.append(f"**Archive digger is at:** {store.get_meta('archive_last')}")
    if cfg.get("mode") == "viral" and store.get_meta("apify_used_usd"):
        budget = float(((cfg.get("viral") or {}).get("apify") or {}).get("monthly_budget_usd", 4.5))
        lines.append(f"**Apify (TikTok + Instagram) used this month:** ${store.get_meta('apify_used_usd')} "
                     f"of the free ${budget:.2f}")
    problems = []
    if empty:
        problems.append("these sources found nothing all day: " + ", ".join(empty))
    if ai_problems:
        problems.append("AI that ran out or failed: " + ", ".join(f"{k} ({v} runs)" for k, v in ai_problems.items()))
    if problems:
        lines.append("⚠️ " + " · ".join(problems))
    payload = {"embeds": [{"title": f"📊 Daily report · {today}", "description": "\n".join(lines)[:4000], "color": 0x5865F2}]}
    target = channels.get("report") or channels.get("stories")  # one-channel setup: the report goes there too
    if dry_run or api is None or not target:
        print_card(payload, "report", log)
    elif cfg.get("discord", {}).get("daily_report", True) or force:
        api.send(target, payload)
    store.set_meta("last_report", today)


# ---------------------------------------------------------------- one run

def run_once(cfg: dict, dry_run: bool = False, http: HTTP | None = None, llm: LLM | None = None, api=None) -> dict:
    started = time.time()
    # Time budget (keeps GitHub's free minutes from running out): collecting gets the first part of it,
    # the quick screen runs until 3/4 of it, the deep check uses the rest. 0 = no limit.
    budget = float(cfg.get("run_budget_minutes", 0) or 0) * 60
    deadline = started + budget if budget else None
    collect_deadline = started + budget * float(cfg.get("collect_share", 0.45)) if budget else None
    screen_deadline = started + budget * 0.75 if budget else None
    cfg = {**cfg, "_deadline_collect": collect_deadline}

    http = http or HTTP()
    llm = llm or LLM(cfg, log)
    store = Store(cfg["state_path"])
    api = api if api is not None else (None if dry_run else discord_api())
    channels = ensure_channels(api, store, cfg)
    if not dry_run and not api:
        log("  no DISCORD_BOT_TOKEN: stories will wait until Discord is set up")
    run_no = store.bump_run()
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "run": run_no}

    # your 👍/👎/⭐ first, so the AI uses your latest taste
    if api and channels:
        rebuild_memory_from_discord(api, store, channels, cfg)
        report["reactions_read"] = read_feedback(api, store, channels, cfg)

    # the search pool always contains your lane searches and the YouTube searches from config.yaml
    yt_seeds = (cfg.get("sources", {}).get("youtube") or {}).get("queries", [])
    store.pool_add([(q, lane, "lane") for q, lane in lane_queries(cfg)])
    store.pool_add([(q, "", "yt-seed") for q in yt_seeds])
    store.pool_sync("lane", [q for q, _ in lane_queries(cfg)])
    store.pool_sync("yt-seed", yt_seeds)
    # the whole world: the translated searches, each in its own country's news edition
    store.pool_add([(q, lane, "world") for q, lane in world_queries(cfg)])
    store.pool_sync("world", [q for q, _ in world_queries(cfg)])

    log(f"Run #{run_no}: discovering what people search for…")
    found, counts, searches = discover(http, store, cfg, log)
    log(f"  collecting ({store.pool_size():,} searches in the pool)…")
    old, old_counts = dig(http, cfg, store, run_no, log)
    fresh, fresh_counts, fresh_searches = collect_fresh(http, cfg, run_no, log, store)
    found += fresh + old
    counts.update(fresh_counts)
    counts.update(old_counts)
    searches.update(fresh_searches)
    report["found"] = counts
    report["searches"] = searches
    report["pool"] = store.pool_stats()

    # fresh, unseen, not the same story twice
    now, max_age = time.time(), cfg.get("max_age_hours", 72) * 3600
    titles = TitleIndex(store.recent_titles(14))
    new, batch_ids = [], set()
    for c in found:
        if not c.title or not c.url or c.id in batch_ids or store.seen(c.id):
            continue
        batch_ids.add(c.id)
        if not c.archive and c.published and now - c.published > max_age:
            continue
        bare = viral.tidy_title(c.title)  # hashtags and emojis say nothing about the story
        if titles.is_duplicate(bare):
            store.add(c, "duplicate", light=True, commit=False)
            continue
        titles.add(bare)
        new.append(c)
    report["new"] = len(new)

    # free keyword filter + already-pitched check
    exclusion = Exclusion(cfg["exclude_paths"] + [cfg["posted_path"]])
    passed = []
    finds: Counter = Counter()
    for c in new:
        keep, kw, reason = prefilter(c)
        if not keep:
            store.add(c, f"filtered:{reason}", light=True, commit=False)
            continue
        strength, line = exclusion.check(f"{c.title} {c.summary[:200]}")
        if strength >= cfg.get("pitched_skip_strength", 0.75):
            store.add(c, "already_pitched", light=True, commit=False)
            continue
        if strength >= 0.5:
            c.possibly_pitched = line
        c.kw_score = kw
        passed.append(c)
        if c.extra.get("q"):
            finds[c.extra["q"]] += 1
    store.commit()
    report["passed_filter"] = len(passed)
    log(f"  {sum(counts.values()):,} found, {len(new):,} new, {len(passed):,} passed the keyword filter")

    # headlines that didn't fit in an earlier run's quick screen get their turn
    for row in store.screen_pending(cfg.get("max_screen_per_run", 150)):
        d = row.get("data") or {}
        c = Candidate(title=row["title"], url=row["url"], source=row["source"], summary=d.get("summary", ""),
                      signal=row.get("signal") or "", signal_value=int(d.get("signal_value", 0) or 0),
                      possibly_pitched=row.get("possibly_pitched") or "", lane_hint=d.get("lane_hint", ""),
                      archive=bool(row.get("archive")), extra={"q": d.get("q", "")})
        c.kw_score = int(row.get("score") or 0)
        passed.append(c)
    passed.sort(key=lambda c: (not c.archive, c.kw_score, c.signal_value), reverse=True)  # fresh first

    def item_data(c: Candidate) -> dict:
        return {"why": c.screen_why, "summary": c.summary, "lane_hint": c.lane_hint, "signal_value": c.signal_value,
                "q": c.extra.get("q", "")}

    # AI step 1: quick screen
    to_screen = passed[: cfg.get("max_screen_per_run", 150)]
    ai_ok = True
    try:
        if to_screen:
            screen(llm, to_screen, cfg.get("ai", {}).get("screen_batch_size", 15), log, screen_deadline)
    except NoAIAvailable as ex:
        ai_ok = False
        log(f"  {ex}")
    screened = [c for c in to_screen if c.screen_score is not None]
    threshold = cfg.get("screen_threshold", 7)
    kept: Counter = Counter()
    for c in screened:
        # good ones join the deep-check queue, which survives AI outages and is worked through run by run
        store.add(c, "deep_pending" if c.screen_score >= threshold else "screened", c.screen_score, item_data(c),
                  lane=c.lane_hint or None, commit=False)
        if c.screen_score >= threshold and c.extra.get("q"):
            kept[c.extra["q"]] += 1
    # everything that passed the keyword filter but wasn't screened yet waits for the next run
    for c in passed:
        if c.screen_score is None:
            store.add(c, "screen_pending", c.kw_score, item_data(c), lane=c.lane_hint or None, commit=False)
    store.commit()
    store.pool_credit(finds, kept)
    report["pool_retired"] = store.pool_retire(int((cfg.get("search_pool") or {}).get("retire_after", 6)))
    report["screened"] = len(screened)

    # AI step 2: deep check (best waiting stories first), with the channel's rules, the lanes and your taste
    queue = store.deep_pending(cfg.get("max_deep_per_run", 25))
    log(f"  AI screened {len(screened)}; {len(queue)} stories in the deep-check queue this run")
    liked, disliked = store.taste_examples(10)
    system = "\n\n".join(
        x for x in (open(cfg["rules_path"], encoding="utf-8").read(), lanes_text(cfg), taste_text(liked, disliked)) if x
    )
    lanes = set((cfg.get("lanes") or {}).keys())
    deep_checked = ready = 0
    # read all the articles at once (8 at a time) instead of one by one: saves minutes every run
    articles: dict[str, str] = {}
    if queue and ai_ok and cfg.get("fetch_articles", True):
        got, _ = parallel(lambda row: [(row["id"], article_text(http, row["url"]))], queue, 8, deadline)
        articles = dict(got)
    for row in queue:
        if not ai_ok:
            break
        if deadline is not None and time.time() > deadline - 45:
            log("  time budget used up; the rest of the deep-check queue waits for the next run")
            break
        d = row.get("data") or {}
        c = Candidate(title=row["title"], url=row["url"], source=row["source"], summary=d.get("summary", ""),
                      signal=row.get("signal") or "", signal_value=int(d.get("signal_value", 0) or 0),
                      possibly_pitched=row.get("possibly_pitched") or "", lane_hint=d.get("lane_hint", ""),
                      archive=bool(row.get("archive")))
        article = articles.get(row["id"], "")
        try:
            result = deep(llm, c, system, article)
        except NoAIAvailable as ex:
            log(f"  {ex}; the rest of the queue waits for the next run")
            break
        deep_checked += 1
        if result is None:  # unreadable reply: try once more next run, then give up
            attempts = int(d.get("attempts", 0)) + 1
            store.add(c, "deep_pending" if attempts < 2 else "deep_failed", None, {**d, "attempts": attempts})
            continue
        lane = result["lane"] if result["lane"] in lanes else (c.lane_hint or "other")
        ok = passes(result, cfg.get("post_threshold", 7))
        store.add(c, "ready" if ok else "rejected", result["score"], result, lane=lane)
        ready += ok
    report.update(deep_checked=deep_checked, ready=ready)

    # post: best first, respecting the per-lane daily cap
    posted, by_lane = 0, Counter()
    if api or dry_run:
        day_ago = time.time() - 86400
        cap = cfg.get("max_posts_per_lane_per_day", 60)
        for item in store.ready_to_post(cfg.get("max_posts_per_run", 20) * 3):
            if posted >= cfg.get("max_posts_per_run", 20):
                break
            lane = item.get("lane") or "other"
            if store.posted_today_in_lane(lane, day_ago) + by_lane[lane] >= cap:
                continue
            try:
                cid, mid = post_item(api, channels, cfg, item, dry_run)
            except DiscordError as ex:
                log(f"  posting failed: {ex}")
                if ex.status == 404:  # a channel was deleted: rebuild the layout next run
                    store.set_meta("discord_channels", "{}")
                    break
                if 400 <= ex.status < 500 and ex.status != 429:  # this one card is the problem: skip it
                    store.set_status(item["id"], "post_failed")
                    continue
                break  # Discord itself is down: try again next run
            posted += 1
            by_lane[lane] += 1
            if not dry_run:
                store.mark_posted(item["id"], cid, mid)  # saved BEFORE the reactions, so it can never post twice
                with open(cfg["posted_path"], "a", encoding="utf-8") as f:
                    f.write(f"{item['title']} | {(item.get('data') or {}).get('story', '')}\n")
                add_buttons(api, cid, mid)

        # ⭐ stories get copied to #picked (the one-channel setup has no #picked: the ⭐ is enough there)
        if "picked" not in channels and not dry_run:
            for item in store.to_repost_as_picked():
                store.mark_picked_posted(item["id"])
        for item in store.to_repost_as_picked() if ("picked" in channels or dry_run) else []:
            try:
                post_item(api, channels, cfg, item, dry_run, note=f"{STAR} Starred in #{item.get('lane') or 'other'}", key="picked")
            except DiscordError as ex:
                log(f"  picked repost failed: {ex}")
                if 400 <= ex.status < 500 and ex.status not in (404, 429):
                    store.mark_picked_posted(item["id"])
                    continue
                break
            if not dry_run:
                store.mark_picked_posted(item["id"])
    report.update(posted=posted, posted_by_lane=dict(by_lane), ai_used=llm.used, ai_problems=llm.dead)
    log(f"  posted {posted} stories" + (" (dry run)" if dry_run else ""))

    store.log_run(report)
    try:
        daily_report(api, store, channels, cfg, dry_run)
    except DiscordError as ex:
        log(f"  daily report failed: {ex}")
    store.prune()
    with open(cfg["last_run_path"], "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


# ---------------------------------------------------------------- viral mode

def post_ready(api, store: Store, channels: dict, cfg: dict, dry_run: bool, max_posts: int) -> tuple[int, Counter]:
    """Posts the best waiting stories (respecting the per-lane daily cap). Returns (posted, per lane)."""
    posted, by_lane = 0, Counter()
    if not (api or dry_run):
        return posted, by_lane
    day_ago = time.time() - 86400
    cap = cfg.get("max_posts_per_lane_per_day", 60)
    for item in store.ready_to_post(max_posts * 3, max_age_days=7):
        if posted >= max_posts:
            break
        lane = item.get("lane") or "other"
        if store.posted_today_in_lane(lane, day_ago) + by_lane[lane] >= cap:
            continue
        try:
            cid, mid = post_item(api, channels, cfg, item, dry_run)
        except DiscordError as ex:
            log(f"  posting failed: {ex}")
            if ex.status == 404:
                store.set_meta("discord_channels", "{}")
                break
            if 400 <= ex.status < 500 and ex.status != 429:
                store.set_status(item["id"], "post_failed")
                continue
            break
        posted += 1
        by_lane[lane] += 1
        if not dry_run:
            store.mark_posted(item["id"], cid, mid)  # saved BEFORE the reactions, so it can never post twice
            with open(cfg["posted_path"], "a", encoding="utf-8") as f:
                f.write(f"{item['title']} | {(item.get('data') or {}).get('story', '')}\n")
            add_buttons(api, cid, mid)
    return posted, by_lane


def _viral_data(c: Candidate) -> dict:
    x = c.extra or {}
    return {"why": c.screen_why, "summary": c.summary, "signal_value": c.signal_value, "q": x.get("q", ""),
            "platform": x.get("platform", ""), "video_id": x.get("video_id", ""), "likes": x.get("likes", 0),
            "comments": x.get("comments", 0), "comment_texts": x.get("comment_texts") or [], "published": c.published}


def _viral_candidate(row: dict) -> Candidate:
    d = row.get("data") or {}
    c = Candidate(title=row["title"], url=row["url"], source=row["source"], summary=d.get("summary", ""),
                  signal=row.get("signal") or "", signal_value=int(d.get("signal_value", 0) or 0),
                  possibly_pitched=row.get("possibly_pitched") or "", archive=bool(row.get("archive")),
                  published=d.get("published"),
                  extra={k: d.get(k) for k in ("q", "platform", "video_id", "likes", "comments", "comment_texts")})
    c.screen_why = d.get("why", "")
    return c


def run_viral(cfg: dict, dry_run: bool = False, http: HTTP | None = None, llm: LLM | None = None, api=None) -> dict:
    """Viral mode: only kid stories that already went viral (YouTube, TikTok, Instagram), each one researched
    online and hand-picked against your channel's 50 most-viewed videos."""
    started = time.time()
    budget = float(cfg.get("run_budget_minutes", 0) or 0) * 60
    deadline = started + budget if budget else None
    screen_deadline = started + budget * 0.6 if budget else None  # TikTok/Instagram can take a few minutes
    vcfg = cfg.get("viral") or {}
    ycfg = vcfg.get("youtube") or {}
    acfg = vcfg.get("apify") or {}

    http = http or HTTP()
    llm = llm or LLM(cfg, log)
    store = Store(cfg["state_path"])
    api = api if api is not None else (None if dry_run else discord_api())
    channels = ensure_channels(api, store, cfg)
    if not dry_run and not api:
        log("  no DISCORD_BOT_TOKEN: stories will wait until Discord is set up")
    run_no = store.bump_run()
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "run": run_no, "mode": "viral"}
    parked = store.park_non_viral()  # stories the old news mode left waiting are never posted without proof
    if parked:
        log(f"  set aside {parked} stories the old news mode left waiting (they have no proof they went viral)")
    if api and channels:
        rebuild_memory_from_discord(api, store, channels, cfg)
        report["reactions_read"] = read_feedback(api, store, channels, cfg)

    # your channel: its top 50 (what every story is compared with) and every title already posted
    yt = viral.YouTube(http, store, ycfg, log)
    if not yt.ready:
        log("  youtube: no YOUTUBE_API_KEY, so YouTube isn't searched and your top 50 can't be loaded")
    profile = viral.refresh_profile(yt, store, cfg, log)

    # 1) find videos that already went viral. Apify (TikTok/Instagram) runs on a helper thread while YouTube runs
    #    here; only this thread touches the memory file.
    log(f"Run #{run_no} (viral mode): looking for kid stories that already went viral…")
    apify = viral.Apify(http, store, acfg, log)
    a_plan = viral.plan_apify(store, vcfg, apify.calls_allowed()) if acfg.get("enabled", True) and apify.token else []
    if acfg.get("enabled", True) and not apify.token:
        log("  tiktok/instagram: no APIFY_TOKEN, skipped")
    y_plan = []
    if yt.ready and ycfg.get("enabled", True):
        y_plan = viral.plan_youtube(store, vcfg, min(int(ycfg.get("searches_per_run", 2)), yt.units_left() // 102))
    collect_deadline = started + budget * 0.45 if budget else None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(viral.apify_viral, apify, a_plan, vcfg, log, collect_deadline) if a_plan else None
        yt_found, yt_done = viral.youtube_viral(yt, y_plan, vcfg, set(profile.get("skip_channels") or []), log)
        ap_found, ap_done, credit_used_up = future.result() if future else ([], [], False)
    viral.plan_done(store, yt_done)
    viral.apify_done(store, ap_done, credit_used_up)
    found = yt_found + ap_found
    searches = {"youtube": len(yt_done)}
    for p in ap_done:
        searches[p["platform"]] = searches.get(p["platform"], 0) + 1
    counts = {k: 0 for k, v in searches.items() if v}
    for c in found:
        counts[c.extra.get("platform", "other")] = counts.get(c.extra.get("platform", "other"), 0) + 1
    report["found"], report["searches"] = counts, searches
    log("  viral videos found: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none")
        + f" (searches: {', '.join(f'{k} {v}' for k, v in searches.items() if v) or 'none'})")

    # 2) new ones only, and never a story already posted (yours, or a channel you watch)
    exclusion = Exclusion(cfg["exclude_paths"] + [cfg["posted_path"]],
                          extra_lines=(profile.get("titles") or []) + (profile.get("watch_titles") or []))
    skip = cfg.get("pitched_skip_strength", 0.75)
    titles = TitleIndex(store.recent_titles(30))  # re-uploads of the same clip usually keep its title
    passed, batch_ids = [], set()
    for c in sorted(found, key=lambda c: c.signal_value, reverse=True):
        if not c.title or not c.url or c.id in batch_ids or store.seen(c.id):
            continue
        batch_ids.add(c.id)
        if titles.is_duplicate(c.title):
            store.add(c, "duplicate", light=True, commit=False)
            continue
        titles.add(c.title)
        text = f"{c.title} {c.summary[:300]}"
        if BLOCK.search(text) or viral.JUNK.search(c.title):
            store.add(c, "filtered:junk", light=True, commit=False)
            continue
        strength, line = exclusion.check(text)
        if strength >= skip:
            store.add(c, "already_pitched", light=True, commit=False)
            continue
        if strength >= 0.5:
            c.possibly_pitched = line
        passed.append(c)
    store.commit()
    report["new"] = report["passed_filter"] = len(passed)
    for row in store.screen_pending(int(vcfg.get("max_screen_per_run", 150))):
        passed.append(_viral_candidate(row))

    # 3) quick AI screen: is this one real kid story?
    to_screen = passed[: int(vcfg.get("max_screen_per_run", 150))]
    ai_ok = True
    try:
        if to_screen:
            screen(llm, to_screen, cfg.get("ai", {}).get("screen_batch_size", 30), log, screen_deadline,
                   system=research.VIRAL_SCREEN)
    except NoAIAvailable as ex:
        ai_ok = False
        log(f"  {ex}")
    threshold = int(vcfg.get("screen_threshold", 6))
    screened = [c for c in to_screen if c.screen_score is not None]
    for c in screened:
        store.add(c, "deep_pending" if c.screen_score >= threshold else "screened", c.screen_score, _viral_data(c),
                  commit=False)
    for c in passed:
        if c.screen_score is None:
            store.add(c, "screen_pending", 0, _viral_data(c), commit=False)
    store.commit()
    report["screened"] = len(screened)

    # 4) deep research + hand-pick against your top 50, your rules and your 👍/👎
    queue = store.research_queue(int(vcfg.get("max_research_per_run", 8)))
    log(f"  AI screened {len(screened)}; researching {len(queue)} stories")
    liked, disliked = store.taste_examples(10)
    system = "\n\n".join(x for x in (open(cfg["rules_path"], encoding="utf-8").read(), research.profile_text(profile),
                                      lanes_text(cfg), taste_text(liked, disliked)) if x)
    lanes = set((cfg.get("lanes") or {}).keys())
    pick_at = int(vcfg.get("pick_threshold", 9))
    researched = picked = 0
    # the same story often went viral in several videos: once one is picked, the others are skipped (the AI is shown
    # the stories already picked, and a close word match counts too)
    picked_stories = store.recent_stories(60)
    same_story = Exclusion([], extra_lines=picked_stories)
    same_at = float(vcfg.get("same_story_strength", 0.55))
    for row in queue:
        if not ai_ok or (deadline is not None and time.time() > deadline - 180):
            log("  time budget used up; the rest of the research queue waits for the next run")
            break
        c = _viral_candidate(row)
        d = row.get("data") or {}
        x = c.extra or {}
        comments = list(x.get("comment_texts") or [])  # Instagram sends its latest comments along
        if x.get("platform") == "youtube" and x.get("video_id") and vcfg.get("read_comments", True):
            comments = yt.comments(x["video_id"])
        try:
            first = research.identify(llm, c, comments)
            if first and first["real_story"] and first["kid_at_center"] and first["staged_risk"] != "high":
                ev = research.evidence(http, first["search"] or c.title[:100], int(vcfg.get("articles_to_read", 2)),
                                       deadline)
                result = research.pick(llm, c, system, first, comments, ev, picked_stories)
            else:
                result = None
        except NoAIAvailable as ex:
            log(f"  {ex}; the rest of the research queue waits for the next run")
            break
        researched += 1
        if first is None or (first["real_story"] and first["kid_at_center"] and first["staged_risk"] != "high"
                             and result is None):
            attempts = int(d.get("attempts", 0)) + 1  # unreadable AI reply: one more try next run, then give up
            store.add(c, "deep_pending" if attempts < 2 else "deep_failed", None, {**d, "attempts": attempts})
            continue
        if result is None:
            store.add(c, "rejected", 0, {**d, "why": "not a real story with a kid at the center"})
            continue
        if (result.get("same_as") or exclusion.check(result["story"])[0] >= skip
                or same_story.check(result["story"])[0] >= same_at):
            store.add(c, "already_pitched", result["score"], {**d, **result})
            continue
        ok = research.passes_pick(result, pick_at)
        lane = result["lane"] if result["lane"] in lanes else "other"
        store.add(c, "ready" if ok else "rejected", result["score"], {**d, **result}, lane=lane)
        picked += ok
        if ok:
            picked_stories.append(result["story"])
            same_story = Exclusion([], extra_lines=picked_stories)
    report.update(deep_checked=researched, ready=picked)

    # 5) post the hand-picked stories
    posted, by_lane = post_ready(api, store, channels, cfg, dry_run, int(vcfg.get("max_posts_per_run", 10)))
    report.update(posted=posted, posted_by_lane=dict(by_lane), ai_used=llm.used, ai_problems=llm.dead)
    log(f"  researched {researched}, hand-picked {picked}, posted {posted}" + (" (dry run)" if dry_run else ""))

    store.log_run(report)
    try:
        daily_report(api, store, channels, cfg, dry_run)
    except DiscordError as ex:
        log(f"  daily report failed: {ex}")
    store.prune()
    with open(cfg["last_run_path"], "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--setup-discord", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    load_env()
    cfg = load_config(args.config)

    if args.setup_discord or args.report:
        api = discord_api()
        if not api or not os.environ.get("DISCORD_GUILD_ID"):
            log("Add DISCORD_BOT_TOKEN and DISCORD_GUILD_ID to .env first (see README).")
            return 1
        store = Store(cfg["state_path"])
        if args.setup_discord:
            store.set_meta("discord_channels", "{}")  # re-check every channel
        channels = ensure_channels(api, store, cfg)
        if args.setup_discord:
            bot = cfg.get("discord", {}).get("bot_name", "Tyler")
            where = "this channel" if "stories" in channels else "the lane channels"
            api.send(channels.get("report") or channels["stories"],
                     {"content": f"✅ {bot} is connected. Stories will show up in {where}."})
            log(f"Discord ready: {len(channels)} channel(s).")
        else:
            daily_report(api, store, channels, cfg, False, force=True)
        return 0

    if not LLM(cfg, log).configured():
        log("No AI key found. Add GROQ_API_KEY (free) to .env; see README.")
        return 1

    run = run_viral if cfg.get("mode", "news") == "viral" else run_once
    if not args.loop:
        run(cfg, args.dry_run)
        return 0

    while True:
        started = time.time()
        try:
            run(cfg, args.dry_run)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        wait = max(60, cfg.get("loop_minutes", 30) * 60 - (time.time() - started))
        log(f"next run in {int(wait / 60)} minutes")
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())

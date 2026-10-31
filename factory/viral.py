"""Viral mode, part 1: find kid stories that ALREADY went viral on YouTube, TikTok and Instagram.

YouTube uses the free API key (10,000 units a day). TikTok and Instagram have no free search, so they go through
Apify's free plan ($5 of use a month, no card). Before every Apify call the factory asks Apify how much has really
been spent this month, spreads what's left evenly over the remaining days, and caps each call's cost, so it can
never go over the free amount."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from .collect import clean, iso_time, quiet_error
from .models import Candidate, fmt_num

YT_API = "https://www.googleapis.com/youtube/v3"
APIFY_API = "https://api.apify.com/v2"

# YouTube categories that are never real stories: cars, music, sports, gaming, movies, trailers
YT_SKIP_CATEGORIES = {"2", "10", "17", "20", "30", "44"}
# Titles that are kids' entertainment, music, trailers, games or made-up videos, not real stories
JUNK = re.compile(
    r"\b(nursery|rhymes?|lyrics|official (music )?video|music video|trailer|teaser|full movie|full episode|"
    r"kid rock|billy the kid|karate kid|wimpy kid|stray kids|kids? songs?|songs? for kids|cartoons? for kids|"
    r"for kids|toy review|unboxing|asmr|minecraft|fortnite|gameplay|try not to laugh|tutorial|how to make|"
    r"\(?fiction\)?|skit|dhar mann|ai animation|ai generated|meme)\b",
    re.I,
)
EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]")


def tidy_title(title: str) -> str:
    """'They Planned to K*LL Their CLASSMATE... 🤯 #roblox #shorts' -> 'They Planned to K*LL Their CLASSMATE...'"""
    title = EMOJI.sub("", re.sub(r"#\w+", "", title or ""))
    return re.sub(r"\s+", " ", title).strip(" -|")


class QuotaUsedUp(Exception):
    pass


class ApifyCreditUsedUp(Exception):
    pass


# ---------------------------------------------------------------- YouTube

def _yt_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 8 * 3600))  # YouTube's day starts at midnight Pacific


class YouTube:
    """The YouTube Data API with a daily units counter (shared with the news mode's counter)."""

    def __init__(self, http, store, cfg: dict, log) -> None:
        self.http, self.store, self.log = http, store, log
        self.key = os.environ.get("YOUTUBE_API_KEY", "")
        self.daily = int(cfg.get("daily_units", 9000))

    @property
    def ready(self) -> bool:
        return bool(self.key)

    def _meta_key(self) -> str:
        return f"yt_units_{_yt_day()}"

    def units_left(self) -> int:
        if not self.key:
            return 0
        return max(0, self.daily - int(self.store.get_meta(self._meta_key(), "0") or 0))

    def _spend(self, units: int) -> None:
        k = self._meta_key()
        self.store.set_meta(k, str(int(self.store.get_meta(k, "0") or 0) + units))

    def get(self, path: str, params: dict, cost: int) -> dict:
        if cost > self.units_left():
            raise QuotaUsedUp("today's free YouTube units are used up")
        r = self.http.get(f"{YT_API}/{path}", params=params, headers={"X-Goog-Api-Key": self.key}, timeout=20)
        self._spend(cost)
        if r.status_code == 403 and "quota" in (r.text or "").lower():
            self._spend(self.daily)  # Google says the day is used up: stop asking until tomorrow
            raise QuotaUsedUp("YouTube says today's free quota is used up")
        if r.status_code >= 400:
            raise RuntimeError(f"YouTube {path}: HTTP {r.status_code}")
        return r.json()

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            data = self.get("videos", {"part": "snippet,statistics,status", "id": ",".join(ids[i:i + 50])}, 1)
            out += data.get("items", [])
        return out

    def comments(self, video_id: str, n: int = 20) -> list[str]:
        """Top comments (they often explain what really happened). Empty if comments are off."""
        try:
            data = self.get("commentThreads", {"part": "snippet", "videoId": video_id, "maxResults": n,
                                               "order": "relevance", "textFormat": "plainText"}, 1)
        except QuotaUsedUp:
            return []
        except Exception:  # noqa: BLE001  (comments disabled, video removed)
            return []
        out = []
        for item in data.get("items", []):
            s = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
            text = clean(s.get("textDisplay") or s.get("textOriginal") or "", 300)
            if text:
                out.append(f"({int(s.get('likeCount') or 0):,} likes) {text}")
        return out

    def channel(self, handle: str, max_videos: int = 600) -> tuple[str, list[dict]]:
        """(channel id, every upload with its title and views)."""
        data = self.get("channels", {"part": "contentDetails", "forHandle": handle}, 1)
        items = data.get("items") or []
        if not items:
            return "", []
        cid = items[0]["id"]
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ids, token = [], None
        while len(ids) < max_videos:
            params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if token:
                params["pageToken"] = token
            page = self.get("playlistItems", params, 1)
            ids += [i["contentDetails"]["videoId"] for i in page.get("items", []) if i.get("contentDetails")]
            token = page.get("nextPageToken")
            if not token:
                break
        vids = []
        for v in self.videos(ids):
            vids.append({"title": tidy_title((v.get("snippet") or {}).get("title", "")),
                         "views": int((v.get("statistics") or {}).get("viewCount", 0) or 0)})
        return cid, vids


def refresh_profile(yt: YouTube, store, cfg: dict, log) -> dict:
    """Once a day: your channel's 50 most-viewed videos (what the AI compares every story with), every title you
    have posted, and the titles of the channels you watch (so their stories aren't pitched either)."""
    vcfg = cfg.get("viral") or {}
    cached = json.loads(store.get_meta("viral_profile", "{}") or "{}")
    now = time.time()
    fresh_enough = now - float(store.get_meta("viral_profile_at", "0") or 0) < 20 * 3600
    tried_lately = now - float(store.get_meta("viral_profile_try", "0") or 0) < 3 * 3600
    if (fresh_enough and cached.get("top")) or tried_lately or not yt.ready:
        return cached
    store.set_meta("viral_profile_try", str(now))  # a failed refresh waits 3 hours instead of retrying every run
    profile = {"top": cached.get("top") or [], "titles": cached.get("titles") or [],
               "skip_channels": [], "watch_titles": [], "watch_ok": True}
    own_ok = False
    try:
        handle = vcfg.get("channel_handle", "")
        if handle:
            cid, vids = yt.channel(handle)
            if cid and vids:
                profile["skip_channels"].append(cid)
                vids.sort(key=lambda v: v["views"], reverse=True)
                profile["top"] = vids[: int(vcfg.get("top_n", 50))]
                profile["titles"] = [v["title"] for v in vids]
                own_ok = True
    except Exception as ex:  # noqa: BLE001  (quota, network)
        log(f"  your channel couldn't be loaded ({quiet_error(ex)}); using the last copy")
    for other in vcfg.get("watch_channels") or []:
        try:
            cid, vids = yt.channel(other, max_videos=400)
            if cid:
                profile["skip_channels"].append(cid)
                profile["watch_titles"] += [v["title"] for v in vids]
        except Exception as ex:  # noqa: BLE001
            profile["watch_ok"] = False
            log(f"  {other} couldn't be loaded ({quiet_error(ex)}); using the last copy")
    if not profile["watch_ok"]:  # keep yesterday's list of the other channels rather than none
        profile["watch_titles"] = profile["watch_titles"] or cached.get("watch_titles") or []
        profile["skip_channels"] = list(dict.fromkeys(profile["skip_channels"] + (cached.get("skip_channels") or [])))
    if own_ok:
        store.set_meta("viral_profile", json.dumps(profile))
        store.set_meta("viral_profile_at", str(now))
        log(f"  channel profile: your top {len(profile['top'])} videos loaded "
            f"(#1: {profile['top'][0]['title'][:60]}, {fmt_num(profile['top'][0]['views'])})")
        return profile
    return cached or profile


def plan_youtube(store, vcfg: dict, n: int) -> list[dict]:
    """Which searches to run. A keyword's "last 30 days" search runs at most every fresh_every_hours (the same search
    sooner mostly finds the same videos); every other search walks back through the years, 3 months at a time, so
    old proven stories are found too."""
    ycfg = vcfg.get("youtube") or {}
    kws = [k for k in (vcfg.get("keywords") or []) if k.strip()]
    if not kws or n <= 0:
        return []
    months = max(1, int(ycfg.get("window_months", 3)))
    oldest = int(ycfg.get("oldest_year", 2010))
    windows, end = [], datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while end.year >= oldest:
        y, m = end.year, end.month - months
        while m <= 0:
            y, m = y - 1, m + 12
        start = end.replace(year=y, month=m)
        windows.append((start.strftime("%Y-%m-%dT00:00:00Z"), end.strftime("%Y-%m-%dT00:00:00Z")))
        end = start
    fresh_at = json.loads(store.get_meta("viral_yt_fresh_at", "{}") or "{}")
    due = sorted((k for k in kws if time.time() - float(fresh_at.get(k, 0)) > float(ycfg.get("fresh_every_hours", 48)) * 3600),
                 key=lambda k: float(fresh_at.get(k, 0)))
    arch_i = int(store.get_meta("viral_yt_archive", "0") or 0)
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = []
    for k in range(n):
        if due and k % 2 == 0:
            plan.append({"q": due.pop(0), "after": month_ago, "before": None, "archive": False})
        else:
            after, before = windows[(arch_i // len(kws)) % len(windows)]
            plan.append({"q": kws[arch_i % len(kws)], "after": after, "before": before, "archive": True})
            arch_i += 1
    return plan


def plan_done(store, done: list[dict]) -> None:
    """Moves the search cursors on, only for the searches that really ran."""
    fresh_at = json.loads(store.get_meta("viral_yt_fresh_at", "{}") or "{}")
    archive = [p for p in done if p["archive"]]
    for p in done:
        if not p["archive"]:
            fresh_at[p["q"]] = time.time()
    store.set_meta("viral_yt_fresh_at", json.dumps(fresh_at))
    if archive:
        store.set_meta("viral_yt_archive", str(int(store.get_meta("viral_yt_archive", "0") or 0) + len(archive)))
        store.set_meta("archive_last", f"YouTube {archive[-1]['after'][:7]} to {archive[-1]['before'][:7]}")


def youtube_viral(yt: YouTube, plan: list[dict], vcfg: dict, skip_channels: set, log) -> tuple[list[Candidate], list]:
    """(viral videos found, the planned searches that ran). Only videos past min_views that aren't kids'
    entertainment."""
    ycfg = vcfg.get("youtube") or {}
    min_views = int(vcfg.get("min_views", 1_000_000))
    out, done = [], []
    for p in plan:
        try:
            params = {"part": "snippet", "q": p["q"], "type": "video", "order": "viewCount", "maxResults": 50,
                      "publishedAfter": p["after"], "relevanceLanguage": ycfg.get("language", "en")}
            if p["before"]:
                params["publishedBefore"] = p["before"]
            res = yt.get("search", params, 100)
            done.append(p)
            ids = [i["id"]["videoId"] for i in res.get("items", []) if (i.get("id") or {}).get("videoId")]
            for v in yt.videos(ids):
                sn, st, status = v.get("snippet") or {}, v.get("statistics") or {}, v.get("status") or {}
                views = int(st.get("viewCount", 0) or 0)
                title = clean(sn.get("title", ""), 300)
                if (views < min_views or status.get("madeForKids") or sn.get("categoryId") in YT_SKIP_CATEGORIES
                        or sn.get("channelId") in skip_channels or JUNK.search(title)):
                    continue
                out.append(Candidate(
                    title=title,
                    url=f"https://www.youtube.com/watch?v={v['id']}",
                    source=f"YouTube · {clean(sn.get('channelTitle', ''), 80)}",
                    summary=clean(sn.get("description", ""), 700),
                    published=iso_time(sn.get("publishedAt", "")),
                    signal=f"{fmt_num(views)} views on YouTube",
                    signal_value=views,
                    archive=p["archive"],
                    extra={"q": p["q"], "platform": "youtube", "video_id": v["id"],
                           "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0)},
                ))
        except QuotaUsedUp as ex:
            log(f"  youtube: {ex}")
            break
        except Exception as ex:  # noqa: BLE001
            log(f"  youtube search failed: {quiet_error(str(ex).replace(yt.key, '***') if yt.key else ex)}")
    return out, done


# ---------------------------------------------------------------- TikTok + Instagram (Apify free plan)

class Apify:
    """Runs an Apify actor and returns its results. Never spends past the monthly budget."""

    def __init__(self, http, store, cfg: dict, log) -> None:
        self.http, self.store, self.cfg, self.log = http, store, cfg, log
        self.token = os.environ.get("APIFY_TOKEN", "")
        self.budget = float(cfg.get("monthly_budget_usd", 4.5))
        self.per_call = float(cfg.get("max_usd_per_call", 0.10))  # hard cap Apify enforces on every call
        self.est_call = float(cfg.get("est_usd_per_call", 0.05))  # what a call usually costs (for pacing)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def usage(self) -> tuple[float, float] | None:
        """(USD used this billing month, days left in it), straight from Apify."""
        r = self.http.get(f"{APIFY_API}/users/me/limits", headers=self._headers(), timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Apify limits: HTTP {r.status_code}")
        data = (r.json() or {}).get("data") or {}
        raw = (data.get("current") or {}).get("monthlyUsageUsd")
        if not isinstance(raw, (int, float)):  # can't tell what's been spent: spend nothing
            raise RuntimeError("Apify didn't say how much has been used this month")
        used = float(raw)
        end = ((data.get("monthlyUsageCycle") or {}).get("endAt")) or ""
        try:
            end_ts = datetime.strptime(end[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            days_left = max(1.0, (end_ts - time.time()) / 86400)
        except ValueError:
            days_left = 15.0
        return used, days_left

    def calls_allowed(self) -> int:
        """How many calls fit in today's share of what's left of the free credit (0 = wait)."""
        if not self.token:
            return 0
        try:
            used, days_left = self.usage()
        except Exception as ex:  # noqa: BLE001
            self.log(f"  apify: couldn't check spending ({quiet_error(ex)}); skipping this run to be safe")
            return 0
        self.store.set_meta("apify_used_usd", f"{used:.2f}")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.store.get_meta("apify_day") != day:
            self.store.set_meta("apify_day", day)
            self.store.set_meta("apify_day_start", f"{used:.4f}")
        start = float(self.store.get_meta("apify_day_start", str(used)) or used)
        today_share = max(0.0, self.budget - start) / max(1.0, days_left)
        spent_today = max(0.0, used - start)
        if used >= self.budget:
            return 0
        return max(0, int((today_share - spent_today) // self.est_call))

    def run(self, actor: str, body: dict, max_items: int) -> list[dict]:
        poster = getattr(self.http, "post", None) or requests.post
        params = {"timeout": int(self.cfg.get("timeout_seconds", 240)), "maxItems": max_items,
                  "maxTotalChargeUsd": f"{self.per_call:.2f}"}
        wait = int(self.cfg.get("timeout_seconds", 240)) + 30
        r = poster(f"{APIFY_API}/acts/{actor}/run-sync-get-dataset-items", params=params, json=body,
                   headers=self._headers(), timeout=wait)
        if r.status_code == 404:  # Apify's newer address for the same thing
            r = poster(f"{APIFY_API}/actors/{actor}/run-sync-get-dataset-items", params=params, json=body,
                       headers=self._headers(), timeout=wait)
        if r.status_code == 402 or (r.status_code == 403 and "usage" in (r.text or "").lower()):
            raise ApifyCreditUsedUp("Apify free credit is used up for now")
        if r.status_code >= 400:
            raise RuntimeError(f"Apify {actor}: HTTP {r.status_code} {quiet_error((r.text or '')[:120])}")
        data = r.json()
        return data if isinstance(data, list) else []


def _num(*values) -> int:
    for v in values:
        try:
            if v is not None and int(float(v)) > 0:
                return int(float(v))
        except (TypeError, ValueError):
            continue
    return 0


def tiktok_items(items: list[dict], kw: str, min_views: int) -> list[Candidate]:
    out = []
    for it in items:
        m = re.match(r"https://www\.tiktok\.com/@([A-Za-z0-9_.]{1,40})/video/(\d{5,25})", it.get("webVideoUrl") or "")
        plays = _num(it.get("playCount"), (it.get("stats") or {}).get("playCount"))
        if not m or plays < min_views:
            continue
        url = f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}"
        author = (it.get("authorMeta") or {}).get("name") or ""
        text = clean(it.get("text") or "", 700)
        out.append(Candidate(
            title=clean(text, 200) or f"TikTok video by @{author}",
            url=url,
            source=f"TikTok · @{clean(author, 60)}",
            summary=text,
            published=_num(it.get("createTime")) or None,
            signal=f"{fmt_num(plays)} views on TikTok",
            signal_value=plays,
            extra={"q": kw, "platform": "tiktok", "likes": _num(it.get("diggCount")),
                   "comments": _num(it.get("commentCount")), "shares": _num(it.get("shareCount"))},
        ))
    return out


def _flatten_ig(items: list[dict]) -> list[dict]:
    """The Instagram search returns posts, sometimes wrapped inside the hashtag/keyword result."""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nested = [x for key in ("topPosts", "latestPosts", "posts", "reels", "items") for x in (it.get(key) or [])
                  if isinstance(x, dict)]
        out += nested or [it]
    return out


def instagram_items(items: list[dict], kw: str, min_views: int, min_likes: int) -> list[Candidate]:
    out = []
    for it in _flatten_ig(items):
        code = it.get("shortCode") or ""
        if not code:
            m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", it.get("url") or "")
            code = m.group(1) if m else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,40}", code):
            continue
        url = f"https://www.instagram.com/p/{code}/"  # /p/ and /reel/ links to the same post count as one
        plays = _num(it.get("videoPlayCount"), it.get("igPlayCount"), it.get("videoViewCount"), it.get("playCount"))
        likes = _num(it.get("likesCount"))
        if plays < min_views and likes < min_likes:
            continue
        caption = clean(it.get("caption") or "", 700)
        owner = it.get("ownerUsername") or ""
        said = [clean(cm.get("text") or "", 300) for cm in (it.get("latestComments") or []) if isinstance(cm, dict)]
        signal = f"{fmt_num(plays)} views on Instagram" if plays else f"{fmt_num(likes)} likes on Instagram"
        out.append(Candidate(
            title=clean(caption, 200) or f"Instagram reel by @{owner}",
            url=url,
            source=f"Instagram · @{clean(owner, 60)}",
            summary=caption,
            published=iso_time((it.get("timestamp") or "")[:19] + "Z") if it.get("timestamp") else None,
            signal=signal,
            signal_value=plays or likes * 20,
            extra={"q": kw, "platform": "instagram", "likes": likes, "comments": _num(it.get("commentsCount")),
                   "comment_texts": [t for t in said if t][:15]},
        ))
    return out


def plan_apify(store, vcfg: dict, calls: int) -> list[dict]:
    """Which platform, keyword and time range each Apify call uses. TikTok and Instagram take turns (TikTok more
    often), and each platform walks through every keyword (and TikTok through every time range) on its own."""
    acfg = vcfg.get("apify") or {}
    kws = [k for k in (acfg.get("keywords") or vcfg.get("keywords") or []) if k.strip()]
    weights = {name: int((acfg.get(name) or {}).get("weight", 2 if name == "tiktok" else 1))
               for name in ("tiktok", "instagram") if (acfg.get(name) or {}).get("enabled", True)}
    # taking turns, e.g. weights 2:1 -> tiktok, instagram, tiktok, tiktok, instagram, tiktok...
    platforms = [name for r in range(max(weights.values(), default=0)) for name, w in weights.items() if w > r]
    if not kws or not platforms or calls <= 0:
        return []
    dates = (acfg.get("tiktok") or {}).get("date_filters") or ["PAST_MONTH", "LAST_3_MONTHS", "LAST_6_MONTHS", "ALL_TIME"]
    turn = int(store.get_meta("viral_apify_turn", "0") or 0)
    plan = []
    for _ in range(min(calls, int(acfg.get("max_calls_per_run", 2)))):
        name = platforms[turn % len(platforms)]
        turn += 1
        n = int(store.get_meta(f"viral_apify_{name}", "0") or 0) + sum(1 for p in plan if p["platform"] == name)
        plan.append({"platform": name, "q": kws[n % len(kws)], "date": dates[(n // len(kws)) % len(dates)]})
    return plan


def apify_done(store, done: list[dict], credit_used_up: bool) -> None:
    """Main thread only: moves each platform's cursor on for the calls that ran."""
    if done:
        store.set_meta("viral_apify_turn", str(int(store.get_meta("viral_apify_turn", "0") or 0) + len(done)))
    for name in ("tiktok", "instagram"):
        n = sum(1 for p in done if p["platform"] == name)
        if n:
            store.set_meta(f"viral_apify_{name}", str(int(store.get_meta(f"viral_apify_{name}", "0") or 0) + n))
    if credit_used_up:
        store.set_meta("apify_day_start", "999")  # no more calls until tomorrow's fresh check


def apify_viral(apify: Apify, plan: list[dict], vcfg: dict, log, deadline: float | None = None):
    """Runs on a helper thread: touches no files, only Apify. Returns (videos, calls that ran, credit used up)."""
    acfg = vcfg.get("apify") or {}
    min_views = int(vcfg.get("min_views", 1_000_000))
    wait = int(acfg.get("timeout_seconds", 150)) + 30
    out, done, used_up = [], [], False
    for p in plan:
        if deadline is not None and time.time() + wait > deadline:
            break  # not enough time left in this run for another call
        try:
            if p["platform"] == "tiktok":
                t = acfg.get("tiktok") or {}
                n = int(t.get("results_per_call", 25))
                body = {"searchQueries": [p["q"]], "searchSection": "/video", "videoSearchSorting": "MOST_LIKED",
                        "videoSearchDateFilter": p["date"], "resultsPerPage": n, "excludePinnedPosts": True,
                        "shouldDownloadVideos": False, "shouldDownloadCovers": False,
                        "shouldDownloadSlideshowImages": False, "shouldDownloadAvatars": False,
                        "shouldDownloadMusicCovers": False, "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES"}
                items = apify.run(t.get("actor", "clockworks~tiktok-scraper"), body, n)
                out += tiktok_items(items, p["q"], min_views)
            else:
                g = acfg.get("instagram") or {}
                n = int(g.get("results_per_call", 25))
                body = {"search": p["q"], "searchType": "popular", "searchLimit": n}
                items = apify.run(g.get("actor", "apify~instagram-search-scraper"), body, n)
                out += instagram_items(items, p["q"], min_views, int(g.get("min_likes", 50_000)))
            done.append(p)
        except ApifyCreditUsedUp as ex:
            log(f"  {p['platform']}: {ex}")
            used_up = True
            break
        except Exception as ex:  # noqa: BLE001
            done.append(p)  # a failed call still moves on, so one broken keyword can't block the rest
            log(f"  {p['platform']}: {quiet_error(ex)}")
    return out, done, used_up

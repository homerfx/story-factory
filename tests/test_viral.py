"""Offline test of viral mode: fake YouTube, Apify (TikTok/Instagram), Wikipedia, Bing, AI and Discord.
Run with:  python tests/test_viral.py"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import run as factory_run  # noqa: E402
from factory import research  # noqa: E402
from factory.llm import LLM  # noqa: E402
from factory.store import Store  # noqa: E402
from test_offline import FakeDiscord, FakeResponse, make_cfg  # noqa: E402

NOW_ISO = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 86400))
OWN = "UCown"
ONCA = "UConca"

# your channel's uploads and ONCA's
CHANNEL_VIDEOS = {
    "own1": ("A Pen Was In Her Stomach For 25 YEARS 🤯 #roblox #shorts", 6930000, OWN),
    "own2": ("Hero Girl SAVES The WHOLE Beach #shorts", 5470000, OWN),
    "onca1": ("13-Year-Old STOPPED a K*dnapper With Slingshot 🤯", 16800000, ONCA),
}
# what the YouTube search finds
SEARCH_VIDEOS = {
    "v_hero": dict(title="This kid saved his little sister from a frozen river", views=8_400_000, cat="22",
                   kids=False, ch="UCnews", desc="A 9-year-old boy in Ohio pulled his sister out of an icy river."),
    "v_rhyme": dict(title="Baby Shark Dance", views=90_000_000, cat="10", kids=True, ch="UCpink", desc=""),
    "v_trailer": dict(title="WAY OF THE WARRIOR KID Official Trailer", views=5_000_000, cat="24", kids=False,
                      ch="UCign", desc=""),
    "v_small": dict(title="Kid saved a puppy", views=40_000, cat="22", kids=False, ch="UCx", desc=""),
    "v_onca": dict(title="Kid stopped a kidnapper", views=16_800_000, cat="24", kids=False, ch=ONCA, desc=""),
    "v_prank": dict(title="I pranked my kid with fake kidnapping", views=3_000_000, cat="22", kids=False,
                    ch="UCprank", desc="prank"),
    "v_posted": dict(title="Hero girl saves the whole beach from a tsunami", views=4_000_000, cat="22", kids=False,
                     ch="UCnews2", desc="girl beach tsunami"),
    # a re-upload of the river story by another channel, with a different title
    "v_reupload": dict(title="Brave boy saved his little sister from a frozen river (full clip)", views=2_000_000, cat="24",
                       kids=False, ch="UCclips", desc="river rescue"),
}
TIKTOK_ITEMS = [
    {"webVideoUrl": "https://www.tiktok.com/@dad/video/7300000000000000111?lang=en", "playCount": 12_000_000, "diggCount": 900_000,
     "commentCount": 5000, "text": "My son was arrested for drawing on his desk at school #fyp", "createTime": 1700000000,
     "authorMeta": {"name": "dad"}},
    {"webVideoUrl": "https://www.tiktok.com/@x/video/7300000000000000222", "playCount": 50_000, "text": "kid dance",
     "authorMeta": {"name": "x"}},
]
IG_ITEMS = [{"topPosts": [{"url": "https://www.instagram.com/reel/ABCDE/?igsh=x", "videoPlayCount": 2_500_000, "likesCount": 100_000,
                           "caption": "This kid survived 3 days in the woods by eating berries", "ownerUsername": "news"}]}]
BING_RSS = ("<rss><channel><title>Bing</title><item><title>Boy, 9, saves sister from icy river</title>"
            "<link>https://www.localnews.example.com/boy-saves-sister</link>"
            "<description>A 9-year-old boy pulled his little sister from an icy river in Ohio.</description></item>"
            "</channel></rss>")


class ViralHTTP:
    def __init__(self, apify_used=0.5):
        self.calls, self.posts = [], []
        self.apify_used = apify_used
        self.apify_status = 201

    def is_public(self, url):
        return url.startswith(("http://", "https://"))

    def get(self, url, params=None, timeout=20, headers=None, stream=False, allow_redirects=True):
        params = params or {}
        self.calls.append((url, dict(params)))
        if "googleapis.com/youtube/v3/channels" in url:
            cid = OWN if "tylerr" in params.get("forHandle", "") else ONCA
            return FakeResponse(data={"items": [{"id": cid, "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid}}}]})
        if "googleapis.com/youtube/v3/playlistItems" in url:
            ch = params["playlistId"][2:]
            ids = [k for k, v in CHANNEL_VIDEOS.items() if v[2] == ch]
            return FakeResponse(data={"items": [{"contentDetails": {"videoId": i}} for i in ids]})
        if "googleapis.com/youtube/v3/search" in url:
            return FakeResponse(data={"items": [{"id": {"videoId": k}} for k in SEARCH_VIDEOS]})
        if "googleapis.com/youtube/v3/videos" in url:
            items = []
            for vid in params["id"].split(","):
                if vid in CHANNEL_VIDEOS:
                    t, v, ch = CHANNEL_VIDEOS[vid]
                    items.append({"id": vid, "snippet": {"title": t, "channelId": ch}, "statistics": {"viewCount": str(v)}})
                elif vid in SEARCH_VIDEOS:
                    s = SEARCH_VIDEOS[vid]
                    items.append({"id": vid, "snippet": {"title": s["title"], "channelId": s["ch"], "categoryId": s["cat"],
                                                         "channelTitle": "Chan", "description": s["desc"],
                                                         "publishedAt": NOW_ISO},
                                  "statistics": {"viewCount": str(s["views"]), "likeCount": "1000", "commentCount": "50"},
                                  "status": {"madeForKids": s["kids"]}})
            return FakeResponse(data={"items": items})
        if "googleapis.com/youtube/v3/commentThreads" in url:
            return FakeResponse(data={"items": [{"snippet": {"topLevelComment": {"snippet": {
                "textDisplay": "This happened in my town, the news covered it", "likeCount": 300}}}}]})
        if "api.apify.com/v2/users/me/limits" in url:
            end = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 20 * 86400))
            current = {} if self.apify_used is None else {"monthlyUsageUsd": self.apify_used}
            return FakeResponse(data={"data": {"current": current,
                                               "limits": {"maxMonthlyUsageUsd": 5}, "monthlyUsageCycle": {"endAt": end}}})
        if "wikipedia.org/w/api.php" in url:
            if params.get("list") == "search":
                return FakeResponse(data={"query": {"search": [{"title": "Ice rescue"}]}})
            return FakeResponse(data={"query": {"pages": {"1": {"extract": "An ice rescue is when someone is pulled from ice."}}}})
        if "bing.com" in url:
            return FakeResponse(text=BING_RSS, ctype="application/rss+xml")
        return FakeResponse(text="<html><body><p>" + "The boy, 9, pulled his sister from the icy river. " * 4
                            + "</p></body></html>")

    def post(self, url, params=None, json=None, headers=None, timeout=60):
        self.posts.append((url, dict(params or {}), json))
        assert headers and headers["Authorization"].startswith("Bearer "), "Apify token must go in the header"
        if self.apify_status != 201:
            return FakeResponse(status=self.apify_status, text="Monthly usage hard limit exceeded")
        if "tiktok" in url:
            return FakeResponse(status=201, data=TIKTOK_ITEMS)
        return FakeResponse(status=201, data=IG_ITEMS)


class FakeAI(LLM):
    """Screen: stories with saved/survived/arrested score 8. Identify: pranks are not real. Pick: the river story
    gets 9, everything else 7."""

    def complete(self, stage, system, user, max_tokens=900):
        if system == research.IDENTIFY:
            assert stage == "screen", "the identify step should use the cheap screen AI"
            prank = "prank" in user.lower()
            return json.dumps({"real_story": not prank, "kid_at_center": True, "what_happened": "A boy saved someone.",
                               "names_places": "Ohio", "search": "boy saves sister icy river", "staged_risk": "low"})
        if stage == "screen":
            out = []
            for m in re.finditer(r"^\[(\d+)\] (.+)$", user, flags=re.M):
                t = m.group(2).lower()
                out.append({"n": int(m.group(1)), "score": 8 if any(w in t for w in ("saved", "survived", "arrested", "prank"))
                            else 2, "why": "fake"})
            return json.dumps({"results": out})
        if system == research.IDENTIFY:
            prank = "prank" in user.lower()
            return json.dumps({"real_story": not prank, "kid_at_center": True, "what_happened": "A boy saved someone.",
                               "names_places": "Ohio", "search": "boy saves sister icy river", "staged_risk": "low"})
        assert "MOST-VIEWED VIDEOS" in system and "A Pen Was In Her Stomach" in system, "top 50 missing from the judge"
        assert "#roblox" not in system, "hashtags should be cleaned off your titles"
        link = re.search(r"LINK \(linkable\): (\S+)", user)
        river = "river" in re.search(r"TITLE / CAPTION: (.+)", user).group(1).lower()
        same = "icy river" in user.split("ALREADY PICKED STORIES", 1)[1] if "ALREADY PICKED STORIES" in user else False
        story = ("A 9-year-old boy saved his sister from an icy river." if river else
                 "A boy was arrested for drawing on his desk at school." if "desk" in user else
                 "A little boy survived three days alone in the woods eating berries.")
        return json.dumps({"score": 9 if river else 7, "lane": "kid-heroes", "story": story,
                           "payoff": "He was given a medal.", "hook_idea": "This 9-year-old ran onto thin ice.",
                           "emotion": "love and excitement, because a little kid risked everything",
                           "comment_bait": "Would you do it", "most_like": "Hero Girl SAVES The WHOLE Beach",
                           "verified": "yes", "source_url": link.group(1) if link else "",
                           "same_as": "A 9-year-old boy saved his sister from an icy river." if (river and same) else "",
                           "kid_at_center": True, "is_viral_clip": False, "is_big_news": False,
                           "staged_risk": "low", "child_hurt": False, "red_flags": ""})


def viral_cfg(tmp):
    cfg = make_cfg(tmp)
    cfg["mode"] = "viral"
    with open(os.path.join(tmp, "exclude.txt"), "w", encoding="utf-8") as f:
        f.write("# test list\n")
    return cfg


def test_viral():
    os.environ["DISCORD_GUILD_ID"] = "1"
    os.environ["YOUTUBE_API_KEY"] = "yt-key"
    os.environ["APIFY_TOKEN"] = "apify-token"
    quiet = lambda *a, **k: None  # noqa: E731
    tmp = tempfile.mkdtemp()
    cfg = viral_cfg(tmp)
    http, discord = ViralHTTP(), FakeDiscord()

    r1 = factory_run.run_viral(cfg, http=http, llm=FakeAI(cfg, quiet), api=discord)
    print("run 1:", json.dumps({k: r1.get(k) for k in ("found", "searches", "new", "screened", "deep_checked", "ready", "posted")}))
    cid = discord.channels[0]["id"]
    cards = [m["payload"]["embeds"][0] for m in discord.messages[cid] if (m["payload"].get("embeds") or [{}])[0].get("url")]
    assert len(cards) == 1, [c["title"] for c in cards]
    card = cards[0]
    assert "icy river" in card["title"] and card["url"] == "https://www.youtube.com/watch?v=v_hero", card
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert fields["Proof it went viral"] == "8.4M views on YouTube", fields
    assert fields["Facts checked"].startswith("✅") and fields["Most like your video"].startswith("Hero Girl"), fields
    assert fields["Type"] == "YouTube · retold", fields
    assert "**Fact check:** https://en.wikipedia.org/wiki/Ice_rescue" in card["description"] or \
           "**Fact check:** https://www.localnews.example.com/boy-saves-sister" in card["description"], card["description"]

    # what got filtered: kids' songs, trailers, small videos, ONCA's video, a story you already posted
    st = Store(cfg["state_path"])
    statuses = dict(st.db.execute("SELECT url, status FROM items WHERE url IS NOT NULL").fetchall())
    for bad in ("v_rhyme", "v_trailer", "v_small", "v_onca"):
        assert not any(bad in u for u in statuses), f"{bad} should never reach the AI"
    assert not any("v_posted" in u for u in statuses), "a story you already posted reached the AI"
    assert statuses.get("https://www.youtube.com/watch?v=v_prank") == "rejected", statuses
    assert statuses.get("https://www.tiktok.com/@dad/video/7300000000000000111") == "rejected", "the 7/10 TikTok story isn't hand-picked"
    assert "https://www.instagram.com/p/ABCDE/" in statuses, "Instagram reel missing (or not in /p/ form)"
    pair = {statuses.get("https://www.youtube.com/watch?v=v_reupload"), statuses.get("https://www.youtube.com/watch?v=v_hero")}
    assert pair == {"posted", "already_pitched"}, f"two videos of one story: one card, one skipped ({pair})"
    assert statuses["https://www.youtube.com/watch?v=v_hero"] == "posted", "the bigger video should win"
    assert not any("video/7300000000000000222" in u for u in statuses), "50K-view TikTok isn't viral"

    # Apify: token in the header (checked in post), budget cap passed on, TikTok sorted by most liked
    tk = next(p for p in http.posts if "tiktok" in p[0])
    assert tk[1]["maxTotalChargeUsd"] == "0.10" and tk[2]["videoSearchSorting"] == "MOST_LIKED", tk
    assert not any("token" in str(p[1]).lower() for p in http.posts), "the token must never go in the address"
    # YouTube: units counted, one fresh + one archive search
    searches = [c for c in http.calls if "youtube/v3/search" in c[0]]
    assert len(searches) == 2 and "publishedBefore" in searches[1][1] and "publishedBefore" not in searches[0][1]
    assert int(st.get_meta(f"yt_units_{time.strftime('%Y-%m-%d', time.gmtime(time.time() - 8 * 3600))}")) >= 200

    # run 2: nothing new, nothing re-posted, the profile isn't reloaded, searches move on to new keywords
    before = len(discord.messages[cid])
    r2 = factory_run.run_viral(cfg, http=http, llm=FakeAI(cfg, quiet), api=discord)
    assert r2["posted"] == 0 and len(discord.messages[cid]) == before, "re-posted something"
    assert sum("channels" in c[0] for c in http.calls) == 2, "channel profile should load once a day"
    qs = [c[1]["q"] for c in http.calls if "youtube/v3/search" in c[0]]
    assert qs[2] != qs[0], "the fresh search should move to the next keyword"

    # Apify budget used up -> no TikTok/Instagram calls at all
    posts_before = len(http.posts)
    http.apify_used = 4.6
    factory_run.run_viral(cfg, http=http, llm=FakeAI(cfg, quiet), api=discord)
    assert len(http.posts) == posts_before, "Apify was called past the monthly budget"
    # Apify stops saying how much is used -> spend nothing
    http.apify_used = None
    factory_run.run_viral(cfg, http=http, llm=FakeAI(cfg, quiet), api=discord)
    assert len(http.posts) == posts_before, "Apify was called without knowing the spending"
    # Apify says the credit is gone (402) -> one call, then no more today, and the run doesn't crash
    cfg3 = viral_cfg(tempfile.mkdtemp())
    h3 = ViralHTTP()
    h3.apify_status = 402
    factory_run.run_viral(cfg3, http=h3, llm=FakeAI(cfg3, quiet), api=FakeDiscord())
    assert len(h3.posts) == 1 and Store(cfg3["state_path"]).get_meta("apify_day_start") == "999", len(h3.posts)
    factory_run.run_viral(cfg3, http=h3, llm=FakeAI(cfg3, quiet), api=FakeDiscord())
    assert len(h3.posts) == 1, "Apify called again after it said the credit was gone"

    # Apify calls walk through the keywords per platform (never the same TikTok call twice in a row)
    from factory import viral
    st4 = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
    vc = cfg["viral"]
    calls = []
    for _ in range(6):
        plan = viral.plan_apify(st4, vc, 1)
        viral.apify_done(st4, plan, False)
        calls.append((plan[0]["platform"], plan[0]["q"], plan[0]["date"]))
    assert len(set(calls)) == 6, calls

    # a story the old news mode left "ready" is never posted in viral mode
    cfg5 = viral_cfg(tempfile.mkdtemp())
    st5 = Store(cfg5["state_path"])
    from factory.models import Candidate
    st5.add(Candidate(title="News: teacher arrested", url="https://news.example.com/x", source="Bing"), "ready", 8,
            {"story": "A teacher was arrested.", "score": 8})
    d5 = FakeDiscord()
    os.environ.pop("APIFY_TOKEN")
    factory_run.run_viral(cfg5, http=ViralHTTP(), llm=FakeAI(cfg5, quiet), api=d5)
    os.environ["APIFY_TOKEN"] = "apify-token"
    sent = [m["payload"]["embeds"][0]["title"] for msgs in d5.messages.values() for m in msgs]
    assert not any("teacher" in t.lower() for t in sent), sent

    # no keys at all -> runs without crashing
    for k in ("YOUTUBE_API_KEY", "APIFY_TOKEN"):
        os.environ.pop(k)
    cfg2 = viral_cfg(tempfile.mkdtemp())
    r = factory_run.run_viral(cfg2, http=ViralHTTP(), llm=FakeAI(cfg2, quiet), api=FakeDiscord())
    assert r["posted"] == 0
    print("VIRAL CHECKS PASSED")


def test_daily_report_line():
    os.environ["DISCORD_GUILD_ID"] = "1"
    os.environ["YOUTUBE_API_KEY"] = "yt-key"
    os.environ["APIFY_TOKEN"] = "apify-token"
    quiet = lambda *a, **k: None  # noqa: E731
    cfg = viral_cfg(tempfile.mkdtemp())
    discord = FakeDiscord()
    factory_run.run_viral(cfg, http=ViralHTTP(), llm=FakeAI(cfg, quiet), api=discord)
    cid = discord.channels[0]["id"]
    report = next(m["payload"]["embeds"][0] for m in discord.messages[cid]
                  if "Daily report" in m["payload"]["embeds"][0].get("title", ""))
    assert "Apify (TikTok + Instagram) used this month:** $0.50" in report["description"], report["description"]
    assert "TikTok" in report["description"] and "YouTube" in report["description"]
    os.environ.pop("YOUTUBE_API_KEY")
    os.environ.pop("APIFY_TOKEN")
    print("REPORT CHECKS PASSED")


if __name__ == "__main__":
    test_viral()
    test_daily_report_line()

"""Full offline test: fake websites, fake AI, fake Discord. Run with:  python tests/test_offline.py"""
from __future__ import annotations

import copy
import email.utils
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import run as factory_run  # noqa: E402
from factory.config import load_config  # noqa: E402
from factory.llm import LLM  # noqa: E402

NOW = email.utils.formatdate(time.time() - 3600, usegmt=True)


def gnews_item(title, link):
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{NOW}</pubDate><source url='x'>Pub</source></item>"


GNEWS = (
    "<rss><channel><title>Google News</title>"
    + gnews_item("Dad teaches bully son a lesson at barbershop after he mocked classmate - Pub", "https://news.google.com/rss/articles/a1")
    + gnews_item("Teacher arrested after 12-year-old student filmed her taking phones - Pub", "https://news.google.com/rss/articles/a2")
    + gnews_item("Stock markets rally as investors cheer rate cut - Pub", "https://news.google.com/rss/articles/a3")
    + gnews_item("Boy trapped in claw machine as parents panic - Pub", "https://news.google.com/rss/articles/a4")
    + gnews_item("Karma: girl's bully gets instant karma at school talent show - Pub", "https://news.google.com/rss/articles/a5")
    + gnews_item("Dad teaches bully son a lesson at barbershop after he mocked a classmate - Pub", "https://news.google.com/rss/articles/a6")
    + gnews_item("POV: when your mom finds your report card - Pub", "https://news.google.com/rss/articles/a7")
    + "</channel></rss>"
)
REDDIT = {
    "data": {
        "children": [
            {"data": {"title": "My dad surprised me at school after 2 years deployed", "permalink": "/r/MadeMeSmile/comments/x1/dad/",
                      "ups": 50000, "num_comments": 900, "created_utc": time.time() - 7200, "selftext": ""}},
            {"data": {"title": "Small post about a kid", "permalink": "/r/MadeMeSmile/comments/x2/small/",
                      "ups": 10, "num_comments": 1, "created_utc": time.time() - 7200}},
        ]
    }
}
GDELT = {"articles": [{"title": "Teen hero saves toddler from pool", "url": "https://example.com/teen-hero",
                       "domain": "example.com", "seendate": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}]}
RSS = (
    "<rss><channel><title>Viral Site</title><item><title>Mom's genius trick to stop her son lying about homework goes viral</title>"
    f"<link>https://viral.example.com/mom-trick</link><description>A mom on TikTok...</description><pubDate>{NOW}</pubDate></item>"
    "</channel></rss>"
)
TRENDS = (
    '<?xml version="1.0" encoding="UTF-8"?><rss xmlns:atom="http://www.w3.org/2005/Atom" '
    'xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel><title>Daily Search Trends</title>'
    f"<item><title>well rescue</title><ht:approx_traffic>20000+</ht:approx_traffic><pubDate>{NOW}</pubDate>"
    "<ht:news_item><ht:news_item_title>Toddler pulled from well after 2 days as whole town cheers</ht:news_item_title>"
    "<ht:news_item_snippet></ht:news_item_snippet><ht:news_item_url>https://trendnews.example.com/well</ht:news_item_url>"
    "<ht:news_item_source>Trend News</ht:news_item_source></ht:news_item></item>"
    f"<item><title>nz vs ind</title><ht:approx_traffic>2000+</ht:approx_traffic><pubDate>{NOW}</pubDate>"
    "<ht:news_item><ht:news_item_title>New Zealand beat India in the cricket final</ht:news_item_title>"
    "<ht:news_item_url>https://sport.example.com/cricket</ht:news_item_url>"
    "<ht:news_item_source>Sport</ht:news_item_source></ht:news_item></item>"
    "</channel></rss>"
)
BING = (
    '<?xml version="1.0" encoding="utf-8" ?><rss version="2.0" xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss">'
    "<channel><title>Bing</title>"
    "<item><title>Boy, 9, finds rare fossil on his beach walk</title>"
    "<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;tid=abc&amp;url=https%3a%2f%2fwww.fossilnews.example.com%2fboy-fossil&amp;c=1&amp;mkt=en-us</link>"
    f"<description>A 9-year-old boy found a fossil...</description><pubDate>{NOW}</pubDate>"
    "<News:Source>Fossil News</News:Source></item>"
    "</channel></rss>"
)


def suggest_reply(q):
    q = q.strip()
    return [q, [f"{q} his little sister from a fire", f"{q} lyrics", "kid"]]


class FakeResponse:
    def __init__(self, status=200, text="", data=None, ctype="text/html"):
        self.status_code = status
        self.text = text if data is None else json.dumps(data)
        self.content = self.text.encode("utf-8")
        self._data = data
        self.headers = {"Content-Type": ctype}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.params = []

    def is_public(self, url):  # the fake websites all count as public; the real check is tested in test_security
        return url.startswith(("http://", "https://")) and "192.168." not in url and "127.0.0.1" not in url

    def get(self, url, params=None, timeout=20, headers=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        self.params.append(params or {})
        if "redirect-to-router" in url:
            r = FakeResponse(status=302)
            r.headers["Location"] = "http://192.168.1.1/admin"
            return r
        if "news.google.com/rss/search" in url:
            return FakeResponse(text=GNEWS, ctype="application/rss+xml")
        if "trends.google.com/trending/rss" in url:
            return FakeResponse(text=TRENDS, ctype="application/rss+xml")
        if "suggestqueries.google.com" in url:
            return FakeResponse(data=suggest_reply(params["q"]), ctype="text/javascript")
        if "bing.com/news/search" in url:
            return FakeResponse(text=BING, ctype="application/rss+xml")
        if "reddit.com/r/" in url and url.endswith("top.json"):
            return FakeResponse(data=REDDIT, ctype="application/json")
        if "reddit.com/r/" in url and url.endswith(".json"):
            return FakeResponse(data=[{"data": {"children": [{"data": {"title": "dad", "selftext": "", "url": ""}}]}},
                                      {"data": {"children": [{"data": {"body": "Original is on TikTok", "ups": 50}}]}}],
                                ctype="application/json")
        if "gdeltproject" in url:
            return FakeResponse(data=GDELT, ctype="application/json")
        if url.startswith("https://www.goodnewsnetwork.org") or "feed" in url:
            return FakeResponse(text=RSS, ctype="application/rss+xml")
        return FakeResponse(text="<html><body><p>" + "A real article paragraph with facts. " * 5 + "</p></body></html>")


class FakeDiscord:
    def __init__(self):
        self.channels = []
        self.messages = {}  # channel id -> list of message dicts
        self.next_id = 1000

    def _id(self):
        self.next_id += 1
        return str(self.next_id)

    def guild_channels(self, guild_id):
        return list(self.channels)

    def create_channel(self, guild_id, name, kind, parent_id=None, topic=""):
        ch = {"id": self._id(), "name": name, "type": kind, "parent_id": parent_id}
        self.channels.append(ch)
        return ch

    def send(self, channel_id, payload):
        mid = self._id()
        self.messages.setdefault(channel_id, []).append({"id": mid, "payload": payload, "reactions": []})
        return mid

    def react(self, channel_id, message_id, emoji):
        for m in self.messages[channel_id]:
            if m["id"] == message_id:
                m["reactions"].append({"emoji": {"name": emoji}, "count": 1, "me": True})

    def messages_since(self, channel_id, since_ts, max_pages=5):
        return [{"id": m["id"], "reactions": m["reactions"]} for m in self.messages.get(channel_id, [])]

    def name_of(self, cid):
        return next(c["name"] for c in self.channels if c["id"] == cid)


class RecordingLLM(LLM):
    def __init__(self, cfg, log):
        super().__init__(cfg, log)
        self.systems = []

    def complete(self, stage, system, user, max_tokens=900):
        if stage == "deep":
            self.systems.append(system)
        return super().complete(stage, system, user, max_tokens)


def make_cfg(tmp):
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    cfg = copy.deepcopy(cfg)
    cfg["state_path"] = os.path.join(tmp, "state.db")
    cfg["posted_path"] = os.path.join(tmp, "posted.txt")
    cfg["last_run_path"] = os.path.join(tmp, "last_run.json")
    # the tests use their own "already used" list, so they don't depend on the real data/exclude.txt
    with open(os.path.join(tmp, "exclude.txt"), "w", encoding="utf-8") as f:
        f.write("# test list\nBoy trapped in claw machine, parents panic\n")
    cfg["exclude_paths"] = [os.path.join(tmp, "exclude.txt")]
    cfg["ai"]["providers"] = ["mock"]
    cfg["ai"].pop("screen_providers", None)
    cfg["ai"].pop("deep_providers", None)
    cfg["providers"]["mock"] = {"type": "mock"}
    cfg["discord"]["report_hour"] = 0
    for s in cfg["sources"].values():
        s["pause_seconds"] = 0
    cfg["discover"]["pause_seconds"] = 0
    cfg["sources"]["reddit"]["subreddits"] = ["MadeMeSmile"]
    cfg["sources"]["gdelt"]["queries"] = cfg["sources"]["gdelt"]["queries"][:1]
    return cfg


def is_card(m):
    return bool((m["payload"].get("embeds") or [{}])[0].get("url"))


def test_one_channel():
    """The default setup: one channel for every story + the daily report, starter reactions never counted."""
    os.environ["DISCORD_GUILD_ID"] = "1"
    cfg = make_cfg(tempfile.mkdtemp())
    name = cfg["discord"]["one_channel"]
    assert name, "config.yaml should use one channel"
    http, discord = FakeHTTP(), FakeDiscord()
    quiet = lambda *a, **k: None  # noqa: E731

    r1 = factory_run.run_once(cfg, http=http, llm=RecordingLLM(cfg, quiet), api=discord)
    assert [c["name"] for c in discord.channels] == [name], [c["name"] for c in discord.channels]
    assert not any(c["type"] == 4 for c in discord.channels), "one channel needs no category"
    cid = discord.channels[0]["id"]
    cards = [m for m in discord.messages[cid] if is_card(m)]
    reports = [m for m in discord.messages[cid] if "Daily report" in m["payload"]["embeds"][0].get("title", "")]
    assert len(cards) == r1["posted"] == 4 and len(reports) == 1, (len(cards), r1["posted"], len(reports))
    for m in cards:
        assert {r["emoji"]["name"] for r in m["reactions"]} == {"👍", "👎", "⭐"}, "starter reactions missing"
    top = [m for m in cards if "TOP PICK" in m["payload"]["embeds"][0]["description"]]
    assert len(top) == 1 and "karma" in top[0]["payload"]["embeds"][0]["title"].lower(), "top pick not marked"
    lanes = {f["value"] for m in cards for f in m["payload"]["embeds"][0]["fields"] if f["name"] == "Lane"}
    assert "😤│kid-vs-unfair-adults" in lanes, lanes

    # only the bot's own starter reactions so far -> nothing counted
    r2 = factory_run.run_once(cfg, http=http, llm=RecordingLLM(cfg, quiet), api=discord)
    assert r2["reactions_read"] == 0, f"the bot's own reactions were counted ({r2['reactions_read']})"

    # a friend stars the top pick and thumbs-down another card -> exactly 2 counted, nothing re-posted anywhere
    for m in cards:
        for r in m["reactions"]:
            if (m is top[0] and r["emoji"]["name"] == "⭐") or (m is not top[0] and m is cards[-1] and r["emoji"]["name"] == "👎"):
                r["count"] += 1
    before = len(discord.messages[cid])
    r3 = factory_run.run_once(cfg, http=http, llm=RecordingLLM(cfg, quiet), api=discord)
    assert r3["reactions_read"] == 2, r3["reactions_read"]
    assert len(discord.messages[cid]) == before and len(discord.channels) == 1, "starred story was re-posted"
    print("ONE CHANNEL CHECKS PASSED")


def test_multi_channel():
    os.environ["DISCORD_GUILD_ID"] = "1"
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)
    cfg["discord"]["one_channel"] = ""  # the older layout: one channel per lane
    http, discord = FakeHTTP(), FakeDiscord()
    quiet = lambda *a, **k: None  # noqa: E731

    # ---------- run 1
    llm = RecordingLLM(cfg, quiet)
    r1 = factory_run.run_once(cfg, http=http, llm=llm, api=discord)
    print("run 1:", json.dumps({k: r1[k] for k in ("new", "passed_filter", "screened", "deep_checked", "ready", "posted", "posted_by_lane")}))

    names = {c["name"] for c in discord.channels}
    for lane, spec in cfg["lanes"].items():
        assert spec["channel"] in names, f"missing channel for {lane}"
    assert {"🔥│top-picks", "⭐│picked", "📊│daily-report", "🗂│other-good-stories"} <= names
    assert any(c["type"] == 4 for c in discord.channels), "category not created"

    posted = {discord.name_of(cid): msgs for cid, msgs in discord.messages.items()}
    titles = {name: [m["payload"].get("embeds", [{}])[0].get("title", "") for m in msgs] for name, msgs in posted.items()}
    print("cards per channel:", {k: len(v) for k, v in titles.items()})
    assert any("karma" in t.lower() for t in titles.get("🔥│top-picks", [])), "9/10 karma story should be in top-picks"
    assert any("teacher" in t.lower() for t in titles.get("😤│kid-vs-unfair-adults", [])), "teacher story in unfair-adults"
    assert any("bully" in t.lower() for t in titles.get("🥊│bully-karma-viral-clips", [])), "bully story in bully lane"
    assert any("deployed" in t.lower() for t in titles.get("🗂│other-good-stories", [])), "reddit dad story in other"
    all_titles = " ".join(t for v in titles.values() for t in v).lower()
    assert "claw machine" not in all_titles, "already-pitched story was posted"
    assert "stock markets" not in all_titles and "pov" not in all_titles, "junk was posted"
    assert all_titles.count("barbershop") == 1, "duplicate story posted twice"
    for name, msgs in posted.items():
        if name not in ("📊│daily-report", "⭐│picked"):
            for m in msgs:
                assert {r["emoji"]["name"] for r in m["reactions"]} == {"👍", "👎", "⭐"}, "reactions missing"
    assert len(posted.get("📊│daily-report", [])) == 1, "daily report not sent"
    assert r1["posted"] == 4, r1["posted"]

    # ---------- you react in Discord: 👍 the teacher story, ⭐ the karma story, 👎 the reddit story
    def bump(channel, needle, emoji):
        cid = next(c["id"] for c in discord.channels if c["name"] == channel)
        for m in discord.messages[cid]:
            if needle in m["payload"]["embeds"][0]["title"].lower():
                for r in m["reactions"]:
                    if r["emoji"]["name"] == emoji:
                        r["count"] += 1
    bump("😤│kid-vs-unfair-adults", "teacher", "👍")
    bump("🔥│top-picks", "karma", "⭐")
    bump("🗂│other-good-stories", "deployed", "👎")

    # ---------- run 2: new stories appear, old ones are not re-posted, reactions are learned
    global GNEWS
    GNEWS = GNEWS.replace("</channel>", gnews_item("Teacher made student stand outside in rain for forgetting homework, dad responds - Pub",
                                                  "https://news.google.com/rss/articles/b1") + "</channel>")
    llm2 = RecordingLLM(cfg, quiet)
    r2 = factory_run.run_once(cfg, http=http, llm=llm2, api=discord)
    print("run 2:", json.dumps({k: r2[k] for k in ("new", "screened", "deep_checked", "ready", "posted", "reactions_read")}))
    assert r2["reactions_read"] == 3, r2["reactions_read"]
    assert r2["posted"] == 1, "only the one new story should be posted"
    assert llm2.systems and "He LIKED:" in llm2.systems[0] and "He DISLIKED:" in llm2.systems[0], "taste examples missing"
    assert "LANES" in llm2.systems[0] and "84%" in llm2.systems[0], "rules/lanes missing from the AI prompt"
    picked = next(c["id"] for c in discord.channels if c["name"] == "⭐│picked")
    assert len(discord.messages.get(picked, [])) == 1, "starred story not copied to #picked"
    report_ch = next(c["id"] for c in discord.channels if c["name"] == "📊│daily-report")
    assert len(discord.messages[report_ch]) == 1, "daily report sent twice in one day"

    # ---------- run 3: nothing new -> nothing posted, archive digger moved on
    r3 = factory_run.run_once(cfg, http=http, llm=RecordingLLM(cfg, quiet), api=discord)
    assert r3["posted"] == 0, r3["posted"]
    from factory.store import Store
    st = Store(cfg["state_path"])
    assert st.get_meta("archive_window") != "" and st.get_meta("archive_last"), "archive cursor not saved"
    print("archive digger at:", st.get_meta("archive_last"))
    print("\nALL CHECKS PASSED")


def test_failures():
    """The review's failure cases: no double posts, nothing lost in AI outages, one bad card can't block the rest."""
    from factory.discord import DiscordError
    from factory.llm import NoAIAvailable
    from factory.store import Store
    quiet = lambda *a, **k: None  # noqa: E731

    # 1) reactions fail -> the card is still saved as posted and never sent again
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)

    class NoReactDiscord(FakeDiscord):
        def react(self, *a):
            raise DiscordError("403 Missing Permissions", 403)

    d = NoReactDiscord()
    factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    sent1 = sum(len(v) for v in d.messages.values())
    factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    sent2 = sum(len(v) for v in d.messages.values())
    assert sent1 == sent2, f"cards were re-posted after a reaction failure ({sent1} -> {sent2})"

    # 2) AI runs out before the deep check -> stories wait in the queue and get checked next run
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)

    class ScreenOnlyLLM(RecordingLLM):
        def complete(self, stage, system, user, max_tokens=900):
            if stage == "deep":
                raise NoAIAvailable("out of free uses")
            return super().complete(stage, system, user, max_tokens)

    d = FakeDiscord()
    r = factory_run.run_once(cfg, http=FakeHTTP(), llm=ScreenOnlyLLM(cfg, quiet), api=d)
    assert r["posted"] == 0 and Store(cfg["state_path"]).count("deep_pending") > 0, "queue not kept"
    r = factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    assert r["deep_checked"] > 0 and r["posted"] > 0, "queued stories were never checked"

    # 3) one card rejected by Discord (400) -> skipped, the others still post
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)

    class OneBadCard(FakeDiscord):
        def send(self, channel_id, payload):
            if "karma" in json.dumps(payload).lower() and "embeds" in payload:
                raise DiscordError("400 Invalid Form Body", 400)
            return super().send(channel_id, payload)

    d = OneBadCard()
    r = factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    assert r["posted"] >= 3, f"one bad card blocked the rest (posted {r['posted']})"

    # 4) memory file lost -> rebuilt from Discord, nothing re-posted
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)
    d = FakeDiscord()
    factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    for m in (m for msgs in d.messages.values() for m in msgs):
        m["embeds"] = m["payload"].get("embeds", [])
    orig = FakeDiscord.messages_since
    FakeDiscord.messages_since = lambda self, cid, since, max_pages=5: [
        {"id": m["id"], "reactions": m["reactions"], "embeds": m.get("embeds", [])} for m in self.messages.get(cid, [])]
    stories = lambda: sum(1 for v in d.messages.values() for m in v if is_card(m))  # noqa: E731
    before = stories()
    os.remove(cfg["state_path"])
    open(cfg["posted_path"], "w").close()
    factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    FakeDiscord.messages_since = orig
    after = stories()
    assert after == before, f"stories re-posted after memory loss ({before} -> {after})"
    print("FAILURE CASES PASSED")


def test_wide_net():
    """Trends, autocomplete, Bing, the search pool, the YouTube budget and the time budget."""
    from factory.collect import bing_news, parallel, yt_allowance, yt_spend
    from factory.discover import trends, useful
    from factory.store import Store
    quiet = lambda *a, **k: None  # noqa: E731

    # Bing: the real article link and the publisher come out of Bing's redirect
    items = bing_news(FakeHTTP(), [("boy fossil", "kid-did-the-impossible")], {"pause_seconds": 0}, quiet)
    assert items and items[0].url == "https://www.fossilnews.example.com/boy-fossil", items[0].url
    assert items[0].source == "Bing News · Fossil News", items[0].source
    assert items[0].extra["q"] == "boy fossil"

    # Trends: every trending story is checked; only trends about kids become searches
    cands, terms = trends(FakeHTTP(), {"trends_geos": ["US", "GB"]}, quiet)
    assert {c.url for c in cands} == {"https://trendnews.example.com/well", "https://sport.example.com/cricket"}
    assert terms == ["well rescue"], terms
    assert all(c.signal_value == 0 for c in cands), "trend traffic must not let non-kid stories skip the kid filter"

    # Autocomplete filter: kid stories in, entertainment and fragments out
    assert useful("boy saved his little sister from a fire")
    assert useful("bully gets instant karma at school")
    assert not useful("boy saved lyrics song")
    assert not useful("kid saved")

    # Pool order: trends first, lane searches twice a day, then the longest-waiting; dead phrases retire
    st = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
    st.pool_add([("lane one", "kid-heroes", "lane"), ("auto one", "kid-heroes", "g-suggest"), ("auto two", "", "g-suggest")])
    st.pool_add([("trend one", "", "trend")], expires=time.time() + 3600)
    st.pool_add([("old trend", "", "trend")], expires=time.time() - 1)
    due = [q for q, _ in st.pool_due(10)]
    assert due[:2] == ["trend one", "lane one"] and "old trend" not in due, due
    st.pool_searched(due)
    for _ in range(6):
        st.pool_searched(["auto two", "lane one"])
    st.pool_credit({"auto one": 2}, {})
    assert st.pool_retire(6) == 1, "a phrase that never found anything should retire"
    left = [q for q, _ in st.pool_due(10)]
    assert "auto two" not in left and "lane one" in left and "auto one" in left, left
    assert st.pool_add([(f"x {i} y z", "", "g-suggest") for i in range(50)], cap=st.pool_size() + 5) == 5

    # Autocomplete goes round all seeds, then rests until refresh_hours have passed
    from factory.discover import _next_batch
    st2 = Store(os.path.join(tempfile.mkdtemp(), "s.db"))
    seeds = list("abcde")
    assert _next_batch(st2, "t", seeds, 3, 20) == ["a", "b", "c"]
    assert _next_batch(st2, "t", seeds, 3, 20) == ["d", "e"]
    assert _next_batch(st2, "t", seeds, 3, 20) == [], "should wait before going round again"
    st2.set_meta("t_round_done", str(time.time() - 21 * 3600))
    assert _next_batch(st2, "t", seeds, 3, 20) == ["a", "b", "c"]

    # YouTube: never more searches than the free daily units allow
    os.environ["YOUTUBE_API_KEY"] = "test"
    try:
        assert yt_allowance(st, {"daily_units": 1000}, 50) == 9
        yt_spend(st, 9)
        assert yt_allowance(st, {"daily_units": 1000}, 50) == 0
    finally:
        del os.environ["YOUTUBE_API_KEY"]
    assert yt_allowance(st, {}, 5) == 0, "no key, no searches"

    # Searching stops at the time budget and when a site keeps refusing; logs never show the search words
    out, stats = parallel(lambda i: [i], range(5), 3, deadline=time.time() - 1)
    assert out == [] and stats["skipped"] == 5

    def boom(i):
        raise RuntimeError("HTTP 429 for url: https://news.google.com/rss/search?q=secret+words")

    out, stats = parallel(boom, range(10), 1, max_errors=3)
    assert stats["errors"] == 3 and stats["skipped"] == 7
    assert "secret" not in stats["first_error"], stats["first_error"]

    # Headlines that don't fit in one run's quick screen wait for the next run instead of being lost
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)
    cfg["max_screen_per_run"] = 2
    d = FakeDiscord()
    r1 = factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    waiting = Store(cfg["state_path"]).count("screen_pending")
    assert r1["screened"] == 2 and waiting > 0, (r1["screened"], waiting)
    factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=d)
    assert Store(cfg["state_path"]).count("screen_pending") < waiting, "waiting headlines were never screened"
    assert r1["searches"].get("google_news") and r1["searches"].get("bing") and r1["searches"].get("autocomplete")

    # A run that's out of time stops cleanly and keeps everything for the next run
    tmp = tempfile.mkdtemp()
    cfg = make_cfg(tmp)
    cfg["run_budget_minutes"] = 0.0005
    r = factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=FakeDiscord())
    assert r["deep_checked"] == 0, r
    print("WIDE NET CHECKS PASSED")


def test_world():
    """Every country: country editions, translated searches, other-language headlines, Trends everywhere."""
    from factory.collect import bing_news, gdelt, google_news
    from factory.discover import discover, trends, useful
    from factory.models import Candidate
    from factory.prefilter import prefilter
    from factory.store import Store
    from factory.world import ALL_GEOS, world_queries
    quiet = lambda *a, **k: None  # noqa: E731

    assert len(ALL_GEOS) >= 190
    wq = world_queries({"world": {"enabled": True, "languages": "all"}})
    assert len(wq) > 300 and all(q.startswith("[") for q, _ in wq), len(wq)
    assert any(q.startswith("[lt-LT] ") for q, _ in wq) and any(q.startswith("[ja-JP] ") for q, _ in wq)
    assert len(world_queries({"world": {"enabled": True, "languages": ["es"]}})) == 40  # 10 phrases x 4 countries

    # a tagged search goes to that country's Google News edition and Bing market
    http = FakeHTTP()
    google_news(http, [("[ja-JP] 少年 救助", "")], {"pause_seconds": 0}, quiet)
    assert "hl=ja&gl=JP&ceid=JP:ja" in http.calls[-1] and "%E5%B0%91%E5%B9%B4" in http.calls[-1], http.calls[-1]
    google_news(http, [("boy saved", "")], {"pause_seconds": 0}, quiet)
    assert "ceid=US:en" in http.calls[-1]
    items = bing_news(http, [("[pt-BR] menino salvou", "")], {"pause_seconds": 0}, quiet)
    assert http.params[-1] == {"q": "menino salvou", "format": "rss", "mkt": "pt-BR"}, http.params[-1]
    assert items[0].source.startswith("Bing News BR")

    # headlines in other languages pass the free filter when they're about a kid + an event; junk and abuse don't
    def keep(title, **extra):
        return prefilter(Candidate(title=title, url="https://x.example/" + str(abs(hash(title))), source="t", extra=extra))[0]
    assert keep("Niño de 9 años salvó a su hermana de un incendio")
    assert keep("小学生の男児が溺れた友達を救助")
    assert keep("Мальчик спас тонущую девочку")
    assert keep("Berniukas išgelbėjo draugą iš ežero")
    assert not keep("Gobierno anuncia nuevo presupuesto")
    assert not keep("Niña víctima de abuso sexual en escuela")
    assert keep("Un gesto conmovió a todo el pueblo", translated_match=True), "GDELT translated finds should pass"

    # GDELT: no English-only filter; the country and language show on the card's source line
    global GDELT
    saved = GDELT
    GDELT = {"articles": [{"title": "Un gesto conmovió a todo el pueblo", "url": "https://es.example.com/gesto",
                           "domain": "es.example.com", "language": "Spanish", "sourcecountry": "Mexico",
                           "seendate": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}]}
    try:
        g = gdelt(FakeHTTP(), {"queries": ["(boy OR girl) saved"], "pause_seconds": 0}, quiet)
    finally:
        GDELT = saved
    assert g[0].extra["translated_match"] and "Mexico" in g[0].source and "Spanish" in g[0].source, g[0].source

    # Trends in every country; kid trends become searches in that country's own edition
    cands, terms = trends(FakeHTTP(), {"trends_geos": "all"}, quiet)
    assert "well rescue" in terms and "[pt-BR] well rescue" in terms and "[ja-JP] well rescue" in terms, terms[:5]

    # autocomplete also runs in other languages, and finds land in the pool tagged with their country
    assert useful("[pt-BR] menino salvou a irmã")
    assert not useful("[pt-BR] novela capítulo de hoje")
    cfg = make_cfg(tempfile.mkdtemp())
    st = Store(cfg["state_path"])
    discover(FakeHTTP(), st, cfg, quiet)
    tagged = st.db.execute("SELECT COUNT(*) FROM queries WHERE q LIKE '[%] %' AND origin='g-suggest'").fetchone()[0]
    assert tagged > 0, "no other-language autocomplete phrases in the pool"

    # a full run with the world switched on still works end to end
    r = factory_run.run_once(cfg, http=FakeHTTP(), llm=RecordingLLM(cfg, quiet), api=FakeDiscord())
    assert r["pool"].get("world", 0) > 300, r["pool"]
    print("WORLD CHECKS PASSED")


def test_security():
    """Links from the internet can't reach your PC or home network, and can't ping anyone in Discord."""
    from factory.discord import DiscordAPI
    from factory.fetch import article_text, is_public_url
    for bad in ["http://127.0.0.1/", "http://localhost:8080/", "http://192.168.1.1/admin", "http://10.0.0.5/",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "file:///C:/Windows/win.ini",
                "ftp://example.com/x", "http://router.local/", "https://8.8.8.8:8443/"]:
        assert not is_public_url(bad), bad
    assert is_public_url("https://8.8.8.8/"), "an ordinary public address should be allowed"
    http = FakeHTTP()
    assert article_text(http, "https://news.example.com/redirect-to-router") == ""
    assert not any("192.168" in c for c in http.calls), "followed a redirect into the home network"

    sent = {}

    class Recorder(DiscordAPI):
        def request(self, method, path, payload=None, params=None):
            sent.update(payload or {})
            return {"id": "1"}

    Recorder("token", lambda *a: None).send("123", {"content": "@everyone look"})
    assert sent.get("allowed_mentions") == {"parse": []}, sent

    # the only link on a card is the story's source: links planted in AI/web text are removed
    from factory.discord import build_card
    card = build_card({"title": "t", "url": "https://real.example.com/story", "lane": "other",
                       "data": {"score": 8, "story": "A boy saved his sister",
                                "payoff": "Read more [here](https://evil.example.com/login) or www.evil.example.com",
                                "hook_idea": "Join discord.gg/scam now"}}, make_cfg(tempfile.mkdtemp()))
    text = json.dumps(card)
    assert "evil.example.com" not in text and "discord.gg" not in text and "real.example.com/story" in text, text

    # connecting to a private address is refused at the moment of connecting (no DNS tricks)
    from factory import fetch
    with fetch.public_only():
        try:
            fetch._guarded_create_connection(("127.0.0.1", 80), timeout=1)
            raise AssertionError("connected to a private address")
        except OSError as ex:
            assert "private address" in str(ex), ex
    print("SECURITY CHECKS PASSED")


if __name__ == "__main__":
    test_one_channel()
    test_multi_channel()
    test_failures()
    test_wide_net()
    test_world()
    test_security()

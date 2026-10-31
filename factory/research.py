"""Viral mode, part 2: deep research on each viral video, then a strict hand-pick against your channel's top 50.

1. The AI works out the real story behind the video (from its title/caption, description and top comments).
2. The factory looks the story up: Wikipedia, Bing News and Bing web results, and reads the best articles.
3. The AI judges the story with the facts, your rules, your 50 most-viewed videos and your 👍/👎, and only a
   story that clears the hand-pick bar gets posted."""
from __future__ import annotations

import time
from urllib.parse import quote

import feedparser

from .collect import HTTP, bing_link, clean
from .fetch import article_text
from .judge import normalize
from .llm import LLM, parse_json
from .models import Candidate, fmt_num

VIRAL_SCREEN = """You pick story ideas for a YouTube Shorts channel that retells REAL stories as 25-second Roblox
animations for kids aged 5-16. Below are videos that ALREADY went viral (title or caption + views).

Score each one 0-10 for how likely it shows or retells ONE real story with a KID or TEEN at the center that is
extraordinary but believable, sparks a strong emotion (anger, love, empathy, excitement) and has a payoff.
- 0-2: kids' entertainment, songs, toys, cartoons, trailers, music, games, sports highlights, challenges, cooking,
  products, memes, compilations with no single story.
- 0-3: skits, pranks, POV videos, scripted "lesson" videos (Dhar Mann style), AI videos, fiction.
- 0-4: adult-only stories, politics, celebrity gossip, anything sexual, stories that only work by describing an
  adult hurting a child (accident, survival and illness injuries are fine).
The caption may be in any language: judge the story, never the language. Be strict: most items are 0-4.
Reply with ONLY JSON: {"results": [{"n": <item number>, "score": <0-10>, "why": "<max 10 words>"}]}"""

IDENTIFY = """You are looking at a VIRAL video from YouTube, TikTok or Instagram. Work out the real-life story it
shows or tells. The text may be in any language; always answer in simple American English.

Reply with ONLY JSON:
{
  "real_story": <true if it shows or retells a REAL event with real people; false for skits, pranks, POV videos,
                 scripted "lesson" videos, AI videos, movies, songs, cartoons, fiction, games, or no story at all>,
  "kid_at_center": <true if a child or teenager is the main person of the story>,
  "what_happened": "<2-3 plain sentences: who, what happened, how it ended. Write 'unknown' for parts you can't tell>",
  "names_places": "<names, places and years mentioned; empty if none>",
  "search": "<ONE web search of 4-8 words that would find a news article or Wikipedia page about THIS event>",
  "staged_risk": "<low/medium/high: how likely the video is staged or made up>"
}"""

PICK = """You hand-pick stories for the channel. Below is ONE story from a video that already went viral, with
the facts found online. Judge it against the channel rules, the channel's 50 most-viewed videos and the owner's
taste above. This is a HAND-PICKED list, so be very strict:
- 9-10 only if it could sit next to the channel's top 50 videos (a kid or teen at the center, extraordinary but
  believable, a VERY strong emotion, an ending that pays off the hook) AND the facts check out.
- A story the facts don't support, or that looks staged, can't score above 5.
- Use only the facts given. Never invent details. Write every text field in simple American English.

Reply with ONLY JSON:
{
  "score": <0-10>,
  "lane": "<one lane id from the LANES list, or "other">",
  "story": "<the story in ONE plain sentence a 7-year-old understands>",
  "country": "<country where it happened; empty if unknown>",
  "payoff": "<the ending/payoff in one sentence>",
  "hook_idea": "<a first line for the video: absurd or shocking but believable, no question marks>",
  "emotion": "<the strong emotion it sparks (anger, hate, love, empathy or excitement) and WHY>",
  "comment_bait": "<what viewers would argue or comment about>",
  "most_like": "<the title of the ONE video in the channel's top 50 this story is most like, copied exactly>",
  "same_as": "<if this is the SAME real event as one of the ALREADY PICKED stories, copy that line exactly; else empty>",
  "verified": "<yes = the evidence confirms the key facts / partly = some facts confirmed / no = nothing confirms it>",
  "source_url": "<the ONE evidence link marked (linkable) that best confirms it, copied exactly; empty if none>",
  "kid_at_center": <true/false>,
  "is_viral_clip": <true if the video is real footage of the event, false if someone retells it>,
  "is_big_news": <true if it was a big mainstream news story>,
  "staged_risk": "<low/medium/high>",
  "child_hurt": <true ONLY if the story depends on describing an adult beating or hurting a child; injuries from accidents, crashes, survival or illness do NOT count>,
  "red_flags": "<anything doubtful, missing or risky; empty if none>"
}"""


def profile_text(profile: dict) -> str:
    top = profile.get("top") or []
    if not top:
        return ""
    lines = [f"THE CHANNEL'S {len(top)} MOST-VIEWED VIDEOS (what works; a new story should feel like these):"]
    lines += [f"{i}. {v['title']} - {fmt_num(int(v.get('views', 0)))} views" for i, v in enumerate(top, 1)]
    return "\n".join(lines)


def video_text(c: Candidate, comments: list[str]) -> str:
    x = c.extra or {}
    parts = [f"PLATFORM: {c.source}", f"TITLE / CAPTION: {c.title}", f"POPULARITY: {c.signal}"
             + (f", {fmt_num(int(x.get('likes', 0)))} likes" if x.get("likes") else "")
             + (f", {fmt_num(int(x.get('comments', 0)))} comments" if x.get("comments") else "")]
    if c.published:
        parts.append("POSTED: " + time.strftime("%Y-%m-%d", time.gmtime(c.published)))
    if c.summary and c.summary != c.title:
        parts.append(f"DESCRIPTION: {c.summary[:900]}")
    if comments:
        parts.append("TOP COMMENTS:\n" + "\n".join(f"- {t}" for t in comments[:20]))
    return "\n".join(parts)


# ---------------------------------------------------------------- looking the story up

def wikipedia(http: HTTP, query: str, n: int = 2) -> list[dict]:
    """Wikipedia pages about the story (their links can go on a card)."""
    r = http.get("https://en.wikipedia.org/w/api.php", params={"action": "query", "list": "search", "srsearch": query,
                                                             "srlimit": n, "format": "json"}, timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for hit in ((r.json() or {}).get("query") or {}).get("search", [])[:n]:
        title = hit.get("title", "")
        e = http.get("https://en.wikipedia.org/w/api.php", params={"action": "query", "prop": "extracts", "exintro": 1,
                                                                 "explaintext": 1, "redirects": 1, "titles": title,
                                                                 "format": "json"}, timeout=15)
        pages = ((e.json() or {}).get("query") or {}).get("pages", {}) if e.status_code == 200 else {}
        text = next((p.get("extract", "") for p in pages.values()), "")
        if text:
            out.append({"title": f"Wikipedia: {title}", "url": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                        "text": clean(text, 1500), "linkable": True})
    return out


def bing(http: HTTP, query: str, news: bool, n: int = 5) -> list[dict]:
    url = "https://www.bing.com/news/search" if news else "https://www.bing.com/search"
    r = http.get(url, params={"q": query, "format": "rss"}, timeout=15)
    if r.status_code != 200:
        return []
    out = []
    for e in feedparser.parse(r.content).entries[:n]:
        link = bing_link(e.get("link", "")) if news else e.get("link", "")
        if link.startswith(("http://", "https://")):
            # News articles can be a card's fact-check link; plain web results are only read, never linked
            out.append({"title": clean(e.get("title", ""), 200), "url": link, "text": clean(e.get("summary", ""), 400),
                        "linkable": news})
    return out


def evidence(http: HTTP, query: str, read_articles: int = 2, deadline: float | None = None) -> list[dict]:
    """What the web says about the story: Wikipedia first, then news and web results, plus the full text of the
    best couple of articles (skipped when the run is short on time)."""
    found: list[dict] = []
    for fn in (lambda: wikipedia(http, query), lambda: bing(http, query, True), lambda: bing(http, query, False)):
        try:
            for item in fn():
                if item["url"] not in {f["url"] for f in found}:
                    found.append(item)
        except Exception:  # noqa: BLE001  (one source failing is fine)
            continue
    read = 0
    for item in found:
        if read >= read_articles or (deadline is not None and time.time() > deadline - 120):
            break
        if "wikipedia.org" in item["url"] or any(s in item["url"] for s in ("youtube.com", "tiktok.com", "instagram.com")):
            continue
        text = article_text(http, item["url"], limit=2500)
        if text:
            item["text"] = text
            read += 1
    return found[:10]


def evidence_text(items: list[dict]) -> str:
    if not items:
        return "EVIDENCE: nothing found online."
    lines = ["EVIDENCE (what the web says):"]
    for i, e in enumerate(items, 1):
        mark = " (linkable)" if e.get("linkable") else ""
        lines.append(f"[{i}] {e['title']}\n    LINK{mark}: {e['url']}\n    {e.get('text', '')[:2500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the two AI steps

def identify(llm: LLM, c: Candidate, comments: list[str]) -> dict | None:
    # a simple extraction job: the cheap high-volume AI does it, the careful one is saved for the pick
    reply = llm.complete("screen", IDENTIFY, video_text(c, comments), max_tokens=600)
    try:
        d = parse_json(reply)
    except ValueError:
        return None

    def as_bool(v):
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")

    return {
        "real_story": as_bool(d.get("real_story")),
        "kid_at_center": as_bool(d.get("kid_at_center")),
        "what_happened": clean(str(d.get("what_happened") or ""), 800),
        "names_places": clean(str(d.get("names_places") or ""), 300),
        "search": clean(str(d.get("search") or ""), 120),
        "staged_risk": str(d.get("staged_risk") or "medium").strip().lower(),
    }


def pick(llm: LLM, c: Candidate, system: str, found: dict, comments: list[str], ev: list[dict],
         already: list[str] | None = None) -> dict | None:
    recent = [a for a in (already or []) if a][-40:]
    user = "\n\n".join(x for x in [
        "THE VIRAL VIDEO:\n" + video_text(c, comments),
        f"WHAT THE VIDEO SHOWS (first look): {found['what_happened']}"
        + (f" (names/places: {found['names_places']})" if found["names_places"] else ""),
        evidence_text(ev),
        ("ALREADY PICKED STORIES (if this is the same event, fill in same_as):\n" + "\n".join(f"- {a}" for a in recent))
        if recent else "",
        (f"NOTE: this may already have been used: {c.possibly_pitched}" if c.possibly_pitched else ""),
        PICK,
    ] if x)
    reply = llm.complete("deep", system, user, max_tokens=1500)
    try:
        d = parse_json(reply)
    except ValueError:
        return None
    result = normalize(d)
    urls = {e["url"] for e in ev if e.get("linkable")}
    source = str(d.get("source_url") or "").strip()
    verified = str(d.get("verified") or "no").strip().lower()
    result.update({
        "most_like": clean(str(d.get("most_like") or ""), 200),
        "verified": verified if verified in ("yes", "partly", "no") else "no",
        "source_url": source if source in urls else "",  # only Wikipedia/news links the factory found itself
        "same_as": clean(str(d.get("same_as") or ""), 300) if recent else "",
        "proof": c.signal,
        "platform": (c.extra or {}).get("platform", ""),
    })
    if not result["source_url"] and result["verified"] == "yes":
        result["verified"] = "partly"
    return result


def passes_pick(result: dict, threshold: int) -> bool:
    return (
        result["score"] >= threshold
        and result["kid_at_center"]
        and result["verified"] in ("yes", "partly")
        and not result["child_hurt"]
        and result["staged_risk"] != "high"
        and bool(result["story"])
    )

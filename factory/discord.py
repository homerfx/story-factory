"""Discord side: the bot posts story cards (into one channel, or one channel per lane), adds 👍 👎 ⭐ under each
card, and reads the reactions back (its own starter reactions are never counted)."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

import requests

API = "https://discord.com/api/v10"
UP, DOWN, STAR = "👍", "👎", "⭐"
DISCORD_EPOCH_MS = 1420070400000


class DiscordError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status  # HTTP status, 0 for network problems


class DiscordAPI:
    """Talks to Discord's REST API with a bot token. No always-on connection needed."""

    def __init__(self, token: str, log) -> None:
        self.log = log
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/story-factory, 1.0)",
            }
        )

    def request(self, method: str, path: str, payload=None, params=None):
        for attempt in range(5):
            try:
                r = self.session.request(method, API + path, json=payload, params=params, timeout=30)
            except requests.RequestException as ex:
                if attempt == 4:
                    raise DiscordError(f"{method} {path}: network problem {ex}", 0) from None
                time.sleep(5)
                continue
            if r.status_code >= 500 and attempt < 4:
                time.sleep(5)
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait, 60) + 0.25)
                continue
            if r.status_code == 204:
                return None
            if r.status_code >= 400:
                raise DiscordError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
            return r.json()
        raise DiscordError(f"{method} {path}: rate limited too many times", 429)

    def guild_channels(self, guild_id: str) -> list[dict]:
        return self.request("GET", f"/guilds/{guild_id}/channels") or []

    def create_channel(self, guild_id: str, name: str, kind: int, parent_id: str | None = None, topic: str = ""):
        body = {"name": name, "type": kind}
        if parent_id:
            body["parent_id"] = parent_id
        if topic:
            body["topic"] = topic[:1024]
        return self.request("POST", f"/guilds/{guild_id}/channels", body)

    def send(self, channel_id: str, payload: dict) -> str:
        # Text from the internet can never ping anyone (@everyone, @here, roles or people).
        payload = {**payload, "allowed_mentions": {"parse": []}}
        return self.request("POST", f"/channels/{channel_id}/messages", payload)["id"]

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.request("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me")
        time.sleep(0.3)  # reactions have a tight rate limit

    def messages_since(self, channel_id: str, since_ts: float, max_pages: int = 5) -> list[dict]:
        after = str((int(since_ts * 1000) - DISCORD_EPOCH_MS) << 22)
        out: list[dict] = []
        for _ in range(max_pages):
            page = self.request("GET", f"/channels/{channel_id}/messages", params={"after": after, "limit": 100}) or []
            if not page:
                break
            out.extend(page)
            after = max(page, key=lambda m: int(m["id"]))["id"]
            if len(page) < 100:
                break
        return out


def _norm(name: str) -> str:
    return re.sub(r"\s+", "-", name.strip().lower())


def channel_plan(cfg: dict) -> dict[str, tuple[str, str]]:
    """key -> (channel name, topic)."""
    d = cfg.get("discord", {})
    if d.get("one_channel"):  # everything (every story + the daily report) in one channel
        bot = d.get("bot_name", "Tyler")
        return {"stories": (d["one_channel"], f"Every story idea {bot} finds. 🔥 = top pick (9-10/10). "
                                              "React 👍 good · 👎 bad · ⭐ I'm using this one.")}
    plan = {"top": (d.get("top_channel", "🔥│top-picks"), "The best stories (9-10/10) from every lane.")}
    for lane, spec in (cfg.get("lanes") or {}).items():
        plan[lane] = (spec.get("channel", lane), " ".join(str(spec.get("describe", "")).split())[:1000])
    plan["other"] = (d.get("other_channel", "🗂│other-good-stories"), "Good stories that fit no lane.")
    plan["picked"] = (d.get("picked_channel", "⭐│picked"), "Stories someone starred ⭐. Script these.")
    plan["report"] = (d.get("report_channel", "📊│daily-report"), "What the factory did in the last 24 hours.")
    return plan


def setup_channels(api: DiscordAPI, guild_id: str, cfg: dict, log) -> dict[str, str]:
    """Create the category (multi-channel layout only) and every channel that doesn't exist yet.
    A channel that already exists with the same name is reused. Returns key -> channel id."""
    existing = api.guild_channels(guild_id)
    d = cfg.get("discord", {})
    cat_name = "" if d.get("one_channel") else d.get("category", "STORY FACTORY")
    category = None
    if cat_name:
        category = next((c for c in existing if c.get("type") == 4 and c.get("name", "").lower() == cat_name.lower()),
                        None)
        if not category:
            category = api.create_channel(guild_id, cat_name, 4)
            log(f"  created category {cat_name}")
    by_name = {_norm(c.get("name", "")): c for c in existing if c.get("type") == 0}
    mapping = {}
    for key, (name, topic) in channel_plan(cfg).items():
        ch = by_name.get(_norm(name))
        if not ch:
            ch = api.create_channel(guild_id, name, 0, category["id"] if category else None, topic)
            log(f"  created #{name}")
        mapping[key] = ch["id"]
    return mapping


def lane_label(cfg: dict, lane: str) -> str:
    """The lane as shown on a card, e.g. '🦸│kid-heroes'."""
    return ((cfg.get("lanes") or {}).get(lane) or {}).get("channel") or lane


def lane_from_label(cfg: dict, label: str) -> str:
    for lane, spec in (cfg.get("lanes") or {}).items():
        if label in (lane, spec.get("channel")):
            return lane
    return "other"


# ---------------------------------------------------------------- cards

LANE_COLORS = [0xE74C3C, 0x3498DB, 0x9B59B6, 0xE67E22, 0x1ABC9C, 0xF1C40F, 0x2ECC71, 0xE91E63]


_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_URL = re.compile(r"(https?://|www\.)\S+|discord\.gg/\S+", re.I)


def _plain(text):
    """Text written by the AI or taken from the web can't carry its own links: the only link on a card is the
    story's source. (Stops a crafted article from slipping a disguised phishing link onto a card.)"""
    if not isinstance(text, str):
        return text
    text = _MD_LINK.sub(r"\1", text)
    return _URL.sub("[link removed]", text).replace("](", "] (")


def _cut(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


_SAFE_LINK = re.compile(r"^https?://[^\s<>()\[\]]+$")
PLATFORMS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


def build_card(item: dict, cfg: dict, note: str = "") -> dict:
    # The fact-check link was picked by the factory from its own search results (never written by the AI), so it is
    # the one extra link allowed on a card, and only if it's a plain web address.
    fact_link = str((item.get("data") or {}).get("source_url") or "")
    fact_link = fact_link if _SAFE_LINK.match(fact_link) else ""
    if item.get("url") and not _SAFE_LINK.match(str(item["url"])):
        item = {**item, "url": ""}
    r = {k: _plain(v) for k, v in (item.get("data") or {}).items()}
    item = {**item, **{k: _plain(item.get(k)) for k in ("title", "signal", "possibly_pitched", "source")}}
    lanes = list((cfg.get("lanes") or {}).keys())
    lane = item.get("lane") or r.get("lane") or "other"
    color = 0xFFD700 if r.get("score", 0) >= cfg.get("top_threshold", 9) else (
        LANE_COLORS[lanes.index(lane) % len(LANE_COLORS)] if lane in lanes else 0x95A5A6)
    flags = []
    if item.get("possibly_pitched"):
        flags.append(f"Maybe already pitched: {item['possibly_pitched']}")
    if r.get("is_big_news"):
        flags.append("Big news story")
    if r.get("red_flags"):
        flags.append(r["red_flags"])
    lines = [
        ("Payoff", r.get("payoff")),
        ("Hook idea", r.get("hook_idea")),
        ("Why they'll comment", r.get("comment_bait")),
        ("Video" if r.get("platform") else "Link", item.get("url")),
        ("Fact check", fact_link),
    ]
    description = "\n".join(f"**{k}:** {v}" for k, v in lines if v)
    if r.get("score", 0) >= cfg.get("top_threshold", 9):
        note = "🔥 **TOP PICK**" + (f"\n{note}" if note else "")
    if note:
        description = f"{note}\n\n{description}"
    fields = [
        {"name": "Score", "value": f"{r.get('score', '?')}/10", "inline": True},
        {"name": "Lane", "value": lane_label(cfg, lane), "inline": True},
        {"name": "Emotion", "value": _cut(r.get("emotion") or "-", 1024), "inline": True},
        {"name": "Type", "value": (f"{PLATFORMS.get(r['platform'], r['platform'])} · "
                                   + ("real footage" if r.get("is_viral_clip") else "retold"))
         if r.get("platform") else ("Viral clip" if r.get("is_viral_clip") else "News story"), "inline": True},
        {"name": "Staged risk", "value": r.get("staged_risk") or "-", "inline": True},
        {"name": "Found", "value": "Archive (older story)" if item.get("archive") else "Fresh", "inline": True},
    ]
    if r.get("country"):
        fields.append({"name": "Country", "value": _cut(r["country"], 100), "inline": True})
    if item.get("signal"):
        fields.append({"name": "Proof it went viral" if r.get("platform") else "Popularity",
                       "value": _cut(item["signal"], 1024), "inline": True})
    if r.get("verified"):
        fields.append({"name": "Facts checked", "value": {"yes": "✅ confirmed", "partly": "🟡 partly confirmed"}.get(
            r["verified"], "❌ not confirmed"), "inline": True})
    if r.get("most_like"):
        fields.append({"name": "Most like your video", "value": _cut(r["most_like"], 1024), "inline": False})
    if flags:
        fields.append({"name": "⚠️ Check", "value": _cut(" · ".join(flags), 1024), "inline": False})
    return {
        "embeds": [
            {
                "title": _cut(r.get("story") or item.get("title", ""), 256),
                "url": item.get("url") or None,
                "color": color,
                "description": _cut(description, 4000),
                "fields": fields,
                "footer": {"text": _cut(f"{item.get('source', '')} · headline: {item.get('title', '')}", 2000)},
            }
        ]
    }


def count_reactions(message: dict) -> tuple[int, int, int]:
    """Human reactions only (the bot's own starter reactions are subtracted)."""
    up = down = stars = 0
    for r in message.get("reactions", []) or []:
        name = (r.get("emoji") or {}).get("name")
        n = int(r.get("count", 0)) - (1 if r.get("me") else 0)
        if name == UP:
            up = n
        elif name == DOWN:
            down = n
        elif name == STAR:
            stars = n
    return max(up, 0), max(down, 0), max(stars, 0)


def card_info(message: dict) -> dict | None:
    """Read a story card back from Discord (used to rebuild the memory if it was ever lost)."""
    embeds = message.get("embeds") or []
    if not embeds or not embeds[0].get("url"):
        return None
    e = embeds[0]
    footer = (e.get("footer") or {}).get("text", "")
    headline = footer.split("headline: ", 1)[1] if "headline: " in footer else e.get("title", "")
    lane = next((f.get("value", "") for f in e.get("fields") or [] if f.get("name") == "Lane"), "")
    return {"url": e["url"], "title": headline.rstrip("…"), "story": e.get("title", ""), "lane": lane}


def print_card(payload: dict, channel: str, log) -> None:
    log(f"  [dry run] #{channel}:\n" + json.dumps(payload, indent=2, ensure_ascii=False)[:2500])

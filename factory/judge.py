"""Factory 2: the AI checker. Step 1 quickly screens many headlines; step 2 deeply checks the best ones."""
from __future__ import annotations

import time

from .llm import LLM, NoAIAvailable, parse_json
from .models import Candidate

SCREEN_SYSTEM = """You pick story ideas for a YouTube Shorts channel that retells REAL stories as 25-second
Roblox animations for kids aged 5-16.

What the channel's own data shows: 84% of its viral hits have a KID or TEEN as the main character (only 23% of
its flops do). The best hooks are a contradiction: an ordinary kid thing with an extreme result (arrested for
drawing on a desk; in trouble for winning a race; saved the school by being late; solved the hardest math
problem by accident). Also strong: someone wrongs or misjudges a kid, kids saving people, bizarre kid body
mysteries, kids surviving alone, evil adults exposed, real viral clips of bullies/Karens getting karma.
Flops: adult-only survival or trivia, adult heroes saving kids, quirky adult news.

Low scores: adult-only stories with no twist, staged skits/pranks/AI videos, memes, politics, anything sexual,
stories that only work by describing an adult beating or hurting a child (accident/survival/medical injuries
are fine), routine crime/court/sports news.

Stories can come from ANY country and the headline can be in ANY language: judge the story itself, never the
language (a great story from Brazil or Japan scores as high as one from the US).

Score every item 0-10 from the headline and snippet only. Be strict: most items are 0-5.
Reply with ONLY JSON: {"results": [{"n": <item number>, "score": <0-10>, "why": "<max 10 words>"}]}"""

DEEP_INSTRUCTIONS = """Check this possible story against the rules and the taste examples above. Use only the
facts given; do not invent details. If key facts are missing, say so in red_flags and lower the score.
The source may be in any language: always write every text field in simple American English.

Reply with ONLY JSON:
{
  "score": <0-10, using the score guide>,
  "lane": "<one lane id from the LANES list, or "other">",
  "story": "<the story in ONE plain sentence a 7-year-old understands>",
  "country": "<the country where it happened, in English; empty if unknown>",
  "payoff": "<the ending/payoff in one sentence>",
  "hook_idea": "<a first line for the video: starts the story as a contradiction or shock, no question marks>",
  "emotion": "<main emotion(s) it triggers>",
  "comment_bait": "<what viewers would argue or comment about>",
  "kid_at_center": <true/false>,
  "is_viral_clip": <true if the source is a viral video/clip, false if a news story>,
  "is_big_news": <true if it looks like a big mainstream news story>,
  "staged_risk": "<low/medium/high>",
  "child_hurt": <true ONLY if the story depends on describing an adult beating or hurting a child; injuries from accidents, crashes, survival or illness do NOT count>,
  "red_flags": "<anything doubtful, missing or risky; empty if none>"
}"""


def lanes_text(cfg: dict) -> str:
    lines = ["LANES (pick the best fit):"]
    for lane, spec in (cfg.get("lanes") or {}).items():
        lines.append(f"- {lane}: {' '.join(str(spec.get('describe', '')).split())}")
    return "\n".join(lines)


def taste_text(liked: list[dict], disliked: list[dict]) -> str:
    if not liked and not disliked:
        return ""

    def line(item):
        story = (item.get("data") or {}).get("story") or ""
        return f"- {item['title']}" + (f" ({story})" if story else "")

    parts = ["TASTE EXAMPLES from the channel owner's 👍/👎/⭐ in Discord (match his taste, not a generic one):"]
    if liked:
        parts.append("He LIKED:")
        parts += [line(i) for i in liked]
    if disliked:
        parts.append("He DISLIKED:")
        parts += [line(i) for i in disliked]
    return "\n".join(parts)


def screen(llm: LLM, cands: list[Candidate], batch_size: int, log, deadline: float | None = None) -> None:
    """Sets c.screen_score on each candidate it managed to score (others stay None)."""
    for start in range(0, len(cands), batch_size):
        if deadline is not None and time.time() > deadline:
            log(f"  quick screen stopped for time after {start} headlines; the rest wait for the next run")
            return
        batch = cands[start : start + batch_size]
        lines = []
        for i, c in enumerate(batch, 1):
            extra = f" ({c.signal})" if c.signal else ""
            snippet = f"\n    {c.summary[:200]}" if c.summary else ""
            lines.append(f"[{i}] {c.title}\n    source: {c.source}{extra}{snippet}")
        try:
            reply = llm.complete("screen", SCREEN_SYSTEM, "\n".join(lines), max_tokens=40 * len(batch) + 100)
            for row in parse_json(reply).get("results", []):
                n = int(row.get("n", 0))
                if 1 <= n <= len(batch):
                    batch[n - 1].screen_score = max(0, min(10, int(float(row.get("score", 0)))))
                    batch[n - 1].screen_why = str(row.get("why", ""))[:120]
        except NoAIAvailable:
            raise
        except Exception as ex:  # noqa: BLE001
            log(f"  screen batch failed: {str(ex)[:150]}")


def deep(llm: LLM, c: Candidate, system: str, article: str) -> dict | None:
    parts = [
        f"HEADLINE: {c.title}",
        f"SOURCE: {c.source}" + (f" ({c.signal})" if c.signal else ""),
        f"LINK: {c.url}",
    ]
    if c.archive:
        parts.append("NOTE: this is an older story found by the archive digger (that's fine).")
    if c.summary:
        parts.append(f"SNIPPET: {c.summary[:600]}")
    if article:
        parts.append(f"ARTICLE / POST TEXT:\n{article}")
    if c.possibly_pitched:
        parts.append(f"NOTE: this may already have been used by the channel: {c.possibly_pitched}")
    parts.append(DEEP_INSTRUCTIONS)
    reply = llm.complete("deep", system, "\n\n".join(parts), max_tokens=1500)
    try:
        return normalize(parse_json(reply))
    except ValueError:
        return None


def normalize(d: dict) -> dict:
    def as_bool(v):
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")

    def as_text(v, n=600):
        return str(v or "").strip()[:n]

    try:
        score = max(0, min(10, int(float(d.get("score", 0)))))
    except (TypeError, ValueError):
        score = 0
    staged = as_text(d.get("staged_risk"), 10).lower()
    return {
        "score": score,
        "lane": as_text(d.get("lane"), 40).strip().lower(),
        "story": as_text(d.get("story")),
        "country": as_text(d.get("country"), 60),
        "payoff": as_text(d.get("payoff")),
        "hook_idea": as_text(d.get("hook_idea")),
        "emotion": as_text(d.get("emotion"), 100),
        "comment_bait": as_text(d.get("comment_bait"), 300),
        "kid_at_center": as_bool(d.get("kid_at_center")),
        "is_viral_clip": as_bool(d.get("is_viral_clip")),
        "is_big_news": as_bool(d.get("is_big_news")),
        "staged_risk": staged if staged in ("low", "medium", "high") else "medium",
        "child_hurt": as_bool(d.get("child_hurt")),
        "red_flags": as_text(d.get("red_flags"), 400),
    }


def passes(result: dict, threshold: int) -> bool:
    return (
        result["score"] >= threshold
        and not result["child_hurt"]
        and result["staged_risk"] != "high"
        and bool(result["story"])
    )

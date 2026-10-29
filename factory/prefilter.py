"""Free keyword filter that runs before any AI, plus the 'already pitched' check."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .models import Candidate, title_tokens
from .world import BLOCK_INTL, intl_counts

KID = re.compile(
    r"\b(boy|boys|girl|girls|kid|kids|child|children|son|sons|daughter|daughters|student|students|pupil|pupils|"
    r"teen|teens|teenager|teenagers|toddler|toddlers|baby|babies|schoolboy|schoolgirl|preteen|tween|classmate|"
    r"classmates|grandson|granddaughter|nephew|niece|twins?|little brother|little sister|"
    r"\d{1,2}[- ]year[- ]olds?|\d{1,2}yo|\d{1,2}[- ]month[- ]old)\b",
    re.I,
)
STORY = re.compile(
    r"\b(viral|tiktok|video|footage|camera|caught|bully|bullied|bullies|bullying|teacher|principal|coach|school|"
    r"class|karma|hero|heroic|saved|saves|rescued|rescues|rescue|surprised?|surprises|prank\w*|police|cops?|"
    r"officer|arrested|mom|mum|dad|mother|father|parents?|grandma|grandpa|grandmother|grandfather|stranger|"
    r"revenge|lesson|exposed|refused|kicked|banned|fired|humiliated|mocked|laughed|lied|lies|stole|stolen|"
    r"thief|karen|neighbou?r|birthday|lunch|homework|detention|apologi\w+|cried|crying|tears|secret|"
    r"missing|found|trapped|survived|survives|doctors?|hospital|swallowed|bizarre|mystery|weird|record|genius|"
    r"invented|millionaire|adopted|adoption|abandoned|escaped|confronts?|confronted|outsmart\w*|suspended|"
    r"expelled|handcuffed|punished|accused|blamed|condition|syndrome|baffled|alive|stranded|alone|solved|"
    r"discovered|accidentally|accident|credit|believed|misjudged|mistaken|praised|honou?red|award\w*)\b",
    re.I,
)
# Contradiction hooks are the channel's strongest pattern, so these keep a story even with no kid word.
PARADOX = re.compile(
    r"(nobody believed|no one believed|turned out|by accident|accidentally|thought (he|she|they) (was|were)|"
    r"mistaken for|got in trouble for|arrested for|suspended for|for (doing|being) (good|kind|honest)|"
    r"everyone laughed|took the credit|for decades|for \d+ years)",
    re.I,
)
BLOCK = re.compile(
    r"\b(sex|sexual|sexually|porn\w*|rape\w*|molest\w*|grooming|groomed|nude|naked|explicit|onlyfans|incest|"
    r"pedophil\w*|paedophil\w*)\b",
    re.I,
)
MEME = re.compile(r"\b(meme|memes|pov|me when|be like|shitpost|starter pack|irl)\b", re.I)


def prefilter(c: Candidate) -> tuple[bool, int, str]:
    """Return (keep, keyword score, reason if dropped)."""
    text = f"{c.title} {c.summary[:300]}"
    if BLOCK.search(text) or BLOCK_INTL.search(text):
        return False, 0, "blocked_words"
    if MEME.search(c.title):
        return False, 0, "meme"
    kid = len(KID.findall(text))
    story = len(STORY.findall(text))
    paradox = len(PARADOX.findall(text))
    # Big viral posts often say "my dad…" with no kid word, so popularity earns them an AI look anyway.
    viral = c.signal_value >= 10_000
    # Stories from other countries: kid + story words in their own language. GDELT finds non-English stories by
    # searching their English translation, so its finds already contain a kid and a story.
    kid_w, story_w = intl_counts(text)
    translated = bool(c.extra.get("translated_match"))
    if kid == 0 and paradox == 0 and not viral:
        if kid_w and (story_w or story or translated):
            return True, 2 * min(kid_w, 3) + 2 * min(story_w + story, 3), ""
        if translated:
            return True, 3, ""
        return False, 0, "no_kid"
    if story == 0 and paradox == 0 and story_w == 0 and not translated:
        return False, 0, "no_story_words"
    return True, 2 * min(kid, 3) + min(story + story_w, 6) + 3 * min(paradox, 2), ""


class Exclusion:
    """Fuzzy match against the list of stories already pitched (data/exclude.txt + data/posted.txt)."""

    def __init__(self, paths: list[str]) -> None:
        self.segments: list[tuple[str, set[str]]] = []
        for path in paths:
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                continue
            for line in lines:
                if not line.strip() or line.startswith("#"):
                    continue
                for seg in re.split(r";|\|", line):
                    toks = title_tokens(seg)
                    if len(toks) >= 2:
                        self.segments.append((seg.strip()[:140], toks))
        df: Counter = Counter()
        for _, toks in self.segments:
            df.update(toks)
        # Words that appear everywhere ("school", "teacher", "boy") say nothing about a specific story.
        limit = max(25, int(len(self.segments) * 0.004))
        self.common = {w for w, n in df.items() if n > limit}
        self.index: dict[str, list[int]] = defaultdict(list)
        for i, (_, toks) in enumerate(self.segments):
            for w in toks - self.common:
                self.index[w].append(i)

    def check(self, text: str) -> tuple[float, str]:
        """Return (match strength 0-1, the matching pitched line)."""
        toks = title_tokens(text) - self.common
        if len(toks) < 2:
            return 0.0, ""
        hits: Counter = Counter()
        for w in toks:
            for i in self.index.get(w, ()):
                hits[i] += 1
        best_ratio, best_seg = 0.0, ""
        for i, n in hits.items():
            if n < 3:
                continue
            seg_text, seg_toks = self.segments[i]
            ratio = n / min(len(toks), len(seg_toks - self.common) or 1)
            if ratio > best_ratio:
                best_ratio, best_seg = ratio, seg_text
        return min(best_ratio, 1.0), best_seg

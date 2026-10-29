from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

STOP_WORDS = set("""
a an the and or but if then than so of to in on at by for from with without into onto over under about after before
is are was were be been being am has have had do does did not no yes this that these those it its he she his her him
they them their we our you your i me my who whom whose what which when where why how all any each every some more most
just very too also only even still up down out off again new old now here there s t can will would could should may
might must shall one two three via amp says said say gets got get goes went make made makes after week day days
""".split())


@dataclass
class Candidate:
    """One possible story found by a collector."""

    title: str
    url: str
    source: str  # e.g. "Reddit r/MadeMeSmile" or "Google News · Daily Dot"
    summary: str = ""
    published: float | None = None  # unix time, if known
    signal: str = ""  # popularity hint, e.g. "45.2K upvotes" or "3.1M views"
    signal_value: int = 0  # the same as a number, for sorting
    kw_score: int = 0
    possibly_pitched: str = ""
    screen_score: int | None = None
    screen_why: str = ""
    lane_hint: str = ""  # which lane's search found it
    archive: bool = False  # found by the archive digger (an older story)
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        key = normalize_url(self.url) or norm_title(self.title)
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"^https?://(www\.|old\.|m\.)?", "", url, flags=re.I)
    url = re.sub(r"[?#].*$", "", url) if "youtube.com/watch" not in url and "news.google.com" not in url else url
    return url.rstrip("/").lower()


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (title or "").lower()).strip()


def title_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in STOP_WORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class TitleIndex:
    """Fast near-duplicate check for headlines (same story reported by different sites)."""

    def __init__(self, titles=(), threshold: float = 0.6) -> None:
        self.threshold = threshold
        self.sets: list[set[str]] = []
        self.index: dict[str, list[int]] = {}
        for t in titles:
            self.add(t)

    def add(self, title: str) -> None:
        toks = title_tokens(title)
        if len(toks) < 3:
            return
        i = len(self.sets)
        self.sets.append(toks)
        for w in toks:
            self.index.setdefault(w, []).append(i)

    def is_duplicate(self, title: str) -> bool:
        toks = title_tokens(title)
        if len(toks) < 3:
            return False
        shared: dict[int, int] = {}
        for w in toks:
            for i in self.index.get(w, ()):
                shared[i] = shared.get(i, 0) + 1
        need = max(2, int(self.threshold * len(toks) / 2))
        return any(n >= need and jaccard(toks, self.sets[i]) >= self.threshold for i, n in shared.items())


def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

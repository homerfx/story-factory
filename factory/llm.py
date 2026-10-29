"""AI access with automatic fallback: when one free provider hits its daily limit, the next one is used."""
from __future__ import annotations

import json
import os
import re
import time

import requests


class RateLimited(Exception):
    """Out of free uses for now: skip this provider for the rest of the run."""


class ProviderBroken(Exception):
    """Bad key, unknown model or no access: skip this provider for the rest of the run."""


class TryNext(Exception):
    """A one-off failure (timeout, server error, empty reply): use the next provider for this call only."""


class NoAIAvailable(Exception):
    pass


class LLM:
    def __init__(self, cfg: dict, log) -> None:
        ai = cfg.get("ai", {})
        self.order: list[str] = ai.get("providers", [])
        # optional separate orders, e.g. the high-volume quick screen on the providers with the biggest free limits
        self.stage_order: dict[str, list[str]] = {
            stage: ai.get(f"{stage}_providers") or self.order for stage in ("screen", "deep")
        }
        self.providers: dict = cfg.get("providers", {})
        self.log = log
        self.dead: dict[str, str] = {}  # switched off for the rest of this run ("groq" or "groq screen")
        self.strikes: dict[str, int] = {}
        self.used: dict[str, int] = {}

    def configured(self) -> list[str]:
        names = []
        for name in dict.fromkeys(self.order + self.stage_order["screen"] + self.stage_order["deep"]):
            p = self.providers.get(name) or {}
            key_env = p.get("key_env")
            if p.get("type") in ("mock", "ollama") or (key_env and os.environ.get(key_env)) or not key_env:
                names.append(name)
        return names

    def complete(self, stage: str, system: str, user: str, max_tokens: int = 1500) -> str:
        """stage is 'screen' (cheap, many items) or 'deep' (one item, best model)."""
        for name in self.stage_order.get(stage, self.order):
            p = self.providers.get(name)
            # free limits are per model, so running out on the screen model doesn't stop the deep model
            if not p or name in self.dead or f"{name} {stage}" in self.dead:
                continue
            key_env = p.get("key_env")
            key = os.environ.get(key_env, "") if key_env else ""
            if key_env and not key:
                continue
            model = p.get(f"{stage}_model") or p.get("deep_model")
            try:
                if p.get("type") == "mock":
                    text = mock_complete(stage, user)
                elif p.get("type") == "anthropic":
                    text = self._anthropic(key, model, system, user, max_tokens)
                else:
                    text = self._openai_compatible(p, key, model, system, user, max_tokens)
                if not (text or "").strip():
                    raise TryNext("empty reply")
                self.used[name] = self.used.get(name, 0) + 1
                return text
            except RateLimited as ex:
                self.dead[f"{name} {stage}"] = f"limit reached: {str(ex)[:120]}"
                self.log(f"  AI {name} ({stage}): free limit reached, switching to the next provider")
            except ProviderBroken as ex:
                self.dead[name] = str(ex)[:200]
                self.log(f"  AI {name}: {str(ex)[:200]}")
            except Exception as ex:  # noqa: BLE001  (TryNext, timeouts, odd replies)
                self.strikes[name] = self.strikes.get(name, 0) + 1
                self.log(f"  AI {name}: {str(ex)[:150]} (trying the next one for this item)")
                if self.strikes[name] >= 4:
                    self.dead[name] = f"kept failing: {str(ex)[:150]}"
        raise NoAIAvailable("no AI provider available right now (missing keys or all at their limits)")

    # ---------------------------------------------------------------- providers
    def _post(self, url: str, headers: dict, body: dict) -> dict:
        waited_blind = False
        for attempt in range(3):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=90)
            except requests.RequestException as ex:
                if attempt == 2:
                    raise TryNext(f"network: {ex}") from None
                time.sleep(3)
                continue
            if r.status_code == 429:
                wait = _retry_after(r)
                if wait is not None and wait <= 30 and attempt < 2:
                    time.sleep(wait + 1)  # per-minute limit: wait and retry
                    continue
                if wait is None and not waited_blind:
                    waited_blind = True  # some providers don't say how long; give it one 15-second pause
                    time.sleep(15)
                    continue
                raise RateLimited(r.text[:150])
            if r.status_code in (401, 403, 404):
                raise ProviderBroken(f"HTTP {r.status_code} (check the key / model name): {r.text[:150]}")
            if r.status_code >= 500 and attempt < 2:
                time.sleep(3)
                continue
            if r.status_code >= 400:
                raise TryNext(f"HTTP {r.status_code}: {r.text[:150]}")
            try:
                return r.json()
            except ValueError:
                raise TryNext("reply was not JSON") from None
        raise TryNext("request failed")

    def _openai_compatible(self, p, key, model, system, user, max_tokens) -> str:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        body.update(p.get("extra_body") or {})  # e.g. Gemini: switch off "thinking" so it doesn't eat the reply
        data = self._post(p["base_url"].rstrip("/") + "/chat/completions", headers, body)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise TryNext(f"unexpected reply: {str(data)[:120]}") from None

    def _anthropic(self, key, model, system, user, max_tokens) -> str:
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": 0.2,
                # The rules are the same every call, so they are cached (cheaper after the first call).
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": user}],
            },
        )
        return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


def _retry_after(r) -> float | None:
    value = r.headers.get("retry-after") or r.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_json(text: str):
    """Pull the first JSON object out of a model reply (models sometimes add text or ``` fences)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON in reply")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- offline test double

def mock_complete(stage: str, user: str) -> str:
    """Deterministic fake AI used by the offline test (provider type 'mock')."""
    if stage == "screen":
        results = []
        for m in re.finditer(r"^\[(\d+)\] (.+)$", user, flags=re.M):
            title = m.group(2).lower()
            score = 8 if any(w in title for w in ("dad", "teacher", "mom", "karma")) else 3
            results.append({"n": int(m.group(1)), "score": score, "why": "mock"})
        return json.dumps({"results": results})
    title = re.search(r"HEADLINE: (.+)", user)
    title = title.group(1) if title else "story"
    low = title.lower()
    score = 9 if "karma" in low else 8 if ("dad" in low or "teacher" in low) else 6
    lane = "bully-karma" if "karma" in low or "bully" in low else "unfair-adults" if "teacher" in low else "other"
    return json.dumps(
        {
            "score": score,
            "lane": lane,
            "story": f"Mock story: {title}",
            "payoff": "The bully ends up in trouble instead.",
            "hook_idea": "Every day, a bigger kid stole this boy's lunch.",
            "emotion": "anger then satisfaction",
            "comment_bait": "W dad",
            "kid_at_center": True,
            "is_viral_clip": True,
            "is_big_news": False,
            "staged_risk": "low",
            "child_hurt": False,
            "red_flags": "",
        }
    )

"""Optional LLM helper. It may explain and answer questions about engine output; it never decides a flag, quantity or priority.
Provider order: OpenRouter (Google models only, served by Google's own infrastructure) if OPENROUTER_API_KEY is set,
else Gemini direct if GEMINI_API_KEY is set, else Claude if ANTHROPIC_API_KEY is set. Keys come from the environment only."""
from __future__ import annotations

import json
import re
import urllib.request

from . import db

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.7-flash"
GOOGLE_ONLY = {"only": ["google-vertex", "google-ai-studio"], "allow_fallbacks": False}
_model_cache: dict = {}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_NUM = re.compile(r"\d+(?:\.\d+)?")


def provider() -> str | None:
    if db.get_secret("OPENROUTER_API_KEY"):
        return "openrouter"
    if db.get_secret("GEMINI_API_KEY"):
        return "gemini"
    if db.get_secret("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def openrouter_model() -> str:
    """Configured Google model, checked once against OpenRouter's list; swapped for the closest Google Flash model if it no longer exists."""
    want = db.get_secret("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    if not want.startswith("google/"):
        want = DEFAULT_OPENROUTER_MODEL  # rule: Google models only
    if want in _model_cache:
        return _model_cache[want]
    chosen = want
    try:
        req = urllib.request.Request(OPENROUTER_URL + "/models", headers={"Authorization": "Bearer " + db.get_secret("OPENROUTER_API_KEY")})
        with urllib.request.urlopen(req, timeout=10) as r:
            ids = [m["id"] for m in json.load(r)["data"]]
        if want not in ids:
            cands = [i for i in ids if i.startswith("google/gemini") and "flash" in i and "image" not in i and "lite" not in i and not i.endswith(":free")]
            chosen = sorted(cands)[-1] if cands else want
    except Exception:  # noqa: BLE001
        pass
    _model_cache[want] = chosen
    return chosen


def complete(system: str, user: str, max_tokens: int = 500, budget_s: float = 22.0) -> str | None:
    """Returns text, or None on any failure (callers fall back to deterministic text).
    OpenRouter gets one retry, but the whole call is capped at budget_s so a serverless function can never hit its time limit."""
    import time
    start = time.monotonic()
    out = _complete_once(system, user, max_tokens, timeout=budget_s)
    left = budget_s - (time.monotonic() - start)
    if out is None and provider() == "openrouter" and left > 4:
        out = _complete_once(system, user, max_tokens, timeout=left)
    return out


def _complete_once(system: str, user: str, max_tokens: int, timeout: float = 22.0) -> str | None:
    p = provider()
    try:
        if p == "openrouter":
            body = json.dumps({"model": openrouter_model(), "max_tokens": max(max_tokens, 1500), "temperature": 0.2,
                               "reasoning": {"effort": "low"},  # explanations don't need long hidden reasoning: ~2s instead of 7-40s
                               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                               "provider": GOOGLE_ONLY}).encode()
            req = urllib.request.Request(OPENROUTER_URL + "/chat/completions", data=body, method="POST",
                                         headers={"Authorization": "Bearer " + db.get_secret("OPENROUTER_API_KEY"), "Content-Type": "application/json",
                                                  "X-Title": "Freshroots Demand Planning"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            return (data["choices"][0]["message"].get("content") or "").strip() or None
        if p == "gemini":
            model = db.get_secret("GEMINI_MODEL") or "gemini-2.5-flash"
            body = json.dumps({"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                               "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2}}).encode()
            req = urllib.request.Request(GEMINI_URL.format(model=model), data=body, method="POST",
                                         headers={"content-type": "application/json", "x-goog-api-key": db.get_secret("GEMINI_API_KEY")})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"]).strip() or None
        if p == "claude":
            import anthropic
            msg = anthropic.Anthropic(api_key=db.get_secret("ANTHROPIC_API_KEY")).messages.create(
                model="claude-haiku-5-5", max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}])
            return msg.content[0].text.strip()
    except Exception as e:  # noqa: BLE001  (never leak provider errors, which may echo request details)
        import sys
        print("AI request failed:", type(e).__name__, file=sys.stderr, flush=True)
        return None
    return None


def numbers_in(text: str) -> set[str]:
    return set(_NUM.findall(text.replace(",", "")))


def allowed_numbers(facts: dict, question: str = "") -> set[str]:
    out = numbers_in(json.dumps(facts, default=str)) | numbers_in(question)

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            for d in (0, 1, 2):
                out.add(f"{abs(v):.{d}f}")
            out.add(f"{abs(v) * 100:.0f}")
            out.add(f"{abs(v) * 100:.1f}")
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(facts)
    return out


ASK_SYSTEM = (
    "You assist a demand planner at a food distributor. You are given JSON facts computed by a deterministic rules engine for ONE SKU. "
    "Answer the planner's question using ONLY those facts. You must not change, override or second-guess the engine's flag, quantity, urgency or action, "
    "and you must not invent numbers, SKUs, suppliers or events. You may explain why the engine said what it said, compare the options shown, "
    "and suggest what a human should check before approving (for example lead time, open POs, supplier constraints). "
    "If the facts do not contain the answer, say so plainly. Be brief: at most 5 short sentences, plain language."
)


def ask(facts: dict, question: str) -> dict:
    q = (question or "").strip()[:500]
    if not q:
        return {"ok": False, "text": "Ask a question about this SKU.", "source": "none"}
    if provider() is None:
        return {"ok": False, "text": "No AI key is configured on this server.", "source": "none"}
    text = complete(ASK_SYSTEM, "Facts:\n" + json.dumps(facts, default=str) + "\n\nQuestion: " + q)
    if text is None:
        return {"ok": False, "text": "The assistant is unavailable right now. The engine's explanation above is unaffected.", "source": "error"}
    stray = numbers_in(text) - allowed_numbers(facts, q)
    if stray:
        return {"ok": False, "text": "The assistant's answer used numbers that are not in the engine's data, so it was discarded. Try rephrasing.", "source": "blocked"}
    return {"ok": True, "text": text, "source": provider()}

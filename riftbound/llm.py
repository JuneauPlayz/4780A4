"""Thin LLM clients for Gemini and Groq that return parsed JSON.

Keys come only from environment variables (or a local .env file that is gitignored):
    GEMINI_API_KEY (or GOOGLE_API_KEY)  -> Google Gemini
    GROQ_API_KEY                        -> Groq
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

from . import config

GEMINI_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash"]
GROQ_FALLBACK_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"]


class LLMError(RuntimeError):
    pass


class MissingKeyError(LLMError):
    pass


@dataclass
class LLMResult:
    data: dict
    raw: str
    provider: str
    model: str
    seconds: float


def gemini_key() -> str:
    return config.env("GEMINI_API_KEY") or config.env("GOOGLE_API_KEY")


def groq_key() -> str:
    return config.env("GROQ_API_KEY")


def available_provider() -> str | None:
    pref = config.LLM_PROVIDER
    if pref == "gemini":
        return "gemini" if gemini_key() else None
    if pref == "groq":
        return "groq" if groq_key() else None
    if gemini_key():
        return "gemini"
    if groq_key():
        return "groq"
    return None


def parse_json(text: str) -> dict:
    """Parse a JSON object even if the model wrapped it in a code fence or prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _is_model_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("not found", "404", "does not exist", "decommissioned", "not supported", "model_not_found"))


def _gemini(system: str, user: str) -> tuple[str, str]:
    from google import genai
    from google.genai import types

    # google-genai 3.x defaults to API v1, which rejects system instructions; they need v1beta.
    client = genai.Client(api_key=gemini_key(), http_options=types.HttpOptions(api_version="v1beta"))
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=config.LLM_TEMPERATURE,
        response_mime_type="application/json",
    )
    models = [config.GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != config.GEMINI_MODEL]
    last: Exception | None = None
    for model in models:
        try:
            resp = client.models.generate_content(model=model, contents=user, config=cfg)
            return resp.text or "", model
        except Exception as exc:  # try the next model only when this one is unavailable
            last = exc
            if not _is_model_error(exc):
                break
    raise LLMError(f"Gemini request failed: {last}")


def _groq(system: str, user: str) -> tuple[str, str]:
    from groq import Groq

    client = Groq(api_key=groq_key())
    models = [config.GROQ_MODEL] + [m for m in GROQ_FALLBACK_MODELS if m != config.GROQ_MODEL]
    last: Exception | None = None
    for model in models:
        extra = {}
        if model.startswith("openai/gpt-oss"):
            # Low reasoning keeps answers fast (~3 s instead of ~25 s) and inside free-tier token limits.
            extra["reasoning_effort"] = config.GROQ_REASONING_EFFORT
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=config.LLM_TEMPERATURE,
                response_format={"type": "json_object"},
                max_completion_tokens=config.GROQ_MAX_TOKENS,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **extra,
            )
            return resp.choices[0].message.content or "", model
        except Exception as exc:
            last = exc
            # "Request too large" (413) is a per-model token limit, so a model with a higher limit may work.
            if not (_is_model_error(exc) or "413" in str(exc) or "request too large" in str(exc).lower()):
                break
    raise LLMError(f"Groq request failed: {last}")


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("429", "rate limit", "rate_limit", "resource_exhausted", "quota", "503", "overloaded"))


_throttle_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """Client-side rate limit: keep at least LLM_MIN_INTERVAL seconds between calls from this process."""
    global _last_call
    with _throttle_lock:
        wait = _last_call + config.LLM_MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _call_with_retry(provider: str, system: str, user: str) -> tuple[str, str, str]:
    """Call the provider; on a rate limit wait once and retry, then fall back to the other provider if it has a key."""
    calls = {"gemini": _gemini, "groq": _groq}
    _throttle()
    try:
        return (provider, *calls[provider](system, user))
    except LLMError as exc:
        if not _is_rate_limit(exc):
            raise
        # Providers say how long to wait ("Please try again in 12.5s"); honour it, up to 30 s.
        m = re.search(r"try again in ([\d.]+)\s*s", str(exc))
        time.sleep(min(30.0, float(m.group(1)) + 1) if m else 8)
        try:
            return (provider, *calls[provider](system, user))
        except LLMError as exc2:
            other = "groq" if provider == "gemini" else "gemini"
            if _is_rate_limit(exc2) and (groq_key() if other == "groq" else gemini_key()):
                return (other, *calls[other](system, user))
            raise


def generate_json(system: str, user: str, provider: str | None = None) -> LLMResult:
    provider = provider or available_provider()
    if provider is None:
        raise MissingKeyError(
            "No LLM API key found. Set GEMINI_API_KEY (free key: https://aistudio.google.com/apikey) "
            "or GROQ_API_KEY (https://console.groq.com/keys) in a .env file or your environment."
        )
    t0 = time.time()
    provider, raw, model = _call_with_retry(provider, system, user)
    call = _gemini if provider == "gemini" else _groq
    try:
        data = parse_json(raw)
    except (json.JSONDecodeError, ValueError):
        # One retry with an explicit reminder; models occasionally add prose around JSON.
        raw, model = call(system, user + "\n\nYour previous reply was not valid JSON. Return only the JSON object.")
        try:
            data = parse_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMError(f"{provider} returned invalid JSON: {raw[:300]}") from exc
    return LLMResult(data, raw, provider, model, round(time.time() - t0, 2))

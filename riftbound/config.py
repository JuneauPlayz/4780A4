"""Paths and environment-driven settings. API keys are only ever read from the environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"  # downloaded originals (gitignored)
MANIFEST_PATH = DATA_DIR / "manifest.json"
PASSAGES_PATH = DATA_DIR / "passages.jsonl"
CARDS_PATH = DATA_DIR / "cards.json"

load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# "auto" picks Gemini if GEMINI_API_KEY is set, otherwise Groq if GROQ_API_KEY is set.
LLM_PROVIDER = env("LLM_PROVIDER", "auto").lower()
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")
LLM_TEMPERATURE = float(env("LLM_TEMPERATURE", "0.2"))
# Groq free tier allows 8,000 tokens per minute for gpt-oss-120b, counting prompt + max completion.
GROQ_MAX_TOKENS = int(env("GROQ_MAX_TOKENS", "2000"))
GROQ_REASONING_EFFORT = env("GROQ_REASONING_EFFORT", "low")

# Retrieval sizes: how many passages of each kind go into the prompt.
TOP_K_CORE = int(env("TOP_K_CORE", "6"))
TOP_K_OTHER = int(env("TOP_K_OTHER", "3"))
# Long passages are cut to this many characters in the prompt (the full text stays linked).
PROMPT_PASSAGE_CHARS = int(env("PROMPT_PASSAGE_CHARS", "2400"))

# ------------------------------------------------------------------ agent (Assignment 4)
# Each browser session gets its own folder (decks, exports, SQLite file, audit log) under this root.
# On Streamlit Community Cloud the file system is ephemeral, so these are lost when the app restarts.
SANDBOX_ROOT = Path(env("SANDBOX_ROOT") or (Path(os.getenv("TMPDIR", "/tmp")) / "riftbound-agent"))
SESSION_REQUEST_LIMIT = int(env("SESSION_REQUEST_LIMIT", "20"))  # natural-language requests per session
MAX_LLM_CALLS_PER_REQUEST = int(env("MAX_LLM_CALLS_PER_REQUEST", "8"))
MAX_ACTIONS_PER_PLAN = int(env("MAX_ACTIONS_PER_PLAN", "8"))
MAX_ACTIONS_PER_REQUEST = int(env("MAX_ACTIONS_PER_REQUEST", "15"))
# Spacing between LLM calls from this process, to stay under free-tier requests-per-minute limits.
LLM_MIN_INTERVAL = float(env("LLM_MIN_INTERVAL", "2.0"))
ALLOW_REFRESH = env("ALLOW_REFRESH", "true").lower() in ("1", "true", "yes")
REFRESH_COOLDOWN_SECONDS = int(env("REFRESH_COOLDOWN_SECONDS", "3600"))  # shared by every session
VERSION_CHECK_COOLDOWN_SECONDS = int(env("VERSION_CHECK_COOLDOWN_SECONDS", "60"))  # per session

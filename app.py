"""Riftbound Deck Agent: Streamlit UI.

Run locally:  streamlit run app.py
Deployed on Streamlit Community Cloud; API keys come from st.secrets or the environment.
"""

from __future__ import annotations

import os

import streamlit as st

st.set_page_config(page_title="Riftbound Deck Agent", page_icon="🃏", layout="wide")


def _load_secrets() -> None:
    """Copy Streamlit secrets into the environment before the settings module reads it."""
    try:
        for key, value in st.secrets.items():
            if isinstance(value, (str, int, float, bool)) and not os.getenv(key):
                os.environ[key] = str(value)
    except Exception:  # no secrets file locally is fine; .env is used instead
        pass


_load_secrets()

import pandas as pd  # noqa: E402

from agent import prompts  # noqa: E402
from agent.deckrules import validate_deck  # noqa: E402
from agent.decks import EXPORTERS  # noqa: E402
from agent.loop import Agent, Turn  # noqa: E402
from agent.session import AgentSession, cleanup_old_sandboxes  # noqa: E402
from riftbound import llm  # noqa: E402

STATUS_BADGE = {
    "validated": ":blue-badge[validated]",
    "awaiting_confirmation": ":orange-badge[waiting for confirmation]",
    "done": ":green-badge[done]",
    "failed": ":red-badge[failed]",
    "blocked": ":red-badge[blocked]",
    "rejected": ":red-badge[rejected by validator]",
    "cancelled": ":gray-badge[cancelled]",
    "skipped": ":gray-badge[skipped]",
}
TIER_BADGE = {"read": ":gray-badge[read]", "write": ":blue-badge[write]", "destructive": ":red-badge[destructive]"}
TURN_BADGE = {
    "done": ":green-badge[done]",
    "needs_input": ":violet-badge[needs your answer]",
    "awaiting_confirmation": ":orange-badge[waiting for confirmation]",
    "failed": ":red-badge[failed]",
    "limit": ":orange-badge[stopped at limit]",
    "running": ":blue-badge[running]",
}
EXAMPLES = [
    "Build a Standard-legal Jinx deck, save it as jinx-aggro, and log that I beat an Ahri deck with it.",
    "Which of my saved decks have a banned card? Delete the old Draven one.",
    "What's my win rate with jinx-starter, and against which opponents do I lose?",
    "Can I play 3 Signature cards and 3 copies of my Chosen Champion in the same deck?",
    "Export jinx-starter as a text decklist.",
    "Are my rules data up to date?",
]


@st.cache_resource
def _startup() -> bool:
    cleanup_old_sandboxes()
    return True


def _new_session() -> None:
    old = st.session_state.get("agent")
    if old is not None:
        old.s.close()
    st.session_state.agent = Agent(AgentSession.create())
    st.session_state.turns = []


_startup()
if "agent" not in st.session_state:
    _new_session()
agent: Agent = st.session_state.agent
sess = agent.s
turns: list[Turn] = st.session_state.turns
provider = llm.available_provider()

# ====================================================================== sidebar

with st.sidebar:
    st.title("🃏 Riftbound Deck Agent")
    st.caption(
        "An AI agent for Riftbound players. You ask in plain English; the model plans actions, the app "
        "validates them, asks before anything destructive, runs them, and logs everything."
    )
    if provider:
        st.success(f"LLM: {provider}", icon="✅")
    else:
        st.error("No LLM API key. Set GEMINI_API_KEY in `.env` or Streamlit secrets.", icon="🔑")
    used, limit = sess.requests_used, sess.request_limit
    st.progress(min(1.0, used / limit), text=f"Requests this session: {used} / {limit}")
    show_prompts = st.toggle("Show prompts sent to the LLM", value=False, help="Shows the exact system and user prompt for each step, plus the raw model reply.")
    st.subheader("Try")
    for i, ex in enumerate(EXAMPLES):
        if st.button(ex, key=f"ex{i}", width="stretch", disabled=not provider):
            st.session_state.queued = ex
    st.divider()
    if st.button("Start a new session", width="stretch", help="Fresh sandbox with the two demo decks"):
        _new_session()
        st.rerun()
    st.caption(f"Session `{sess.id}`. Decks, matches and the audit log live in a per-session folder that is "
               "deleted when the app restarts.")
    st.caption("Not affiliated with Riot Games. Riftbound is a trademark of Riot Games, Inc.")

# ====================================================================== header + rules banner

manifest = sess.kb.manifest
core = manifest["core_rules"]
leg = sess.catalog.legality
st.info(
    f"**Rules data:** Core Rules updated {core['last_updated']} (effective {core['effective_date']}) · "
    f"banned list {leg.updated or 'unknown'} · Standard sets {', '.join(leg.standard_sets) or 'unknown'} · "
    f"{manifest['counts']['cards']} cards · index fetched {manifest['fetched_at'][:10]}",
    icon="📘",
)

tab_agent, tab_decks, tab_stats, tab_audit, tab_prompts = st.tabs(
    ["💬 Agent", "🗂️ Saved decks", "📊 Match stats", "🧾 Audit log", "🧠 Prompt patterns"]
)


# ====================================================================== rendering helpers


def render_report(report: dict) -> None:
    st.markdown(f"**{'✅ Legal' if report['legal'] else '❌ Not legal'}** in {report['format']}: {report['summary']}")
    rows = ["| | Check | Rules | Problems |", "|---|---|---|---|"]
    for c in report["checks"]:
        rules = ", ".join(f"[{r['ref']}]({r['link']})" if r["link"] else f"{r['ref']} ⚠" for r in c["rules"])
        probs = "<br>".join(p.replace("|", "/") for p in c["problems"]) or ""
        rows.append(f"| {'✅' if c['ok'] else '❌'} | {c['title']} | {rules} | {probs} |")
    st.markdown("\n".join(rows), unsafe_allow_html=True)
    for w in report["warnings"]:
        st.warning(w)


def render_action(rec, key: str) -> None:
    line = f"{STATUS_BADGE.get(rec.status, rec.status)} {TIER_BADGE.get(rec.tier, '')} `{rec.name}` · *{rec.domain}*"
    detail = rec.summary or rec.error or ""
    st.markdown(f"{line}  \n{detail}")
    if rec.changes and rec.status == "done":
        st.markdown("**Changed:**\n" + "\n".join(f"- `{c}`" for c in rec.changes[:25]))
    with st.expander("Arguments and result", expanded=False):
        st.caption("Arguments (after validation)")
        st.json(rec.args, expanded=False)
        if rec.error:
            st.error(rec.error)
        if rec.display and isinstance(rec.display, dict) and "checks" in rec.display:
            render_report(rec.display)
        elif rec.data is not None:
            st.caption("Result (what the model observed)")
            st.json(rec.data, expanded=False)
    if rec.file:
        st.download_button(f"Download {rec.file['name']}", rec.file["content"], file_name=rec.file["name"], mime=rec.file["mime"], key=f"dl-{key}")


def render_turn(turn: Turn, latest: bool) -> None:
    with st.chat_message("user"):
        st.markdown(turn.request)
    with st.chat_message("assistant", avatar="🃏"):
        n_actions = len([a for a in turn.actions if a.status == "done"])
        st.markdown(f"{TURN_BADGE.get(turn.status, turn.status)} · {turn.llm_calls} model call(s) · {n_actions} action(s) done")
        for step in turn.steps:
            with st.container(border=True):
                st.markdown(f"**Step {step.n}**" + (f" — {step.llm.get('model', '')}, {step.llm.get('seconds', '')} s" if step.llm else ""))
                if step.thought:
                    st.markdown(f"🧭 *{step.thought}*")
                if step.rejected:
                    st.error("**Plan rejected by the validator — nothing in it ran.**\n" + "\n".join(f"- {e}" for e in step.rejected))
                for j, rec in enumerate(step.actions):
                    render_action(rec, f"{turn.id}-{step.n}-{j}")
                for note in step.notes:
                    st.caption(f"Note: {note}")
                if show_prompts and step.user_prompt:
                    with st.expander("Prompt sent to the LLM"):
                        st.caption("System prompt")
                        st.code(step.system_prompt, language="markdown", wrap_lines=True)
                        st.caption("User prompt")
                        st.code(step.user_prompt, language="markdown", wrap_lines=True)
                        st.caption("Raw model reply")
                        st.code(step.raw or "(none)", language="json", wrap_lines=True)

        if turn.status == "awaiting_confirmation" and turn.pending is not None:
            rec = turn.pending
            with st.container(border=True):
                st.markdown(f":orange-badge[waiting for confirmation] {TIER_BADGE['destructive']} `{rec.name}`")
                st.markdown("\n".join(f"- {line}" for line in rec.preview))
                if latest:
                    c1, c2, _ = st.columns([1, 1, 4])
                    if c1.button("Confirm", type="primary", key=f"yes-{turn.id}-{rec.id}"):
                        st.session_state.resume = (turn.id, True)
                        st.rerun()
                    if c2.button("Cancel", key=f"no-{turn.id}-{rec.id}"):
                        st.session_state.resume = (turn.id, False)
                        st.rerun()

        if turn.final_answer:
            st.markdown(turn.final_answer)
        if turn.error:
            st.error(turn.error)
        for w in turn.warnings:
            st.warning(w, icon="⚠️")
        if turn.status == "needs_input" and turn.ask_user:
            st.markdown(f"❓ **{turn.ask_user['question']}**")
            if latest and turn.ask_user.get("options"):
                cols = st.columns(len(turn.ask_user["options"]))
                for col, opt in zip(cols, turn.ask_user["options"]):
                    if col.button(opt, key=f"opt-{turn.id}-{opt}"):
                        st.session_state.queued = opt
                        st.rerun()


def run_with_status(label: str, fn):
    with st.status(label, expanded=True) as status:
        agent.progress = lambda msg: status.write(msg)
        try:
            result = fn()
        finally:
            agent.progress = lambda msg: None
        status.update(label="Finished", state="complete", expanded=False)
    return result


# ====================================================================== agent tab

with tab_agent:
    if not turns:
        st.markdown(
            "Ask for something to **do**: build and save a deck, check decks against the banned list, log matches, "
            "export a decklist, or look up a deck-building rule. Two demo decks (`jinx-starter`, `old-draven`) and a few "
            "matches are already saved in this session."
        )
    for i, t in enumerate(turns):
        render_turn(t, latest=(i == len(turns) - 1))

    resume = st.session_state.pop("resume", None)
    if resume and turns and turns[-1].id == resume[0]:
        run_with_status("Confirmed, continuing..." if resume[1] else "Cancelled, continuing...",
                        lambda: agent.resume(turns[-1], resume[1]))
        st.rerun()

    waiting = bool(turns) and turns[-1].status == "awaiting_confirmation"
    typed = st.chat_input(
        "Confirm or cancel the pending action first" if waiting else "Ask the deck agent...",
        disabled=waiting or not provider,
    )
    request = st.session_state.pop("queued", None) or typed
    if request and not waiting:
        with st.chat_message("user"):
            st.markdown(request)
        turn = run_with_status("Working...", lambda: agent.submit(request))
        turns.append(turn)
        st.rerun()

# ====================================================================== decks tab

with tab_decks:
    decks = sess.decks.list()
    if not decks:
        st.info("No saved decks yet. Ask the agent to build one.")
    else:
        st.dataframe(pd.DataFrame(decks), hide_index=True, width="stretch")
        name = st.selectbox("Deck", [d["name"] for d in decks])
        deck = sess.decks.load(name)
        left, right = st.columns([2, 3])
        with left:
            st.code(EXPORTERS["text"][0](deck), language=None)
            for fmt, (render, ext) in EXPORTERS.items():
                st.download_button(f"Download .{ext}", render(deck), file_name=f"{deck['name']}.{ext}", key=f"deckdl-{fmt}")
        with right:
            st.caption("Validation (deterministic code, no LLM)")
            render_report(validate_deck(deck, sess.catalog))

# ====================================================================== stats tab

with tab_stats:
    stats = sess.db.match_stats()
    if not stats["games"]:
        st.info("No matches logged yet.")
    else:
        total_w = sum(r["wins"] for r in stats["by_deck"])
        c1, c2, c3 = st.columns(3)
        c1.metric("Games", stats["games"])
        c2.metric("Wins", total_w)
        c3.metric("Win rate", f"{total_w / stats['games']:.0%}")
        st.subheader("By deck")
        by_deck = pd.DataFrame(stats["by_deck"]).rename(columns={"key": "deck"})
        st.dataframe(by_deck, hide_index=True, width="stretch")
        st.bar_chart(by_deck.set_index("deck")["win_rate"], y_label="win rate", height=220)
        st.subheader("By opponent")
        st.dataframe(pd.DataFrame(stats["by_opponent"]).rename(columns={"key": "opponent"}), hide_index=True, width="stretch")
        st.subheader("All matches")
        st.dataframe(pd.DataFrame(sess.db.all_matches()), hide_index=True, width="stretch")

# ====================================================================== audit tab

with tab_audit:
    entries = sess.audit.read()
    st.caption("One JSON line per event: every proposed, validated, rejected, confirmed, cancelled, executed and failed action, plus model calls.")
    if entries:
        df = pd.DataFrame(entries)
        events = sorted(df["event"].dropna().unique())
        pick = st.multiselect("Events", events, default=[e for e in events if e not in ("llm_call",)])
        cols = [c for c in ("ts", "request", "event", "action", "tier", "outcome", "error", "args") if c in df]
        view = df[df["event"].isin(pick)][cols].iloc[::-1]
        st.dataframe(view.astype(str), hide_index=True, width="stretch", height=420)
    st.download_button("Download audit log (JSONL)", sess.audit.raw(), file_name=f"audit-{sess.id}.jsonl", mime="application/json")

# ====================================================================== prompts tab

with tab_prompts:
    st.markdown(
        "The system prompt is built from these named blocks, one per prompt pattern, in this order. "
        "Turn on **Show prompts sent to the LLM** in the sidebar to see the full prompt for each step."
    )
    blocks = list(prompts.SYSTEM_PROMPT_BLOCKS)
    blocks.insert(3, ("Action catalog (generated from the action registry)", prompts.action_catalog_block()))
    for title, text in blocks:
        with st.expander(title):
            st.code(text, language="markdown", wrap_lines=True)

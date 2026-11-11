"""End-to-end scenarios against the real LLM (needs GEMINI_API_KEY or GROQ_API_KEY).

Each scenario gets a fresh session (with the two demo decks), runs one request, answers the
Confirm / Cancel prompt the way the scenario says, and checks the outcome. Results go to
scenarios/last_run.md.

    python scripts/run_scenarios.py                  # all scenarios
    python scripts/run_scenarios.py --only win-rate  # one scenario
    python scripts/run_scenarios.py --pause 30       # seconds between scenarios (free-tier quota)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from riftbound import config, llm  # noqa: E402

from agent.deckrules import validate_deck  # noqa: E402
from agent.loop import Agent  # noqa: E402
from agent.session import AgentSession  # noqa: E402


def check(expect: dict, turn, sess: AgentSession, sandbox_root: Path) -> list[str]:
    fails = []
    done = {a.name for a in turn.actions if a.status == "done"}
    answer = (turn.final_answer or "") + " " + ((turn.ask_user or {}).get("question") or "")
    if turn.status not in expect.get("status_in", [turn.status]):
        fails.append(f"status {turn.status} not in {expect['status_in']}")
    for a in expect.get("actions_done", []):
        if a not in done:
            fails.append(f"{a} was not done")
    for a in expect.get("actions_not_done", []):
        if a in done:
            fails.append(f"{a} should not have run")
    for d in expect.get("decks_exist", []):
        if not sess.decks.exists(d):
            fails.append(f"deck {d} missing")
    for d in expect.get("decks_absent", []):
        if sess.decks.exists(d):
            fails.append(f"deck {d} still exists")
    if expect.get("deck_legal") and sess.decks.exists(expect["deck_legal"]):
        report = validate_deck(sess.decks.load(expect["deck_legal"]), sess.catalog)
        if not report["legal"]:
            fails.append(f"{expect['deck_legal']} is not legal: {report['summary']}")
    if expect.get("matches_for") and not sess.db.match_stats(expect["matches_for"])["games"]:
        fails.append(f"no match logged for {expect['matches_for']}")
    if expect.get("final_contains_any") and not any(s.lower() in answer.lower() for s in expect["final_contains_any"]):
        fails.append(f"answer mentions none of {expect['final_contains_any']}")
    for s in expect.get("final_contains_all", []):
        if s.lower() not in answer.lower():
            fails.append(f"answer does not mention {s}")
    if expect.get("no_citation_warnings") and turn.warnings:
        fails.append(f"citation warnings: {turn.warnings}")
    if expect.get("no_files_outside_sandbox"):
        stray = [p for p in sandbox_root.parent.rglob("*passwd*") if sandbox_root not in p.parents]
        if stray or any("passwd" in p.name for p in (sess.sandbox.root / "decks").iterdir()):
            fails.append("a file named like passwd was written")
    for f in expect.get("exports_exist", []):
        if not (sess.sandbox.root / "exports" / f).exists():
            fails.append(f"export {f} missing")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="run one scenario id")
    ap.add_argument("--pause", type=float, default=20.0, help="seconds to wait between scenarios")
    ap.add_argument("--min-interval", type=float, default=6.0, help="minimum seconds between model calls")
    args = ap.parse_args(argv)
    if not llm.available_provider():
        print("No GEMINI_API_KEY / GROQ_API_KEY set (.env or environment).", file=sys.stderr)
        return 2
    config.LLM_MIN_INTERVAL = args.min_interval

    scenarios = json.loads((ROOT / "scenarios" / "scenarios.json").read_text(encoding="utf-8"))
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only]
    rows, details = [], []
    tmp = Path(tempfile.mkdtemp(prefix="riftbound-scenarios-"))
    for i, sc in enumerate(scenarios):
        if i:
            time.sleep(args.pause)
        sess = AgentSession.create(root=tmp, session_id=sc["id"])
        agent = Agent(sess)
        t0 = time.time()
        turn = agent.submit(sc["request"])
        confirmations = []
        while turn.status == "awaiting_confirmation":
            confirmations.append(f"{turn.pending.name} -> {'confirm' if sc.get('confirm') else 'cancel'}")
            turn = agent.resume(turn, bool(sc.get("confirm")))
        fails = check(sc.get("expect", {}), turn, sess, sess.sandbox.root)
        secs = round(time.time() - t0, 1)
        ok = "PASS" if not fails else "FAIL"
        print(f"[{ok}] {sc['id']}: {turn.status}, {turn.llm_calls} calls, {secs}s" + (f" -> {fails}" if fails else ""))
        rows.append(f"| {sc['id']} | {sc['pattern']} | {ok} | {turn.status} | {turn.llm_calls} | {len([a for a in turn.actions if a.status == 'done'])} | {secs} |")
        steps = "\n".join(
            f"  {st.n}. {st.thought}\n" + "".join(f"     - {a.name} {json.dumps(a.args)[:160]} -> {a.status}: {a.summary or a.error}\n" for a in st.actions)
            + (f"     - REJECTED: {st.rejected}\n" if st.rejected else "")
            for st in turn.steps
        )
        details.append(
            f"### {sc['id']} ({ok})\n\n**Request:** {sc['request']}\n\n"
            + (f"**Confirmations:** {', '.join(confirmations)}\n\n" if confirmations else "")
            + f"**Steps:**\n```\n{steps}```\n\n"
            + f"**Answer:** {turn.final_answer or turn.ask_user or turn.error}\n\n"
            + (f"**Failed checks:** {fails}\n\n" if fails else "")
        )
        sess.close()

    model = f"{llm.available_provider()} ({config.GEMINI_MODEL if llm.available_provider() == 'gemini' else config.GROQ_MODEL})"
    passed = sum("| PASS |" in r for r in rows)
    out = [
        "# Scenario run",
        "",
        f"{time.strftime('%Y-%m-%d %H:%M')} · {model} · {passed}/{len(rows)} passed",
        "",
        "| Scenario | Pattern | Result | Status | Model calls | Actions done | Seconds |",
        "|---|---|---|---|---|---|---|",
        *rows,
        "",
        *details,
    ]
    (ROOT / "scenarios" / "last_run.md").write_text("\n".join(out), encoding="utf-8")
    print(f"\n{passed}/{len(rows)} passed; details in scenarios/last_run.md")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())

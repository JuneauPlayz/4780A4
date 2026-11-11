"""The ReAct planning loop: plan -> validate -> (confirm) -> execute -> observe -> repeat.

The loop can pause. When a destructive action needs confirmation, submit()/resume() return a
Turn with status "awaiting_confirmation"; the UI shows Confirm / Cancel and calls resume().
That keeps it compatible with Streamlit, which reruns the script on every click.

Turn status: running | awaiting_confirmation | needs_input | done | failed | limit
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from riftbound import config
from riftbound.explainer import CitationChecker
from riftbound.knowledge import Retrieval
from riftbound.llm import LLMError, LLMResult, generate_json

from . import prompts
from .executor import ActionRecord, Executor
from .validator import Plan, PlanError, parse_plan

HISTORY_TURNS = 4


@dataclass
class StepRecord:
    n: int
    thought: str = ""
    actions: list[ActionRecord] = field(default_factory=list)
    final_answer: str | None = None
    ask_user: dict | None = None
    notes: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # validation errors, if the plan was rejected
    raw: str = ""
    system_prompt: str = ""
    user_prompt: str = ""
    llm: dict = field(default_factory=dict)

    def for_prompt(self) -> dict:
        out: dict = {"step": self.n, "thought": self.thought}
        if self.rejected:
            out["rejected"] = self.rejected
        if self.actions:
            out["observations"] = [a.observation() for a in self.actions]
        return out


@dataclass
class Turn:
    id: str
    request: str
    status: str = "running"
    steps: list[StepRecord] = field(default_factory=list)
    final_answer: str | None = None
    ask_user: dict | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0
    actions_run: int = 0
    queue: list[ActionRecord] = field(default_factory=list)  # remaining actions of the current step
    pending: ActionRecord | None = None
    cancelled: set[str] = field(default_factory=set)  # "action:target" the player declined this request
    feedback: list[str] | None = None  # validation errors to send back once

    @property
    def actions(self) -> list[ActionRecord]:
        return [a for s in self.steps for a in s.actions]


def _target(rec: ActionRecord) -> str:
    """Identity of an action for cancellation: the action plus the deck it touches."""
    return f"{rec.name}:{rec.args.get('name', '')}"


class Agent:
    def __init__(self, session, llm=None):
        self.s = session
        self.llm = llm or generate_json
        self.ex = Executor(session)
        self.history: list[dict] = []
        self.progress = lambda msg: None  # the UI sets this to show live status lines

    # ------------------------------------------------------------------ public

    def submit(self, request: str) -> Turn:
        turn = Turn(id=uuid.uuid4().hex[:8], request=request.strip())
        if not turn.request:
            turn.status, turn.error = "failed", "Type a request first."
            return turn
        if self.s.requests_used >= self.s.request_limit:
            turn.status = "limit"
            turn.error = f"This session has used its {self.s.request_limit} requests (a limit that protects the shared free API quota). Reload the page to start a new session."
            self.s.audit.log("limit", request_id=turn.id, outcome="request_limit", error=turn.error)
            return turn
        self.s.requests_used += 1
        self.s.retrieved = []
        self.s.created_this_request = set()
        self.s.audit.log("request", request_id=turn.id, args={"text": turn.request}, outcome="started")
        return self._run(turn)

    def resume(self, turn: Turn, confirm: bool) -> Turn:
        rec = turn.pending
        if turn.status != "awaiting_confirmation" or rec is None:
            return turn
        turn.pending = None
        turn.status = "running"
        if confirm:
            self.ex.execute(rec, turn.id, confirmed=True)
            turn.actions_run += 1
        else:
            self.ex.cancel(rec, turn.id)
            turn.cancelled.add(_target(rec))
        if rec.status != "done":
            for r in turn.queue:
                self.ex.skip(r, turn.id, f"skipped because {rec.name} was {rec.status}")
            turn.queue = []
        return self._run(turn)

    # ------------------------------------------------------------------ loop

    def _run(self, turn: Turn) -> Turn:
        while True:
            if turn.queue and not self._drain(turn):
                return turn  # paused for confirmation
            if turn.llm_calls >= config.MAX_LLM_CALLS_PER_REQUEST:
                return self._stop_at_limit(turn, f"the step limit ({config.MAX_LLM_CALLS_PER_REQUEST} model calls per request)")

            step = StepRecord(n=len(turn.steps) + 1)
            self.progress(f"Step {step.n}: asking the model for a plan...")
            step.system_prompt = prompts.build_system_prompt()
            step.user_prompt = prompts.build_user_prompt(
                turn.request,
                self.s.state_summary(),
                self.history[-HISTORY_TURNS:],
                [st.for_prompt() for st in turn.steps],
                turn.feedback,
                config.MAX_LLM_CALLS_PER_REQUEST - turn.llm_calls,
                self.s.kb.manifest,
            )
            try:
                result: LLMResult = self.llm(step.system_prompt, step.user_prompt)
            except LLMError as exc:
                turn.status, turn.error = "failed", f"The language model could not be reached: {exc}"
                self.s.audit.log("llm_error", request_id=turn.id, outcome="failed", error=str(exc))
                turn.steps.append(step)
                return turn
            turn.llm_calls += 1
            step.raw = result.raw
            step.llm = {"provider": result.provider, "model": result.model, "seconds": result.seconds}
            self.s.audit.log("llm_call", request_id=turn.id, outcome="ok", **step.llm)
            turn.steps.append(step)

            try:
                plan: Plan = parse_plan(result.data if result.data else result.raw)
            except PlanError as exc:
                if self._reject(turn, step, exc.errors):
                    continue
                return turn
            step.thought, step.notes = plan.thought, plan.notes

            if not plan.valid:
                step.actions = [self.ex.rejected(a, turn.id) for a in plan.actions if not a.valid]
                if self._reject(turn, step, plan.errors):
                    continue
                return turn
            turn.feedback = None

            if plan.actions:
                if turn.actions_run + len(plan.actions) > config.MAX_ACTIONS_PER_REQUEST:
                    return self._stop_at_limit(turn, f"the action limit ({config.MAX_ACTIONS_PER_REQUEST} actions per request)")
                step.actions = [self.ex.prepare(a, turn.id, f"{turn.id}-{step.n}-{a.index}") for a in plan.actions]
                turn.queue = list(step.actions)
                continue
            if plan.ask_user:
                step.ask_user = turn.ask_user = plan.ask_user
                turn.status = "needs_input"
                self._finish(turn, f"(asked) {plan.ask_user['question']}")
                return turn
            step.final_answer = plan.final_answer
            turn.final_answer, warnings = self._check_citations(plan.final_answer)
            turn.warnings += warnings
            turn.status = "done"
            self._finish(turn, plan.final_answer)
            return turn

    def _drain(self, turn: Turn) -> bool:
        """Run queued actions in order. Returns False if paused for confirmation."""
        while turn.queue:
            rec = turn.queue.pop(0)
            self.progress(f"Running {rec.name}...")
            if _target(rec) in turn.cancelled:
                self.ex.block(rec, turn.id, "the player cancelled this action earlier in this request; do not propose it again "
                              "(the player can ask for it again in a new message)")
            else:
                self.ex.execute(rec, turn.id)
            if rec.status == "awaiting_confirmation":
                turn.pending = rec
                turn.status = "awaiting_confirmation"
                return False
            if rec.status == "done":
                turn.actions_run += 1
            else:  # failed or blocked: later actions in this step may depend on it
                for r in turn.queue:
                    self.ex.skip(r, turn.id, f"skipped because {rec.name} {rec.status}")
                turn.queue = []
        return True

    def _reject(self, turn: Turn, step: StepRecord, errors: list[str]) -> bool:
        """Record a rejected plan. Returns True if the model gets one more try."""
        step.rejected = errors
        self.s.audit.log("plan_rejected", request_id=turn.id, outcome="rejected", error="; ".join(errors))
        if turn.feedback is None:
            turn.feedback = errors
            return True
        turn.status = "failed"
        turn.error = "The model's plan failed validation twice, so nothing from it was run:\n- " + "\n- ".join(errors)
        self._finish(turn, turn.error)
        return False

    def _stop_at_limit(self, turn: Turn, what: str) -> Turn:
        done = [f"{a.name}: {a.summary}" for a in turn.actions if a.status == "done"]
        turn.status = "limit"
        turn.final_answer = f"I stopped at {what}. " + (
            "Completed so far:\n- " + "\n- ".join(done) if done else "No actions were completed."
        )
        self.s.audit.log("limit", request_id=turn.id, outcome="step_limit", error=what)
        self._finish(turn, turn.final_answer)
        return turn

    def _finish(self, turn: Turn, answer: str) -> None:
        self.history.append({"request": turn.request, "answer": answer or ""})
        self.s.audit.log("final", request_id=turn.id, outcome=turn.status, summary=(answer or "")[:500])

    def _check_citations(self, text: str) -> tuple[str, list[str]]:
        """Link verified rule numbers in the answer and flag invented ones (A3 CitationChecker)."""
        retrieval = Retrieval("agent", core=[p for p in self.s.retrieved if p["source_kind"] == "core_rules"],
                              other=[p for p in self.s.retrieved if p["source_kind"] != "core_rules"])
        checker = CitationChecker(self.s.kb, retrieval, {})
        linked = checker.linkify(text)
        bad = [c.ref for c in checker.seen.values() if not c.verified and c.kind != "source"]
        warnings = [f"{ref} is not a rule number in the official index, so it was not linked." for ref in bad]
        return linked, warnings

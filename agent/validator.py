"""Parse the model's plan and validate every proposed action BEFORE anything runs.

The model must return one JSON object:
    {"thought": str, "actions": [{"action": str, "args": {...}}], "final_answer": str | null,
     "ask_user": null | {"question": str, "options": [str]}}

Validation is all-or-nothing per plan: if any action is invalid, none of them run, and the
errors are sent back to the model once so it can correct the plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from riftbound import config
from riftbound.llm import parse_json

from .actions import REGISTRY, ActionSpec, Arg
from .sandbox import UnsafeNameError, deck_slug

TOP_LEVEL_KEYS = {"thought", "actions", "final_answer", "ask_user"}


class PlanError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ProposedAction:
    index: int
    name: str
    raw_args: Any
    spec: ActionSpec | None = None
    args: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.spec is not None and not self.errors


@dataclass
class Plan:
    thought: str
    actions: list[ProposedAction]
    final_answer: str | None
    ask_user: dict | None
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f"action {a.index + 1} ({a.name}): {e}" for a in self.actions for e in a.errors]

    @property
    def valid(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------- arguments


def _check_arg(name: str, arg: Arg, value: Any) -> Any:
    """Return the normalized value or raise ValueError with a message for the model."""
    t = arg.type
    if t in ("str", "deck_name"):
        if not isinstance(value, str):
            raise ValueError(f"'{name}' must be a string")
        value = value.strip()
        if len(value) > arg.max_len:
            raise ValueError(f"'{name}' is longer than {arg.max_len} characters")
        if t == "deck_name":
            try:
                return deck_slug(value)
            except UnsafeNameError as exc:
                raise ValueError(f"'{name}': {exc}") from exc
        return value
    if t == "int":
        if isinstance(value, bool):
            raise ValueError(f"'{name}' must be an integer")
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ValueError(f"'{name}' must be an integer")
        if (arg.min is not None and value < arg.min) or (arg.max is not None and value > arg.max):
            raise ValueError(f"'{name}' must be between {arg.min} and {arg.max}")
        return value
    if t == "bool":
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError(f"'{name}' must be true or false")
        return value
    if t == "enum":
        if not isinstance(value, str):
            raise ValueError(f"'{name}' must be one of: {', '.join(arg.choices)}")
        match = next((c for c in arg.choices if c.lower() == value.strip().lower()), None)
        if match is None:
            raise ValueError(f"'{name}' must be one of: {', '.join(arg.choices)} (got {value!r})")
        return match
    if t == "str_list":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"'{name}' must be a list of strings")
        if len(value) > arg.max_items:
            raise ValueError(f"'{name}' has {len(value)} items; at most {arg.max_items}")
        out = []
        for v in value:
            v = v.strip()
            if len(v) > arg.max_len:
                raise ValueError(f"an item in '{name}' is longer than {arg.max_len} characters")
            if arg.choices:
                m = next((c for c in arg.choices if c.lower() == v.lower()), None)
                if m is None:
                    raise ValueError(f"'{name}' items must be from: {', '.join(arg.choices)} (got {v!r})")
                v = m
            out.append(v)
        return out
    if t == "card_list":
        if isinstance(value, dict):  # {"Jinx, Rebel": 3} is accepted too
            value = [{"card": k, "count": v} for k, v in value.items()]
        if not isinstance(value, list):
            raise ValueError(f"'{name}' must be a list of {{\"card\": name, \"count\": n}}")
        if len(value) > arg.max_items:
            raise ValueError(f"'{name}' has {len(value)} entries; at most {arg.max_items}")
        out = []
        for i, entry in enumerate(value):
            if not isinstance(entry, dict):
                raise ValueError(f"'{name}'[{i}] must be an object like {{\"card\": \"Jinx, Rebel\", \"count\": 3}}")
            extra = set(entry) - {"card", "count", "name"}
            if extra:
                raise ValueError(f"'{name}'[{i}] has unknown keys {sorted(extra)}")
            card = entry.get("card", entry.get("name"))
            count = entry.get("count", 1)
            if not isinstance(card, str) or not card.strip() or len(card) > 120:
                raise ValueError(f"'{name}'[{i}].card must be a card name")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12:
                raise ValueError(f"'{name}'[{i}].count must be an integer from 1 to 12")
            out.append({"card": card.strip(), "count": count})
        return out
    raise ValueError(f"internal: unknown arg type {t}")


def validate_args(spec: ActionSpec, raw: Any) -> tuple[dict, list[str]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return {}, ["'args' must be a JSON object"]
    errors: list[str] = []
    unknown = sorted(set(raw) - set(spec.args))
    if unknown:
        allowed = ", ".join(spec.args) or "none"
        errors.append(f"unknown argument(s) {unknown}; allowed: {allowed}")
    out: dict = {}
    for name, arg in spec.args.items():
        value = raw.get(name)
        if value is None or value == "" or value == []:
            if arg.required:
                errors.append(f"missing required argument '{name}'")
            elif arg.default is not None:
                out[name] = arg.default
            continue
        try:
            out[name] = _check_arg(name, arg, value)
        except ValueError as exc:
            errors.append(str(exc))
    return out, errors


# ---------------------------------------------------------------------- plan


def load_plan_json(raw: str | dict) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = parse_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlanError([f"the reply is not valid JSON ({exc})"]) from exc
    if not isinstance(data, dict):
        raise PlanError(["the reply must be a single JSON object"])
    return data


def parse_plan(raw: str | dict, max_actions: int | None = None) -> Plan:
    """Structure + per-action validation. Raises PlanError only for an unusable top-level shape."""
    max_actions = max_actions or config.MAX_ACTIONS_PER_PLAN
    data = load_plan_json(raw)
    notes: list[str] = []
    extra = sorted(set(data) - TOP_LEVEL_KEYS)
    if extra:
        notes.append(f"ignored unexpected keys {extra}")

    errors: list[str] = []
    thought = data.get("thought") or ""
    if not isinstance(thought, str):
        errors.append("'thought' must be a string")
    actions_raw = data.get("actions") or []
    if not isinstance(actions_raw, list):
        errors.append("'actions' must be a list")
        actions_raw = []
    if len(actions_raw) > max_actions:
        errors.append(f"too many actions in one plan ({len(actions_raw)}); the limit is {max_actions}")
    final = data.get("final_answer")
    if final is not None and not isinstance(final, str):
        errors.append("'final_answer' must be a string or null")
    ask = data.get("ask_user")
    if ask is not None:
        if not isinstance(ask, dict) or not isinstance(ask.get("question"), str) or not ask["question"].strip():
            errors.append("'ask_user' must be null or {\"question\": str, \"options\": [str]}")
            ask = None
        else:
            opts = ask.get("options") or []
            ask = {"question": ask["question"].strip(), "options": [str(o) for o in opts if isinstance(o, (str, int))][:4]}
    if errors:
        raise PlanError(errors)

    actions: list[ProposedAction] = []
    for i, item in enumerate(actions_raw):
        if not isinstance(item, dict) or not isinstance(item.get("action"), str):
            actions.append(ProposedAction(i, str(item)[:40], None, errors=['each action must be {"action": "<name>", "args": {...}}']))
            continue
        name = item["action"].strip()
        extra_keys = set(item) - {"action", "args"}
        pa = ProposedAction(i, name, item.get("args"))
        spec = REGISTRY.get(name)
        if spec is None:
            pa.errors.append(f"'{name}' is not an allowed action; allowed: {', '.join(REGISTRY)}")
        else:
            pa.spec = spec
            pa.args, pa.errors = validate_args(spec, item.get("args"))
        if extra_keys:
            pa.errors.append(f"unexpected keys {sorted(extra_keys)}; put parameters inside 'args'")
        actions.append(pa)

    final = final.strip() if isinstance(final, str) and final.strip() else None
    if actions and final:
        notes.append("final_answer ignored because the plan also has actions; answer after observing their results")
        final = None
    if actions and ask:
        notes.append("ask_user ignored because the plan also has actions")
        ask = None
    if not actions and not final and not ask:
        raise PlanError(["the plan has no actions, no final_answer and no ask_user; provide one"])
    return Plan(thought=thought.strip(), actions=actions, final_answer=final, ask_user=ask, notes=notes)

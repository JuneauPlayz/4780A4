"""Question -> retrieval -> prompt -> LLM -> citation check.

The citation check is deterministic: every rule number the model writes is looked up in
the local index of the current Core Rules / Tournament Rules. Numbers that do not exist
are flagged and never shown as links, so a hallucinated citation cannot look official.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import prompts
from .knowledge import KnowledgeBase, Retrieval, norm_rule
from .llm import LLMError, LLMResult, generate_json
from .sources import CARD_GALLERY_URL

STATUSES = ("answered", "needs_clarification", "not_supported")
INLINE_REF = re.compile(r"\[(CR|TR)\s*(\d{3}(?:\.(?:\d+|[a-z]))*)\.?\]|\[([RS]\d{1,2})\]")
BARE_RULE = re.compile(r"\b[Rr]ules?\s+(\d{3}(?:\.(?:\d+|[a-z]))*)")


@dataclass
class Citation:
    ref: str
    kind: str  # core_rules | tournament_rules | source
    verified: bool
    in_context: bool
    title: str = ""
    link: str | None = None
    supports: str = ""


@dataclass
class Explanation:
    question: str
    result: dict
    retrieval: Retrieval
    citations: list[Citation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    system_prompt: str = ""
    user_prompt: str = ""
    llm: LLMResult | None = None
    error: str | None = None


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def normalize(data: dict) -> dict:
    """Fill defaults so the UI never has to guess at the model's output shape."""
    out = {
        "status": str(data.get("status", "")).strip().lower(),
        "refined_question": str(data.get("refined_question") or ""),
        "short_answer": str(data.get("short_answer") or ""),
        "explanation": str(data.get("explanation") or ""),
        "steps": [],
        "example": str(data.get("example") or ""),
        "terms": [],
        "citations": [],
        "assumptions": [str(a) for a in _as_list(data.get("assumptions")) if a],
        "missing_details": [str(a) for a in _as_list(data.get("missing_details")) if a],
        "clarifying_question": str(data.get("clarifying_question") or ""),
        "clarifying_options": [str(a) for a in _as_list(data.get("clarifying_options")) if a][:4],
        "gameplay_vs_tournament": str(data.get("gameplay_vs_tournament") or ""),
        "confidence": str(data.get("confidence") or "low").lower(),
        "board": data.get("board") if isinstance(data.get("board"), dict) else None,
    }
    if out["status"] not in STATUSES:
        out["status"] = "not_supported"
    if out["confidence"] not in ("high", "medium", "low"):
        out["confidence"] = "low"
    for s in _as_list(data.get("steps")):
        if isinstance(s, dict):
            out["steps"].append(
                {"title": str(s.get("title") or ""), "detail": str(s.get("detail") or ""), "citations": [str(c) for c in _as_list(s.get("citations"))]}
            )
        elif s:
            out["steps"].append({"title": str(s)[:60], "detail": str(s), "citations": []})
    for t in _as_list(data.get("terms")):
        if isinstance(t, dict) and t.get("term"):
            out["terms"].append({"term": str(t["term"]), "meaning": str(t.get("meaning") or ""), "citation": str(t.get("citation") or "")})
    for c in _as_list(data.get("citations")):
        if isinstance(c, dict) and c.get("ref"):
            out["citations"].append({"ref": str(c["ref"]), "supports": str(c.get("supports") or "")})
        elif isinstance(c, str):
            out["citations"].append({"ref": c, "supports": ""})
    if out["status"] == "needs_clarification" and not out["clarifying_question"]:
        out["clarifying_question"] = "Could you add the missing detail: " + "; ".join(out["missing_details"] or ["the exact card text"]) + "?"
    return out


class CitationChecker:
    def __init__(self, kb: KnowledgeBase, retrieval: Retrieval, labels: dict[str, dict]):
        self.kb = kb
        self.labels = labels
        self.context_rules = {
            kind: {norm_rule(n) for p in retrieval.passages if p["source_kind"] == kind for n in p.get("rule_ids", [])}
            for kind in ("core_rules", "tournament_rules")
        }
        self.seen: dict[str, Citation] = {}

    def resolve(self, ref: str, supports: str = "") -> Citation:
        ref = ref.strip().strip("[]").strip()
        # Accept trailing commentary such as "CR 467 (definition in the rule text)".
        m = re.match(r"^(CR|TR|Core Rules|Tournament Rules)?\s*(?:rule\s*)?(\d{3}(?:\.(?:\d+|[a-z]))*)\.?(?=$|[\s(,;:-])", ref, re.I)
        if m:
            kind = "tournament_rules" if (m.group(1) or "").upper().startswith("T") else "core_rules"
            num = norm_rule(m.group(2))
            # A number given to the model from the other rulebook under the wrong prefix (CR 601.3.b for TR 601.3.b).
            other = "core_rules" if kind == "tournament_rules" else "tournament_rules"
            if not self.kb.rule_exists(kind, num) and num in self.context_rules[other]:
                kind = other
            key = f"{'TR' if kind == 'tournament_rules' else 'CR'} {num}"
            if key in self.seen:
                return self.seen[key]
            exists = self.kb.rule_exists(kind, num)
            doc = "Core Rules" if kind == "core_rules" else "Tournament Rules"
            version = self.kb.manifest["core_rules"]["last_updated"] if kind == "core_rules" else ""
            cit = Citation(
                ref=key,
                kind=kind,
                verified=exists,
                in_context=num in self.context_rules[kind],
                title=f"{doc} {num}" + (f" ({version})" if version else ""),
                link=self.kb.rule_link(kind, num) if exists else None,
                supports=supports,
            )
            self.seen[key] = cit
            return cit
        label = ref.split()[0].upper() if ref else ref
        if label in self.seen:
            return self.seen[label]
        src = self.labels.get(label)
        if src is None:
            cit = Citation(ref=label, kind="source", verified=False, in_context=False, title="Unknown source label", supports=supports)
        elif src.get("source_kind") == "card":
            card = src["card"]
            link = card["errata"]["url"] if card.get("errata") else CARD_GALLERY_URL
            where = f"errata: {card['errata']['source_title']}" if card.get("errata") else "Card Gallery"
            cit = Citation(label, "source", True, True, f"Card: {card['name']} ({where})", link, supports)
        else:
            date = src.get("doc_version") or src.get("date") or ""
            cit = Citation(label, "source", True, True, f"{src['source_title']} ({date}): {src['title'][:90]}", src.get("link"), supports)
        self.seen[label] = cit
        return cit

    def linkify(self, text: str) -> str:
        """Turn [CR 467.2] / [S2] into links; mark anything that cannot be verified."""

        def repl(m: re.Match) -> str:
            ref = f"{m.group(1)} {m.group(2)}" if m.group(1) else m.group(3)
            cit = self.resolve(ref)
            if cit.verified and cit.link:
                return f"[[{cit.ref}]]({cit.link})"
            if cit.verified:
                return f"[{cit.ref}]"
            return f"[⚠ {ref} not found]"

        text = INLINE_REF.sub(repl, text)

        def bare(m: re.Match) -> str:
            cit = self.resolve(m.group(1))
            return m.group(0) if cit.verified else m.group(0) + " ⚠"

        return BARE_RULE.sub(bare, text)


def _is_ref(ref: str) -> bool:
    """A citation needs a rule number or a source label; a bare "CR" is a formatting slip, not a claim."""
    return bool(ref) and bool(re.search(r"\d", ref))


def check_citations(kb: KnowledgeBase, retrieval: Retrieval, labels: dict, result: dict) -> tuple[dict, list[Citation], list[str]]:
    warnings: list[str] = []
    checker = CitationChecker(kb, retrieval, labels)
    for key in ("short_answer", "explanation", "example", "gameplay_vs_tournament"):
        result[key] = checker.linkify(result[key])
    for step in result["steps"]:
        step["detail"] = checker.linkify(step["detail"])
        step["resolved"] = [checker.resolve(c) for c in step["citations"] if _is_ref(c)]
        step["citations"] = [c.ref for c in step["resolved"] if c.verified]
    for term in result["terms"]:
        term["resolved"] = checker.resolve(term["citation"]) if _is_ref(term["citation"]) else None
    for c in result["citations"]:
        if not _is_ref(c["ref"]):
            continue
        cit = checker.resolve(c["ref"])
        if c["supports"] and not cit.supports:
            cit.supports = c["supports"]

    cites = list(checker.seen.values())
    for c in cites:
        if not c.verified:
            warnings.append(f"The model referred to {c.ref}, which is not in the indexed official sources, so it was not used as a citation.")
    outside = [c for c in cites if c.verified and not c.in_context and c.kind != "source"]
    if outside:
        warnings.append(
            "These rule numbers exist in the official rules but were not among the passages given to the model, "
            "so check them yourself: " + ", ".join(c.ref for c in outside)
        )
    good = [c for c in cites if c.verified]
    if result["status"] == "answered" and not good:
        warnings.append("The answer has no verifiable citation, so treat it as unconfirmed.")
        result["confidence"] = "low"
    return result, good, list(dict.fromkeys(warnings))


def explain(
    kb: KnowledgeBase,
    question: str,
    clarification: str = "",
    user_card_text: str = "",
    llm=None,
) -> Explanation:
    """llm(system, user) -> LLMResult; defaults to the configured Gemini/Groq client."""
    llm = llm or generate_json
    retrieval = kb.retrieve(question + " " + clarification, extra_text=user_card_text)
    system = prompts.build_system_prompt()
    user, labels = prompts.build_user_prompt(
        question, retrieval, kb.manifest, clarification, user_card_text, card_summary=kb.card_summary
    )
    exp = Explanation(question=question, result={}, retrieval=retrieval, system_prompt=system, user_prompt=user)
    try:
        exp.llm = llm(system, user)
    except LLMError as exc:
        exp.error = str(exc)
        return exp
    result = normalize(exp.llm.data)
    exp.result, exp.citations, exp.warnings = check_citations(kb, retrieval, labels, result)
    return exp

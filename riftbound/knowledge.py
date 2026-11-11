"""Load the local rules index and retrieve passages and card text for a question.

Retrieval is a small BM25 keyword search (no embeddings, no extra services), plus:
  * exact rule-number lookups ("rule 467.2"),
  * a boost when a rule's title (e.g. "Hidden", "Scoring") appears in the question,
  * card-name detection against the official Card Gallery, with errata applied, and the
    keywords on those cards fed back into the search ("[Hidden]" pulls in rule 811).
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import CARDS_PATH, MANIFEST_PATH, PASSAGES_PATH, TOP_K_CORE, TOP_K_OTHER
from .parsing import norm_name

STOPWORDS = set(
    """a an the and or but if then than of to in on at by for from with without into onto is are was were be been
    being do does did can could should would will shall may might must i me my mine you your yours we our they their
    them he she it its this that these those what which who whom when where why how there here so not no yes as up
    down out over under again also just only very too more most less any each other some such own same both all
    about after before while during because until get gets got happen happens happening""".split()
)

# Player phrasing -> Core Rules vocabulary. Kept small and explicit so it is easy to audit.
SYNONYMS = {
    "attack": "attacker combat", "attacking": "attacker combat", "attacks": "attacker combat",
    "block": "defender combat", "blocker": "defender combat", "blocking": "defender combat",
    "defend": "defender combat", "defending": "defender combat",
    "die": "kill killed", "dies": "kill killed", "died": "kill killed", "destroy": "kill", "destroyed": "kill",
    "graveyard": "trash", "discard": "discard trash", "tap": "exhaust", "tapped": "exhaust",
    "untap": "ready", "untapped": "ready", "mana": "energy power rune", "stack": "chain",
    "respond": "reaction chain priority", "response": "reaction chain priority",
    "counterspell": "counter", "facedown": "hidden facedown", "face-down": "hidden facedown",
    "points": "score point", "point": "score", "win": "winning score", "summon": "play unit",
    "lane": "battlefield", "location": "location battlefield", "might": "might", "power": "power rune",
    "damage": "damage", "heal": "heal", "banned": "banned legality tournament", "legal": "legality tournament",
    "ban": "banned legality", "tournament": "tournament", "deck": "deck construction",
}


def stem(tok: str) -> str:
    for suf in ("ing", "ies", "ed", "es", "s"):
        if len(tok) > 4 and tok.endswith(suf):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def tokenize(text: str, expand: bool = False) -> list[str]:
    raw = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace("’", "'"))
    toks = []
    for t in raw:
        if t in STOPWORDS:
            continue
        toks.append(stem(t))
        if expand and t in SYNONYMS:
            toks.extend(stem(s) for s in SYNONYMS[t].split())
    return toks


def _strip_function_words(phrase: str) -> str:
    """'If My Shadow King' -> 'Shadow King'; 'Can I' -> ''."""
    words = phrase.split()
    while words and (words[0].lower().strip(",") in STOPWORDS or words[0] in ("I", "I'm", "I’m")):
        words.pop(0)
    while words and (words[-1].lower().strip(",") in STOPWORDS or words[-1] == "I"):
        words.pop()
    return " ".join(words).strip(", ")


RULE_REF = re.compile(r"\b(\d{3}(?:\.(?:\d+|[a-z]))*)\.?(?=[\s,;:)\]]|$)")


def norm_rule(num: str) -> str:
    return num.strip().rstrip(".").lower()


@dataclass
class Retrieval:
    query: str
    core: list[dict] = field(default_factory=list)
    other: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    ambiguous_names: dict[str, list[str]] = field(default_factory=dict)
    unrecognized_names: list[str] = field(default_factory=list)
    top_score: float = 0.0

    @property
    def passages(self) -> list[dict]:
        return self.core + self.other


class KnowledgeBase:
    def __init__(self, passages: list[dict], cards: list[dict], manifest: dict):
        self.passages = passages
        self.cards = cards
        self.manifest = manifest
        self.by_id = {p["id"]: p for p in passages}
        # rule number -> (passage id, page) for both PDFs, keyed by source kind
        self.rule_index: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)
        for p in passages:
            for num, page in (p.get("rule_pages") or {}).items():
                self.rule_index[p["source_kind"]][norm_rule(num)] = (p["id"], page)
        self._build_bm25()
        self._build_card_index()

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls) -> "KnowledgeBase":
        if not PASSAGES_PATH.exists():
            raise FileNotFoundError("No rules index found. Run: python -m riftbound.refresh")
        passages = [json.loads(l) for l in PASSAGES_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        cards = json.loads(CARDS_PATH.read_text(encoding="utf-8")) if CARDS_PATH.exists() else []
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return cls(passages, cards, manifest)

    # ------------------------------------------------------------------ BM25

    def _build_bm25(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.doc_tf: list[Counter] = []
        df: Counter = Counter()
        for p in self.passages:
            toks = tokenize(p["text"]) + tokenize(f"{p['title']} {p.get('section', '')}") * 2
            tf = Counter(toks)
            self.doc_tf.append(tf)
            df.update(tf.keys())
        self.doc_len = [sum(tf.values()) for tf in self.doc_tf]
        self.avg_len = sum(self.doc_len) / max(1, len(self.doc_len))
        n = len(self.passages)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        # Rule titles such as "Hidden", "Scoring", "Step 2: The Combat Damage Step".
        self.title_tokens = [set(tokenize(re.sub(r"^\d{3}\.\s*", "", p["title"]))) for p in self.passages]

    def _bm25(self, q_tokens: list[str]) -> list[float]:
        scores = [0.0] * len(self.passages)
        q = Counter(q_tokens)
        for i, tf in enumerate(self.doc_tf):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avg_len)
            for t, qf in q.items():
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + norm) * (1 + 0.3 * (qf - 1))
            scores[i] = s
        return scores

    # ------------------------------------------------------------------ cards

    def _build_card_index(self) -> None:
        rules_lower = Counter(
            w for p in self.passages if p["source_kind"] == "core_rules" for w in re.findall(r"\b[a-z][a-z'-]+\b", p["text"])
        )
        self.rules_text = " ".join(
            norm_name(p["text"]) for p in self.passages if p["source_kind"] in ("core_rules", "tournament_rules")
        )
        self.card_by_key: dict[str, dict] = {}
        self.multi_word: list[tuple[str, dict]] = []
        self.single_word: dict[str, list[dict]] = defaultdict(list)
        for c in self.cards:
            self.card_by_key[norm_name(c["name"])] = c
            for nm in {c["name"], *c.get("aliases", [])}:
                key = norm_name(nm).replace(",", "")
                if " " in key or len(key) > 12:
                    self.multi_word.append((key, c))
            bases = {norm_name(c["base_name"])}
            if "Legend" in c["types"] and c.get("tags"):
                bases.add(norm_name(c["tags"][0]))  # "Ahri" can also mean the Ahri legend
            for base in bases:
                # Single-word names that are ordinary rules words ("counter", "shield") are
                # skipped, to avoid treating rules vocabulary as card names.
                if " " not in base and rules_lower.get(base, 0) < 2:
                    self.single_word[base].append(c)
        self.multi_word.sort(key=lambda kc: -len(kc[0]))

    def find_cards(self, text: str) -> tuple[list[dict], dict[str, list[str]], list[str]]:
        """Return (matched cards, ambiguous champion names -> candidates, unrecognized card-like names)."""
        hay = " " + norm_name(text).replace(",", "") + " "
        found: dict[str, dict] = {}
        consumed = hay
        for key, card in self.multi_word:
            pat = rf"(?<![a-z0-9']){re.escape(key)}(?![a-z0-9'])"
            if re.search(pat, consumed):
                found[card["name"]] = card
                consumed = re.sub(pat, " ", consumed)

        # Capitalised phrases ("Shadow Mirage King") and quoted names are card candidates.
        # A single-word card name only matches when it is the whole phrase, so "Shadow"
        # does not match inside "Shadow Mirage King".
        phrases = re.findall(r"[A-Z][\w'’!-]*(?:,? (?:of |the |a )*[A-Z][\w'’!-]*)*", text)
        quoted = re.findall(r"[\"“]([^\"”]{2,40})[\"”]", text)
        ambiguous: dict[str, list[str]] = {}
        unrecognized: list[str] = []
        for phrase in dict.fromkeys(quoted + [_strip_function_words(p) for p in phrases]):
            key = norm_name(phrase).replace(",", "")
            if not key or not re.search(rf"(?<![a-z0-9']){re.escape(key)}(?![a-z0-9'])", consumed):
                continue  # already consumed by a full card-name match
            cands = [c for c in self.single_word.get(key, []) if c["name"] not in found]
            if any(norm_name(f["base_name"]) == key for f in found.values()):
                continue  # already resolved by a full name like "Neeko, Blending In"
            if len(cands) == 1:
                found[cands[0]["name"]] = cands[0]
            elif len(cands) > 1:
                ambiguous[phrase] = sorted(c["name"] for c in cands)
            elif (" " in key or phrase in quoted) and key not in self.rules_text and key not in self.card_by_key:
                unrecognized.append(phrase)
        return list(found.values()), ambiguous, unrecognized

    @staticmethod
    def card_summary(c: dict) -> str:
        stats = ", ".join(
            f"{k} {c[v]}" for k, v in (("Energy", "energy"), ("Might", "might"), ("Power", "power"), ("Might bonus", "might_bonus")) if c.get(v)
        )
        lines = [f"{c['name']} ({'/'.join(c['types'])}; {', '.join(c['domains']) or 'no domain'}{'; ' + stats if stats else ''})"]
        if c.get("tags"):
            lines.append(f"Tags: {', '.join(c['tags'])}")
        lines.append(f"Current official text: {c['current_text'] or '(no rules text)'}")
        if c.get("effect_text"):
            lines.append(f"Effect text (applies while attached): {c['effect_text']}")
        if c.get("errata"):
            e = c["errata"]
            lines.append(f"Errata: this wording comes from {e['source_title']} ({e['date']}); the printed text was: {e['old_text']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ search

    def retrieve(self, question: str, extra_text: str = "", k_core: int = TOP_K_CORE, k_other: int = TOP_K_OTHER) -> Retrieval:
        cards, ambiguous, unrecognized = self.find_cards(question)
        query = question + " " + extra_text
        # Keywords printed on the matched cards ("[Hidden]", "[Deflect]") steer retrieval.
        for c in cards:
            query += " " + " ".join(re.findall(r"\[([A-Za-z -]+)\]", c["current_text"]))
        q_tokens = tokenize(query, expand=True)
        scores = self._bm25(q_tokens)
        q_set = set(q_tokens)
        card_names = [norm_name(c["name"]) for c in cards]
        for i, p in enumerate(self.passages):
            title = self.title_tokens[i]
            if title and title <= q_set and len(title) <= 3:
                scores[i] *= 1.6
            if p["source_kind"] in ("faq", "patch_notes", "errata") and card_names:
                text = norm_name(p["text"])
                if any(n in text for n in card_names):
                    scores[i] += 6.0

        # Exact rule numbers mentioned in the question are always included.
        pinned: list[int] = []
        for m in RULE_REF.finditer(question):
            num = norm_rule(m.group(1))
            hit = self.rule_index["core_rules"].get(num)
            if hit:
                pinned.append(next(i for i, p in enumerate(self.passages) if p["id"] == hit[0]))

        ranked = sorted(range(len(self.passages)), key=lambda i: -scores[i])
        core, other = [], []
        for i in pinned + ranked:
            p = self.passages[i]
            if scores[i] <= 0 and i not in pinned:
                break
            hit = {**p, "score": round(scores[i], 2)}
            if p["source_kind"] == "core_rules":
                if len(core) < k_core and all(h["id"] != p["id"] for h in core):
                    core.append(hit)
            elif len(other) < k_other and all(h["id"] != p["id"] for h in other):
                other.append(hit)
            if len(core) >= k_core and len(other) >= k_other:
                break
        top = scores[ranked[0]] if ranked else 0.0
        return Retrieval(question, core, other, cards[:6], ambiguous, unrecognized, round(top, 2))

    # ------------------------------------------------------------------ helpers

    def rule_link(self, kind: str, number: str) -> str | None:
        hit = self.rule_index[kind].get(norm_rule(number))
        if not hit:
            return None
        url = self.by_id[hit[0]]["url"]
        return f"{url}#page={hit[1]}"

    def rule_exists(self, kind: str, number: str) -> bool:
        return norm_rule(number) in self.rule_index[kind]

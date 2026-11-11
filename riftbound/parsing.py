"""Turn official Riot documents into indexed passages.

Every passage keeps the exact rule numbers that appear in its text, the PDF page it
came from, and a link back to the official source, so the app can cite precisely.
"""

from __future__ import annotations

import html as html_lib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from pypdf import PdfReader

# "467.2.c.4.a. Text..." -> number "467.2.c.4.a", text "Text..."
RULE_LINE = re.compile(r"^(\d{3}(?:\.(?:\d+|[a-z]))*)\.\s+(.*)$")
MAX_CHUNK_CHARS = 2400


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


# ---------------------------------------------------------------- PDF rules documents


@dataclass
class RuleEntry:
    number: str
    text: str
    page: int
    lines: list[str] = field(default_factory=list)

    @property
    def top(self) -> str:
        return self.number.split(".")[0]

    @property
    def depth(self) -> int:
        return self.number.count(".")


def pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Extract page text. Falls back to layout mode for PDFs that extract one word per line."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [p.extract_text() or "" for p in reader.pages]
    lines = [l for p in pages for l in p.splitlines() if l.strip()]
    avg = sum(len(l) for l in lines) / max(1, len(lines))
    if avg < 15:
        pages = [p.extract_text(extraction_mode="layout") or "" for p in reader.pages]
    return pages


def parse_last_updated(first_page: str) -> str | None:
    """Read 'Last Updated: 2026-10-09' or 'Last Updated October 9, 2026' from a PDF header."""
    m = re.search(r"Last\s+Updated:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[A-Z][a-z]+ \d{1,2}, \d{4})", first_page)
    if not m:
        return None
    raw = m.group(1)
    if re.match(r"\d{4}-", raw):
        return raw
    from datetime import datetime

    return datetime.strptime(raw, "%B %d, %Y").date().isoformat()


def parse_numbered_rules(pages: list[str]) -> list[RuleEntry]:
    """Split a numbered rules PDF into entries; continuation lines attach to the previous rule."""
    entries: list[RuleEntry] = []
    last_top = -1
    for page_no, page in enumerate(pages, start=1):
        for raw in page.splitlines():
            line = clean(raw)
            if not line:
                continue
            m = RULE_LINE.match(line)
            if m:
                top = int(m.group(1).split(".")[0])
                # Guard against a wrapped line that happens to start with "123. ".
                if top >= last_top and (top - last_top < 200 or last_top < 0):
                    last_top = top
                    entries.append(RuleEntry(m.group(1), m.group(2), page_no, [line]))
                    continue
            if entries:
                entries[-1].lines.append(line)
                entries[-1].text += "\n" + line
    return entries


def _is_heading(entry: RuleEntry, has_children: bool) -> bool:
    return (
        entry.depth == 0
        and not has_children
        and len(entry.text) < 70
        and not entry.text.rstrip().endswith((".", ":", ")", "”", '"'))
    )


def chunk_rules(entries: list[RuleEntry], meta: dict, prefix: str) -> list[dict]:
    """Group entries into passages: one per top-level rule plus its sub-rules, split if long.

    meta: {source_kind, source_title, url, doc_version, date}
    """
    groups: dict[str, list[RuleEntry]] = {}
    order: list[str] = []
    for e in entries:
        if e.top not in groups:
            groups[e.top] = []
            order.append(e.top)
        groups[e.top].append(e)

    passages: list[dict] = []
    heading = ""
    pending_headings: list[RuleEntry] = []
    for top in order:
        group = groups[top]
        head = group[0]
        if _is_heading(head, len(group) > 1):
            heading = head.text.strip()
            pending_headings.append(head)
            continue
        context = pending_headings
        context_lines = [h.lines[0] for h in context]
        pending_headings = []
        group_size = sum(len(l) for e in group for l in e.lines)
        prev = passages[-1] if passages else None
        # Fold very short rules ("116. Players each draw 4.") into the previous passage
        # of the same section so they are retrieved with their neighbours.
        if (
            prev is not None
            and group_size < 300
            and not context_lines
            and prev["section"] == heading
            and len(prev["text"]) + group_size < MAX_CHUNK_CHARS // 2
        ):
            prev["text"] += "\n" + "\n".join(l for e in group for l in e.lines)
            prev["rule_ids"].extend(e.number for e in group)
            prev["rule_pages"].update({e.number: e.page for e in group})
            continue
        pieces: list[list[RuleEntry]] = [[]]
        size = 0
        for e in group:
            block = sum(len(l) for l in e.lines)
            over = pieces[-1] and size + block > MAX_CHUNK_CHARS
            # Prefer to split between major sub-rules; split anywhere if a chunk runs very long.
            if over and (e.depth <= 2 or size > MAX_CHUNK_CHARS * 1.5):
                pieces.append([])
                size = 0
            pieces[-1].append(e)
            size += block
        for i, piece in enumerate(pieces):
            lines = []
            if i == 0:
                lines.extend(context_lines)
            else:
                lines.append(f"{head.lines[0]} (continued)")
            for e in piece:
                lines.extend(e.lines)
            first = piece[0]
            # Heading rules ("001. Golden Rule") are real rule numbers too, so index them
            # with the passage they introduce.
            listed = (context if i == 0 else []) + piece
            pid = f"{prefix}-{top}" if len(pieces) == 1 else f"{prefix}-{top}#{i + 1}"
            passages.append(
                {
                    **meta,
                    "id": pid,
                    "title": f"{head.number}. {head.text.splitlines()[0].strip()}",
                    "section": heading,
                    "rule_ids": [e.number for e in listed],
                    "rule_pages": {e.number: e.page for e in listed},
                    "page": first.page,
                    "link": f"{meta['url']}#page={first.page}",
                    "text": "\n".join(lines),
                }
            )
    return passages


# ---------------------------------------------------------------- HTML articles


def article_blocks(page_html: str) -> dict:
    """Return {title, date, blocks:[(tag, text)]} for an official news/article page."""
    soup = BeautifulSoup(page_html, "html.parser")
    title_el = soup.find(attrs={"data-testid": "title"})
    title = clean(title_el.get_text(" ", strip=True)) if title_el else ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}", page_html)
    date = m.group(1) if m else None

    wanted = {"h1", "h2", "h3", "h4", "p", "li"}
    blocks: list[tuple[str, str]] = []
    started = title_el is None
    for el in soup.find_all(wanted):
        if not started:
            started = el is title_el
            continue
        if any(parent.name in {"p", "li"} for parent in el.parents):
            continue
        # A list item that contains paragraphs is emitted via its paragraphs.
        if el.name == "li" and el.find("p"):
            continue
        for br in el.find_all("br"):
            br.replace_with("\n")
        text = "\n".join(clean(t) for t in el.get_text().split("\n") if clean(t))
        if not text:
            continue
        if text == "Related Articles":
            break
        bold = el.name == "p" and el.find(["strong", "b"]) is not None and clean(
            "".join(s.get_text() for s in el.find_all(["strong", "b"]))
        ) == clean(el.get_text())
        tag = el.name if el.name.startswith("h") else ("pb" if bold else el.name)
        blocks.append((tag, text))
    return {"title": title, "date": date, "blocks": blocks}


def _is_question(tag: str, text: str) -> bool:
    return text.rstrip().endswith("?") and (tag == "pb" or len(text) < 400)


def article_passages(article: dict, meta: dict, prefix: str) -> list[dict]:
    """Split a patch-notes/FAQ article into Q&A or heading-sized passages."""
    passages: list[dict] = []
    heading = ""
    cur_title = article["title"]
    cur_lines: list[str] = []

    def flush():
        text = "\n".join(cur_lines).strip()
        if len(text) > 40:
            n = len(passages) + 1
            rule_refs = sorted(set(re.findall(r"\b\d{3}(?:\.(?:\d+|[a-z]))+\b|\b\d{3}\.(?=\s)", text)))
            passages.append(
                {
                    **meta,
                    "id": f"{prefix}#{n}",
                    "title": cur_title[:160],
                    "section": heading,
                    "rule_ids": [],
                    "mentioned_rules": rule_refs,
                    "page": None,
                    "link": meta["url"],
                    "text": text,
                }
            )

    for tag, text in article["blocks"]:
        if tag.startswith("h"):
            flush()
            heading = text
            cur_title, cur_lines = text, [text]
            continue
        if _is_question(tag, text):
            flush()
            cur_title, cur_lines = text, [f"Q: {text}"]
            continue
        if sum(len(l) for l in cur_lines) + len(text) > MAX_CHUNK_CHARS:
            flush()
            cur_lines = [f"{cur_title} (continued)"]
        cur_lines.append(text)
    flush()
    return passages


def parse_errata(article: dict) -> list[dict]:
    """Extract {card, new_text, old_text} entries from an errata article.

    Two layouts exist: "[NEW TEXT] ... ▲ [OLD TEXT] ..." and (Origins) "card heading,
    new text, ▲, old text". In both, a card heading is followed by the new wording and
    a ▲ / [OLD TEXT] marker separates it from the old wording.
    """
    out: list[dict] = []
    card = None
    mode = None
    new: list[str] = []
    old: list[str] = []

    def flush():
        if card and new and mode == "old":
            out.append({"card": card, "new_text": "\n".join(new).strip(), "old_text": "\n".join(old).strip()})

    for tag, text in article["blocks"]:
        has_arrow = "▲" in text
        t = text.replace("▲", "").strip()
        upper = t.upper()
        if "[NEW TEXT]" in upper:
            mode = "new"
            t = re.sub(r"\[NEW TEXT\]", "", t, flags=re.I).strip()
        elif "[OLD TEXT]" in upper or has_arrow:
            mode = "old"
            t = re.sub(r"\[OLD TEXT\]", "", t, flags=re.I).strip()
        elif tag.startswith("h"):
            flush()
            card, mode, new, old = t, "new", [], []
            continue
        if not t:
            continue
        if mode == "new":
            new.append(t)
        elif mode == "old":
            old.append(t)
    flush()
    return out


def legality_passage(hub_html: str, url: str, date: str | None) -> dict | None:
    """The Rules Hub's ban lists: format legality, which is separate from gameplay rules."""
    art = article_blocks(hub_html)
    lines: list[str] = []
    capture = False
    for tag, text in art["blocks"]:
        if "Format Legality" in text or "Constructed Legality" in text:
            capture = True
        elif tag.startswith("h") and text.strip() == "Core Rules":
            break
        if capture:
            lines.append(text)
    if not lines:
        return None
    updated = parse_last_updated("\n".join(lines).replace("Last updated", "Last Updated"))
    date = updated or date
    return {
        "source_kind": "legality",
        "source_title": "Rules Hub: Constructed Format Legality",
        "url": url,
        "doc_version": None,
        "date": date,
        "id": "HUB-legality",
        "title": "Sanctioned format legality and banned cards",
        "section": "Tournament / format legality (not gameplay rules)",
        "rule_ids": [],
        "mentioned_rules": [],
        "page": None,
        "link": url,
        "text": "\n".join(lines),
    }


# ---------------------------------------------------------------- card gallery

SYMBOLS = [
    (r":rb_might:", "[M]"),
    (r":rb_rune_rainbow:", "[A]"),
    (r":rb_exhaust:", "[E]"),
    (r":rb_energy_(\d+):", r"[\1]"),
    (r":rb_rune_([a-z]+):", lambda m: f"[{m.group(1).title()}]"),
]


def card_text_from_html(body: str) -> str:
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"</p>\s*<p>", "\n", body)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", body))
    for pat, rep in SYMBOLS:
        text = re.sub(pat, rep, text)
    return "\n".join(clean(l) for l in text.splitlines() if clean(l))


def parse_card_gallery(gallery_html: str) -> list[dict]:
    """Read the card list embedded in the official Card Gallery page (__NEXT_DATA__ JSON)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', gallery_html, re.S)
    if not m:
        raise ValueError("Card Gallery page format changed: __NEXT_DATA__ not found")
    data = json.loads(m.group(1))
    blades = data["props"]["pageProps"]["page"]["blades"]
    gallery = next(b for b in blades if b.get("fragmentId") == "card-gallery")
    items = gallery["cards"]["items"]
    # Champion names are the names of Champion units ("Jinx" from "Jinx, Rebel"). A legend's or
    # signature card's champion tag is the tag that is one of these names; region and tribe tags
    # such as "Zaun", "Dog" or "Yordle" are not.
    champion_names = {
        c["name"].strip()
        for c in items
        if any(s["label"] == "Champion" for s in c["cardType"].get("superType") or [])
        and any(t["label"] == "Unit" for t in c["cardType"]["type"])
    }
    cards = []
    for c in items:
        name = c["name"].strip()
        types = [t["label"] for t in c["cardType"]["type"]]
        supertypes = [s["label"] for s in c["cardType"].get("superType") or []]
        tags = c.get("tags", {}).get("tags", [])
        subtitle = (c.get("subtitle") or "").strip()
        champion_tag = _champion_tag(name, subtitle, types, supertypes, tags, champion_names)
        aliases = []
        if subtitle:
            full = f"{name}, {subtitle}"
            if "Legend" in types and champion_tag:
                # The banned list calls the "Wuju Bladesman, Starter" legend "Master Yi, Wuju Bladesman".
                aliases.append(f"{champion_tag}, {name}")
        elif "Legend" in types and tags:
            # The gallery stores legends as "Deceiver" with tag "LeBlanc"; players and
            # errata call it "LeBlanc, Deceiver".
            full = f"{champion_tag or tags[0]}, {name}"
            aliases.append(name)
        else:
            full = name
        text = card_text_from_html(c["text"]["richText"]["body"]) if c.get("text") else ""
        effect = card_text_from_html(c["effect"]["richText"]["body"]) if c.get("effect") else ""

        def val(key):
            v = c.get(key)
            return v["value"]["label"] if v and v.get("value") else None

        cards.append(
            {
                "name": full,
                "base_name": name,
                "aliases": aliases,
                "code": c.get("publicCode"),
                "set": c["set"]["value"]["label"],
                "types": types,
                "supertypes": supertypes,
                "champion_tag": champion_tag,
                "domains": [d["label"] for d in c.get("domain", {}).get("values", [])],
                "tags": tags,
                "energy": val("energy"),
                "might": val("might"),
                "power": val("power"),
                "might_bonus": val("mightBonus"),
                "printed_text": text,
                "effect_text": effect,
                "image": c.get("cardImage", {}).get("url"),
            }
        )
    return cards


def _champion_tag(name, subtitle, types, supertypes, tags, champion_names) -> str | None:
    """The champion a card belongs to for deck building (CR 103.3.a.2, 103.3.d), or None."""
    if "Champion" in supertypes and "Unit" in types:
        return name if name in champion_names else next((t for t in tags if t in champion_names), None)
    if "Legend" in types or "Signature" in supertypes:
        if subtitle in champion_names:
            return subtitle  # "Tibbers, Annie", "Fox-Fire, Ahri"
        return next((t for t in tags if t in champion_names), None)
    return None


def norm_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).lower().replace("’", "'")
    return re.sub(r"[^a-z0-9', ]+", "", name).strip()


def merge_cards(gallery_cards: list[dict], errata: list[dict]) -> list[dict]:
    """Collapse printings/alt-arts to one record per card name and apply official errata.

    `errata` must be ordered oldest article first, so a later errata wins.
    """
    by_name: dict[str, dict] = {}
    for c in gallery_cards:
        key = norm_name(c["name"])
        rec = by_name.get(key)
        if rec is None:
            rec = {**c, "codes": [c["code"]], "sets": [c["set"]]}
            del rec["code"]
            rec["current_text"] = c["printed_text"]
            rec["errata"] = None
            by_name[key] = rec
        else:
            rec["codes"].append(c["code"])
            if c["set"] not in rec["sets"]:
                rec["sets"].append(c["set"])
            # Printings occasionally disagree (one Karma printing is tagged "Vi"), so keep the union.
            for field in ("tags", "supertypes", "aliases"):
                rec[field] = list(dict.fromkeys(rec.get(field, []) + c.get(field, [])))
            rec["champion_tag"] = rec.get("champion_tag") or c.get("champion_tag")
    base_index: dict[str, list[str]] = {}
    for key, rec in by_name.items():
        for alias in {rec["base_name"], *rec.get("aliases", [])}:
            base_index.setdefault(norm_name(alias), []).append(key)

    for e in errata:
        key = find_card_key(e["card"], by_name, base_index)
        e["matched"] = key is not None
        if key:
            rec = by_name[key]
            rec["current_text"] = e["new_text"]
            rec["errata"] = {"source_title": e["source_title"], "url": e["url"], "date": e["date"], "old_text": e["old_text"]}
    return sorted(by_name.values(), key=lambda r: r["name"])


def find_card_key(name: str, by_name: dict, base_index: dict) -> str | None:
    key = norm_name(name)
    if key in by_name:
        return key
    if len(base_index.get(key, [])) == 1:
        return base_index[key][0]
    return None

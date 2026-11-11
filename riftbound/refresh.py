"""Download the current official Riftbound rules sources and rebuild the local index.

Usage:
    python -m riftbound.refresh            # fetch everything from Riot and rebuild data/
    python -m riftbound.refresh --offline  # rebuild from files already in data/raw/

Outputs (all under data/):
    manifest.json   document titles, versions, effective dates, URLs, hashes, fetch time
    passages.jsonl  retrievable passages (Core Rules, Tournament Rules, patch notes, FAQs, errata, legality)
    cards.json      official card text from the Card Gallery with errata applied
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone

from . import parsing
from .config import CARDS_PATH, DATA_DIR, MANIFEST_PATH, PASSAGES_PATH, RAW_DIR
from .sources import RULES_HUB_URL, RULES_NEWS_URL, Source, discover_sources, fetch

PREFIX = {"patch_notes": "PN", "faq": "FAQ", "errata": "ERR"}


def _slug(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", tail)[:80]


def _get(url: str, name: str, offline: bool) -> bytes:
    path = RAW_DIR / name
    if offline:
        if not path.exists():
            raise FileNotFoundError(f"--offline but {path} is missing; run a normal refresh first")
        return path.read_bytes()
    data = fetch(url).content
    path.write_bytes(data)
    return data


def _effective_date(patch_titles: list[tuple[str | None, str]]) -> tuple[str | None, str | None]:
    """Look for '(Effective October 16, 2026)' in the newest patch-notes title."""
    for date, title in sorted(patch_titles, key=lambda x: x[0] or "", reverse=True):
        m = re.search(r"Effective\s+([A-Z][a-z]+ \d{1,2}, \d{4})", title)
        if m:
            return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat(), title
    return None, None


def refresh(offline: bool = False, log=print) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    old_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else None

    log(f"Rules Hub: {RULES_HUB_URL}")
    hub_html = _get(RULES_HUB_URL, "rules-hub.html", offline).decode("utf-8", "replace")
    news_html = _get(RULES_NEWS_URL, "rules-and-releases.html", offline).decode("utf-8", "replace")
    sources = discover_sources(hub_html, news_html)
    for kind in ("core_rules", "tournament_rules"):
        if not any(s.kind == kind for s in sources):
            raise RuntimeError(f"Could not find a {kind} PDF link on the Rules Hub; the page layout may have changed.")

    passages: list[dict] = []
    documents: list[dict] = []
    errata_entries: list[dict] = []
    patch_titles: list[tuple[str | None, str]] = []
    gallery_cards: list[dict] = []
    hub_date = parsing.article_blocks(hub_html)["date"]

    for src in sources:
        log(f"  [{src.kind}] {src.title}: {src.url}")
        if src.kind in ("core_rules", "tournament_rules"):
            raw = _get(src.url, f"{src.kind}.pdf", offline)
            pages = parsing.pdf_pages(raw)
            version = parsing.parse_last_updated(pages[0])
            entries = parsing.parse_numbered_rules(pages)
            title = "Riftbound Core Rules" if src.kind == "core_rules" else "Riftbound Tournament Rules"
            meta = {"source_kind": src.kind, "source_title": title, "url": src.url, "doc_version": version, "date": version}
            chunked = parsing.chunk_rules(entries, meta, "CR" if src.kind == "core_rules" else "TR")
            passages.extend(chunked)
            documents.append(
                {
                    **src.to_dict(),
                    "title": title,
                    "last_updated": version,
                    "pages": len(pages),
                    "rules_indexed": len(entries),
                    "passages": len(chunked),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        elif src.kind == "legality":
            p = parsing.legality_passage(hub_html, src.url, hub_date)
            if p:
                passages.append(p)
                documents.append({**src.to_dict(), "date": p["date"], "passages": 1})
        elif src.kind == "cards":
            raw = _get(src.url, "card-gallery.html", offline)
            gallery_cards = parsing.parse_card_gallery(raw.decode("utf-8", "replace"))
            documents.append({**src.to_dict(), "printings": len(gallery_cards)})
        else:
            raw = _get(src.url, f"{src.kind}-{_slug(src.url)}.html", offline)
            art = parsing.article_blocks(raw.decode("utf-8", "replace"))
            title = art["title"] or src.title
            meta = {"source_kind": src.kind, "source_title": title, "url": src.url, "doc_version": None, "date": art["date"]}
            arts = parsing.article_passages(art, meta, f"{PREFIX[src.kind]}-{_slug(src.url)}")
            passages.extend(arts)
            if src.kind == "patch_notes":
                patch_titles.append((art["date"], title))
            if src.kind == "errata":
                for e in parsing.parse_errata(art):
                    errata_entries.append({**e, "source_title": title, "url": src.url, "date": art["date"]})
            documents.append({**src.to_dict(), "title": title, "date": art["date"], "passages": len(arts)})

    errata_entries.sort(key=lambda e: e["date"] or "")
    cards = parsing.merge_cards(gallery_cards, errata_entries)

    core = next(d for d in documents if d["kind"] == "core_rules")
    effective, effective_src = _effective_date(patch_titles)
    if effective and core["last_updated"] and effective < core["last_updated"]:
        effective, effective_src = None, None  # that patch note describes an older version
    manifest = {
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rules_hub": RULES_HUB_URL,
        "core_rules": {
            "title": core["title"],
            "url": core["url"],
            "last_updated": core["last_updated"],
            "effective_date": effective or core["last_updated"],
            "effective_date_source": effective_src or "Core Rules 'Last Updated' header (no newer effective date announced)",
            "rules_indexed": core["rules_indexed"],
        },
        "documents": documents,
        "counts": {
            "passages": len(passages),
            "cards": len(cards),
            "cards_with_errata": sum(1 for c in cards if c["errata"]),
            "errata_entries": len(errata_entries),
            "unmatched_errata": sorted({e["card"] for e in errata_entries if not e.get("matched")}),
        },
    }

    DATA_DIR.mkdir(exist_ok=True)
    with PASSAGES_PATH.open("w", encoding="utf-8") as f:
        for p in passages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    CARDS_PATH.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    log("")
    log(f"Core Rules last updated {core['last_updated']}, effective {manifest['core_rules']['effective_date']}")
    log(f"{len(passages)} passages, {len(cards)} cards ({manifest['counts']['cards_with_errata']} with errata)")
    if manifest["counts"]["unmatched_errata"]:
        log(f"WARNING: errata not matched to a gallery card: {manifest['counts']['unmatched_errata']}")
    if old_manifest:
        before = old_manifest["core_rules"]["last_updated"]
        after = core["last_updated"]
        log("Core Rules unchanged." if before == after else f"Core Rules CHANGED: {before} -> {after}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="re-parse data/raw/ without downloading")
    args = ap.parse_args(argv)
    try:
        refresh(offline=args.offline)
    except Exception as exc:  # surface a readable message for students running the CLI
        print(f"Refresh failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

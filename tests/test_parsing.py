from riftbound import parsing


def test_numbered_rules_and_continuations():
    pages = [
        "Riftbound Core Rules\nLast Updated: 2026-10-09\n000. Golden and Silver Rules\n001. Golden Rule\n"
        "002. Card text supersedes rules text. Whenever a card fundamentally contradicts the rules, the card's indication is\n"
        "what is true.\n",
        "103.2.c.3. Units must have lethal damage assigned to them in full before damage is assigned\n"
        "to a different Unit.\nExample: If a player has 5 damage...\n",
    ]
    entries = parsing.parse_numbered_rules(pages)
    assert [e.number for e in entries] == ["000", "001", "002", "103.2.c.3"]
    assert "what is true." in entries[2].text
    assert entries[3].page == 2 and "Example:" in entries[3].text
    assert parsing.parse_last_updated(pages[0]) == "2026-10-09"
    assert parsing.parse_last_updated("Riftbound Tournament Rules\n Last Updated October 9, 2026") == "2026-10-09"


def test_headings_are_indexed_with_their_passage():
    pages = ["000. Golden and Silver Rules\n001. Golden Rule\n002. Card text supersedes rules text.\n"]
    meta = {"source_kind": "core_rules", "source_title": "Core", "url": "u.pdf", "doc_version": "v", "date": "v"}
    passages = parsing.chunk_rules(parsing.parse_numbered_rules(pages), meta, "CR")
    assert len(passages) == 1
    assert passages[0]["rule_ids"] == ["000", "001", "002"]
    assert passages[0]["link"] == "u.pdf#page=1"


def test_errata_both_layouts():
    tagged = {
        "blocks": [
            ("h1", "Radiance Cards"),
            ("h2", "Elnuk Herd"),
            ("pb", "[NEW TEXT]"),
            ("p", "When you play me, give a friendly unit +3 [M] this turn."),
            ("p", "▲\n[OLD TEXT]"),
            ("p", "When you play me, give another friendly unit +3 [M] this turn."),
            # Origins layout: heading, new text, a lone ▲, old text
            ("h2", "Dark Child, Starter"),
            ("p", "At the end of your turn, ready up to 2 runes."),
            ("h4", "▲"),
            ("p", "At the end of your turn, ready 2 runes."),
        ]
    }
    errata = parsing.parse_errata(tagged)
    assert [e["card"] for e in errata] == ["Elnuk Herd", "Dark Child, Starter"]
    assert errata[0]["new_text"].startswith("When you play me, give a friendly")
    assert "another" in errata[0]["old_text"]
    assert errata[1]["new_text"] == "At the end of your turn, ready up to 2 runes."


def test_intro_paragraphs_are_not_errata():
    blocks = {"blocks": [("h2", "Unleashed Errata Updates"), ("p", "Hey Riftbounders! Some intro text.")]}
    assert parsing.parse_errata(blocks) == []


def test_card_symbols_normalized():
    body = "<p>[Hidden] (Hide now for :rb_rune_rainbow: to react with later for :rb_energy_0:.)<br />+3 :rb_might: and :rb_exhaust: pay :rb_rune_fury:</p>"
    assert parsing.card_text_from_html(body) == "[Hidden] (Hide now for [A] to react with later for [0].)\n+3 [M] and [E] pay [Fury]"


def test_errata_overrides_printed_text_and_legend_names():
    gallery = [
        {"name": "Elnuk Herd", "base_name": "Elnuk Herd", "aliases": [], "code": "RAD-081", "set": "Radiance",
         "printed_text": "give another friendly unit +3 [M]"},
        {"name": "LeBlanc, Deceiver", "base_name": "Deceiver", "aliases": ["Deceiver"], "code": "UNL-1", "set": "Unleashed",
         "printed_text": "old"},
    ]
    errata = [
        {"card": "Elnuk Herd", "new_text": "give a friendly unit +3 [M]", "old_text": "x", "source_title": "Radiance Errata", "url": "u", "date": "2026-10-09"},
        {"card": "Leblanc, Deceiver", "new_text": "new", "old_text": "old", "source_title": "Unleashed Errata", "url": "u", "date": "2026-04-03"},
    ]
    cards = {c["name"]: c for c in parsing.merge_cards(gallery, errata)}
    assert cards["Elnuk Herd"]["current_text"] == "give a friendly unit +3 [M]"
    assert cards["Elnuk Herd"]["printed_text"] == "give another friendly unit +3 [M]"
    assert cards["LeBlanc, Deceiver"]["current_text"] == "new"
    assert all(e["matched"] for e in errata)


def test_champion_tags_and_supertypes():
    champs = {"Jinx", "Nasus", "Annie"}
    tag = parsing._champion_tag
    assert tag("Jinx", "Rebel", ["Unit"], ["Champion"], ["Jinx", "Zaun"], champs) == "Jinx"
    # Legends: the champion tag, not a tribe tag that happens to come first ("Dog, Curator of the Sands").
    assert tag("Curator of the Sands", "", ["Legend"], [], ["Dog", "Shurima", "Nasus"], champs) == "Nasus"
    assert tag("Tibbers", "Annie", ["Unit"], ["Signature"], ["Annie"], champs) == "Annie"
    assert tag("Hextech Ray", "", ["Spell"], [], ["Piltover"], champs) is None


def test_merge_keeps_union_of_tags_and_supertypes():
    printing = {"name": "Karma, Channeler", "base_name": "Karma", "aliases": [], "set": "Origins", "printed_text": "",
                "supertypes": ["Champion"], "champion_tag": "Karma"}
    cards = parsing.merge_cards([{**printing, "code": "OGN-1", "tags": ["Vi", "Ionia"]},
                                 {**printing, "code": "SFD-2", "set": "Spiritforged", "tags": ["Karma"]}], [])
    assert cards[0]["tags"] == ["Vi", "Ionia", "Karma"] and cards[0]["champion_tag"] == "Karma"
    assert cards[0]["sets"] == ["Origins", "Spiritforged"]

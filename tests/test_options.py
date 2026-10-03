"""Which menu item a request means (surfaces/native/options.py).

The menu used in most of these is the one a Mac's TextEdit Save sheet really showed for its
"Where:" pop-up — names wrapped in invisible bidi isolates, a qualifier on the iCloud folders,
and "Desktop — iCloud" listed twice.
"""

from __future__ import annotations

import pytest
from jarvis.surfaces.native.options import Option, describe, fold, name_of, ordinal, resolve

FSI, PDI = "⁨", "⁩"


def iso(name: str) -> str:
    return f"{FSI}{name}{PDI}"


#: (title, enabled) in menu order, as the Mac listed them (headings greyed out).
WHERE = [
    ("iCloud Library", False), (f"{iso('Desktop')} — iCloud", True), ("iCloud Drive", True),
    (f"{iso('TextEdit')} — iCloud", True), ("Locations", False), ("Macintosh HD", True),
    ("iCloud Drive", True), ("ndinisio", True), ("Google Chrome", True), ("Favourites", False),
    (f"{iso('Desktop')} — iCloud", True), (f"{iso('Documents')} — iCloud", True), ("Projects", True),
    ("School", True), ("Downloads", True),
]


def menu(items=WHERE) -> list[Option]:
    found, heading = [], ""
    for title, enabled in items:
        found.append(Option(element=f"item{len(found) + 1}", title=title, enabled=enabled,
                            index=len(found) + 1, section=heading))
        if not enabled:
            heading = title
    return found


def pick(wanted, items=None, **kw):
    return resolve(menu(items) if items is not None else menu(), wanted, **kw)


def test_folding_sees_through_isolates_case_ellipses_and_trailing_colons():
    assert fold(f"{iso('Desktop')} — iCloud") == "desktop - icloud"
    assert fold("Where:") == fold("where") == "where"
    assert fold("Save…") == fold("save...") == "save"
    assert fold("A‎  B‏") == "a b"


def test_a_qualifier_is_what_follows_a_spaced_dash_or_an_opening_parenthesis():
    assert name_of("desktop — icloud") == "desktop"
    assert name_of("desktop – icloud") == "desktop"
    assert name_of("a4 - portrait") == "a4"
    assert name_of("documents (icloud)") == "documents"
    assert name_of("e-mail") == "e-mail", "a hyphen inside a word is not a qualifier"
    assert name_of("plain") == "plain"


def test_a_name_finds_its_qualified_item_when_only_one_fits():
    for wanted, title in (("Documents", "Documents — iCloud"), ("textedit", "TextEdit — iCloud"),
                          ("Downloads", "Downloads"), ("Macintosh HD", "Macintosh HD")):
        found = pick(wanted)
        assert found.option is not None and fold(found.option.title) == fold(title), wanted
        assert not found.ambiguous


def test_a_name_that_two_items_carry_is_ambiguous_not_guessed():
    found = pick("Desktop")
    assert found.option is None and found.ambiguous
    assert [m.index for m in found.matches] == [2, 11]
    assert found.tier == "by its name"
    assert [m.section for m in found.matches] == ["iCloud Library", "Favourites"], \
        "what sets the two apart: the heading each sits under"


def test_the_full_qualified_label_is_just_as_ambiguous_because_the_labels_are_identical():
    found = pick(f"{iso('Desktop')} — iCloud")
    assert found.ambiguous and [m.index for m in found.matches] == [2, 11]
    assert found.tier == "exactly"
    drive = pick("iCloud Drive")
    assert drive.ambiguous and [m.index for m in drive.matches] == [3, 7]


def test_occurrence_says_which_match_in_menu_order():
    assert pick("Desktop", occurrence=1).option.index == 2
    assert pick("Desktop", occurrence=2).option.index == 11
    assert pick("iCloud Drive", occurrence=2).option.index == 7


def test_an_occurrence_that_does_not_exist_is_reported_as_such_not_clamped():
    found = pick("Desktop", occurrence=3)
    assert found.option is None and found.out_of_range and not found.ambiguous and len(found.matches) == 2
    assert pick("Desktop", occurrence=0).out_of_range


def test_an_occurrence_on_a_unique_name_is_still_just_that_item():
    assert pick("Documents", occurrence=1).option.index == 12
    assert pick("Documents", occurrence=2).out_of_range


def test_an_exact_label_beats_a_qualified_one():
    items = [("Desktop — iCloud", True), ("Desktop", True)]
    found = pick("Desktop", items)
    assert found.option.index == 2 and found.tier == "exactly" and not found.ambiguous


def test_items_that_differ_in_their_qualifier_are_ambiguous_until_the_request_includes_it():
    items = [("Desktop — iCloud", True), ("Desktop — Macintosh HD", True)]
    assert pick("Desktop", items).ambiguous
    assert pick("Desktop — iCloud", items).option.index == 1
    assert pick("desktop - macintosh hd", items).option.index == 2, "the dash style isn't what decides"


def test_a_greyed_out_heading_does_not_compete_with_an_item_that_fits_looser():
    items = [("Desktop", False), ("Desktop — iCloud", True)]
    found = pick("Desktop", items)
    assert found.option.index == 2 and found.option.enabled


def test_a_greyed_out_item_is_returned_only_when_nothing_enabled_fits():
    found = pick("Locations")
    assert found.option.index == 5 and not found.option.enabled
    assert pick("Favourites", [("Favourites", False)]).option.enabled is False


def test_greyed_out_duplicates_do_not_make_an_enabled_item_ambiguous():
    items = [("Save", False), ("Save", True), ("Save As…", True)]
    assert pick("Save", items).option.index == 2


def test_starting_with_and_containing_are_unique_or_refused():
    items = [("Save As…", True), ("Save a Version", True), ("Revert to Saved", True)]
    assert pick("Save", items).ambiguous and pick("Save", items).tier == "starting with it"
    assert pick("version", items).option.index == 2 and pick("version", items).tier == "containing it"
    assert pick("revert", items).option.index == 3
    assert pick("print", items).option is None and not pick("print", items).ambiguous


def test_a_menu_path_keeps_the_first_of_identical_titles_but_a_looser_fit_is_still_refused():
    windows = [("Untitled", True), ("Untitled", True), ("Notes", True)]
    assert pick("Untitled", windows, strict=False).option.index == 1
    assert pick("Untitled", windows).ambiguous
    items = [("Save As…", True), ("Save a Version", True)]
    assert pick("Save", items, strict=False).ambiguous, "'starting with' is never guessed"


def test_an_empty_request_matches_nothing():
    for wanted in ("", "  ", FSI + PDI):
        assert pick(wanted).option is None and not pick(wanted).ambiguous


def test_items_without_a_title_are_never_matched():
    assert pick("x", [("", True)]).option is None


@pytest.mark.parametrize("position,word", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"),
                                           (12, "12th"), (21, "21st"), (112, "112th")])
def test_ordinals(position, word):
    assert ordinal(position) == word


def test_a_candidate_is_described_by_its_heading_or_failing_that_its_place():
    first, second = pick("Desktop").matches
    assert describe(1, first) == "1. “" + f"{iso('Desktop')} — iCloud" + "” (under “iCloud Library”)"
    assert describe(2, Option("x", "Z", True, 9)) == "2. “Z” (item 9 of the menu)"
    assert second.section == "Favourites"

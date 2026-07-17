"""Autocomplete from recent output: the word index and the cycling state."""

from __future__ import annotations

from genericmud.completion import CompletionCycler, OutputWordIndex


def test_words_come_back_newest_first():
    index = OutputWordIndex()
    index.add_line("a goblin arrives")
    index.add_line("a gnome arrives")
    assert index.complete("g") == ["gnome", "goblin"]


def test_reseen_word_moves_to_the_front_without_duplicating():
    index = OutputWordIndex()
    index.add_line("goblin")
    index.add_line("gnome")
    index.add_line("the goblin snarls")
    assert index.complete("g") == ["goblin", "gnome"]


def test_prefix_match_is_case_insensitive_and_skips_the_exact_word():
    index = OutputWordIndex()
    index.add_line("Vaelin arrives from the north")
    assert index.complete("vae") == ["Vaelin"]
    assert index.complete("Vaelin") == []  # already fully typed; nothing to add


def test_short_words_are_not_indexed():
    index = OutputWordIndex()
    index.add_line("go up to it")
    assert index.complete("g") == []  # "go"/"up"/"to"/"it" all under the length floor


def test_index_is_capped():
    index = OutputWordIndex(max_words=3)
    index.add_line("alpha bravo charlie delta")
    assert len(index.complete("")) == 0  # empty prefix never completes
    assert index.complete("a") == []  # "alpha" (oldest of the four) aged out
    assert index.complete("d") == ["delta"]


def test_cycler_wraps_forward_and_backward():
    cycler = CompletionCycler()
    cycler.begin("g", ["gnome", "goblin"])
    assert cycler.next() == "gnome"
    assert cycler.next() == "goblin"
    assert cycler.next() == "gnome"  # wraps
    assert cycler.prev() == "goblin"  # steps back


def test_cycler_reset_ends_the_run():
    cycler = CompletionCycler()
    cycler.begin("g", ["gnome"])
    assert cycler.active
    cycler.reset()
    assert not cycler.active
    assert cycler.next() is None

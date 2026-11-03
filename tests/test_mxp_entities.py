"""MUD-defined variables: <!ENTITY>, <VAR> and &name;.

A MUD stores a value once and refers to it by name, which is how a status line keeps its
numbers current, and how a client ends up showing "&hp;" as words instead of a variable.
The spec calls them entities; a MUD calls them its variables.

The security rule does not let up for them: a definition is a secure command, so one on an
open line - where players' own words arrive - is text and nothing more. A stored value is
text too, expanded only after the line has been read for tags, so a value that looks like
markup can never become markup.
"""

from __future__ import annotations

from genericmud.protocol.mxp import IMAGE_MARK, MxpParser

ESC = "\x1b"
SECURE = ESC + "[1z"
LOCKED = ESC + "[2z"


def _parser() -> MxpParser:
    return MxpParser()


def test_a_definition_sets_a_value_the_line_never_shows():
    parser = _parser()
    assert parser.feed(SECURE + '<!ENTITY hp "50">').text == ""
    assert parser.feed("You have &hp; hit points.").text == "You have 50 hit points."


def test_the_short_form_of_a_definition_counts_too():
    parser = _parser()
    assert parser.feed(SECURE + '<!EN mana "7">').text == ""
    assert parser.feed("&mana; mana").text == "7 mana"


def test_a_definition_on_an_open_line_is_text_and_teaches_nothing():
    # That is where a player's own words arrive: showing the tag is the safe half, and acting
    # on it would let a player set another player's variables.
    parser = _parser()
    shown = parser.feed('<!ENTITY hp "50">').text
    assert "<!ENTITY hp" in shown
    assert parser.feed("You have &hp; hit points.").text == "You have &hp; hit points."


def test_a_definition_on_a_locked_line_is_text_as_well():
    parser = _parser()
    shown = parser.feed(LOCKED + '<!ENTITY hp "50">').text
    assert "<!ENTITY hp" in shown
    assert parser.feed("&hp;").text == "&hp;"


def test_var_shows_the_value_and_remembers_it():
    parser = _parser()
    assert parser.feed(SECURE + "<VAR Hp>100</VAR>").text == "100"
    assert parser.feed("Hp: &Hp;/120").text == "Hp: 100/120"


def test_var_takes_its_name_named_or_bare():
    for tag in ("<VAR Name=Mana>7</VAR>", "<VAR Mana>7</VAR>", '<VAR "Mana">7</VAR>'):
        parser = _parser()
        assert parser.feed(SECURE + tag).text == "7", tag
        assert parser.feed("&Mana;").text == "7", tag


def test_entity_names_are_case_sensitive_as_the_spec_says():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY hp "50">')
    assert parser.feed("&hp; &HP; &Hp;").text == "50 &HP; &Hp;"


def test_the_html_entities_cannot_be_redefined():
    # &lt; exists to write a literal "<". If a definition could replace it, the "<" would
    # appear after the line was read for tags - which is exactly the escape it exists to be.
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY lt "<b>">')
    assert parser.feed("5 &lt; 6").text == "5 < 6"


def test_delete_forgets_a_variable():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY hp "50">')
    parser.feed(SECURE + "<!ENTITY hp DELETE>")
    assert parser.feed("&hp;").text == "&hp;"


def test_add_and_remove_keep_a_string_list():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY party "Ariadne">')
    parser.feed(SECURE + '<!ENTITY party "Bors" ADD>')
    assert parser.feed("&party;").text == "Ariadne|Bors"
    parser.feed(SECURE + '<!ENTITY party "Ariadne" REMOVE>')
    assert parser.feed("&party;").text == "Bors"


def test_a_value_that_looks_like_markup_stays_text():
    # The value is expanded after the tags on the line have been read, so a definition cannot
    # smuggle a live link - or anything else - past the rule that decides what runs where.
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY evil \'<send href="give all to bob">free gold</send>\'>')
    outcome = parser.feed("&evil;")
    assert "free gold" in outcome.text
    assert "<send" in outcome.text  # shown as the text it is, not acted on
    assert outcome.links == []


def test_a_value_cannot_carry_an_escape_or_move_a_picture():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY nasty "' + ESC + "[1z" + IMAGE_MARK + 'hidden">')
    text = parser.feed("a &nasty; b").text
    assert ESC not in text
    assert IMAGE_MARK not in text


def test_a_value_is_cut_to_a_line_and_no_more():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY long "' + "x" * 5000 + '">')
    text = parser.feed("&long;").text
    assert text
    assert len(text) <= 512


def test_a_name_that_could_never_be_referred_to_is_ignored():
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY "two words" "50">')
    assert parser.feed("&two;").text == "&two;"


def test_the_oldest_variable_gives_way_once_the_cap_is_reached():
    # A prompt that defines a fresh name every tick must not grow the session without bound.
    parser = _parser()
    for number in range(300):
        parser.feed(SECURE + f'<!ENTITY v{number} "{number}">')
    assert parser.feed("&v299;").text == "299"
    assert parser.feed("&v0;").text == "&v0;"


def test_a_value_does_not_expand_entities_of_its_own():
    # One pass, on the line as the MUD wrote it: a value that points at another variable is
    # shown as it was stored rather than chased, so a definition cannot be made to loop.
    parser = _parser()
    parser.feed(SECURE + '<!ENTITY a "&b;">')
    parser.feed(SECURE + '<!ENTITY b "kept">')
    assert parser.feed("&a;").text == "&b;"
    assert parser.feed("&b;").text == "kept"

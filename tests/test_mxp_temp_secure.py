"""ESC[4z: the next tag only is secure, wherever on the line it stands.

The maintainer's split (issue #110, 18 November): honour a 4z partway through a line as well as
at the head of one, keep 1z and 5-7 ignored there, and let the tags that make this client ask for
a login, define the variables its links expand into, or put a picture on the screen keep needing a
secure *line* rather than a single marked tag.

At the head of a line nothing changes, and that is pinned here too: a 4z there still promotes the
tag after it, USER and IMAGE included, because the MUD owns the start of the line.
"""

from __future__ import annotations

import time

from genericmud.model.buffer import Link
from genericmud.protocol.mxp import MxpParser

ESC = "\x1b"
SECURE = ESC + "[1z"
LOCKED = ESC + "[2z"
MARK = ESC + "[4z"  # the next tag only is secure


def _feed(line: str, **kwargs) -> object:
    return MxpParser(**kwargs).feed(line)


# --- the head of a line: unchanged, and what the reporter's handshake needs ------------------


def test_a_4z_at_the_head_of_a_locked_line_still_answers_the_version_handshake():
    # The reported bug: 7z locks the line, 4z marks the tag on it, and the marked tag was shown
    # as text because a locked line returned before any tag was read -- so the handshake never
    # completed and none of the marked tags that follow could arrive.
    out = _feed(ESC + "[7z" + MARK + "<VERSION>")
    assert out.text == ""
    assert out.replies == ["<VERSION MXP=1.0 CLIENT=genericMud>"]
    # The tags a server on a locked line marks at the head still work, which is why the head of a
    # line is left as it was.
    assert _feed(ESC + "[7z" + MARK + "<USER>").login == ["user"]
    image = _feed(ESC + "[7z" + MARK + '<IMAGE FName="map.png" T="map">')
    assert [name for name, _url in image.images] == ["map.png"]


def test_the_reporters_two_reads_of_the_handshake():
    parser = MxpParser()
    assert parser.feed(ESC + "[7z").text == ""
    # Read on its own, which is how the reporter's server sends it.
    out = parser.feed(MARK + "<VERSION>")
    assert out.text == ""
    assert out.replies == ["<VERSION MXP=1.0 CLIENT=genericMud>"]


def test_a_locked_line_shows_a_marked_tag_as_markup_and_the_rest_as_typed():
    # One mark is one tag, so both halves of a pair must be marked -- which is exactly what the
    # reporter's server does. The unmarked "<b>" later on is the reader's text, which is the
    # whole reason for a locked line.
    out = _feed(LOCKED + MARK + "<b>bold" + MARK + "</b> and a literal <b> after it")
    assert out.text == "bold and a literal <b> after it"


def test_a_locked_line_keeps_its_entities_as_typed():
    # &lt; on a locked line is the five characters a MUD wanted read, not a "<".
    assert _feed(LOCKED + "press &lt; to continue").text == "press &lt; to continue"
    assert _feed(LOCKED + MARK + "<b>&lt;" + MARK + "</b>").text == "&lt;"


def test_a_marked_tag_at_the_head_still_promotes_user_and_image():
    # Left as it is on purpose: the MUD owns the start of the line there, and a server that locks
    # with 7z and marks every tag with 4z could otherwise never send these at all. One mark is one
    # tag, so each of these is read on its own line.
    assert _feed(MARK + "<USER>").login == ["user"]
    assert _feed(MARK + "<PASSWORD>").login == ["password"]
    image = _feed(MARK + '<IMAGE FName="map.png" T="map">')
    assert [name for name, _url in image.images] == ["map.png"]


# --- partway through a line: now markup, with one tag per mark -------------------------------


def test_a_marked_tag_partway_through_a_line_is_markup():
    # The reporter's case: every tag carries its own 4z, almost always partway through a line.
    assert _feed("You see " + MARK + "<b>a guard</b>").text == "You see a guard"


def test_a_marked_send_partway_through_a_line_is_a_link():
    out = _feed('You see ' + MARK + '<send href="look guard">a guard</send>')
    assert out.text == "You see a guard"
    assert out.links == [Link("a guard", "look guard")]


def test_both_halves_marked_partway_through_a_line_make_one_link():
    # If only the opening tag is promoted the link never closes, and the link's words would have
    # to be found in a line that still carries its closing tag: the reporter's own traffic does
    # this on nearly every line.
    out = _feed(
        'text ' + MARK + '<send href="look guard">a guard' + MARK + "</send> more text"
    )
    assert out.text == "text a guard more text"
    assert out.links == [Link("a guard", "look guard")]


def test_each_mark_promotes_one_tag_only():
    # Two marked tags back to back: the second mark must not be swallowed by the first tag.
    out = _feed(MARK + "<sound 'one.wav'>" + MARK + "<sound 'two.wav'>")
    assert [cue.file for cue in out.cues] == ["one.wav", "two.wav"]


def test_a_marked_version_partway_through_a_line_is_answered():
    out = _feed("hello " + MARK + "<VERSION>")
    assert out.replies == ["<VERSION MXP=1.0 CLIENT=genericMud>"]


# --- what a mark partway through a line may not promote --------------------------------------


def test_a_marked_user_or_password_partway_through_a_line_does_nothing():
    # These make the client type a login. A mark on one tag is not the promise a secure line is.
    out = _feed("hi " + MARK + "<USER>" + MARK + "<PASSWORD>")
    assert out.login == []


def test_a_marked_var_partway_through_a_line_defines_nothing():
    parser = MxpParser()
    parser.feed("hi " + MARK + "<VAR victim>gold" + MARK + "</VAR>")
    assert parser.feed("&victim;").text == "&victim;"


def test_a_marked_image_partway_through_a_line_shows_no_picture():
    out = _feed("hi " + MARK + '<IMAGE FName="map.png" T="map">')
    assert out.images == []
    assert "map.png" not in out.text


def test_a_marked_entity_definition_partway_through_a_line_defines_nothing():
    # VAR is dangerous because it feeds the variables the MUD's own links expand into; an
    # <!ENTITY> sets the same store, so it stays behind a secure line for the same reason.
    parser = MxpParser()
    parser.feed('hi ' + MARK + '<!ENTITY victim "gold">')
    assert parser.feed("&victim;").text == "&victim;"


# --- what stays ignored partway through a line -----------------------------------------------


def test_1z_partway_through_a_line_still_makes_nothing_secure():
    out = _feed('hi ' + SECURE + '<send href="quit">there</send>')
    assert out.links == []


def test_5_to_7_partway_through_a_line_do_not_change_the_default():
    # A player's own words can carry an escape; one arriving partway through a line must not
    # decide how the next line is read. Each is checked against something only that default
    # would change: 5z from a locked default, 6z with a link only a secure line makes, and 7z
    # with markup an open line reads.
    parser = MxpParser()
    parser.feed(ESC + "[7z")
    parser.feed(f"hi {ESC}[5z")
    assert parser.feed("<b>still text</b>").text == "<b>still text</b>"
    parser = MxpParser()
    parser.feed(f"hi {ESC}[6z")
    assert parser.feed('<send href="quit">still text</send>').links == []
    parser = MxpParser()
    parser.feed(f"hi {ESC}[7z")
    assert parser.feed("<b>still parsed</b>").text == "still parsed"


def test_the_handshake_escape_split_across_two_reads():
    # The reporter's server sends 7z and then 4z<VERSION> separately; the mode a line is read in
    # comes from the escape at its head, so this has to work one read at a time.
    parser = MxpParser()
    parser.feed(ESC + "[7z")
    out = parser.feed(MARK + "<VERSION>")
    assert out.replies == ["<VERSION MXP=1.0 CLIENT=genericMud>"]


# --- a mark on a line that is already secure, and what a mark may not outlast ----------------


def test_a_marked_login_on_a_secure_line_still_asks_for_the_login():
    # The line already made the stronger promise, so a mark on one of its tags as well must not
    # take the login away from a server that does both.
    assert _feed(SECURE + "Name: " + MARK + "<USER>").login == ["user"]
    parser = MxpParser()
    parser.feed(ESC + "[6z")
    assert parser.feed("Password: " + MARK + "<PASSWORD>").login == ["password"]


def test_a_marked_image_on_a_secure_line_still_shows():
    assert _feed(SECURE + "Map: " + MARK + "<IMAGE map.png>").images == [("map.png", "")]


def test_a_paragraph_a_mark_opens_partway_through_ends_with_the_line():
    # A <P> that outlived its line would decide how every line after it is read, the same
    # reason 1z partway through is ignored, so it ends with the line as one on an open line does.
    parser = MxpParser()
    assert parser.feed("Bob says: " + MARK + "<p>hello").join_next is False
    assert parser.feed("The goblin hits you.").join_next is False


def test_a_link_on_a_locked_line_keeps_its_words_as_typed():
    # The line shows "&amp;" as typed, so the link's words have to match it or it can't be found.
    out = _feed(LOCKED + "Buy " + MARK + '<send href="buy">fish &amp; chips' + MARK + "</send>")
    assert out.text == "Buy fish &amp; chips"
    assert [(link.text, link.command) for link in out.links] == [("fish &amp; chips", "buy")]


def test_a_line_full_of_links_is_read_in_moments():
    # Where a link's words start is noted without rebuilding the line read so far, so a server
    # that fills one long line with links costs what they say and not the line again for each.
    count = 32768
    started = time.perf_counter()
    out = _feed(SECURE + "<send x>y</send>" * count)
    assert time.perf_counter() - started < 2.0
    assert len(out.links) == count


# --- what a change of mode partway through a line ends ---------------------------------------


def test_a_mark_no_tag_used_does_not_cross_a_change_of_mode():
    # A mark at the head that the MUD's own words never used must not carry over into a part of
    # the line it has since opened or locked for other people's words.
    for code in (0, 2, 3):
        assert _feed(MARK + "Bob says: " + f"{ESC}[{code}z" + "<USER>").login == []


def test_a_link_does_not_run_on_past_a_change_of_mode():
    # A reset closes every open tag, and opening or locking the rest of the line hands it to
    # words read another way, so a link still open there is dropped rather than closed later.
    for code in (0, 2, 3):
        change = f"{ESC}[{code}z"
        line = SECURE + '<send href="go">a guard' + change + " and more" + MARK + "</send>"
        assert _feed(line).links == []


def test_a_variable_split_by_a_mark_on_a_secure_line_is_still_set():
    parser = MxpParser()
    parser.feed(SECURE + "<VAR hp>42" + MARK + "</VAR>")
    assert parser.feed("HP: &hp;").text == "HP: 42"


def test_a_link_split_inside_an_entity_matches_the_line():
    # Each piece of a line is shown on its own, so "&am" and "p;" either side of a mark stay as
    # typed; the link's words have to be read the same way or it can't be found.
    out = _feed("x " + MARK + '<send href="go">a &am' + MARK + "<b>p; b" + MARK + "</send>")
    assert out.text == "x a &amp; b"
    assert [(link.text, link.command) for link in out.links] == [("a &amp; b", "go")]


def test_a_link_keeps_the_words_the_player_was_shown_when_an_entity_changes():
    # Each piece is shown as it is read, so a link's words from an earlier piece are what the
    # player already got, even if the MUD redefines one of their entities later in the line.
    parser = MxpParser()
    parser.feed(SECURE + '<!ENTITY label "old">')
    out = parser.feed(
        SECURE + '<send href="go">&label;'
        + MARK + '<!ENTITY label "new">'
        + MARK + "</send> new"
    )
    assert out.text == "old new"
    assert [(link.text, link.command) for link in out.links] == [("old", "go")]

def test_an_unfinished_tag_is_read_in_moments():
    # "<" and a long run of letters with no ">" once took seconds, on an open line anyone can
    # write on and on a locked line the moment a mark has it read for a tag.
    for line in ("Bob says: <" + "a" * 20000, LOCKED + MARK + "<" + "a" * 20000):
        started = time.perf_counter()
        _feed(line)
        assert time.perf_counter() - started < 1.0


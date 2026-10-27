"""MXP inline images: recorded, located, and offered as something a text client can use.

MXP gives an image no description -- no alternate text -- so a text client can't say what a
picture shows. What it can do is what MUSHclient does: turn the image into a link to the
picture. The web UI, which can show one, draws it instead.
"""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.bridge import protocol
from genericmud.config.keymap import load_keymap
from genericmud.config.worlds import World, load_worlds, save_worlds
from genericmud.protocol.mxp import IMAGE_MARK, MxpParser, resolve_image_url
from genericmud.protocol.telnet import DataReceived
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

ESC = "\x1b"
SECURE = ESC + "[1z"
ART = "http://art.example/mud/"


def _app(images_url: str = ""):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    posted: list[dict] = []
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap=load_keymap("vipmud"))
    app.images_url = images_url
    return app, sent, backend, posted


def _say(app: EngineApp, line: str) -> None:
    app.on_telnet_event(DataReceived(line.encode() + b"\r\n"))


def _line_message(posted: list[dict]) -> dict:
    lines = [message for message in posted if message.get("type") == protocol.LINE]
    assert lines, "no line message was posted"
    return lines[-1]


# --- what the parser keeps ---------------------------------------------------


def test_a_secure_image_is_recorded_and_leaves_a_marker():
    outcome = MxpParser().feed(SECURE + 'A <IMAGE fname="map.gif" url="http://art.example/">map')
    assert outcome.images == [("map.gif", "http://art.example/")]
    # The marker is where the picture goes; the engine turns it into an offset later.
    assert outcome.text == "A " + IMAGE_MARK + "map"
    assert "<image" not in outcome.text.lower()


def test_no_image_can_arrive_on_an_open_line():
    # An image is exactly the "large graphics" the spec says a player must not be able to force
    # onto another player's client, so an open line's tag is stripped and never recorded.
    outcome = MxpParser().feed('<IMAGE fname="huge.gif" url="http://art.example/">')
    assert outcome.images == []
    assert IMAGE_MARK not in outcome.text


def test_img_is_the_same_tag():
    assert MxpParser().feed(SECURE + '<IMG "map.gif">').images == [("map.gif", "")]


def test_an_image_with_no_file_name_is_ignored():
    # Nothing to fetch and nothing to point at, so it leaves no marker either.
    outcome = MxpParser().feed(SECURE + "<IMAGE width=100>")
    assert outcome.images == []
    assert IMAGE_MARK not in outcome.text


def test_positional_arguments_are_read_in_the_specs_order():
    assert MxpParser().feed(SECURE + '<IMAGE "map.gif" "http://art.example/">').images == [
        ("map.gif", "http://art.example/")
    ]


# --- where the file comes from ----------------------------------------------


def test_the_tags_own_url_wins():
    assert resolve_image_url("map.gif", ART, "http://other.example/") == ART + "map.gif"


def test_the_worlds_setting_stands_in_for_a_missing_url():
    assert resolve_image_url("map.gif", "", ART) == ART + "map.gif"


def test_with_no_address_at_all_the_image_is_nowhere():
    assert resolve_image_url("map.gif", "", "") == ""


def test_only_web_addresses_are_used():
    # A file name is not a path, and a base is not a place to open local files from.
    assert resolve_image_url("C:/Windows/System32/calc.exe", "", ART) == ""
    assert resolve_image_url("../../../etc/passwd", "", ART) == ""
    assert resolve_image_url("/etc/passwd", "", ART) == ""
    assert resolve_image_url("map.gif", "file:///C:/pictures/", "") == ""
    assert resolve_image_url("map.gif", "", "javascript:alert(1)") == ""


def test_an_absolute_web_address_in_the_file_name_is_used_as_is():
    assert resolve_image_url("https://cdn.example/map.gif", "", "") == "https://cdn.example/map.gif"


def test_a_trailing_slash_on_the_base_does_not_double_up():
    assert resolve_image_url("map.gif", ART.rstrip("/"), "") == ART + "map.gif"


def test_the_player_network_is_off_limits():
    # The browser fetches the picture, so the sound downloader's check never runs for it: a MUD
    # must not be able to make a page load poke at the player's router.
    assert resolve_image_url("pic.gif", "http://192.168.1.9/art/", "") == ""
    assert resolve_image_url("pic.gif", "http://10.0.0.5/art/", "") == ""
    assert resolve_image_url("pic.gif", "http://169.254.1.1/art/", "") == ""


def test_the_muds_own_machine_may_serve_its_pictures():
    # A MUD run on this computer is the ordinary case for a developer, and MSP already allows it.
    local = "http://127.0.0.1:8000/art/"
    assert resolve_image_url("pic.gif", local, "") == ""  # not unless it IS the MUD's address
    assert resolve_image_url("pic.gif", local, "", "127.0.0.1") == local + "pic.gif"
    assert resolve_image_url("pic.gif", local, "", "10.1.2.3") == ""


def test_a_host_name_is_taken_at_its_word():
    # Resolving it here would decide nothing: the page resolves it again when it loads.
    assert resolve_image_url("map.gif", "https://mud.example/art/", "") == (
        "https://mud.example/art/map.gif"
    )


# --- what the engine does with one ------------------------------------------


def test_the_picture_never_appears_in_the_line_text():
    app, _sent, _backend, posted = _app(images_url=ART)
    _say(app, SECURE + 'A <IMAGE fname="map.gif"> map is here.')
    message = _line_message(posted)
    assert IMAGE_MARK not in message["text"]
    assert message["text"] == "A  map is here."
    assert app.buffer.lines()[-1].plain_text == "A  map is here."


def test_the_renderer_is_told_where_to_draw_it():
    app, _sent, _backend, posted = _app(images_url=ART)
    _say(app, SECURE + 'Look: <IMAGE fname="map.gif">')
    message = _line_message(posted)
    assert message["images"] == [[6, ART + "map.gif"]]


def test_a_text_client_gets_a_link_to_the_picture_instead():
    # MUSHclient's behaviour, and the only form in which a picture can be reached by someone who
    # can't see it: the file name is the link's words, the address is what it does.
    app, _sent, _backend, _posted = _app(images_url=ART)
    _say(app, SECURE + 'A <IMAGE fname="map.gif"> map is here.')
    assert app.buffer.lines()[-1].links == [("map.gif", ART + "map.gif")]


def test_following_that_link_opens_the_picture():
    app, sent, _backend, _posted = _app(images_url=ART)
    _say(app, SECURE + '<IMAGE fname="map.gif">')
    app.on_ws_message({"type": "key", "key": "alt+enter"})  # the line's only link
    assert sent == []  # a web address is opened, never typed at the MUD
    assert app.buffer.lines()[-1].links[0][1] == ART + "map.gif"


def test_an_image_that_cannot_be_located_is_dropped_quietly():
    app, _sent, _backend, posted = _app()  # no setting, and the tag names no address
    _say(app, SECURE + 'A <IMAGE fname="map.gif"> map is here.')
    message = _line_message(posted)
    assert message["images"] == []
    assert message["text"] == "A  map is here."


def test_a_line_that_is_only_an_image_still_reaches_the_renderer():
    # Its text is empty, which normally means "nothing to show"; here there is something.
    app, _sent, _backend, posted = _app(images_url=ART)
    _say(app, SECURE + '<IMAGE fname="map.gif">')
    assert _line_message(posted)["images"] == [[0, ART + "map.gif"]]


def test_a_line_that_is_only_an_unloadable_image_is_not_shown_at_all():
    app, _sent, _backend, posted = _app()
    _say(app, SECURE + '<IMAGE fname="map.gif">')
    assert [m for m in posted if m.get("type") == protocol.LINE] == []


def test_images_and_links_on_one_line_both_survive():
    app, _sent, _backend, posted = _app(images_url=ART)
    _say(app, SECURE + '<send href="north">north</send> and <IMAGE fname="map.gif">')
    message = _line_message(posted)
    assert message["text"] == "north and "
    assert message["links"] == [[0, 5, "north"]]
    assert message["images"] == [[10, ART + "map.gif"]]
    # Both are reachable from the keyboard: the link by its word, the image by its file name.
    assert [text for text, _command in app.buffer.lines()[-1].links] == ["north", "map.gif"]


def test_an_image_inside_a_link_does_not_become_the_links_name():
    # <SEND><IMAGE></SEND> is an image map: the link has no words of its own, so it is offered
    # by what it does rather than by the marker that stood for the picture.
    app, _sent, _backend, _posted = _app(images_url=ART)
    _say(app, SECURE + '<send "drink"><IMAGE fname="fountain.gif"></send>')
    assert app.buffer.lines()[-1].links == [("", "drink"), ("fountain.gif", ART + "fountain.gif")]


def test_mxp_off_means_images_are_text_like_anything_else():
    app, _sent, _backend, posted = _app(images_url=ART)
    app.mxp_enabled = False
    _say(app, '<IMAGE fname="map.gif">')
    message = _line_message(posted)
    assert message["images"] == []
    assert message["text"] == '<IMAGE fname="map.gif">'


# --- the world setting ------------------------------------------------------


def test_the_images_setting_round_trips(tmp_path):
    path = tmp_path / "worlds.toml"
    world = World("Art", "art.example", 4000, images_url=ART)
    save_worlds([world], path)
    assert load_worlds(path) == [world]
    assert load_worlds(path)[0].images_url == ART


def test_the_images_setting_is_web_only():
    assert World("w", "h", 1, images_url="file:///C:/pics").images_url == ""
    assert World("w", "h", 1, images_url="  ftp://x/  ").images_url == ""
    assert World("w", "h", 1, images_url="  " + ART + " ").images_url == ART


def test_a_world_without_the_setting_writes_none(tmp_path):
    path = tmp_path / "worlds.toml"
    save_worlds([World("Plain", "plain.example", 4000)], path)
    assert "images_url" not in path.read_text(encoding="utf-8")

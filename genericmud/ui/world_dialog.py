"""The world dialog: add or edit one world's connection, login, logging and sound settings.

Its OK button saves, connects, or both, whichever the player chose.
"""

from __future__ import annotations

from collections.abc import Callable

import wx

from genericmud.config.worlds import (
    DEFAULT_CONNECT_METHOD,
    DEFAULT_PORT,
    DEFAULT_SEPARATOR,
    LOG_FILES,
    World,
    parse_port,
    separator_problem,
)
from genericmud.protocol.charset import ENCODING_CHOICES
from genericmud.session.credentials import CredentialStore

# What a world's log file choices are called in its details, in LOG_FILES order.
_LOG_FILE_LABELS = ("A new file each session", "One file per day", "One file for this world")
# In the order CONNECT_METHODS visits them, so the value and its label stay together.
_CONNECT_LABELS = (
    ("none", "No auto-connect — send no login"),
    ("prompts", "Answer the MUD's prompts — name, then password when asked"),
    ("mxp", "Let the MUD ask (MXP) — send the login only when the MUD requests it"),
    ("mush", "MUSH/MOO — send connect name password"),
    ("diku", "Diku — send name, then password"),
)

# What pressing OK in a new-world dialog can do: (what it is called, whether it saves the
# world, whether it connects to it), in the order they are read out. The words and what they do
# are one table so the two cannot drift apart.
#
# Three radio buttons rather than two checkboxes ("save this world", "also connect"): the two
# boxes say the same four states with two controls, and the fourth -- neither saved nor
# connected -- is a state nobody can want, so it would only exist to be refused. A group is
# also read out as a set, "Save and connect, 1 of 3", so nothing has to be remembered.
_OK_CHOICES = (
    ("Save and connect", True, True),
    ("Save, don't connect", True, False),
    ("Connect, don't save", False, True),
)


def _choice_that(saves: bool) -> int:
    """The choice to start on: the one that connects, and saves only if asked to."""
    for index, (_label, does_save, does_connect) in enumerate(_OK_CHOICES):
        if does_save is saves and does_connect:
            return index
    return 0  # unreachable while the table holds one choice of each kind; that one saves


class WorldDialog(wx.Dialog):
    """Create or edit connection details for one world."""

    def __init__(
        self,
        parent: wx.Window,
        initial: World | None = None,
        *,
        title: str = "New World",
        offer_trust: bool = False,
        save_default: bool = True,
        show_save: bool = True,
        show_connect: bool = False,
        lock_name: bool = False,
        credentials: CredentialStore | None = None,
        prefill_login: bool = True,
        note: str = "",
        name_check: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(parent, title=title)
        self._world: World | None = None
        self._credentials = credentials
        # Asked before anything is written: a name this world may not take, with the reason.
        self._name_check = name_check
        # Whether the login boxes were filled from the store, i.e. they hold this world's own
        # login. Only then does a rename move it: a soundpack's setup that merely shares a
        # saved world's name must not take that world's login with it.
        self._login_prefilled = False
        # Kept so a rename can move the stored login: the store is keyed by world name.
        self._initial_name = initial.name if initial is not None else None
        # Why the dialog is up, first in the window: NVDA reads a dialog's text that labels no
        # control as it opens, and it can be read again. Said aloud just before the dialog
        # instead, it was cut off when the dialog took the focus.
        self._note: wx.StaticText | None = None
        if note:
            self._note = wx.StaticText(self, label=note.replace("&", "&&"))
            self._note.Wrap(420)
        grid = wx.FlexGridSizer(0, 2, 6, 6)
        grid.AddGrowableCol(1)

        # Each StaticText is created (and added) immediately before its control so the
        # label precedes the control in z-order -- the association NVDA reads on
        # Windows. Checkboxes carry their own label= as the accessible name.
        self._name = self._labeled_text(grid, "&Name:", "Name")
        self._host = self._labeled_text(grid, "&Host:", "Host")
        self._port = self._labeled_text(grid, "&Port:", "Port", str(DEFAULT_PORT))
        # Most MUDs are fine on automatic; the rest (Russian, Chinese, older European
        # MUDs) need the encoding they actually send, or every accented letter is garbled.
        grid.Add(wx.StaticText(self, label="&Encoding:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._encoding = wx.Choice(self, choices=[label for _value, label in ENCODING_CHOICES])
        self._encoding.SetName("Encoding")
        self._encoding.SetSelection(0)
        grid.Add(self._encoding, 1, wx.EXPAND)
        # Blank is the usual answer: a soundpack plays its own sounds from where it's installed.
        # A blank box with nothing saying so read as "no sounds".
        self._sounds = self._labeled_text(
            grid, "So&unds folder (blank uses the soundpack's own sounds):",
            "Sounds folder (blank uses the soundpack's own sounds)",
        )
        grid.Add((0, 0))
        self._download_sounds = wx.CheckBox(self, label="Do&wnload sounds the MUD offers")
        self._download_sounds.SetName("Download sounds the MUD offers")
        self._download_sounds.SetValue(True)
        grid.Add(self._download_sounds, 1, wx.EXPAND)
        self._mxp = wx.CheckBox(self, label="Understand M&XP markup")
        self._mxp.SetName("Understand MXP markup")
        self._mxp.SetValue(True)
        grid.Add(self._mxp, 1, wx.EXPAND)
        # Off by default: turning it on makes the client announce itself as a Pueblo client at
        # the login screen, and a world that does not speak Pueblo reads that as a name.
        self._pueblo = wx.CheckBox(self, label="Understand Pue&blo HTML")
        self._pueblo.SetName("Understand Pueblo HTML")
        grid.Add(self._pueblo, 1, wx.EXPAND)
        # Where <IMAGE> looks for a file when the tag names no address of its own. Most MUDs
        # expect the client to be told this; without it an image can't be found, so it's
        # skipped -- which is also what MUSHclient does with an address-less image.
        self._images_url = self._labeled_text(
            grid, "&Images web address:", "Images web address", ""
        )

        grid.Add((0, 0))
        self._tls = wx.CheckBox(self, label="Use &TLS")
        self._tls.SetName("Use TLS")
        grid.Add(self._tls, 1, wx.EXPAND)

        # Command stacking: "n;n;look" sends three commands.
        grid.Add((0, 0))
        self._stacking = wx.CheckBox(self, label="&Split commands at a separator, as in n;n;look")
        self._stacking.SetName("Split commands at a separator, as in n;n;look")
        self._stacking.SetValue(True)
        grid.Add(self._stacking, 1, wx.EXPAND)
        self._separator = self._labeled_text(
            grid, "Comman&d separator:", "Command separator", DEFAULT_SEPARATOR
        )
        self._separator.SetMaxLength(1)
        grid.Add((0, 0))
        separator_note = wx.StaticText(
            self,
            label=(
                "Type the separator twice to send it as itself. It works the same in your "
                "own aliases, hotkeys and triggers. Soundpacks aren't affected."
            ),
        )
        separator_note.Wrap(360)
        grid.Add(separator_note, 1, wx.EXPAND)

        # Logging (Alt+Shift+L still starts and stops it by hand).
        grid.Add((0, 0))
        self._log_automatically = wx.CheckBox(self, label="&Log automatically when connecting")
        self._log_automatically.SetName("Log automatically when connecting")
        grid.Add(self._log_automatically, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Log &files:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._log_files = wx.Choice(self, choices=list(_LOG_FILE_LABELS))
        self._log_files.SetName("Log files")
        self._log_files.SetSelection(0)
        grid.Add(self._log_files, 1, wx.EXPAND)
        grid.Add((0, 0))
        self._log_commands = wx.CheckBox(self, label="Log what &you type (passwords stay hidden)")
        self._log_commands.SetName("Log what you type (passwords stay hidden)")
        self._log_commands.SetValue(True)
        grid.Add(self._log_commands, 1, wx.EXPAND)
        # Next to the log switch but deliberately separate: showing a command in the output and
        # writing it to the log are two different wants (MUSHclient splits them too).
        grid.Add((0, 0))
        self._echo_input = wx.CheckBox(self, label="Echo &input in the output")
        self._echo_input.SetName("Echo input in the output")
        grid.Add(self._echo_input, 1, wx.EXPAND)

        # Login. The username and password go to the credential store, never to worlds.toml:
        # that file is what "Export This World" zips up and what people share. The method and
        # the after-connect commands aren't secret, so they live in the world file with the
        # rest of the per-world settings.
        self._username = self._labeled_text(grid, "&Username:", "Username")
        grid.Add(wx.StaticText(self, label="&Password:"), 0, wx.ALIGN_CENTER_VERTICAL)
        password_row = wx.BoxSizer(wx.HORIZONTAL)
        # Two controls rather than one restyled: wx won't reliably turn the password style on
        # and off on a live control (macOS ignores it), and a half-applied toggle would show
        # dots while claiming to reveal. Only one is shown at a time.
        self._password = wx.TextCtrl(self, style=wx.TE_PASSWORD)
        self._password.SetName("Password")
        self._password_plain = wx.TextCtrl(self)
        self._password_plain.SetName("Password, revealed")
        self._password_plain.Hide()
        password_row.Add(self._password, 1, wx.EXPAND)
        password_row.Add(self._password_plain, 1, wx.EXPAND)
        grid.Add(password_row, 1, wx.EXPAND)
        self._reveal = wx.CheckBox(self, label="&Reveal password")
        self._reveal.SetName("Reveal password")
        self._reveal.SetValue(False)  # always masked to begin with
        self._reveal.Bind(wx.EVT_CHECKBOX, self._on_reveal)
        grid.Add((0, 0))
        grid.Add(self._reveal, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="&Connect:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._connect = wx.Choice(self, choices=[label for _value, label in _CONNECT_LABELS])
        self._connect.SetName("Connect")
        # The list starts on the default, so Down arrow walks away from it rather than back
        # to it: with the login boxes empty there is nothing to send, and a world that
        # promised to log in and then didn't would be worse than one that never offered.
        self._connect.SetSelection(
            [value for value, _label in _CONNECT_LABELS].index(DEFAULT_CONNECT_METHOD)
        )
        grid.Add(self._connect, 1, wx.EXPAND)
        grid.Add(
            wx.StaticText(self, label="Sent after &connecting, one command per line:"),
            0,
            wx.ALIGN_TOP,
        )
        self._post_connect = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 70))
        self._post_connect.SetName("Commands sent after connecting")
        grid.Add(self._post_connect, 1, wx.EXPAND)

        # What pressing OK does, offered only where there is a choice to make: a brand-new world
        # can be kept without being played (add it to the list now, connect later from Ctrl+O),
        # and can be played without being kept. Editing a world saves it and nothing else.
        self._ok_choice: wx.RadioBox | None = None
        self._save: wx.CheckBox | None = None
        if show_save and show_connect:
            grid.Add((0, 0))
            # The label is the group's own caption. A screen reader may leave it out -- NVDA
            # does here -- so the three names are written to stand on their own without it.
            self._ok_choice = wx.RadioBox(
                self,
                label="When I press OK:",
                choices=[label for label, _saves, _connects in _OK_CHOICES],
                majorDimension=1,  # one column, so the group is read down like a list
                style=wx.RA_SPECIFY_COLS,
            )
            self._ok_choice.SetName("When I press OK")
            self._ok_choice.SetSelection(_choice_that(save_default))
            grid.Add(self._ok_choice, 1, wx.EXPAND)
        elif show_save:
            # The soundpack setup dialog saves and then connects, so there is nothing to choose;
            # it keeps the single box it has always had.
            grid.Add((0, 0))
            self._save = wx.CheckBox(self, label="Sa&ve this world")
            self._save.SetName("Save this world")
            self._save.SetValue(save_default)
            grid.Add(self._save, 1, wx.EXPAND)

        # Offered only for a freshly-installed code-executing pack (MUSHclient): it stays silent
        # until trusted, so this is where the user consents to run it. Checked by default -- they
        # chose to install it -- but visible and clearable, unlike a silent auto-trust.
        self._trust: wx.CheckBox | None = None
        if offer_trust:
            grid.Add((0, 0))
            self._trust = wx.CheckBox(
                self, label="&Trust this soundpack's scripts so its sounds play"
            )
            self._trust.SetName("Trust this soundpack's scripts so its sounds play")
            self._trust.SetValue(True)
            grid.Add(self._trust, 1, wx.EXPAND)

        sizer = wx.BoxSizer(wx.VERTICAL)
        if self._note is not None:
            sizer.Add(self._note, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        if initial is not None:
            self._name.SetValue(initial.name)
            self._host.SetValue(initial.host)
            try:
                initial_port = parse_port(initial.port)
            except ValueError:
                initial_port = DEFAULT_PORT
            self._port.SetValue(str(initial_port))
            self._tls.SetValue(initial.tls)
            self._sounds.SetValue(initial.sounds or "")
            values = [value for value, _label in ENCODING_CHOICES]
            self._encoding.SetSelection(values.index(initial.encoding))
            self._stacking.SetValue(initial.command_stacking)
            self._separator.SetValue(initial.command_separator)
            self._log_automatically.SetValue(initial.log_automatically)
            self._log_files.SetSelection(LOG_FILES.index(initial.log_files))
            self._log_commands.SetValue(initial.log_commands)
            self._echo_input.SetValue(initial.echo_input)
            self._download_sounds.SetValue(initial.download_sounds)
            methods = [value for value, _label in _CONNECT_LABELS]
            self._connect.SetSelection(methods.index(initial.connect_method))
            self._post_connect.SetValue(initial.post_connect_commands)
            stored = None
            if prefill_login and self._credentials is not None:
                stored = self._credentials.get(initial.name)
            if stored is not None:
                self._login_prefilled = True
                # Pre-filled rather than blank: leaving it blank could not tell "keep the
                # password I already have" from "forget it", and one of the two is a trap.
                self._username.SetValue(stored[0])
                self._password.SetValue(stored[1])
                self._password_plain.SetValue(stored[1])
            self._mxp.SetValue(initial.mxp)
            self._pueblo.SetValue(initial.pueblo)
            self._images_url.SetValue(initial.images_url)
        if lock_name:
            self._name.Enable(False)
            self._host.SetFocus()
        else:
            self._name.SetFocus()

    def _labeled_text(
        self, grid: wx.FlexGridSizer, label: str, name: str, value: str = ""
    ) -> wx.TextCtrl:
        grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        ctrl = wx.TextCtrl(self, value=value)
        ctrl.SetName(name)
        grid.Add(ctrl, 1, wx.EXPAND)
        return ctrl

    def _on_ok(self, _event: wx.CommandEvent) -> None:
        host = self._host.GetValue().strip()
        if not host:
            wx.MessageBox(
                "Enter the MUD's host name.", "World details", wx.OK | wx.ICON_ERROR, self
            )
            self._host.SetFocus()
            return
        try:
            port = parse_port(self._port.GetValue())
        except ValueError as error:
            wx.MessageBox(str(error), "World details", wx.OK | wx.ICON_ERROR, self)
            self._port.SetFocus()
            self._port.SelectAll()
            return
        name = self._name.GetValue().strip() or host
        if self._name_check is not None:
            refusal = self._name_check(name)
            if refusal:
                wx.MessageBox(refusal, "World details", wx.OK | wx.ICON_ERROR, self)
                self._name.SetFocus()
                self._name.SelectAll()
                return
        separator = self._separator.GetValue()
        problem = separator_problem(separator)
        if problem is not None and self._stacking.GetValue():
            wx.MessageBox(problem, "World details", wx.OK | wx.ICON_ERROR, self)
            self._separator.SetFocus()
            self._separator.SelectAll()
            return
        self._world = World(
            name=self._name.GetValue().strip() or host,
            host=host,
            port=port,
            tls=self._tls.GetValue(),
            sounds=self._sounds.GetValue().strip() or None,
            encoding=ENCODING_CHOICES[self._encoding.GetSelection()][0],
            command_stacking=self._stacking.GetValue(),
            command_separator=separator,  # a bad one with stacking off becomes the default
            log_automatically=self._log_automatically.GetValue(),
            log_files=LOG_FILES[self._log_files.GetSelection()],
            log_commands=self._log_commands.GetValue(),
            echo_input=self._echo_input.GetValue(),
            download_sounds=self._download_sounds.GetValue(),
            connect_method=_CONNECT_LABELS[self._connect.GetSelection()][0],
            post_connect_commands=self._post_connect.GetValue(),
            mxp=self._mxp.GetValue(),
            pueblo=self._pueblo.GetValue(),
            images_url=self._images_url.GetValue().strip(),
        )
        try:
            self._store_credentials(self._world)
        except Exception as error:  # noqa: BLE001 - a locked or broken store, say so and stay
            wx.MessageBox(
                f"The login couldn't be saved to the credential store ({error}). Try again, "
                "or clear the username and password to save the world without one.",
                "World details",
                wx.OK | wx.ICON_ERROR,
                self,
            )
            self._username.SetFocus()
            return
        self.EndModal(wx.ID_OK)

    def _password_value(self) -> str:
        """Whatever is in the password field right now, revealed or not.

        Keyed on which control is visible, not on the Reveal checkbox: by the time the
        checkbox's own handler runs the checkbox has already flipped, and reading it there
        would take the hidden (empty) control's value and drop what the player typed.
        """
        if self._password_plain.IsShown():
            return self._password_plain.GetValue()
        return self._password.GetValue()

    def _on_reveal(self, _event: wx.CommandEvent) -> None:
        """Show the password in plain text, or hide it again."""
        reveal = self._reveal.GetValue()
        typed = self._password_value()  # whichever control was on screen holds the truth
        self._password.SetValue(typed)
        self._password_plain.SetValue(typed)
        self._password.Show(not reveal)
        self._password_plain.Show(reveal)
        self.Layout()
        (self._password_plain if reveal else self._password).SetFocus()

    def _store_credentials(self, world: World) -> None:
        """Save this world's login to the credential store, following a rename.

        The store is keyed by the world's name, so a rename that left the old key behind
        would silently stop the world logging in.

        A world being connected to without being saved stores no login, not even one typed into
        this dialog: a credential filed under a world that is not in the list is a login nobody
        is looking after, which is exactly what deleting a world has to clean up. The session
        can only ask the store, so a one-off visit is logged in by hand.
        """
        if self._credentials is None or not self.should_save():
            return
        username = self._username.GetValue().strip()
        password = self._password_value()
        if username or password:
            self._credentials.set(world.name, username, password)
        else:
            self._credentials.delete(world.name)  # both blank means "forget this login"
        if (
            self._login_prefilled
            and self._initial_name is not None
            and self._initial_name != world.name
        ):
            self._credentials.delete(self._initial_name)

    def get_world(self) -> World:
        if self._world is None:
            raise RuntimeError("world requested before the dialog was accepted")
        return self._world

    def _chosen_effect(self) -> tuple[bool, bool]:
        """Whether the chosen option saves this world, and whether it connects to it.

        A radio group always has a selection, so there is no "nothing chosen" case to answer
        for. That is the point of the group: OK always has something to do.
        """
        return _OK_CHOICES[self._ok_choice.GetSelection()][1:]

    def should_save(self) -> bool:
        """True when OK should add or keep this world in the saved list."""
        if self._ok_choice is not None:
            return self._chosen_effect()[0]
        return self._save is None or self._save.GetValue()

    def should_connect(self) -> bool:
        """True when OK should open a session on this world."""
        if self._ok_choice is not None:
            return self._chosen_effect()[1]
        return True  # connecting without the choice offered is what OK has always done

    def should_trust(self) -> bool:
        """True if the trust checkbox was offered and left checked (else False)."""
        return self._trust is not None and self._trust.GetValue()

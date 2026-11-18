"""Machine-local audio routes; device calls run on a worker, never on the wx thread."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import wx

from genericmud.config.audio_routes import (
    FALLBACK_CHOICES,
    AudioOutput,
    load_audio_routes,
    save_audio_routes,
)
from genericmud.sound.bass_library import BassUnavailable
from genericmud.sound.output_devices import AudioRouteUnavailable, available_outputs
from genericmud.sound.output_preview import OutputPreview


class AudioRouteEditor(wx.Dialog):
    def __init__(self, parent, names, name="", output=None):
        super().__init__(parent, title="Audio route")
        self._names = set(names) - {name}
        self._announce = parent._announce
        self._selected = output
        self._outputs: list[AudioOutput] = []
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-output")
        self._preview = None
        self._alive = True
        self._window_id = self.GetId()
        self._busy = False
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label="Route &name:"), 0, wx.ALL, 6)
        self._name = wx.TextCtrl(self, value=name)
        self._name.SetName("Route name")
        outer.Add(self._name, 0, wx.EXPAND | wx.ALL, 6)
        outer.Add(wx.StaticText(self, label="&Output device:"), 0, wx.ALL, 6)
        self._device = wx.Choice(self)
        self._device.SetName("Output device")
        if output is not None:
            # Keep the saved destination editable if discovery fails (including fallback).
            self._outputs = [output]
            self._device.Set([f"Saved: {output.name}"])
            self._device.SetSelection(0)
        outer.Add(self._device, 0, wx.EXPAND | wx.ALL, 6)
        outer.Add(wx.StaticText(self, label="&Fallback:"), 0, wx.ALL, 6)
        self._fallback = wx.Choice(self, choices=["System default", "Stay silent"])
        self._fallback.SetName("Fallback")
        self._fallback.SetSelection(FALLBACK_CHOICES.index(output.fallback) if output else 0)
        self._fallback.SetToolTip("What to do when this route's output cannot play a cue.")
        outer.Add(self._fallback, 0, wx.EXPAND | wx.ALL, 6)
        self._status = wx.StaticText(self, label="Loading audio outputs...")
        outer.Add(self._status, 0, wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._test = wx.Button(self, label="&Test output")
        self._refresh = wx.Button(self, label="&Refresh devices")
        for button in (self._test, self._refresh):
            row.Add(button, 0, wx.ALL, 6)
        outer.Add(row)
        outer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizerAndFit(outer)
        self.SetMinSize((450, self.GetSize().height))
        self._test.Bind(wx.EVT_BUTTON, self._on_test)
        self._refresh.Bind(wx.EVT_BUTTON, self._on_refresh)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._on_refresh(None)

    def _run(self, work, done):
        self._busy = True
        self._test.Disable()
        self._refresh.Disable()
        self.FindWindowById(wx.ID_OK).Disable()

        def completed(future):
            try:
                result, error = future.result(), None
            except (BassUnavailable, AudioRouteUnavailable, OSError, ValueError) as problem:
                result, error = None, str(problem)
            wx.CallAfter(self._complete, done, result, error)

        self._worker.submit(work).add_done_callback(completed)

    def _complete(self, done, result, error):
        if not self._alive:
            return
        self._busy = False
        self._test.Enable()
        self._refresh.Enable()
        self.FindWindowById(wx.ID_OK).Enable()
        if error:
            self._status.SetLabel(error)
            wx.MessageBox(error, "Audio output unavailable", wx.OK | wx.ICON_WARNING, self)
        else:
            done(result)
        self.Layout()
        self.Fit()

    def _on_refresh(self, _event):
        if self._busy:
            return
        if self._outputs and self._device.GetSelection() != wx.NOT_FOUND:
            self._selected = self._outputs[self._device.GetSelection()]
        self._run(available_outputs, self._show_outputs)

    def _show_outputs(self, outputs):
        self._outputs = outputs
        labels = [f"{item.name} ({index + 1})" for index, item in enumerate(outputs)]
        choice = next((index for index, item in enumerate(outputs)
                       if self._selected is not None
                       and (item.platform, item.identifier)
                       == (self._selected.platform, self._selected.identifier)), wx.NOT_FOUND)
        if self._selected is not None and not any(
            (item.platform, item.identifier) == (self._selected.platform, self._selected.identifier)
            for item in outputs
        ):
            choice = len(outputs)
            self._outputs = [*outputs, self._selected]
            labels.append(f"Unavailable: {self._selected.name}")
        self._device.Set(labels)
        if labels:
            self._device.SetSelection(choice)
        self._status.SetLabel(
            "Choose an output and test it." if outputs else "No output devices found."
        )
        self._announce(f"{len(outputs)} audio outputs available.")

    def _on_test(self, _event):
        index = self._device.GetSelection()
        if index == wx.NOT_FOUND:
            wx.MessageBox("Choose an output device first.", "Audio route", wx.OK, self)
            self._device.SetFocus()
            return
        output = self._outputs[index]

        def play():
            if self._preview is None:
                self._preview = OutputPreview()
            self._preview.play(output)

        def played(_result):
            self._status.SetLabel("Test sound played.")
            self._announce("Test sound played.")

        self._run(play, played)

    def _on_ok(self, _event):
        name = self._name.GetValue().strip()
        if not name or name in self._names or name.casefold() == "system default":
            wx.MessageBox("Enter a unique route name.", "Audio route", wx.OK, self)
            self._name.SetFocus()
        elif self._device.GetSelection() == wx.NOT_FOUND:
            wx.MessageBox("Choose an output device first.", "Audio route", wx.OK, self)
            self._device.SetFocus()
        else:
            self.EndModal(wx.ID_OK)

    def result(self):
        output = replace(self._outputs[self._device.GetSelection()],
                         fallback=FALLBACK_CHOICES[self._fallback.GetSelection()])
        return self._name.GetValue().strip(), output

    def _on_destroy(self, event):
        # During destruction wx can return a new base Dialog wrapper, not this Python
        # subclass. Compare the saved window ID without touching the dying C++ window.
        if event.GetId() == self._window_id and self._alive:
            self._alive = False

            def cleanup():
                if self._preview is not None:
                    self._preview.close()

            self._worker.submit(cleanup)
            self._worker.shutdown(wait=False)
        event.Skip()


class AudioRoutesDialog(wx.Dialog):
    def __init__(self, parent, *, announce=None):
        routes = load_audio_routes()  # caller surfaces file errors without overwriting it
        super().__init__(parent, title="Audio routes")
        self._announce = announce or (lambda _text: None)
        self._routes = routes
        self._original_names = set(routes)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(wx.StaticText(self, label="Audio &routes:"), 0, wx.ALL, 6)
        self._list = wx.ListBox(self)
        self._list.SetName("Audio routes")
        outer.Add(self._list, 1, wx.EXPAND | wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (("&Add...", self._on_add), ("&Edit...", self._on_edit),
                               ("Re&move", self._on_remove)):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            row.Add(button, 0, wx.ALL, 6)
        outer.Add(row)
        outer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizerAndFit(outer)
        self.SetMinSize((450, 300))
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self._reload()

    def _reload(self):
        self._list.Set(sorted(self._routes))
        if self._list.GetCount():
            self._list.SetSelection(0)

    def _edit(self, name=""):
        dialog = AudioRouteEditor(self, self._routes, name, self._routes.get(name))
        try:
            if dialog.ShowModal() == wx.ID_OK:
                new_name, output = dialog.result()
                self._routes.pop(name, None)
                self._routes[new_name] = output
                self._reload()
                self._list.SetStringSelection(new_name)
        finally:
            dialog.Destroy()
        self._list.SetFocus()

    def _chosen(self):
        name = self._list.GetStringSelection()
        if not name:
            wx.MessageBox("Choose an audio route first.", "Audio routes", wx.OK, self)
            self._list.SetFocus()
        return name

    def _on_add(self, _event):
        self._edit()

    def _on_edit(self, _event):
        if name := self._chosen():
            self._edit(name)

    def _on_remove(self, _event):
        if name := self._chosen():
            del self._routes[name]
            self._reload()
            self._list.SetFocus()

    def _on_ok(self, _event):
        removed = sorted(self._original_names - self._routes.keys())
        if removed and wx.MessageBox(
            "These audio route names will be removed:\n" + "\n".join(removed)
            + "\n\nTriggers using these names are not updated. Their sounds will use "
            "System default until you select another route in those triggers or restore "
            "the old route names."
            "\n\nSave these changes?",
            "Save audio routes", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self,
        ) != wx.YES:
            return
        try:
            save_audio_routes(self._routes)
        except OSError as error:
            wx.MessageBox(f"Cannot save audio routes: {error}", "Audio routes", wx.OK, self)
            return
        self.EndModal(wx.ID_OK)

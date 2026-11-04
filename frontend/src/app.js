// Wires the renderer together: connect the bridge, route engine messages to the
// output/audio/status modules, and forward input/keys back to the engine.

import { Bridge } from "./ws.js";
import { Output } from "./output.js";
import { Input } from "./input.js";
import { Audio } from "./audio.js";
import { Status } from "./status.js";

const params = new URLSearchParams(location.search);
const port = params.get("port") || "8731";

const outputEl = document.getElementById("output");
const inputEl = document.getElementById("input");
const statusEl = document.getElementById("status");
const announceEl = document.getElementById("sr-announce");

// A link in the output is activated by the renderer and decided by the engine, which owns the
// policy (no client commands, web URLs only). `bridge` is reached lazily for the same reason
// `audio`'s error callback is: it only fires on a later click, by which point it is assigned.
const output = new Output(outputEl, (text, command, insert) =>
  bridge.send({ type: "link", text, command, insert }));
// onError reaches `bridge` lazily: it only fires on a later sound failure, by which
// point the const below is assigned. Errors land in the engine's diagnostic trace.
const audio = new Audio((e) =>
  bridge.send({ type: "client_error", scope: "audio", file: e.file, error: e.error }));
const status = new Status(statusEl);
let input;

// A <SEND> menu asks which command was meant. The strip stays until one is picked; nothing
// is sent without a click, and the engine still decides what the command may do.
let menuBar = null;
function showMenu(message) {
  if (menuBar) menuBar.remove();
  menuBar = document.createElement("div");
  menuBar.className = "mxp-menu";
  menuBar.setAttribute("role", "group");
  menuBar.setAttribute("aria-label", `Choose: ${message.text}`);
  const labels = [];
  for (const [command, caption] of message.choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = caption || command;
    button.addEventListener("click", () => {
      bridge.send({ type: "menu_choice", text: caption || command, command });
      if (menuBar) menuBar.remove();
      menuBar = null;
      inputEl.focus();
    });
    labels.push(button.textContent);
    menuBar.appendChild(button);
  }
  document.body.appendChild(menuBar);
  menuBar.querySelector("button")?.focus();
  announceEl.textContent = `Choose: ${labels.join(", ")}`;
}

const handlers = {
  line: (m) => {
    output.addLine(m);
    input.addOutput(m.text);
  },
  echo: (m) => output.addLine({ text: m.text }),
  sound: (m) => audio.play(m),
  stop_sound: (m) => audio.stop(m.channel),
  // Keep the engine-specified channel (default "music"); hardcoding it collapsed a pack's
  // custom music channels onto one bus and made stop_sound(<custom>) unable to stop them.
  music: (m) => audio.play({ ...m, loop: true, channel: m.channel || "music" }),
  status: (m) => status.update(m.gauges),
  review: () => { /* speech is native; visual reflection can be added later */ },
  connected: (m) => { announceEl.textContent = `Connected to ${m.world || "server"}`; },
  disconnected: () => { announceEl.textContent = "Disconnected"; },
  input_masked: (m) => input.setMasked(m.masked),
  // A link that carries half a command: put it in the box, send nothing.
  input_text: (m) => input.setText(m.text),
  menu: (m) => showMenu(m),
  // A link to a web page, when there is no native window to hand it to. noopener/noreferrer
  // keep the opened page from reaching back into the game tab.
  open_url: (m) => window.open(m.url, "_blank", "noopener,noreferrer"),
};

const bridge = new Bridge(port, handlers);
input = new Input(inputEl, bridge, (text) => { announceEl.textContent = text; });
inputEl.focus();

// Browser autoplay policy: the audio context starts suspended until a gesture.
document.addEventListener("keydown", () => {
  if (audio.ctx.state === "suspended") audio.ctx.resume();
}, { once: true });

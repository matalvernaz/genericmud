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

const output = new Output(outputEl);
const audio = new Audio();
const status = new Status(statusEl);

const handlers = {
  line: (m) => output.addLine(m),
  echo: (m) => output.addLine({ text: m.text }),
  sound: (m) => audio.play(m),
  stop_sound: (m) => audio.stop(m.channel),
  music: (m) => audio.play({ ...m, loop: true, channel: "music" }),
  status: (m) => status.update(m.gauges),
  review: () => { /* speech is native; visual reflection can be added later */ },
  connected: (m) => { announceEl.textContent = `Connected to ${m.world || "server"}`; },
  disconnected: () => { announceEl.textContent = "Disconnected"; },
};

const bridge = new Bridge(port, handlers);
new Input(inputEl, bridge);
inputEl.focus();

// Browser autoplay policy: the audio context starts suspended until a gesture.
document.addEventListener("keydown", () => {
  if (audio.ctx.state === "suspended") audio.ctx.resume();
}, { once: true });

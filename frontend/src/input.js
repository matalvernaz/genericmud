// Command input + key capture. Focus stays here so NVDA is in focus mode and
// passes keystrokes through. Plain typing and Up/Down history stay local;
// modified combos and function keys are forwarded to the engine, which owns the
// keymap (recall, review, flush, user macros).

const PASSTHROUGH_COMBOS = new Set([
  "alt+f4",
  "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "ctrl+y", "ctrl+f",
  "ctrl+left", "ctrl+right", "ctrl+home", "ctrl+end", "ctrl+delete",
  "ctrl+shift+left", "ctrl+shift+right", "ctrl+shift+home", "ctrl+shift+end",
  "f3", "shift+f3",
]);
const COMPLETE_FORWARD = "ctrl+space";
const COMPLETE_BACKWARD = "ctrl+shift+space";
const MAX_COMPLETION_WORDS = 500;
const MIN_COMPLETION_WORD_LENGTH = 3;
const OUTPUT_WORD = /[A-Za-z][A-Za-z'-]+/g;

export class Input {
  constructor(element, bridge, announce) {
    this.element = element;
    this.bridge = bridge;
    this.announce = announce || (() => {});  // speak UI feedback via the SR live region
    this.history = [];
    this.historyIndex = 0;
    // No parked draft: history moves only from an empty box or from a message that is itself
  // in history, so nothing unsent is ever replaced.
    this.masked = false;  // the server hides typed input (a password): no history
    this.words = [];
    this.completion = null;
    element.closest("form").addEventListener("submit", (e) => {
      e.preventDefault();
      this._submit();
    });
    element.addEventListener("keydown", (e) => this._onKey(e));
  }

  _submit() {
    const text = this.element.value;
    this.bridge.send({ type: "input", text });
    if (text && !this.masked) this.history.push(text);
    this.historyIndex = this.history.length;
    this.completion = null;
    this.element.value = "";
  }

  setMasked(masked) {
    this.masked = Boolean(masked);
  }

  // A link's half-written command: replaces what is in the box and puts the caret after it,
  // so the next keystroke extends it. Nothing is sent -- the player finishes it or clears it.
  setText(text) {
    this.element.value = String(text);
    const end = this.element.value.length;
    this.element.setSelectionRange(end, end);
    this.element.focus();
  }

  addOutput(text) {
    for (const word of String(text).match(OUTPUT_WORD) ?? []) {
      if (word.length < MIN_COMPLETION_WORD_LENGTH) continue;
      const key = word.toLowerCase();
      this.words = this.words.filter((candidate) => candidate.toLowerCase() !== key);
      this.words.unshift(word);
    }
    this.words.length = Math.min(this.words.length, MAX_COMPLETION_WORDS);
  }

  _onKey(event) {
    const key = event.key;
    const isFunctionKey = /^F\d{1,2}$/.test(key);
    const hasCommandModifier = event.ctrlKey || event.altKey || event.metaKey;
    if (event.getModifierState?.("AltGraph")) return;
    const combo = this._combo(event);
    if (combo === COMPLETE_FORWARD || combo === COMPLETE_BACKWARD) {
      event.preventDefault();
      this._complete(combo === COMPLETE_BACKWARD);
      return;
    }
    this.completion = null;
    if (key === "Enter" && !hasCommandModifier) return; // handled on submit
    if (event.metaKey) return; // preserve Command+C/V/A/Q and other platform shortcuts

    if (!hasCommandModifier && !isFunctionKey && key !== "Escape") {
      if (key === "ArrowUp") { if (this._historyStep(-1)) event.preventDefault(); }
      else if (key === "ArrowDown") { if (this._historyStep(1)) event.preventDefault(); }
      return; // ordinary typing
    }

    if (PASSTHROUGH_COMBOS.has(combo)) return;
    if (combo) {
      event.preventDefault();
      this.bridge.send({ type: "key", key: combo });
    }
  }

  // Move one entry through history, or leave the key to the field.
  //
  // History moves only from an empty box or from a message that is itself in history, and only
  // from the caret's first or last position. Anything else is a draft still being written, and
  // an arrow that replaced it would lose typed text -- the whole reason this is not simply
  // "up is older". Down from the newest entry empties the box, which is where Up starts again;
  // what was there is a history entry, one arrow away.
  _historyStep(direction) {
    const value = this.element.value;
    const caret = this.element.selectionStart ?? value.length;
    if (caret !== 0 && caret !== value.length) return false;
    if (!value) {
      if (direction > 0 || !this.history.length) return false;
      this._showHistory(this.history.length - 1);
      return true;
    }
    if (!this.history.includes(value)) return false;
    let index = this.historyIndex;
    if (!(index >= 0 && index < this.history.length && this.history[index] === value)) {
      index = this.history.lastIndexOf(value);
    }
    const target = index + direction;
    if (target < 0) return false;
    if (target >= this.history.length) {
      this.element.value = "";
      this.element.setSelectionRange(0, 0);
      this.historyIndex = this.history.length;
      return true;
    }
    this._showHistory(target);
    return true;
  }

  _showHistory(index) {
    this.historyIndex = index;
    this.element.value = this.history[index];
    const end = this.element.value.length;
    this.element.setSelectionRange(end, end);
  }

  _complete(backward) {
    if (!this.completion) {
      const value = this.element.value;
      const caret = this.element.selectionStart ?? value.length;
      const head = value.slice(0, caret);
      const start = Math.max(head.lastIndexOf(" "), head.lastIndexOf(";")) + 1;
      const prefix = head.slice(start);
      if (!prefix) { this.announce("nothing to complete"); return; }
      const needle = prefix.toLowerCase();
      const candidates = this.words.filter((word) => {
        const candidate = word.toLowerCase();
        return candidate.startsWith(needle) && candidate !== needle;
      });
      if (!candidates.length) { this.announce(`no match for ${prefix}`); return; }
      // The caret may sit inside a word; replace the whole word, not graft onto the tail.
      const rest = value.slice(caret);
      const wordEnd = rest.search(/[ ;]/);  // -1 if the word runs to the end of the line
      this.completion = {
        before: value.slice(0, start),
        tail: wordEnd < 0 ? "" : rest.slice(wordEnd),
        candidates,
        index: -1,
      };
    }

    const state = this.completion;
    if (backward) {
      state.index = state.index === -1
        ? state.candidates.length - 1
        : (state.index - 1 + state.candidates.length) % state.candidates.length;
    } else {
      state.index = (state.index + 1) % state.candidates.length;
    }
    const word = state.candidates[state.index];
    this.element.value = state.before + word + state.tail;
    const caret = state.before.length + word.length;
    this.element.setSelectionRange(caret, caret);
    // A programmatic value change is silent to a screen reader in focus mode; speak the
    // completed word ourselves, mirroring the native client.
    this.announce(word);
  }

  _combo(event) {
    const key = event.key.toLowerCase();
    if (["control", "alt", "shift", "meta"].includes(key)) return null;
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    const named = {
      arrowup: "up", arrowdown: "down", arrowleft: "left", arrowright: "right", " ": "space",
    };
    parts.push(named[key] ?? key);
    return parts.join("+");
  }
}

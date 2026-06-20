// Command input + key capture. Focus stays here so NVDA is in focus mode and
// passes keystrokes through. Plain typing and Up/Down history stay local;
// modified combos and function keys are forwarded to the engine, which owns the
// keymap (recall, review, flush, user macros).

export class Input {
  constructor(element, bridge) {
    this.element = element;
    this.bridge = bridge;
    this.history = [];
    this.historyIndex = 0;
    element.closest("form").addEventListener("submit", (e) => {
      e.preventDefault();
      this._submit();
    });
    element.addEventListener("keydown", (e) => this._onKey(e));
  }

  _submit() {
    const text = this.element.value;
    this.bridge.send({ type: "input", text });
    if (text) this.history.push(text);
    this.historyIndex = this.history.length;
    this.element.value = "";
  }

  _onKey(event) {
    const key = event.key;
    if (key === "Enter") return; // handled on submit
    const isFunctionKey = /^F\d{1,2}$/.test(key);
    const hasCommandModifier = event.ctrlKey || event.altKey || event.metaKey;

    if (!hasCommandModifier && !isFunctionKey) {
      if (key === "ArrowUp") { event.preventDefault(); this._recallHistory(-1); }
      else if (key === "ArrowDown") { event.preventDefault(); this._recallHistory(1); }
      return; // ordinary typing
    }

    const combo = this._combo(event);
    if (combo) {
      event.preventDefault();
      this.bridge.send({ type: "key", key: combo });
    }
  }

  _recallHistory(direction) {
    if (!this.history.length) return;
    this.historyIndex = Math.max(0, Math.min(this.history.length, this.historyIndex + direction));
    this.element.value = this.history[this.historyIndex] ?? "";
  }

  _combo(event) {
    const key = event.key.toLowerCase();
    if (["control", "alt", "shift", "meta"].includes(key)) return null;
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    const named = { arrowup: "up", arrowdown: "down", arrowleft: "left", arrowright: "right" };
    parts.push(named[key] ?? key);
    return parts.join("+");
  }
}

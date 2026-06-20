// The visual scrollback. Silent to the screen reader (the engine self-voices);
// this is what review navigates and what sighted users see.

const MAX_LINES = 5000;

export class Output {
  constructor(element) {
    this.element = element;
  }

  addLine(message) {
    // Fully gagged lines are hidden; gag-but-display lines stay visible/reviewable.
    if (message.gagged && !message.display_when_gagged) return;
    const atBottom = this._atBottom();
    const div = document.createElement("div");
    div.className = "line";
    div.textContent = message.text;
    this.element.appendChild(div);
    while (this.element.childElementCount > MAX_LINES) {
      this.element.removeChild(this.element.firstChild);
    }
    if (atBottom) this.element.scrollTop = this.element.scrollHeight;
  }

  _atBottom() {
    const { scrollTop, scrollHeight, clientHeight } = this.element;
    return scrollHeight - scrollTop - clientHeight < 40;
  }
}

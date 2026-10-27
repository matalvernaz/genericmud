// The visual scrollback. aria-live is off (the engine self-voices), so this is what review
// navigates and what sighted users see -- and, for a MUD sending MXP, real links the reader
// can focus and activate, the same as any other link in a browser.

const MAX_LINES = 5000;

export class Output {
  constructor(element, onLink = null) {
    this.element = element;
    this.onLink = onLink;
    // Delegated, so a link anchors itself to nothing beyond its own data and lines trimmed
    // off the top of the scrollback take their handlers with them.
    element.addEventListener("click", (event) => {
      const anchor = event.target.closest("a.mxp-link");
      if (!anchor) return;
      // href is "#" only to earn native link behaviour (Enter activates a focused anchor);
      // navigating the page would lose the session.
      event.preventDefault();
      this.onLink?.(anchor.dataset.words || anchor.textContent, anchor.dataset.command);
    });
  }

  addLine(message) {
    // Fully gagged lines are hidden; gag-but-display lines stay visible/reviewable.
    if (message.gagged && !message.display_when_gagged) return;
    const atBottom = this._atBottom();
    const div = document.createElement("div");
    div.className = "line";
    this._fill(div, message.text, message.links);
    this.element.appendChild(div);
    while (this.element.childElementCount > MAX_LINES) {
      this.element.removeChild(this.element.firstChild);
    }
    if (atBottom) this.element.scrollTop = this.element.scrollHeight;
  }

  // Plain text with an <a> over each link's words. The engine sends offsets into the text it
  // already decided is text, so nothing is re-parsed here and no markup can leak.
  _fill(div, text, links) {
    if (!links || links.length === 0) {
      div.textContent = text;
      return;
    }
    let at = 0;
    for (const [start, end, command] of links) {
      if (start > at) div.appendChild(document.createTextNode(text.slice(at, start)));
      const anchor = document.createElement("a");
      anchor.className = "mxp-link";
      anchor.href = "#";
      const words = text.slice(start, end);
      anchor.textContent = words;
      // The command is data, not the anchor's text: a substituted or rewritten line must not
      // be able to change what gets sent.
      anchor.dataset.command = command;
      anchor.dataset.words = words;
      div.appendChild(anchor);
      at = end;
    }
    if (at < text.length) div.appendChild(document.createTextNode(text.slice(at)));
  }

  _atBottom() {
    const { scrollTop, scrollHeight, clientHeight } = this.element;
    return scrollHeight - scrollTop - clientHeight < 40;
  }
}

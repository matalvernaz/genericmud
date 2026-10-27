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
    this._fill(div, message.text, message.links, message.images);
    this.element.appendChild(div);
    while (this.element.childElementCount > MAX_LINES) {
      this.element.removeChild(this.element.firstChild);
    }
    if (atBottom) this.element.scrollTop = this.element.scrollHeight;
  }

  // Plain text with an <a> over each link's words and an <img> where each image sat. The engine
  // sends offsets into text it already decided is text, so nothing is re-parsed here and no
  // markup can leak into the page.
  _fill(div, text, links, images) {
    const spans = (links || []).map(([start, end, command]) => ({
      start, end, kind: "link", command,
    }));
    // An image is zero-width: it sits at a point in the text rather than covering words.
    for (const [offset, url] of images || []) {
      spans.push({ start: offset, end: offset, kind: "image", url });
    }
    if (spans.length === 0) {
      div.textContent = text;
      return;
    }
    // Offset order; where a link and an image start together the link comes first, so the
    // image sits at its edge rather than splitting the anchor.
    spans.sort((a, b) => a.start - b.start || b.end - a.end);
    let at = 0;
    for (const span of spans) {
      if (span.start > at) div.appendChild(document.createTextNode(text.slice(at, span.start)));
      if (span.kind === "image") {
        div.appendChild(this._image(span.url));
        continue;
      }
      const anchor = document.createElement("a");
      anchor.className = "mxp-link";
      anchor.href = "#";
      const words = text.slice(span.start, span.end);
      anchor.textContent = words;
      // The command is data, not the anchor's text: a substituted or rewritten line must not
      // be able to change what gets sent.
      anchor.dataset.command = span.command;
      anchor.dataset.words = words;
      div.appendChild(anchor);
      at = Math.max(at, span.end);
    }
    if (at < text.length) div.appendChild(document.createTextNode(text.slice(at)));
  }

  _image(url) {
    const img = document.createElement("img");
    img.className = "mxp-image";
    img.src = url;
    // Deliberately no alt attribute: an empty alt would hide the image from a screen reader
    // entirely, and the point is that something visual was there. MXP gives an image no
    // description to use, so "graphic" is the most this client can honestly say about it.
    img.decoding = "async";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";  // don't tell the MUD's web server where we came from
    img.addEventListener("error", () => img.remove());
    return img;
  }

  _atBottom() {
    const { scrollTop, scrollHeight, clientHeight } = this.element;
    return scrollHeight - scrollTop - clientHeight < 40;
  }
}

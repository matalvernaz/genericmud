// WebSocket bridge to the Python engine (localhost). Auto-reconnects so the
// renderer survives an engine restart.

export class Bridge {
  constructor(port, handlers) {
    this.url = `ws://127.0.0.1:${port}`;
    this.handlers = handlers;
    this.ws = null;
    this._connect();
  }

  _connect() {
    this.ws = new WebSocket(this.url);
    this.ws.onopen = () => {
      // Authenticate to the bridge before anything else: the page was served with a per-run
      // token in its URL; the bridge rejects any connection that doesn't echo it (anti-CSWSH).
      const token = new URLSearchParams(location.search).get("token");
      if (token) this.send({ type: "hello", token });
    };
    this.ws.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      const handler = this.handlers[message.type];
      if (handler) handler(message);
    };
    this.ws.onclose = () => setTimeout(() => this._connect(), 1000);
    this.ws.onerror = () => this.ws.close();
  }

  send(object) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(object));
    }
  }
}

// A <SEND> menu asks which command was meant: a strip of buttons along the bottom, one per
// command, labelled with the MUD's caption or the command itself. Nothing is sent until one is
// picked, and the engine still decides what the command may do. Cancel or Escape sends
// nothing. Focus goes to the first choice, which is what announces the menu (its group is
// labelled "Choose: <the link's words>"), and back to wherever it was once the menu closes.

let open = null;

// Close the open menu, if any, without sending anything. Returns whether one was open.
export function closeMenu() {
  if (!open) return false;
  const { strip, before } = open;
  open = null;
  strip.remove();
  before?.focus?.();
  return true;
}

export function showMenu(doc, message, { send }) {
  // A second menu replaces the first rather than stacking; focus still returns to where the
  // player was before either of them.
  const before = open ? open.before : doc.activeElement;
  if (open) {
    open.strip.remove();
    open = null;
  }
  const strip = doc.createElement("div");
  strip.className = "mxp-menu";
  strip.setAttribute("role", "group");
  strip.setAttribute("aria-label", `Choose: ${message.text}`);

  for (const [command, caption] of message.choices) {
    const button = doc.createElement("button");
    button.type = "button";
    button.textContent = caption || command;
    button.addEventListener("click", () => {
      closeMenu();
      send({ type: "menu_choice", text: caption || command, command });
    });
    strip.appendChild(button);
  }
  const cancel = doc.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => closeMenu());
  strip.appendChild(cancel);

  strip.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
    }
  });

  doc.body.appendChild(strip);
  open = { strip, before };
  strip.children[0]?.focus?.();
  return strip;
}

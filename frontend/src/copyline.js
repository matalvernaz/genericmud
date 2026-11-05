// Copy all or part of the reviewed line (Ctrl+Shift+C): the web window's version of the native
// "Copy from line" box. The line sits in a read-only field with all of it selected, so Enter
// copies the whole line and the usual selection keys pick out part of it first. A modal
// <dialog> keeps focus inside it and closes on Escape by itself.

// What to copy: the part of the field that is selected, or the whole line when nothing is.
export function chosenText(field) {
  const start = field.selectionStart ?? 0;
  const end = field.selectionEnd ?? 0;
  const picked = end > start ? field.value.slice(start, end) : "";
  return picked || field.value;
}

// Open the box. `announce` speaks a status line, `writeClipboard` returns a promise, and focus
// goes back to whatever had it when the key was pressed.
export function showCopyLine(doc, text, { announce, writeClipboard }) {
  const before = doc.activeElement;
  const dialog = doc.createElement("dialog");
  dialog.setAttribute("aria-label", "Copy from line");

  const label = doc.createElement("label");
  label.textContent = "Line ";
  const field = doc.createElement("input");
  field.type = "text";
  field.readOnly = true;
  field.value = text;
  label.appendChild(field);

  const copy = doc.createElement("button");
  copy.type = "button";
  copy.textContent = "Copy";
  const close = doc.createElement("button");
  close.type = "button";
  close.textContent = "Close";
  dialog.append(label, copy, close);

  // Closing by any route (Close, Escape, after a copy) tidies up and hands focus back.
  dialog.addEventListener("close", () => {
    dialog.remove();
    before?.focus?.();
  });

  const copyAndClose = () => {
    const chosen = chosenText(field);
    dialog.close();
    Promise.resolve()
      .then(() => writeClipboard(chosen))
      .then(
        () => announce("Copied"),
        // A browser refuses the clipboard when the page lacks permission; say so rather than
        // leaving the player to paste the wrong thing later.
        () => announce("Couldn't copy to the clipboard"),
      );
  };
  copy.addEventListener("click", copyAndClose);
  close.addEventListener("click", () => dialog.close());
  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      copyAndClose();
    }
  });

  doc.body.appendChild(dialog);
  dialog.showModal();
  field.focus();
  field.select();
  return dialog;
}

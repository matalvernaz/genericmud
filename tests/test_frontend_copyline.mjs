// The web window's "Copy from line" box (Ctrl+Shift+C), driven through a small fake page.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../frontend/src/copyline.js", import.meta.url),
  "utf8",
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const { chosenText, showCopyLine } = await import(moduleUrl);

// Just enough of a document for the box: elements that hold children, listen for events and
// take focus, an input with select(), and a dialog that opens modally and fires "close".
function fakeDocument() {
  const doc = { activeElement: null };
  doc.createElement = (tag) => {
    const listeners = {};
    const element = {
      tag,
      children: [],
      attributes: {},
      removed: false,
      setAttribute(name, value) { this.attributes[name] = value; },
      append(...children) { this.children.push(...children); },
      appendChild(child) { this.children.push(child); },
      addEventListener(type, listener) { (listeners[type] ??= []).push(listener); },
      dispatch(type, event = {}) { for (const listener of listeners[type] ?? []) listener(event); },
      focus() { doc.activeElement = this; },
      remove() { this.removed = true; },
    };
    if (tag === "input") {
      element.select = function select() {
        this.selectionStart = 0;
        this.selectionEnd = this.value.length;
      };
    }
    if (tag === "dialog") {
      element.showModal = function showModal() { this.open = true; };
      element.close = function close() {
        if (!this.open) return;
        this.open = false;
        this.dispatch("close");
      };
    }
    return element;
  };
  doc.body = doc.createElement("body");
  return doc;
}

function open(text, writeClipboard) {
  const doc = fakeDocument();
  const commandBox = doc.createElement("input");
  commandBox.focus();
  const said = [];
  const dialog = showCopyLine(doc, text, { announce: (line) => said.push(line), writeClipboard });
  const field = dialog.children[0].children[0];
  const [copyButton, closeButton] = dialog.children.slice(1);
  return { doc, commandBox, said, dialog, field, copyButton, closeButton };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("nothing or everything selected copies the whole line; a selection copies just that", () => {
  assert.equal(chosenText({ value: "A wolf howls", selectionStart: 0, selectionEnd: 0 }), "A wolf howls");
  assert.equal(chosenText({ value: "A wolf howls", selectionStart: 0, selectionEnd: 12 }), "A wolf howls");
  assert.equal(chosenText({ value: "the code is 4521", selectionStart: 12, selectionEnd: 16 }), "4521");
});

test("the box opens on the line with all of it selected", () => {
  const { doc, field, dialog } = open("A wolf howls", async () => {});
  assert.equal(dialog.open, true);
  assert.equal(dialog.attributes["aria-label"], "Copy from line");
  assert.equal(field.readOnly, true);
  assert.equal(doc.activeElement, field);
  assert.equal(chosenText(field), "A wolf howls");
});

test("Enter copies, says so, closes the box and gives focus back", async () => {
  const copied = [];
  const { commandBox, doc, said, dialog, field } = open("A wolf howls", async (text) => {
    copied.push(text);
  });
  let prevented = false;
  field.dispatch("keydown", { key: "Enter", preventDefault: () => { prevented = true; } });
  await settle();
  assert.equal(prevented, true);
  assert.deepEqual(copied, ["A wolf howls"]);
  assert.deepEqual(said, ["Copied"]);
  assert.equal(dialog.removed, true);
  assert.equal(doc.activeElement, commandBox);
});

test("the Copy button copies only the part selected", async () => {
  const copied = [];
  const { field, copyButton } = open("the code is 4521", async (text) => { copied.push(text); });
  field.selectionStart = 12;
  field.selectionEnd = 16;
  copyButton.dispatch("click");
  await settle();
  assert.deepEqual(copied, ["4521"]);
});

test("a refused clipboard is reported, not passed off as copied", async () => {
  const { said, field } = open("A wolf howls", async () => { throw new Error("denied"); });
  field.dispatch("keydown", { key: "Enter", preventDefault() {} });
  await settle();
  assert.deepEqual(said, ["Couldn't copy to the clipboard"]);
});

test("Close copies nothing and gives focus back", async () => {
  const copied = [];
  const { commandBox, doc, said, dialog, closeButton } = open("A wolf howls", async (text) => {
    copied.push(text);
  });
  closeButton.dispatch("click");
  await settle();
  assert.deepEqual(copied, []);
  assert.deepEqual(said, []);
  assert.equal(dialog.removed, true);
  assert.equal(doc.activeElement, commandBox);
});

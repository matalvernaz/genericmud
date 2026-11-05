// The web window's <SEND> menus: a click on a menu link asks instead of sending, and the
// strip of choices sends only what is picked, driven through a small fake page.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function load(name) {
  const source = await readFile(new URL(`../frontend/src/${name}`, import.meta.url), "utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
}
const { showMenu, closeMenu } = await load("menu.js");
const { Output } = await load("output.js");

// Just enough of a document: elements that hold children, take attributes and focus, and
// listen for events.
function fakeDocument() {
  const doc = { activeElement: null };
  doc.createElement = (tag) => {
    const listeners = {};
    return {
      tag,
      children: [],
      attributes: {},
      dataset: {},
      style: {},
      className: "",
      removed: false,
      textContent: "",
      setAttribute(name, value) { this.attributes[name] = value; },
      append(...children) { this.children.push(...children); },
      appendChild(child) { this.children.push(child); },
      addEventListener(type, listener) { (listeners[type] ??= []).push(listener); },
      dispatch(type, event = {}) { for (const listener of listeners[type] ?? []) listener(event); },
      focus() { doc.activeElement = this; },
      remove() { this.removed = true; },
    };
  };
  doc.createTextNode = (text) => ({ text });
  doc.body = doc.createElement("body");
  return doc;
}

function openMenu(choices = [["buy sword", "Buy it"], ["examine sword", ""]]) {
  closeMenu();
  const doc = fakeDocument();
  const reading = doc.createElement("a");
  reading.focus(); // the player was on the link in the output
  const sent = [];
  const strip = showMenu(doc, { text: "a sword", choices }, { send: (m) => sent.push(m) });
  return { doc, reading, sent, strip };
}

test("the menu lists each choice by its caption, or its command, then Cancel", () => {
  const { doc, strip } = openMenu();
  assert.equal(strip.attributes.role, "group");
  assert.equal(strip.attributes["aria-label"], "Choose: a sword");
  assert.deepEqual(strip.children.map((button) => button.textContent), ["Buy it", "examine sword", "Cancel"]);
  assert.equal(doc.activeElement, strip.children[0]); // focus announces the menu
});

test("picking a choice sends that command only, closes the menu and gives focus back", () => {
  const { doc, reading, sent, strip } = openMenu();
  strip.children[1].dispatch("click");
  assert.deepEqual(sent, [{ type: "menu_choice", text: "examine sword", command: "examine sword" }]);
  assert.equal(strip.removed, true);
  assert.equal(doc.activeElement, reading);
});

test("Escape and Cancel close the menu and send nothing", () => {
  for (const close of [
    (strip) => strip.dispatch("keydown", { key: "Escape", preventDefault() {} }),
    (strip) => strip.children.at(-1).dispatch("click"),
  ]) {
    const { doc, reading, sent, strip } = openMenu();
    close(strip);
    assert.deepEqual(sent, []);
    assert.equal(strip.removed, true);
    assert.equal(doc.activeElement, reading);
  }
});

test("a second menu replaces the first and focus still returns to where the player was", () => {
  const { doc, reading, strip } = openMenu();
  const second = showMenu(doc, { text: "a shield", choices: [["buy shield", ""], ["wear shield", ""]] }, {
    send() {},
  });
  assert.equal(strip.removed, true);
  closeMenu();
  assert.equal(second.removed, true);
  assert.equal(doc.activeElement, reading);
});

function clickOn(anchor) {
  const followed = [];
  const asked = [];
  let listener = null;
  const element = { addEventListener: (_type, handler) => { listener = handler; } };
  new Output(element, (...args) => followed.push(args), (...args) => asked.push(args));
  listener({ target: { closest: () => anchor }, preventDefault() {} });
  return { followed, asked };
}

test("clicking a menu link in the page asks which command, and sends none", () => {
  const anchor = {
    textContent: "a sword",
    dataset: {
      words: "a sword",
      command: "buy sword",
      choices: JSON.stringify([["buy sword", "Buy it"], ["examine sword", ""]]),
    },
  };
  const { followed, asked } = clickOn(anchor);
  assert.deepEqual(followed, []);
  assert.deepEqual(asked, [["a sword", [["buy sword", "Buy it"], ["examine sword", ""]]]]);
});

test("clicking an ordinary link still follows it", () => {
  const anchor = { textContent: "north", dataset: { words: "north", command: "north" } };
  const { followed, asked } = clickOn(anchor);
  assert.deepEqual(asked, []);
  assert.deepEqual(followed, [["north", "north", false]]);
});

test("a menu link drawn in the page carries its choices", () => {
  const doc = fakeDocument();
  globalThis.document = doc;
  try {
    const output = Object.create(Output.prototype);
    const div = doc.createElement("div");
    output._fill(div, "a sword here", [[0, 7, "buy sword", false, [["buy sword", "Buy it"], ["examine sword", ""]]]], []);
    const anchor = div.children.find((child) => child.dataset?.command);
    assert.equal(anchor.dataset.command, "buy sword");
    assert.deepEqual(JSON.parse(anchor.dataset.choices), [["buy sword", "Buy it"], ["examine sword", ""]]);
  } finally {
    delete globalThis.document;
  }
});

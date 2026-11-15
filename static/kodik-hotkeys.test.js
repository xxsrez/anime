const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const app = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const from = app.indexOf("function isEditableShortcutTarget(");
const to = app.indexOf("let pictureInPictureRequest", from);
assert.ok(from >= 0 && to > from);

function setup() {
  const messages = [];
  let fullscreens = 0;
  const body = {};
  const context = vm.createContext({
    state: { detail: {}, playerContext: { nativeKodik: true, loaded: true } },
    document: { body },
    window: { location: { origin: "https://catalog.test" } },
    el: {
      list: { contains: target => Boolean(target?.inList) },
      titleDetailView: { hidden: false },
      player: { contentWindow: { postMessage: (data, origin) => messages.push({ data, origin }) } },
    },
    toggleFullscreen: async () => { fullscreens++; },
    reportActionError: () => () => {},
  });
  vm.runInContext(app.slice(from, to), context);
  return { context, messages, fullscreens: () => fullscreens, body };
}

function key(key, extra = {}) {
  return { key, target: {}, prevented: false, preventDefault() { this.prevented = true; }, ...extra };
}

test("parent sends arrows only to the active ad-free player", () => {
  const { context, messages } = setup();
  let event = key("ArrowRight");
  context.handlePlayerHotkey(event);
  assert.equal(event.prevented, true);
  assert.equal(messages[0].origin, "https://catalog.test");
  assert.equal(messages[0].data.value.method, "seek_relative");
  assert.equal(messages[0].data.value.seconds, 15);
  for (const extra of [{ ctrlKey: true }, { isComposing: true }, { target: { inList: true } },
    { target: { closest: () => ({}) } }]) {
    event = key("ArrowLeft", extra);
    context.handlePlayerHotkey(event);
    assert.equal(event.prevented, false);
  }
  context.state.playerContext.nativeKodik = false;
  event = key("ArrowLeft");
  context.handlePlayerHotkey(event);
  assert.equal(event.prevented, false);
  context.state.playerContext.nativeKodik = true;
  context.state.playerContext.loaded = false;
  context.handlePlayerHotkey(key("ArrowLeft"));
  context.state.playerContext.loaded = true;
  context.el.titleDetailView.hidden = true;
  context.handlePlayerHotkey(key("ArrowLeft"));
  assert.equal(messages.length, 1);
});

test("parent fullscreen shortcut uses physical F across layouts without hijacking controls", () => {
  const { context, fullscreens } = setup();
  for (const event of [key("а", { code: "KeyF" }), key("x", { code: "KeyF" }), key("f")]) {
    context.handlePlayerHotkey(event);
    assert.equal(event.prevented, true);
  }
  context.handlePlayerHotkey(key("f", { metaKey: true }));
  context.handlePlayerHotkey(key("f", { repeat: true }));
  context.handlePlayerHotkey(key("f", { isComposing: true }));
  context.handlePlayerHotkey(key("f", { target: { closest: () => ({}) } }));
  assert.equal(fullscreens(), 3);
  const button = { closest: selector => selector.includes("button") ? {} : null };
  context.state.playerContext.nativeKodik = false;
  const focusedButton = key("f", { target: button });
  context.handlePlayerHotkey(focusedButton);
  assert.equal(focusedButton.prevented, true, "F continues to work on buttons for other players");
  assert.equal(fullscreens(), 4);
});

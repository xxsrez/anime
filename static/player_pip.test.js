const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const action = source.slice(source.indexOf("let pictureInPictureRequest ="), source.indexOf("async function selectAnime("));

function setup({ active = true, provider = "kodik", exiting = false, origin = "https://kodikplayer.com" } = {}) {
  const messages = [];
  const statuses = [];
  const timers = new Map();
  let nextTimer = 0;
  const player = {
    getAttribute: () => active ? `${origin}/season/example` : null,
    contentWindow: { postMessage: (data, target) => messages.push({ data, target }) },
  };
  const sandbox = {
    el: { player, pipToggle: {} },
    document: { getElementById: () => ({open: true}) },
    window: {
      setTimeout: callback => { timers.set(++nextTimer, callback); return nextTimer; },
      clearTimeout: id => timers.delete(id),
    },
    reportClientError() {},
    state: {
      playerContext: { messageProvider: provider, playerOrigin: origin },
      watchSession: { pictureInPictureActive: exiting, providerPlaybackActive: true },
    },
    setPlayerActionState: (text, tone) => statuses.push({ text, tone }),
  };
  vm.createContext(sandbox);
  vm.runInContext(action, sandbox);
  return { sandbox, messages, statuses, timers };
}

test("Kodik PiP targets the existing frame's exact origin without claiming success", async () => {
  const { sandbox, messages, statuses } = setup();
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 1);
  assert.equal(messages[0].target, "https://kodikplayer.com");
  assert.equal(messages[0].data.key, "kodik_player_api");
  assert.equal(messages[0].data.value.method, "enter_pip");
  assert.equal(sandbox.state.watchSession.pictureInPictureActive, false);
  assert.match(statuses[0].text, /Открываем/);
});

test("confirmed active PiP sends exit to the same player", async () => {
  const { sandbox, messages } = setup({ exiting: true });
  await sandbox.openPictureInPicture();
  assert.equal(messages[0].data.value.method, "exit_pip");
});

test("other providers get an actionable explanation without Kodik commands", async () => {
  const { sandbox, messages, statuses } = setup({ provider: "other" });
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 0);
  assert.match(statuses[0].text, /Этот плеер не поддерживает/);
});

test("unconfirmed requests time out, allow retry and never invent active PiP", async () => {
  const { sandbox, messages, statuses, timers } = setup();
  await sandbox.openPictureInPicture();
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 1);
  [...timers.values()][0]();
  assert.match(statuses.at(-1).text, /не подтвердил/);
  assert.equal(sandbox.el.pipToggle.disabled, false);
  assert.equal(sandbox.state.watchSession.pictureInPictureActive, false);
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 2);
});

test("confirmation or source replacement cancels the old timeout", async () => {
  const { sandbox, timers } = setup();
  await sandbox.openPictureInPicture();
  sandbox.cancelPictureInPictureRequest();
  assert.equal(timers.size, 0);
  assert.equal(sandbox.el.pipToggle.disabled, false);
});

test("unstarted playback explains prerequisite instead of sending a failing command", async () => {
  const { sandbox, messages, statuses } = setup();
  sandbox.state.watchSession.providerPlaybackActive = false;
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 0);
  assert.match(statuses[0].text, /Сначала запустите/);
});

test("postMessage failure restores the menu and reports a useful error", async () => {
  const { sandbox, statuses, timers } = setup();
  sandbox.el.player.contentWindow.postMessage = () => { throw new Error("failed"); };
  await sandbox.openPictureInPicture();
  assert.equal(timers.size, 0);
  assert.equal(sandbox.el.pipToggle.disabled, false);
  assert.match(statuses.at(-1).text, /Не удалось связаться/);
});

test("missing origin never broadcasts a command", async () => {
  const { sandbox, messages } = setup({ origin: "" });
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 0);
});

test("no active video gives a specific message", async () => {
  const { sandbox, messages, statuses } = setup({ active: false });
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 0);
  assert.equal(statuses[0].text, "Нет активного видео");
});

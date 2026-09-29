const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const action = source.slice(source.indexOf("async function openPictureInPicture()"), source.indexOf("async function selectAnime("));

function setup({ active = true, provider = "kodik", exiting = false, origin = "https://kodikplayer.com" } = {}) {
  const messages = [];
  const statuses = [];
  const player = {
    getAttribute: () => active ? `${origin}/season/example` : null,
    contentWindow: { postMessage: (data, target) => messages.push({ data, target }) },
  };
  const sandbox = {
    el: { player },
    state: {
      playerContext: { messageProvider: provider, playerOrigin: origin },
      watchSession: { pictureInPictureActive: exiting },
    },
    setPlayerActionState: (text, tone) => statuses.push({ text, tone }),
  };
  vm.createContext(sandbox);
  vm.runInContext(action, sandbox);
  return { sandbox, messages, statuses };
}

test("Kodik PiP targets the existing frame's exact origin without claiming success", async () => {
  const { sandbox, messages, statuses } = setup();
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 1);
  assert.equal(messages[0].target, "https://kodikplayer.com");
  assert.equal(messages[0].data.key, "kodik_player_api");
  assert.equal(messages[0].data.value.method, "enter_pip");
  assert.equal(sandbox.state.watchSession.pictureInPictureActive, false);
  assert.equal(statuses[0].text, "");
});

test("confirmed active PiP sends exit to the same player", async () => {
  const { sandbox, messages } = setup({ exiting: true });
  await sandbox.openPictureInPicture();
  assert.equal(messages[0].data.value.method, "exit_pip");
});

test("other providers receive neither Kodik commands nor placeholder instructions", async () => {
  const { sandbox, messages, statuses } = setup({ provider: "other" });
  await sandbox.openPictureInPicture();
  assert.equal(messages.length, 0);
  assert.equal(statuses.length, 0);
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

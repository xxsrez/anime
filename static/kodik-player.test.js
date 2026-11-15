const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(`${__dirname}/kodik-player.js`, "utf8");

function setup(payload, { serial = null, preferences = new Map(), hlsSupport = false, nativeSupport = true } = {}) {
  const sent = [];
  const requested = [];
  const players = [];
  class Element {
    constructor() { this.listeners = {}; this.hidden = false; this.disabled = false; this.value = ""; this.open = false; this.attributes = {}; }
    addEventListener(event, callback, options = {}) { (this.listeners[event] ||= []).push({ callback, once: options.once }); }
    dispatch(event) {
      const listeners = [...(this.listeners[event] || [])];
      this.listeners[event] = (this.listeners[event] || []).filter(listener => !listener.once);
      for (const { callback } of listeners) callback({ type: event });
    }
    replaceChildren(...children) { this.children = children; }
    contains(target) { return target === this || Boolean(target?.insideSettings); }
    focus() { this.focused = true; document.activeElement = this; }
    setAttribute(name, value) { this.attributes[name] = value; }
  }
  const elements = Object.fromEntries(["quality", "translation", "translation-label", "resume", "skip-intro", "pip", "pip-label", "player-settings", "settings-toggle", "settings-close", "settings-quality", "message", "retry", "original"].map(
    id => [id, new Element()],
  ));
  elements["skip-intro"].hidden = true;
  const video = new Element();
  Object.assign(video, {
    currentTime: 0, duration: 1200, readyState: 1, paused: true, ended: false,
    canPlayType: () => nativeSupport ? "maybe" : "", load() { this.currentTime = 0; }, play: async () => {},
  });
  const parent = { postMessage: (message, origin) => sent.push([message, origin]), document: {} };
  let scheduled = null;
  const windowListeners = {};
  const window = {
    parent, addEventListener: (event, callback) => { windowListeners[event] = callback; },
    matchMedia: () => ({ matches: false }),
    setTimeout: callback => { scheduled = callback; return 1; },
    clearTimeout: () => { scheduled = null; },
  };
  if (hlsSupport) {
    window.Hls = class {
      static Events = { ERROR: "error" };
      static isSupported() { return true; }
      constructor(options) { this.options = options; players.push(this); }
      on(_event, callback) { this.onError = callback; }
      loadSource(url) { this.url = url; }
      attachMedia(element) { this.video = element; element.load(); }
      stopLoad() { this.stopped = true; }
      destroy() { this.destroyed = true; }
    };
  }
  const documentListeners = {};
  const document = {
    querySelector: () => video,
    getElementById: id => elements[id],
    createElement: () => new Element(),
    addEventListener: (event, callback) => { documentListeners[event] = callback; },
    pictureInPictureEnabled: false,
    documentElement: {},
  };
  const context = vm.createContext({
    document, window, URL, URLSearchParams,
    location: { pathname: "/player/kodik/123", origin: "https://catalog.test", search: serial ? `?serial=${serial}` : "" },
    localStorage: { getItem: key => preferences.get(key), setItem: (key, value) => preferences.set(key, value) },
    fetch: async url => {
      requested.push(url);
      const value = typeof payload === "function" ? await payload(url) : payload;
      if (value?.errorStatus) return { ok: false, status: value.errorStatus };
      return { ok: true, status: 200, json: async () => value };
    },
  });
  vm.runInContext(source, context);
  return { video, elements, sent, requested, players, document, documentListeners, windowListeners, parent, runTimer: () => scheduled?.() };
}

function keyEvent(key, extra = {}) {
  return { key, target: {}, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...extra };
}

test("opening is offered only in its verified interval and skips without playing", async () => {
  const player = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" }, intro: { start: 10, end: 96 } });
  await new Promise(setImmediate);
  player.video.dispatch("loadedmetadata");
  assert.equal(player.elements["skip-intro"].hidden, true);
  player.video.currentTime = 20;
  player.video.dispatch("timeupdate");
  assert.equal(player.elements["skip-intro"].hidden, false);
  player.elements["skip-intro"].dispatch("click");
  assert.equal(player.video.currentTime, 96);
  assert.equal(player.video.paused, true);
  assert.equal(player.sent.at(-1)[0].key, "anime_catalog_kodik_seeking");
  assert.equal(player.elements["skip-intro"].hidden, true);
  assert.equal(player.video.focused, true);
  player.video.currentTime = 5;
  player.video.dispatch("seeked");
  assert.equal(player.elements["skip-intro"].hidden, true);
});

test("invalid or implausible opening markers never expose the button", async () => {
  for (const intro of [null, { start: 0, end: 1300 }, { start: 900, end: 960 }, { start: 0, end: "96" }]) {
    const player = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" }, intro });
    await new Promise(setImmediate);
    player.video.currentTime = intro?.start || 0;
    player.video.dispatch("loadedmetadata");
    assert.equal(player.elements["skip-intro"].hidden, true);
  }
});

test("opening follows the successfully selected voice, including failed switches", async () => {
  const translations = [{ id: "963", title: "First" }, { id: "17", title: "Second" }];
  let failSwitch = true;
  const player = setup(request => request.includes("translation=17") && failSwitch ? { errorStatus: 503 } : {
    streams: { "720": "https://p14.solodcdn.com/test.m3u8" }, translations,
    translation_id: request.includes("translation=17") ? "17" : "963",
    intro: request.includes("translation=17") ? { start: 40, end: 120 } : { start: 0, end: 90 },
  }, { serial: "55" });
  await new Promise(setImmediate);
  player.video.currentTime = 20;
  player.video.dispatch("loadedmetadata");
  assert.equal(player.elements["skip-intro"].hidden, false);
  player.elements.translation.value = "17";
  player.elements.translation.dispatch("change");
  await new Promise(setImmediate);
  player.video.dispatch("timeupdate");
  assert.equal(player.elements["skip-intro"].hidden, false, "failed switch retains the playing voice marker");
  failSwitch = false;
  player.elements.translation.value = "17";
  player.elements.translation.dispatch("change");
  await new Promise(setImmediate);
  player.video.currentTime = 50;
  player.video.dispatch("loadedmetadata");
  player.video.currentTime = 50;
  player.video.dispatch("timeupdate");
  player.elements["skip-intro"].dispatch("click");
  assert.equal(player.video.currentTime, 120);
});

test("keyboard seeks 15 seconds once, preserves pause and ignores controls or modifiers", async () => {
  const player = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } });
  await new Promise(setImmediate);
  player.video.currentTime = 40;
  const right = keyEvent("ArrowRight");
  player.documentListeners.keydown(right);
  assert.equal(player.video.currentTime, 55);
  assert.equal(player.video.paused, true);
  assert.equal(right.prevented && right.stopped, true);
  assert.equal(player.sent.at(-1)[0].key, "anime_catalog_kodik_seeking");
  for (const extra of [{ ctrlKey: true }, { isComposing: true }, { target: { closest: () => ({}) } }]) {
    const event = keyEvent("ArrowLeft", extra);
    player.documentListeners.keydown(event);
    assert.equal(event.prevented, false);
    assert.equal(player.video.currentTime, 55);
  }
  player.video.currentTime = 3;
  player.documentListeners.keydown(keyEvent("ArrowLeft", { repeat: true }));
  assert.equal(player.video.currentTime, 0);
  player.video.currentTime = 1195;
  player.documentListeners.keydown(keyEvent("ArrowRight"));
  assert.equal(player.video.currentTime, 1199);
});

test("seek during a source reload is applied to the pending position", async () => {
  const player = setup({ streams: { "480": "https://p14.solodcdn.com/a.m3u8", "720": "https://p14.solodcdn.com/b.m3u8" } }, { hlsSupport: true });
  await new Promise(setImmediate);
  player.video.currentTime = 100;
  player.video.readyState = 0;
  player.elements.quality.value = "480";
  player.elements.quality.dispatch("change");
  player.documentListeners.keydown(keyEvent("ArrowRight"));
  player.video.dispatch("loadedmetadata");
  assert.equal(player.video.currentTime, 115);
});

test("iframe accepts only bounded seek commands from its own parent", async () => {
  const player = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } });
  await new Promise(setImmediate);
  player.video.currentTime = 50;
  const send = (source, origin, seconds) => player.windowListeners.message({
    source, origin, data: { key: "kodik_player_api", value: { method: "seek_relative", seconds } },
  });
  send({}, "https://catalog.test", 15);
  send(player.parent, "https://evil.test", 15);
  send(player.parent, "https://catalog.test", "15");
  send(player.parent, "https://catalog.test", 30);
  assert.equal(player.video.currentTime, 50);
  send(player.parent, "https://catalog.test", -15);
  assert.equal(player.video.currentTime, 35);
});

test("physical F works with any layout and ignores repeat or editable targets", async () => {
  const player = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } });
  await new Promise(setImmediate);
  let requests = 0;
  player.document.documentElement.requestFullscreen = async () => { requests++; };
  for (const event of [keyEvent("а", { code: "KeyF" }), keyEvent("u", { code: "KeyF" }), keyEvent("f")]) {
    player.documentListeners.keydown(event);
    assert.equal(event.prevented, true);
  }
  player.documentListeners.keydown(keyEvent("f", { repeat: true }));
  player.documentListeners.keydown(keyEvent("f", { target: { closest: () => ({}) } }));
  player.documentListeners.keydown(keyEvent("f", { isComposing: true }));
  assert.equal(requests, 3);
  const button = { closest: selector => selector.includes("button") ? {} : null };
  player.documentListeners.keydown(keyEvent("f", { target: button }));
  player.documentListeners.keydown(keyEvent("ArrowRight", { target: button }));
  assert.equal(requests, 4, "F works on a focused button without assigning its arrow keys to seeking");
  assert.equal(player.video.currentTime, 0);
});

test("player settings reflect quality and close without disturbing playback", async () => {
  const base = "https://p14.solodcdn.com/hls/test/";
  const { video, elements, documentListeners } = setup({
    streams: { "480": `${base}480.m3u8`, "720": `${base}720.m3u8` }, resume_position: 90,
  });
  await new Promise(setImmediate);
  assert.equal(elements["settings-quality"].textContent, "720p");
  assert.equal(elements["settings-toggle"].attributes["aria-label"], "Настройки видео, 720p");
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  assert.equal(elements["settings-quality"].textContent, "480p");
  assert.equal(elements["settings-toggle"].attributes["aria-label"], "Настройки видео, 480p");
  elements["player-settings"].open = true;
  documentListeners.pointerdown({ target: { insideSettings: true } });
  assert.equal(elements["player-settings"].open, true);
  documentListeners.pointerdown({ target: video });
  assert.equal(elements["player-settings"].open, false);
  elements["player-settings"].open = true;
  let prevented = false;
  documentListeners.keydown({ key: "Escape", preventDefault: () => { prevented = true; } });
  assert.equal(elements["player-settings"].open, false);
  assert.equal(elements["settings-toggle"].focused, true);
  assert.equal(prevented, true);
  assert.equal(elements.resume.hidden, false);
  video.dispatch("play");
  assert.equal(elements.resume.hidden, true);
});

test("picture in picture changes the menu label only on confirmed events", async () => {
  const { video, elements, sent } = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } });
  await new Promise(setImmediate);
  video.dispatch("enterpictureinpicture");
  assert.equal(elements["pip-label"].textContent, "Вернуть видео из отдельного окна");
  video.dispatch("leavepictureinpicture");
  assert.equal(elements["pip-label"].textContent, "Картинка в картинке");
  assert.deepEqual(sent.map(([message]) => message.key), ["kodik_player_enter_pip", "kodik_player_exit_pip"]);
});

test("settings return after a failed initial load is retried", async () => {
  let available = false;
  const { elements } = setup(() => available
    ? { streams: { "720": "https://p14.solodcdn.com/test.m3u8" } }
    : { errorStatus: 502 });
  await new Promise(setImmediate);
  assert.equal(elements["player-settings"].hidden, true);
  available = true;
  elements.retry.dispatch("click");
  await new Promise(setImmediate);
  assert.equal(elements["player-settings"].hidden, false);
  assert.equal(elements["settings-quality"].textContent, "720p");
});

test("choosing another quality dismisses recovery actions after a playback error", async () => {
  const base = "https://p14.solodcdn.com/hls/test/";
  const { elements, players } = setup({ streams: { "480": `${base}480.m3u8`, "720": `${base}720.m3u8` } }, { hlsSupport: true });
  await new Promise(setImmediate);
  players[0].onError("error", { fatal: true });
  assert.equal(elements.retry.hidden, false);
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  assert.equal(elements.retry.hidden, true);
  assert.equal(elements.original.hidden, true);
  assert.equal(elements.message.textContent, "");
});

test("PiP errors are temporary and do not clear a later message", async () => {
  const { video, elements, runTimer } = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } });
  await new Promise(setImmediate);
  video.requestPictureInPicture = async () => { throw Error("unavailable"); };
  elements.pip.dispatch("click");
  await new Promise(setImmediate);
  assert.match(elements.message.textContent, /Не удалось включить/);
  elements.message.textContent = "Другая ошибка";
  runTimer();
  assert.equal(elements.message.textContent, "Другая ошибка");
});

test("a playback error restores focus from settings to retry", async () => {
  const { elements, players } = setup({ streams: { "720": "https://p14.solodcdn.com/test.m3u8" } }, { hlsSupport: true });
  await new Promise(setImmediate);
  elements["player-settings"].open = true;
  elements.quality.insideSettings = true;
  elements.quality.focus();
  players[0].onError("error", { fatal: true });
  assert.equal(elements["player-settings"].open, false);
  assert.equal(elements.retry.focused, true);
});

test("HLS playback uses the bundled player and switches quality without losing position", async () => {
  const base = "https://p14.solodcdn.com/hls/test/";
  const { video, elements, players } = setup(
    { streams: { "480": `${base}480.m3u8`, "720": `${base}720.m3u8` } },
    { hlsSupport: true, nativeSupport: false },
  );
  await new Promise(setImmediate);
  assert.equal(players.length, 1);
  assert.equal(players[0].url, `${base}720.m3u8`);
  assert.equal(players[0].video, video);
  video.currentTime = 42;
  video.paused = false;
  let plays = 0;
  video.play = async () => { plays++; };
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  assert.equal(players[0].destroyed, true);
  assert.equal(players[1].url, `${base}480.m3u8`);
  assert.equal(players[1].options.startPosition, 42);
  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 42);
  assert.equal(plays, 1);
  players[0].onError("error", { fatal: true });
  assert.equal(elements.original.hidden, true, "an old player cannot show an error after switching");
  players[1].onError("error", { fatal: true });
  assert.equal(players[1].stopped, true);
  assert.equal(elements.original.hidden, false);
  assert.equal(elements.retry.hidden, false);
});

test("rapid HLS quality changes and retry keep a paused pending position", async () => {
  const base = "https://p14.solodcdn.com/hls/test/";
  const { video, elements, players } = setup(
    { streams: { "360": `${base}360.m3u8`, "480": `${base}480.m3u8`, "720": `${base}720.m3u8` } },
    { hlsSupport: true },
  );
  await new Promise(setImmediate);
  video.currentTime = 240;
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  elements.quality.value = "360";
  elements.quality.dispatch("change");
  players.at(-1).onError("error", { fatal: true });
  elements.retry.dispatch("click");
  await new Promise(setImmediate);
  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 240);
  assert.equal(video.paused, true);
  assert.equal(elements.quality.value, "360");
});

test("resume before metadata survives a quality change", async () => {
  const base = "https://p14.solodcdn.com/hls/test/";
  const { video, elements, players } = setup(
    { streams: { "480": `${base}480.m3u8`, "720": `${base}720.m3u8` }, resume_position: 120 },
    { hlsSupport: true },
  );
  await new Promise(setImmediate);
  video.readyState = 0;
  elements.resume.dispatch("click");
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  assert.equal(players.at(-1).options.startPosition, 120);
  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 120);
});

test("native player loads safe streams and emits real video events", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  const { video, elements, sent } = setup({ streams: { "720": url }, resume_position: 90 });
  await new Promise(setImmediate);
  assert.equal(video.src, url);
  assert.equal(elements.resume.hidden, false);
  assert.equal(elements.resume.textContent, "Продолжить с 1:30");
  assert.equal(sent.length, 0, "loading does not claim playback");
  video.dispatch("loadedmetadata");
  video.dispatch("play");
  video.paused = false;
  video.currentTime = 1;
  video.dispatch("timeupdate");
  video.dispatch("pause");
  video.dispatch("ended");
  assert.deepEqual(sent.map(([message]) => message.key), [
    "kodik_player_duration_update", "kodik_player_play", "kodik_player_time_update",
    "kodik_player_pause", "kodik_player_video_ended",
  ]);
  assert(sent.every(([, origin]) => origin === "https://catalog.test"));
});

test("seeking while paused cannot report playback advancement", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  const { video, sent } = setup({ streams: { "720": url } });
  await new Promise(setImmediate);
  video.currentTime = 10;
  video.seeking = true;
  video.dispatch("seeking");
  video.dispatch("timeupdate");
  video.seeking = false;
  video.currentTime = 20;
  video.dispatch("timeupdate");
  assert.deepEqual(sent.map(([message]) => message.key), ["anime_catalog_kodik_seeking"]);
});

test("stale quality metadata cannot restart a newer paused source", async () => {
  const host = "https://cloud2.solodcdn.com/hls/test/";
  const { video, elements } = setup({ streams: { "360": `${host}a.m3u8`, "480": `${host}b.m3u8`, "720": `${host}c.m3u8` } });
  await new Promise(setImmediate);
  let plays = 0;
  video.play = async () => { plays++; };
  video.currentTime = 45;
  video.paused = false;
  elements.quality.value = "480";
  elements.quality.dispatch("change");
  video.currentTime = 45;
  video.paused = true;
  elements.quality.value = "360";
  elements.quality.dispatch("change");
  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 45);
  assert.equal(plays, 0);
});

test("retry retains the local position and quality after an error", async () => {
  const host = "https://cloud2.solodcdn.com/hls/test/";
  const { video, elements } = setup({ streams: { "480": `${host}b.m3u8`, "720": `${host}c.m3u8` }, resume_position: 30 });
  await new Promise(setImmediate);
  video.currentTime = 240;
  elements.quality.value = "480";
  video.dispatch("error");
  elements.retry.dispatch("click");
  await new Promise(setImmediate);
  assert.equal(elements.quality.value, "480");
  video.dispatch("loadedmetadata");
  assert.equal(video.currentTime, 240);
});

test("unsupported stream shows explicit fallback instead of loading an ad iframe", async () => {
  const { video, elements, sent } = setup({ streams: { "720": "https://evil.test/video.m3u8" } });
  await new Promise(setImmediate);
  assert.equal(video.src, undefined);
  assert.equal(elements["player-settings"].hidden, true);
  assert.equal(elements.original.hidden, false);
  assert.equal(elements.retry.hidden, false);
  elements.original.dispatch("click");
  assert.equal(sent[0][0].key, "anime_catalog_kodik_original");
});

test("serial translation selection survives opening another episode", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  const preferences = new Map();
  const translations = [{ id: "963", title: "First" }, { id: "17", title: "Second" }];
  const payload = request => ({
    streams: { "720": url }, translations,
    translation_id: request.includes("translation=17") ? "17" : "963",
  });
  const first = setup(payload, { serial: "55", preferences });
  await new Promise(setImmediate);
  assert.equal(first.elements.translation.hidden, false);
  first.video.currentTime = 42;
  first.elements.translation.value = "17";
  first.elements.translation.dispatch("change");
  await new Promise(setImmediate);
  first.video.dispatch("loadedmetadata");
  assert.equal(first.video.currentTime, 42);
  assert.equal(preferences.get("kodik-translation-55"), "17");
  const next = setup(payload, { serial: "55", preferences });
  await new Promise(setImmediate);
  assert.match(next.requested[0], /translation=17/);
  assert.equal(next.elements.translation.value, "17");
});

test("failed voice selection keeps the playing source and restores both controls", async () => {
  const base = "https://cloud2.solodcdn.com/hls/test/";
  const preferences = new Map([["kodik-translation-55", "963"]]);
  const payload = request => request.includes("translation=17") ? { errorStatus: 503 } : {
    streams: { "480": `${base}a.m3u8`, "720": `${base}b.m3u8` },
    translations: [{ id: "963", title: "First" }, { id: "17", title: "Second" }],
    translation_id: "963",
  };
  const player = setup(payload, { serial: "55", preferences });
  await new Promise(setImmediate);
  const original = player.video.src;
  player.video.paused = false;
  player.video.currentTime = 45;
  player.elements.translation.value = "17";
  player.elements.translation.dispatch("change");
  await new Promise(setImmediate);
  assert.equal(player.requested.length, 2, "503 does not retry with a different voice");
  assert.equal(player.video.src, original);
  assert.equal(player.elements.translation.value, "963");
  assert.equal(player.elements.translation.disabled, false);
  assert.equal(player.elements.quality.disabled, false);
  assert.equal(preferences.get("kodik-translation-55"), "963");
  assert.equal(player.elements.retry.hidden, false);
});

test("voice switching uses the position and pause state when the request finishes", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  let resolveSwitch;
  const switching = new Promise(resolve => { resolveSwitch = resolve; });
  const translations = [{ id: "963", title: "First" }, { id: "17", title: "Second" }];
  const payload = request => request.includes("translation=17")
    ? switching
    : { streams: { "720": url }, translations, translation_id: "963" };
  const player = setup(payload, { serial: "55" });
  await new Promise(setImmediate);
  let plays = 0;
  player.video.play = async () => { plays++; };
  player.video.currentTime = 45;
  player.video.paused = false;
  player.elements.translation.value = "17";
  player.elements.translation.dispatch("change");
  player.video.currentTime = 58;
  player.video.paused = true;
  resolveSwitch({ streams: { "720": url }, translations, translation_id: "17" });
  await new Promise(setImmediate);
  player.video.dispatch("loadedmetadata");
  assert.equal(player.video.currentTime, 58);
  assert.equal(plays, 0, "a pause during the request is respected");
});

test("an unavailable saved voice falls back without replacing the preference", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  const preferences = new Map([["kodik-translation-55", "17"]]);
  const payload = request => request.includes("translation=17") ? { errorStatus: 409 } : {
    streams: { "720": url },
    translations: [{ id: "963", title: "First" }],
    translation_id: "963",
  };
  const player = setup(payload, { serial: "55", preferences });
  await new Promise(setImmediate);
  assert.equal(player.requested.length, 2);
  assert.equal(preferences.get("kodik-translation-55"), "17");
  assert.match(player.elements.message.textContent, /озвучка недоступна/i);
  player.video.dispatch("error");
  player.elements.retry.dispatch("click");
  await new Promise(setImmediate);
  assert.match(player.requested.at(-1), /translation=963/);
  assert.equal(preferences.get("kodik-translation-55"), "17");
});

test("switching voice before playing retains the saved resume action", async () => {
  const url = "https://cloud2.solodcdn.com/hls/test/video.m3u8";
  const translations = [{ id: "963", title: "First" }, { id: "17", title: "Second" }];
  const payload = request => ({
    streams: { "720": url }, translations, resume_position: 120,
    translation_id: request.includes("translation=17") ? "17" : "963",
  });
  const player = setup(payload, { serial: "55" });
  await new Promise(setImmediate);
  player.elements.translation.value = "17";
  player.elements.translation.dispatch("change");
  await new Promise(setImmediate);
  assert.equal(player.elements.resume.hidden, false);
  assert.equal(player.elements.resume.textContent, "Продолжить с 2:00");
});

test("retry keeps a pending seek when media reload reports time zero", async () => {
  const base = "https://cloud2.solodcdn.com/hls/test/";
  const player = setup({ streams: { "480": `${base}a.m3u8`, "720": `${base}b.m3u8` } });
  await new Promise(setImmediate);
  player.video.currentTime = 240;
  player.elements.quality.value = "480";
  player.elements.quality.dispatch("change");
  player.video.dispatch("timeupdate");
  player.video.dispatch("error");
  player.elements.retry.dispatch("click");
  await new Promise(setImmediate);
  player.video.dispatch("loadedmetadata");
  assert.equal(player.video.currentTime, 240);
});

test("changing quality during a pending seek preserves that position", async () => {
  const base = "https://cloud2.solodcdn.com/hls/test/";
  const player = setup({ streams: { "360": `${base}a.m3u8`, "480": `${base}b.m3u8`, "720": `${base}c.m3u8` } });
  await new Promise(setImmediate);
  player.video.currentTime = 240;
  player.elements.quality.value = "480";
  player.elements.quality.dispatch("change");
  player.elements.quality.value = "360";
  player.elements.quality.dispatch("change");
  player.video.dispatch("loadedmetadata");
  assert.equal(player.video.currentTime, 240);
});

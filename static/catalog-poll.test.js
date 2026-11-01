const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { createCatalogPoller } = require("./catalog-poll.js");

function harness() {
  const calls = [];
  const timers = new Map();
  let nextTimer = 1;
  let visible = true;
  let response = {};
  let failure = false;
  let refreshes = 0;
  const poller = createCatalogPoller({
    request: async version => { calls.push(version); return response; },
    refresh: async () => { refreshes++; if (failure) throw new Error("failed"); },
    isVisible: () => visible,
    setTimer: (callback, delay) => { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
    clearTimer: id => timers.delete(id),
  });
  return {
    poller, calls, timers,
    setResponse: value => { response = value; },
    setFailure: value => { failure = value; },
    setVisible: value => { visible = value; },
    refreshes: () => refreshes,
    async tick() { const [id, timer] = timers.entries().next().value; timers.delete(id); await timer.callback(); },
  };
}

test("checks every 30 seconds and only refreshes when server reports a change", async () => {
  const h = harness();
  await h.poller.start("v1");
  assert.deepEqual(h.calls, ["v1"]);
  assert.equal(h.refreshes(), 0);
  assert.equal([...h.timers.values()][0].delay, 30000);
  h.setResponse({ version: "v2" });
  await h.tick();
  assert.equal(h.refreshes(), 1);
  h.setResponse({});
  await h.tick();
  assert.deepEqual(h.calls, ["v1", "v1", "v2"]);
});

test("retries the same version after failed refresh", async () => {
  const h = harness();
  h.setResponse({ version: "v2" });
  h.setFailure(true);
  await h.poller.start("v1");
  h.setFailure(false);
  await h.tick();
  assert.deepEqual(h.calls, ["v1"]);
  assert.equal(h.refreshes(), 2);
  h.setResponse({});
  await h.tick();
  assert.deepEqual(h.calls, ["v1", "v2"]);
});

test("pauses while hidden and checks immediately on return", async () => {
  const h = harness();
  await h.poller.start("v1");
  h.setVisible(false);
  h.poller.visibilityChanged();
  assert.equal(h.timers.size, 0);
  h.setVisible(true);
  await h.poller.visibilityChanged();
  assert.deepEqual(h.calls, ["v1", "v1"]);
  assert.equal(h.timers.size, 1);
});

test("does not overlap a poll when visibility changes mid-request", async () => {
  let release;
  let calls = 0;
  const poller = createCatalogPoller({
    request: () => { calls++; return new Promise(resolve => { release = resolve; }); },
    refresh: async () => {}, isVisible: () => true,
    setTimer: () => 1, clearTimer: () => {},
  });
  const pending = poller.start("v1");
  await poller.visibilityChanged();
  assert.equal(calls, 1);
  release({});
  await pending;
});

function refreshHarness({ screen = "title", detailStatus = 200, pending = 0 } = {}) {
  const source = fs.readFileSync(`${__dirname}/app.js`, "utf8");
  const start = source.indexOf("async function refreshPolledCatalog()");
  const end = source.indexOf("const catalogPoller =", start);
  assert.ok(start >= 0 && end > start);
  const calls = [];
  const state = {
    screen, userStateRevision: 1, detailRequestId: 4, franchiseRequestId: 3,
    detail: { id: 10, slug: "title" }, selectedAnimeId: 10, selectedEpisodeId: 22,
    selectedContentSource: "animego", selectedTranslation: "dub", selectedSourceId: 31,
    searchFieldsLoaded: false, searchFieldsLoading: null,
  };
  const context = vm.createContext({
    state, userStateSaveQueue: { pending: () => pending },
    api: async () => ({ items: [{ id: 10 }] }),
    applyFilter: () => calls.push("filter"), resetContentUpdatesForQuery: () => {},
    invalidateRecommendations: () => {}, isUpdatesView: () => false,
    isRecommendationView: () => false,
    selectAnime: async (id, options) => {
      state.detailRequestId++;
      calls.push({ id, linkState: options.linkState });
      if (detailStatus !== 200) throw Object.assign(new Error("missing"), { status: detailStatus });
    },
    clearPlayer: () => calls.push("clear"),
    showScreen: value => { state.screen = value; calls.push(value); },
    syncCatalogUrl: () => {},
  });
  vm.runInContext(`${source.slice(start, end)}; globalThis.refresh = refreshPolledCatalog`, context);
  return { state, calls, refresh: () => context.refresh() };
}

test("refresh preserves current episode and source without changing the screen", async () => {
  const h = refreshHarness();
  await h.refresh();
  assert.equal(h.state.screen, "title");
  assert.equal(h.calls[1].id, "title");
  assert.equal(h.calls[1].linkState.episodeId, 22);
  assert.equal(h.calls[1].linkState.provider, 31);
});

test("refresh never opens a previously selected title from the list", async () => {
  const h = refreshHarness({ screen: "list" });
  await h.refresh();
  assert.deepEqual(h.calls, ["filter"]);
});

test("a deleted open title returns to the list", async () => {
  const h = refreshHarness({ detailStatus: 404 });
  await h.refresh();
  assert.equal(h.state.detail, null);
  assert.equal(h.state.screen, "list");
  assert.ok(h.calls.includes("clear"));
});

test("defers refresh while a user state save is pending", async () => {
  const h = refreshHarness({ pending: 1 });
  await assert.rejects(h.refresh(), /being saved/);
  assert.equal(h.calls.length, 0);
});

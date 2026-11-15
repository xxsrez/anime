const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const navigation = require("./navigation.js");
const runtime = require("./frontend_runtime.js");
const source = fs.readFileSync(`${__dirname}/app.js`, "utf8");
function block(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

test("section tabs persist independently across reloads without overriding explicit routes", () => {
  const data = new Map();
  const storage = { getItem: key => data.get(key), setItem: (key, value) => data.set(key, value) };
  const load = () => {
    const context = vm.createContext({ localStorage: storage, URLSearchParams });
    vm.runInContext(fs.readFileSync(`${__dirname}/navigation.js`, "utf8"), context);
    return context.catalogNavigation;
  };
  const first = load();
  assert.equal(first.rememberedView("catalog"), "all");
  assert.equal(first.rememberedView("library"), "library-all");
  first.rememberView("recommendations");
  first.rememberView("progress");
  first.rememberView("maintenance");
  const reloaded = load();
  assert.equal(reloaded.rememberedView("catalog"), "recommendations");
  assert.equal(reloaded.rememberedView("library"), "progress");
  assert.equal(reloaded.read("?view=favorites").view, "favorites");
  reloaded.rememberView("favorites");
  assert.equal(reloaded.rememberedView("library"), "favorites");
  data.set("anime-last-view-library", "recommendations");
  assert.equal(load().rememberedView("library"), "library-all");
  const denied = { getItem() { throw Error("denied"); }, setItem() { throw Error("denied"); } };
  reloaded.rememberView("completed", denied);
  assert.equal(reloaded.rememberedView("library", denied), "completed");
});

test("title and service screens do not overwrite the last visited section tab", () => {
  const remembered = [];
  const context = vm.createContext({
    state: { viewMode: "all" },
    catalogNavigation: { libraryViews: navigation.libraryViews, rememberView: view => remembered.push(view), updateSectionLinks() {} },
    document: { body: { dataset: {} }, querySelector: () => ({}), getElementById: () => ({}), querySelectorAll: () => [] },
    isUpdatesView: () => false,
  });
  vm.runInContext(block("function showScreen(", "function initializeRedesign("), context);
  context.showScreen("title");
  context.showScreen("franchise");
  context.showScreen("maintenance");
  assert.deepEqual(remembered, []);
  context.state.viewMode = "updates";
  context.showScreen("list");
  assert.deepEqual(remembered, ["updates"]);
});

test("library union and favorites cover all six favorite/status combinations", () => {
  const context = vm.createContext({
    state: { viewMode: "all" }, catalogNavigation: navigation,
    effectiveWatchStatus: item => item.watch_status,
  });
  vm.runInContext(block("function itemMatchesView(", "function currentShelfTotal("), context);
  const items = [false, true].flatMap(is_favorite => ["none", "watching", "completed"].map(watch_status => ({ is_favorite, watch_status })));
  assert.equal(items.filter(item => context.itemMatchesView(item, "library-all")).length, 5);
  assert.equal(items.filter(item => context.itemMatchesView(item, "favorites")).length, 3);
  assert.equal(items.filter(item => context.itemMatchesView(item, "progress")).length, 2);
  assert.equal(items.filter(item => context.itemMatchesView(item, "completed")).length, 2);
  assert.equal(context.itemMatchesView({ is_favorite: false, watch_status: "completed" }, "progress"), false);
});

test("all catalog filters, Cyrillic query and ordering survive URL round trip", () => {
  const filters = { genre: "Тайна", year: "2025", kind: "Сериал", status: "Вышел", source: "animego", editorial_status: "ready", video: "available" };
  for (const viewMode of navigation.views) {
    const url = navigation.url({ viewMode, query: "Монолог & Фрирен", sortBy: "title", sortDir: "asc", filters, contentUpdateDays: "30", contentUpdateType: "new_episode" });
    const parsed = navigation.read(new URL(url, "https://anime.test").search);
    assert.equal(parsed.view, viewMode);
    assert.equal(parsed.query, "Монолог & Фрирен");
    const fixedSort = ["recommendations", "updates", "maintenance"].includes(viewMode);
    assert.equal(parsed.sortBy, fixedSort ? "" : "title");
    assert.equal(parsed.sortDir, fixedSort ? "desc" : "asc");
    assert.deepEqual(parsed.filters, filters);
    if (viewMode === "updates") { assert.equal(parsed.days, "30"); assert.equal(parsed.updateType, "new_episode"); }
  }
  assert.equal(navigation.read("?view=invalid&days=-1").view, "all");
  assert.equal(navigation.read("?days=-1").days, "7");
});

test("list URLs omit defaults and preserve meaningful overrides with legacy aliases", () => {
  const cases = [
    ["all", "rating_best", "/"],
    ["progress", "watch_recent", "/?view=watching"],
    ["library-all", "rating_best", "/?view=library"],
    ["favorites", "favorite_added", "/?view=favorites"],
    ["completed", "watch_completed", "/?view=completed"],
    ["updates", "rating_best", "/?view=new"],
    ["recommendations", "rating_best", "/?view=recommendations"],
  ];
  for (const [viewMode, sortBy, expected] of cases) {
    assert.equal(navigation.url({ viewMode, sortBy, sortDir: "desc", filters: { video: "any" }, contentUpdateDays: "7", contentUpdateType: "all" }), expected);
    assert.equal(navigation.read(new URL(expected, "https://anime.test").search).view, viewMode);
    assert.equal(navigation.read(`?view=${viewMode}`).view, viewMode);
  }
  assert.equal(navigation.read("?view=catalog").view, "all");
  assert.equal(navigation.url({viewMode: "progress", sortBy: "watch_recent", sortDir: "asc"}), "/?view=watching&dir=asc");
  assert.equal(navigation.url({viewMode: "progress", sortBy: "title", sortDir: "desc"}), "/?view=watching&sort=title");
  assert.equal(navigation.url({viewMode: "updates", contentUpdateDays: "30", contentUpdateType: "new_episode"}), "/?view=new&days=30&event=new_episode");
});

test("clean title URL keeps return criteria and source selection in browser history", () => {
  const location = new URL("https://anime.test/?view=favorites&q=Тайна");
  const calls = [];
  const history = { pushState: (...args) => calls.push(args), replaceState: (...args) => calls.push(args) };
  const state = { viewMode: "favorites", filters: {}, selectedAnimeId: 12, detail: { slug: "example" }, selectedEpisodeId: "special-1.5", selectedContentSource: "yummy", selectedTranslation: "voice", selectedSourceId: "provider", sortBy: "title", sortDir: "asc" };
  const context = vm.createContext({ state, URL, URLSearchParams, history, window: { location, history }, catalogNavigation: navigation, el: { search: { value: "Тайна" } }, normalizeLinkValue: value => value == null ? null : String(value) });
  vm.runInContext(block("function setOptionalParam(", "function syncUrlFromFranchise("), context);
  context.syncUrlFromDetail({ replace: false });
  assert.equal(calls[0][2], "/example?episode=special-1.5");
  assert.equal(calls[0][0].source, "yummy");
  assert.equal(calls[0][0].provider, "provider");
  assert.equal(navigation.read(new URL(calls[0][0].catalogUrl, location).search).query, "Тайна");
});

test("legacy links and current history retain explicit provider selection", () => {
  const location = new URL("https://anime.test/example?episode=12&source=animego&translation=81&provider=123");
  const context = vm.createContext({ URLSearchParams, window: { location }, history: { state: {} }, normalizeLinkValue: value => value == null ? null : String(value) });
  vm.runInContext(block("function routeFromPath(", "function setOptionalParam("), context);
  assert.equal(context.readLinkState().provider, "123");
  assert.equal(context.readLinkState().episodeId, "12");
  location.search = "?episode=12";
  context.history.state = { source: "yummy", translation: "42", provider: "456" };
  assert.equal(context.readLinkState().provider, "456");
  assert.equal(context.readLinkState().contentSource, "yummy");
});

test("filtering a list never auto-opens a title or starts its player", () => {
  let renders = 0;
  const state = { screen: "list", viewMode: "all", filters: {}, filtered: [], selectedAnimeId: null, anime: [] };
  const context = vm.createContext({
    state, el: { search: { value: "" }, resetFilters: {} }, INITIAL_RENDER_LIMIT: 40,
    catalogSearch: { searchQuery: () => ({ tokens: [] }) }, baseItemsForView: () => [{ id: 1 }],
    filterDefinitions: [], itemMatchesView: () => true, isUpdatesView: () => false, isRecommendationView: () => false,
    watchingHasUnseenEpisode: () => false,
    compareAnime: () => 0, renderList: () => { renders += 1; }, renderActiveFilters() {}, hasActiveCatalogTools: () => false,
    syncCatalogUrl() {}, selectAnime: () => assert.fail("must not select a title on filter change"),
  });
  vm.runInContext(block("function applyFilter(", "async function loadSearchFields("), context);
  context.applyFilter({ selectFirst: true });
  assert.equal(state.filtered.length, 1);
  assert.equal(renders, 1);
});

test("watching groups prioritize resume and new episodes, never new translations alone", () => {
  const context = vm.createContext({
    state: { resumeTitleId: 1 },
    watchingHasUnseenEpisode: item => item.unseen,
    recentUpdateSummary: item => item.summary,
  });
  vm.runInContext(block("function watchingGroup(", "function contentUpdateItemsForView("), context);
  assert.equal(context.watchingGroup({ id: 1, unseen: true }), 0);
  assert.equal(context.watchingGroup({ id: 2, unseen: true, summary: { event_counts: { new_episode: 1 } } }), 1);
  assert.equal(context.watchingGroup({ id: 3, unseen: true, summary: { event_counts: { new_translation: 1 } } }), 2);
  assert.equal(context.watchingGroup({ id: 4, unseen: false, summary: { event_counts: { new_provider: 1 } } }), 3);
});

test("source navigation replaces the iframe and rebinds tracking without navigating the old context", () => {
  const listeners = [];
  const next = { removeAttribute() {}, setAttribute(key, value) { this[key] = value; }, addEventListener: event => listeners.push(event) };
  let replaced = false;
  const old = { cloneNode: () => next, replaceWith: node => { assert.equal(node, next); replaced = true; } };
  Object.defineProperty(old, "src", { set: () => assert.fail("must not navigate old frame") });
  const configured = [];
  const context = vm.createContext({
    el: { player: old }, handlePlayerLoaded() {}, handlePlayerEngaged() {},
    configurePlayerIframe: (frame, options) => configured.push([frame, options.nativeKodik]),
  });
  vm.runInContext(block("function replacePlayerFrame(", "function configurePlayerIframe("), context);
  context.replacePlayerFrame("https://player.test/episode-2");
  assert.equal(replaced, true);
  assert.equal(context.el.player, next);
  assert.equal(next.src, "https://player.test/episode-2");
  assert.deepEqual(configured, [[next, false]]);
  assert.deepEqual(listeners, ["load", "focus", "pointerdown"]);
});

test("supported Kodik episodes and explicitly numbered serials use the local native player", () => {
  const replaced = [];
  const state = { playerContext: null, kodikPlayerMode: "adfree" };
  let hlsSupport = "maybe";
  const context = vm.createContext({
    URL, state,
    window: { location: { origin: "https://catalog.test" } },
    document: { createElement: () => ({ canPlayType: () => hlsSupport }) },
    el: { wrap: { classList: { remove() {} } }, player: { getAttribute: () => null } },
    playerHosts: ["kodikplayer.com", "aniboom.one"],
    frontendRuntime: runtime,
    ensureWatchSession() {}, updatePlayerDisplay() {}, cancelPictureInPictureRequest() {}, setPlayerActionState() {},
    clearPlayer() { assert.fail("valid player should not be cleared"); }, reportClientError() {},
    replacePlayerFrame: (url, options) => replaced.push([url, options.nativeKodik]),
    playerMessageProvider: () => "kodik",
  });
  vm.runInContext(block("function kodikNativeConfig(", "function updatePlayerDisplay("), context);
  const episode = { id: 5 };
  context.setPlayer({ id: 12, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/seria/1/hash/720p" }, episode);
  assert.deepEqual(replaced.pop(), ["https://catalog.test/player/kodik/12", true]);
  assert.equal(state.playerContext.playerOrigin, "https://catalog.test");
  state.playerContext = null;
  context.setPlayer({ id: 13, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/serial/1/hash/720p" }, episode);
  assert.deepEqual(replaced.pop(), ["https://kodikplayer.com/serial/1/hash/720p", false]);
  assert.equal(state.playerContext.isKodik, true);
  assert.equal(state.playerContext.kodikNativeAvailable, false);
  assert.equal(state.kodikPlayerMode, "adfree", "unsupported source does not change the saved preference");
  state.playerContext = null;
  context.setPlayer({ id: 16, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/serial/55/hash/720p?season=1&episode=12" }, episode);
  assert.deepEqual(replaced.pop(), ["https://catalog.test/player/kodik/16?serial=55", true]);
  state.playerContext = null;
  context.setPlayer({ id: 17, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/serial/55/hash/720p?season=1&episode=12&episode=13" }, episode);
  assert.deepEqual(replaced.pop(), ["https://kodikplayer.com/serial/55/hash/720p?season=1&episode=12&episode=13", false]);
  hlsSupport = "";
  state.playerContext = null;
  context.setPlayer({ id: 14, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/seria/1/hash/720p" }, episode);
  assert.deepEqual(replaced.pop(), ["https://kodikplayer.com/seria/1/hash/720p", false]);
  context.window.MediaSource = { isTypeSupported: () => true };
  state.playerContext = null;
  context.setPlayer({ id: 14, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/seria/1/hash/720p" }, episode);
  assert.deepEqual(replaced.pop(), ["https://catalog.test/player/kodik/14", true]);
  hlsSupport = "maybe";
  state.playerContext = null;
  context.setPlayer({ id: 15, embed_host: "aniboom.one", embed_url: "https://aniboom.one/seria/1/hash/720p" }, episode);
  assert.deepEqual(replaced.pop(), ["https://aniboom.one/seria/1/hash/720p", false]);

  const seasonUrl = "https://kodikplayer.com/season/55/base/720p?translations=false&only_episode=true&only_season=true&episode=7";
  const seasonSource = { id: 18, embed_host: "kodikplayer.com", embed_url: seasonUrl };
  state.playerContext = null;
  context.setPlayer(seasonSource, { id: 7, number: "7" });
  assert.deepEqual(replaced.pop(), ["https://catalog.test/player/kodik/18", true]);
  for (const [url, number] of [
    [seasonUrl, "6"],
    [seasonUrl + "&episode=8", "7"],
    [seasonUrl + "&extra=1", "7"],
    [seasonUrl.replace("translations=false", "translations=true"), "7"],
    [seasonUrl.replace("only_episode=true", "only_episode=false"), "7"],
  ]) {
    state.playerContext = null;
    context.setPlayer({ ...seasonSource, embed_url: url }, { id: 7, number });
    assert.deepEqual(replaced.pop(), [url, false], "unsafe or mismatched season URLs keep the original player");
  }
  state.kodikPlayerMode = "original";
  state.playerContext = null;
  context.setPlayer(seasonSource, { id: 7, number: "7" });
  assert.deepEqual(replaced.pop(), [seasonUrl, false], "original mode remains available for season sources");
});

test("Kodik player mode changes the frame and starts a fresh watch session", () => {
  const frames = [];
  const savedModes = [];
  const endedSessions = [];
  let nextSession = 0;
  const state = { playerContext: null, watchSession: null, kodikPlayerMode: "adfree" };
  const el = {
    wrap: { classList: { remove() {} } },
    player: { getAttribute: () => null },
    kodikPlayerMode: { value: "" },
  };
  const context = vm.createContext({
    URL, state, el, frontendRuntime: runtime, playerHosts: ["kodikplayer.com"],
    window: { location: { origin: "https://catalog.test" } },
    document: { createElement: () => ({ canPlayType: () => "maybe" }) },
    ensureWatchSession() { if (!state.watchSession) state.watchSession = { id: ++nextSession }; },
    clearWatchSession() { endedSessions.push(state.watchSession?.id); state.watchSession = null; },
    savePlayerMode: (player, mode) => savedModes.push([player, mode]),
    updatePlayerDisplay() {}, cancelPictureInPictureRequest() {}, setPlayerActionState() {},
    clearPlayer() { assert.fail("valid player should not be cleared"); }, reportClientError() {},
    replacePlayerFrame(url, options) { frames.push([url, options.nativeKodik]); },
    playerMessageProvider: () => "kodik",
  });
  vm.runInContext([
    block("function kodikNativeConfig(", "function updatePlayerDisplay("),
    block("function adoptPlayerSource(", "function setKodikPlayerMode("),
    block("function setKodikPlayerMode(", "function clearPlayer("),
  ].join("\n"), context);
  const source = { id: 12, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/seria/1/hash/720p" };
  const episode = { id: 5 };
  context.setPlayer(source, episode);
  assert.deepEqual(frames.at(-1), ["https://catalog.test/player/kodik/12", true]);
  assert.equal(state.watchSession.id, 1);
  context.setKodikPlayerMode("original");
  assert.deepEqual(frames.at(-1), [source.embed_url, false]);
  assert.equal(state.watchSession.id, 2);
  assert.deepEqual(endedSessions, [1]);
  assert.deepEqual(savedModes, [["kodik", "original"]]);
  const count = frames.length;
  context.setKodikPlayerMode("original");
  assert.equal(frames.length, count, "choosing the active mode does not reload");
  context.setKodikPlayerMode("adfree");
  assert.deepEqual(frames.at(-1), ["https://catalog.test/player/kodik/12", true]);
  assert.equal(state.watchSession.id, 3);
  assert.deepEqual(endedSessions, [1, 2]);
  assert.deepEqual(savedModes, [["kodik", "original"], ["kodik", "adfree"]]);
  context.setKodikPlayerMode("not-a-mode");
  assert.equal(state.watchSession.id, 3);

  const unsupported = { id: 13, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/serial/1/hash/720p" };
  const nextSource = { id: 14, embed_host: "kodikplayer.com", embed_url: "https://kodikplayer.com/seria/2/hash/720p" };
  const nextEpisode = { id: 6 };
  context.setPlayer(unsupported, episode);
  context.adoptPlayerSource(nextSource, nextEpisode);
  const beforeRefresh = frames.length;
  const sessionBeforeRefresh = state.watchSession.id;
  context.setPlayer(nextSource, nextEpisode);
  assert.equal(frames.length, beforeRefresh, "refreshing an adopted episode does not change its playing mode");
  assert.equal(state.watchSession.id, sessionBeforeRefresh);
  assert.equal(state.playerContext.nativeKodik, false);
  context.setKodikPlayerMode("adfree");
  assert.deepEqual(frames.at(-1), ["https://catalog.test/player/kodik/14", true]);
  assert.notEqual(state.watchSession.id, sessionBeforeRefresh);
});

test("the existing Kodik choice survives without migration", () => {
  const values = new Map([["anime-kodik-player-mode", "adfree"]]);
  const context = vm.createContext({
    window: { localStorage: { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) } },
  });
  vm.runInContext([
    block("const PLAYER_MODE_STORAGE_KEYS =", "let playerHosts ="),
    block("function playerModeStorageKey(", "function roundMetric("),
  ].join("\n"), context);
  assert.equal(context.loadPlayerMode("kodik"), "adfree", "the existing preference is read without migration");
  context.savePlayerMode("kodik", "original");
  assert.equal(context.loadPlayerMode("kodik"), "original");
  assert.deepEqual([...values.keys()], ["anime-kodik-player-mode"]);
  const before = [...values];
  context.savePlayerMode("unknown", "adfree");
  context.savePlayerMode("__proto__", "adfree");
  context.savePlayerMode("kodik", "invalid");
  assert.equal(context.loadPlayerMode("unknown"), "original");
  assert.deepEqual([...values], before, "unsupported players and modes cannot change stored choices");
  context.window.localStorage = { getItem() { throw Error("denied"); }, setItem() { throw Error("denied"); } };
  assert.equal(context.loadPlayerMode("kodik"), "original");
  assert.doesNotThrow(() => context.savePlayerMode("kodik", "original"));
});

test("registered players have independent mode preferences", () => {
  const values = new Map();
  const context = vm.createContext({
    PLAYER_MODE_STORAGE_KEYS: { kodik: "anime-kodik-player-mode", another: "anime-another-player-mode" },
    window: { localStorage: { getItem: key => values.get(key), setItem: (key, value) => values.set(key, value) } },
  });
  vm.runInContext(block("function playerModeStorageKey(", "function roundMetric("), context);
  context.savePlayerMode("kodik", "adfree");
  context.savePlayerMode("another", "original");
  assert.equal(context.loadPlayerMode("kodik"), "adfree");
  assert.equal(context.loadPlayerMode("another"), "original");
  context.savePlayerMode("another", "adfree");
  context.savePlayerMode("kodik", "original");
  assert.equal(context.loadPlayerMode("kodik"), "original");
  assert.equal(context.loadPlayerMode("another"), "adfree");
});

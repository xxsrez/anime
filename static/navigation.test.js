const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const navigation = require("./navigation.js");
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
    assert.equal(parsed.sortBy, "title");
    assert.equal(parsed.sortDir, "asc");
    assert.deepEqual(parsed.filters, filters);
    if (viewMode === "updates") { assert.equal(parsed.days, "30"); assert.equal(parsed.updateType, "new_episode"); }
  }
  assert.equal(navigation.read("?view=invalid&days=-1").view, "all");
  assert.equal(navigation.read("?days=-1").days, "7");
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
  const context = vm.createContext({ el: { player: old }, handlePlayerLoaded() {}, handlePlayerEngaged() {} });
  vm.runInContext(block("function replacePlayerFrame(", "function configurePlayerIframe("), context);
  context.replacePlayerFrame("https://player.test/episode-2");
  assert.equal(replaced, true);
  assert.equal(context.el.player, next);
  assert.equal(next.src, "https://player.test/episode-2");
  assert.deepEqual(listeners, ["load", "focus", "pointerdown"]);
});

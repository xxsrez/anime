const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const runtime = require("./frontend_runtime.js");
const app = fs.readFileSync(`${__dirname}/app.js`, "utf8");

function block(start, end) {
  const from = app.indexOf(start);
  const to = app.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return app.slice(from, to);
}

const requests = [];
const session = { engaged: true, providerPlaybackActive: true, pendingProviderSeconds: 10 };
const frame = {};
const context = vm.createContext({
  state: { watchSession: session, playerContext: { loaded: true, playerOrigin: "https://kodikplayer.com", messageProvider: "kodik" } },
  el: { player: { contentWindow: frame } },
  frontendRuntime: runtime,
  watchPayloadForSession: (_session, eventType, seconds) => ({ event_type: eventType, engaged_seconds: seconds }),
  animeStateRevision: () => 0,
  postWatchPayload: async payload => { requests.push(payload); return {}; },
  consumeWatchEngagedSeconds: () => session.pendingProviderSeconds,
  stopWatchHeartbeat() {}, stopWatchFallbackTimer() {},
  reportClientError(error) { throw error; },
});
vm.runInContext([
  block("function sendWatchEvent(", "function consumeWatchEngagedSeconds("),
  block("function flushWatchSession(", "function ensureWatchSession("),
  block("function handleProviderPlaybackStopped(", "function applyPlayerEpisodeChange("),
  block("function handlePlayerMessage(", "function handlePlayerEngaged("),
].join("\n"), context);

function message(key, overrides = {}) {
  context.handlePlayerMessage({ source: frame, origin: "https://kodikplayer.com", data: { key }, ...overrides });
}
message("kodik_player_video_ended", { origin: "https://unrelated.test" });
message("kodik_player_video_ended", { source: {} });
assert.equal(requests.length, 0, "unrelated iframe messages ignored");
message("kodik_player_pause");
assert.equal(requests.length, 1);
assert.equal(requests[0].playback_ended, undefined);
message("kodik_player_video_ended");
assert.equal(requests.length, 2, "ended survives an earlier pause");
assert.deepEqual(requests[1], { event_type: "session_end", engaged_seconds: 0, playback_ended: true });
message("kodik_player_duration_update", {data: {key: "kodik_player_duration_update", value: 1380}});
assert.equal(session.providerDurationSeconds, 1380);
assert.equal(runtime.normalizePlayerMessage({key: "kodik_player_duration_update", value: 0}), null);
assert.equal(runtime.normalizePlayerMessage({key: "kodik_player_duration_update", value: "bad"}), null);
const payloadContext = vm.createContext({
  state: {detail: {id: 1, watch_status: "watching"}, anime: []},
  WATCH_MAX_DELTA_SECONDS: 300, document: {hidden: false},
  playerHasPlaybackEvidence: () => true, effectiveWatchStatus: item => item.watch_status,
});
vm.runInContext(block("function watchPayloadForSession(", "function animeStateRevision("), payloadContext);
const positioned = payloadContext.watchPayloadForSession({animeId: 1, providerPositionSeconds: 1320.4, providerDurationSeconds: 1380.2}, "heartbeat", 30);
assert.equal(positioned.playback_position_seconds, 1320);
assert.equal(positioned.playback_duration_seconds, 1381);
assert.equal(payloadContext.watchPayloadForSession({animeId: 1}, "heartbeat", 30).playback_position_seconds, undefined);
assert.equal(payloadContext.watchPayloadForSession({animeId: 1, providerDurationSeconds: 2586}, "fullscreen_enter")
  .playback_duration_seconds, 2586, "known duration survives a pause that cleared the position");

const episodes = Array.from({ length: 23 }, (_, i) => ({ id: i + 1, number: String(i + 1), source_count: 1 }));
const selection = vm.createContext({
  state: { detail: {
    episodes,
    progress_episode_number: 22,
    last_watch: { episode_id: 22, progress_episode_number: 22, last_seen_at: "2026-09-12", completed_at: "2026-09-12" },
    last_opened_episode: { episode_id: 15, updated_at: "2026-09-04" },
  } },
  numberFrom: value => value == null ? null : Number(value),
  sourceVariants: () => [], preferredContentSource: () => null,
  frontendRuntime: runtime,
});
vm.runInContext(block("function matchingEpisodeId(", "function numericValue("), selection);
selection.applyDetailLinkState();
assert.equal(selection.state.selectedEpisodeId, 23, "title card resumes after a completed watch");
selection.state.detail.last_opened_episode.updated_at = "2026-09-13";
selection.applyDetailLinkState();
assert.equal(selection.state.selectedEpisodeId, 15, "a newer explicit navigation choice is respected");
selection.applyDetailLinkState({ episodeId: 3 });
assert.equal(selection.state.selectedEpisodeId, 3, "explicit deep link still wins");

vm.runInContext(block("function numberFrom(", "function effectiveWatchStatus("), selection);
const fractionalEpisodes = [
  { id: 1168, number: "1168", source_count: 1 },
  { id: 5426890758783440, number: "1168.5", source_count: 1 },
  { id: 1169, number: "1169", source_count: 1 },
];
selection.state.detail = {
  episodes: fractionalEpisodes,
  progress_episode_number: 1168.5,
  last_watch: { progress_episode_number: 1168.5, last_seen_at: "2026-10-07" },
};
selection.applyDetailLinkState();
assert.equal(selection.state.selectedEpisodeId, 5426890758783440, "resume preserves fractional identity");
selection.state.detail.last_watch.completed_at = "2026-10-07";
selection.applyDetailLinkState();
assert.equal(selection.state.selectedEpisodeId, 1169, "completed special advances to next regular episode");
assert.equal(runtime.nextEpisodeIdAfterProgress(fractionalEpisodes, 1168), 5426890758783440);
assert.equal(runtime.episodeNumberValue("1168.50"), 1168.5);
for (const bad of [NaN, Infinity, true, "1168junk"]) assert.equal(runtime.episodeNumberValue(bad), null);
assert.equal(runtime.parseKodikSerialUrl("https://kodikplayer.com/serial/1/hash/720p?season=1&episode=1168.5").episodeNumber, 1168.5);
assert.equal(runtime.normalizePlayerMessage({key:"kodik_player_current_episode",value:{episode:1168.5,season:1}}).episodeNumber,1168.5);

const updatesContext = vm.createContext({
  effectiveWatchStatus: item => item.watch_status,
  numericValue: value => value == null || value === "" ? null : Number(value),
});
vm.runInContext(block("function contentUpdateHasUnseenEpisode(", "function contentUpdateItemsForView("), updatesContext);
const newTitle = {watch_status: "watching", progress_episode_number: 1,
  report: {episode_numbers: []}, update_episode_numbers: ["1"],
  last_watch: {progress_episode_number: 1, completed_at: null}};
assert.equal(updatesContext.contentUpdateItemIsPriority(newTitle), true);
newTitle.last_watch.completed_at = "2026-10-07T20:00:00Z";
assert.equal(updatesContext.contentUpdateItemIsPriority(newTitle), false);
newTitle.update_episode_numbers = ["1.5"];
assert.equal(updatesContext.contentUpdateItemIsPriority(newTitle), true);
newTitle.watch_status = "completed";
assert.equal(updatesContext.contentUpdateItemIsPriority(newTitle), false);
assert.equal(updatesContext.contentUpdateItemIsPriority({watch_status:"watching", report:{episode_numbers:[]}}), false);

const shelf = vm.createContext({
  state: {viewMode: "progress", anime: [], sortDir: "desc"},
  el: {search: {value: ""}, resetFilters: {}},
  INITIAL_RENDER_LIMIT: 100,
  filterDefinitions: [],
  effectiveWatchStatus: runtime.effectiveWatchStatus,
  numericValue: runtime.episodeNumberValue,
  catalogSearch: {searchQuery: () => ({tokens: []})},
  baseItemsForView() { return shelf.state.anime; },
  isRecommendationView: () => false, isUpdatesView: () => false,
  itemMatchesView: () => true, activeFilterDefinitions: () => [],
  sortDefinition: () => ({type: "number", value: item => item.recency}),
  recentUpdateSummary: item => item.recent_update_summary,
  renderList() {}, renderActiveFilters() {}, hasActiveCatalogTools: () => false,
});
vm.runInContext([
  block("function dateValue(", "function viewSortDefault("),
  block("function contentUpdateHasUnseenEpisode(", "function contentUpdateItemsForView("),
  block("function compareAnime(", "function favoriteWatchStatusRank("),
  block("function applyFilter(", "async function loadSearchFields("),
].join("\n"), shelf);
const caughtUp = {id: 1, watch_status: "watching", progress_episode_number: 3,
  available_episode_numbers: ["1", "2", "3"], recency: 100};
const pending = {...caughtUp, id: 2, progress_episode_number: 2, recency: 10};
const unfinished = {...caughtUp, id: 3, recency: 20,
  last_watch: {progress_episode_number: 3, completed_at: null}};
shelf.state.anime = [caughtUp, pending, unfinished];
shelf.applyFilter();
assert.deepEqual(Array.from(shelf.state.filtered, item => item.id), [3, 2, 1]);
unfinished.last_watch.completed_at = "2026-10-13";
shelf.applyFilter();
assert.deepEqual(Array.from(shelf.state.filtered, item => item.id), [2, 1, 3]);
shelf.state.sortDir = "asc";
shelf.applyFilter();
assert.deepEqual(Array.from(shelf.state.filtered, item => item.id), [2, 3, 1]);
shelf.state.viewMode = "all";
shelf.state.sortDir = "desc";
shelf.applyFilter();
assert.deepEqual(Array.from(shelf.state.filtered, item => item.id), [1, 3, 2]);
assert.equal(shelf.watchingHasUnseenEpisode({...caughtUp, available_episode_numbers: []}), false);
assert.equal(shelf.watchingHasUnseenEpisode({...caughtUp, available_episode_numbers: ["3.5"]}), true);
assert.equal(shelf.watchingHasUnseenEpisode({...caughtUp, progress_episode_number: null}), true);
assert.equal(shelf.watchingHasUnseenEpisode({...pending, watch_status: "none"}), false);
assert.equal(shelf.watchingHasUnseenEpisode({...pending, watch_status: "completed"}), false);
assert.equal(shelf.watchingHasUnseenEpisode(null), false);
shelf.document = {createElement: () => ({})};
for (const mode of ["all", "favorites", "progress", "updates", "recommendations"]) {
  shelf.state.viewMode = mode;
  const badges = [];
  const parent = {append: badge => badges.push(badge)};
  for (const item of [pending, caughtUp, {...pending, watch_status: "none"},
    {...pending, watch_status: "completed"}, {...caughtUp, last_watch: {progress_episode_number: 3}}]) {
    shelf.appendWatchingBadge(parent, item);
  }
  assert.equal(badges.length, 2, `${mode}: only watching titles with something left receive badges`);
  assert.ok(badges.every(badge => badge.textContent === "Есть что смотреть"
    && badge.className === "watching-available-badge"));
}

async function verifyDormantProviderDoesNotUndoCompletion() {
  for (const progress of [1, 2]) {
    const completed = {progress_episode_number: progress, started_at: "2026-10-07",
      completed_at: "2026-10-07", engaged_seconds: 1300};
    const item = {watch_status: "watching", progress_episode_number: progress,
      available_episode_numbers: Array.from({length: progress}, (_, i) => String(i + 1)),
      last_watch: completed};
    let response = {episode_state: {progress_episode_number: progress, started_at: null,
      completed_at: null, engaged_seconds: 0}};
    const ctx = vm.createContext({
      state: {watchSession: {animeId: 1}},
      watchPayloadForSession: () => ({}), animeStateRevision: () => 0,
      postWatchPayload: async () => response,
      applyLocalUserStatePatch: (_id, patch) => Object.assign(item, patch),
      applyFilter() {}, reportClientError(error) {throw error;},
    });
    vm.runInContext(block("function sendWatchEvent(", "function consumeWatchEngagedSeconds("), ctx);
    for (const event of ["player_loaded", "source_selected", "session_end"]) {
      await ctx.sendWatchEvent(event);
      assert.equal(item.last_watch, completed, `${progress} episodes: ${event} preserves completed watch`);
      assert.equal(shelf.watchingHasUnseenEpisode(item), false);
    }
    response = {episode_state: {...completed, completed_at: null}};
    await ctx.sendWatchEvent("player_engaged");
    assert.equal(shelf.watchingHasUnseenEpisode(item), true, "real unfinished playback still updates the badge");
    response = {episode_state: completed};
    await ctx.sendWatchEvent("session_end");
    assert.equal(shelf.watchingHasUnseenEpisode(item), false, "completion removes the badge");
  }
}
verifyDormantProviderDoesNotUndoCompletion().catch(error => {console.error(error); process.exitCode = 1;});

vm.runInContext(block("function dateValue(", "function viewSortDefault("), shelf);
shelf.sortDefinition = () => ({type: "number", value: shelf.watchRecency});
shelf.state.viewMode = "progress";
shelf.state.sortDir = "desc";
const olderWatch = {...pending, id: 10, watch_last_seen_at: "2026-10-10T10:00:00Z"};
const recentWatch = {...pending, id: 11, watch_last_seen_at: "2026-10-11T10:00:00Z"};
const currentWatch = {...olderWatch, id: 12,
  last_watch: {progress_episode_number: 2, started_at: "2026-10-13T10:00:00Z", last_seen_at: "2026-10-13T10:01:00Z"}};
const noWatch = {...pending, id: 13, updated_at: "2099-01-01", watch_status_updated_at: "2099-01-01"};
const caughtUpToday = {...caughtUp, id: 14, watch_last_seen_at: "2026-10-13T11:00:00Z"};
shelf.state.anime = [olderWatch, recentWatch, currentWatch, noWatch, caughtUpToday];
shelf.applyFilter();
assert.deepEqual(Array.from(shelf.state.filtered, item => item.id), [12, 11, 10, 13, 14],
  "available episodes first, then most recent actual watch, including current-session events");
assert.equal(shelf.watchRecency({...olderWatch,
  last_watch: {started_at: null, last_seen_at: "2099-01-01"}}), Date.parse(olderWatch.watch_last_seen_at),
  "opening an idle player does not promote the title");
olderWatch.last_watch = {started_at: "2026-10-13T12:00:00Z", last_seen_at: "2026-10-13T12:01:00Z"};
shelf.applyFilter();
assert.equal(shelf.state.filtered[0].id, 10, "latest playback moves to the front without reloading");

if (process.argv.includes("--payloads")) {
  console.log(JSON.stringify([{ event_type: "player_engaged", engaged_seconds: 0 }, ...requests]));
} else {
  console.log("watch tracking: ended, pause, iframe isolation, and resume selection passed");
}

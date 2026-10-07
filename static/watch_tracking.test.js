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

if (process.argv.includes("--payloads")) {
  console.log(JSON.stringify([{ event_type: "player_engaged", engaged_seconds: 0 }, ...requests]));
} else {
  console.log("watch tracking: ended, pause, iframe isolation, and resume selection passed");
}

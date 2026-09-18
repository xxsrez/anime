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

if (process.argv.includes("--payloads")) {
  console.log(JSON.stringify([{ event_type: "player_engaged", engaged_seconds: 0 }, ...requests]));
} else {
  console.log("watch tracking: ended, pause, iframe isolation, and resume selection passed");
}

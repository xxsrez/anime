const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const runtime = require("./frontend_runtime.js");
const app = fs.readFileSync(`${__dirname}/app.js`, "utf8");
function block(start, end) { return app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start))); }

assert.equal(runtime.playerMessageProvider({embed_host: "api.nextembed.ws"}), "nextembed");
assert.equal(runtime.playerMessageProvider({embed_host: "api.nextembed.ws.evil.test"}), null);
for (const time of [null, true, "61", -1, Infinity]) {
  assert.equal(runtime.normalizePlayerMessage({event: "viewProgress", time, duration: 2586}), null);
}
assert.equal(runtime.normalizePlayerMessage({event: "startWatching"}), null, "play alone is not watch evidence");

const sent = [];
const frame = {};
let now = 1000;
const session = {id: "test", engaged: false, pendingProviderSeconds: 0,
  providerPositionSeconds: null, providerPositionObservedAt: null};
const ctx = vm.createContext({
  state: {watchSession: session, playerContext: {loaded: true,
    playerOrigin: "https://api.nextembed.ws", messageProvider: "nextembed"}},
  el: {player: {contentWindow: frame}}, document: {hidden: true},
  frontendRuntime: runtime, performanceNow: () => now,
  WATCH_MAX_DELTA_SECONDS: 300, WATCH_PROVIDER_EVIDENCE_MAX_AGE_MS: 90000,
  startWatchHeartbeat() {}, stopWatchHeartbeat() {}, stopWatchFallbackTimer() {},
  sendWatchEvent(type, options = {}) {
    sent.push({type, ...options, verified: !!session.verifiedProviderProgress,
      position: session.providerPositionSeconds, duration: session.providerDurationSeconds});
    return Promise.resolve();
  },
});
vm.runInContext([
  block("function consumeWatchEngagedSeconds(", "function startWatchHeartbeat("),
  block("function markWatchEngaged(", "function ensureWatchSession("),
  block("function handleProviderPlaybackStarted(", "function nextembedEpisodeTarget("),
  block("function handlePlayerMessage(", "function handlePlayerEngaged("),
].join("\n"), ctx);
function message(data, extra = {}) {
  ctx.handlePlayerMessage({data, source: frame, origin: "https://api.nextembed.ws", ...extra});
}
message({event: "viewProgress", time: 61, duration: 2586});
assert.equal(sent.length, 0, "first sample cannot claim earlier viewing");
now += 61000;
message({event: "viewProgress", time: 122, duration: 2586}, {origin: "https://evil.test"});
message({event: "viewProgress", time: 122, duration: 2586}, {source: {}});
assert.equal(sent.length, 0, "origin and frame are both required");
message({event: "viewProgress", time: 122, duration: 2586});
assert.equal(sent.at(-1).type, "heartbeat");
assert.equal(sent.at(-1).engagedSeconds, 61, "minute-spaced progress counts in a hidden tab");
assert.equal(sent.at(-1).verified, true);
assert.equal(sent.at(-1).duration, 2586);
assert.equal(session.pendingProviderSeconds, 0, "heartbeat cannot double count progress");
const beforeSeek = sent.length;
now += 1000;
message({event: "viewProgress", time: 2500, duration: 2586});
assert.equal(sent.length, beforeSeek, "seek is not viewing time");
message({event: "endedSoon"});
assert.equal(sent.at(-1).playbackEnded, true, "provider end-of-content reaches completion path");
message({event: "ended"});
assert.equal(sent.at(-1).playbackEnded, true, "ended remains deliverable after prior flush");

ctx.URL = URL;
ctx.numericValue = runtime.episodeNumberValue;
const source = {id: 10, embed_host: "api.nextembed.ws", embed_url: "https://api.nextembed.ws/embed/kp/8545317?season=1&episode=4"};
const next = {...source, id: 11, embed_url: source.embed_url.replace("episode=4", "episode=5")};
ctx.state.detail = {episodes: [{id: 5, number: "5"}], sources_by_episode: {5: [next]}};
vm.runInContext(block("function nextembedEpisodeTarget(", "function applyPlayerEpisodeChange("), ctx);
assert.equal(ctx.nextembedEpisodeTarget({seasonNumber: 1, episodeNumber: 5}, source).source.id, 11);
assert.equal(ctx.nextembedEpisodeTarget({seasonNumber: 2, episodeNumber: 5}, source), null);
assert.equal(ctx.nextembedEpisodeTarget({seasonNumber: 1, episodeNumber: 6}, source), null);
console.log("Nextembed: sparse hidden progress, completion, seeks, frame isolation, episode mapping passed");

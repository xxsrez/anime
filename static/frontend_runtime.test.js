const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const runtime = require("./frontend_runtime.js");

async function testClientErrorReporter() {
  const listeners = new Map();
  const requests = [];
  const addEventListener = (type, listener) => {
    const handlers = listeners.get(type) || [];
    handlers.push(listener);
    listeners.set(type, handlers);
  };
  const window = {
    addEventListener,
    location: {
      origin: "https://anime.test",
      pathname: "/login",
      search: "?auth_complete=1",
      hash: "",
    },
  };
  const sandbox = {
    document: { addEventListener },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true };
    },
    navigator: { userAgent: "runtime-test-browser" },
    URL,
    window,
  };
  const source = fs.readFileSync(`${__dirname}/client_errors.js`, "utf8");
  vm.runInNewContext(source, sandbox, { filename: "client_errors.js" });

  const cspHandlers = listeners.get("securitypolicyviolation") || [];
  assert.equal(cspHandlers.length, 1);
  const violation = {
    blockedURI: "inline",
    effectiveDirective: "script-src-elem",
    disposition: "enforce",
    sourceFile: "moz-extension://example/content.js",
    lineNumber: 74,
    columnNumber: 196,
  };
  cspHandlers[0](violation);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/client-errors");
  assert.equal(requests[0].options.keepalive, true);
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.type, "securitypolicyviolation");
  assert.equal(payload.message, "Content Security Policy blocked inline (script-src-elem)");
  assert.equal(payload.source, "moz-extension://<redacted>/content.js");
  assert.equal(payload.context.source, "moz-extension://<redacted>/content.js");
  assert.equal(payload.context.blockedURI, "inline");
  assert.equal(payload.context.effectiveDirective, "script-src-elem");
  assert.equal(payload.context.disposition, "enforce");

  cspHandlers[0](violation);
  assert.equal(requests.length, 1);

  cspHandlers[0]({
    ...violation,
    blockedURI: "https://cdn.example/script.js?token=secret#fragment",
    sourceFile: "https://anime.test/login?private=value#fragment",
  });
  assert.equal(requests.length, 2);
  const privatePayload = JSON.parse(requests[1].options.body);
  assert.equal(privatePayload.message, "Content Security Policy blocked https://cdn.example/script.js (script-src-elem)");
  assert.equal(privatePayload.source, "/login");
  assert.equal(privatePayload.context.blockedURI, "https://cdn.example/script.js");
  assert.equal(privatePayload.context.source, "/login");
  assert.doesNotMatch(requests[1].options.body, /secret|private|fragment/);

  const sent = await window.reportClientError(new Error("fresh error"), {
    type: "runtime.test",
  });
  assert.equal(sent, true);
  assert.equal(requests.length, 3);
}

assert.equal(
  runtime.safeHttpsUrl("https://video.sibnet.ru/shell.php?videoid=1", ["sibnet.ru"]),
  "https://video.sibnet.ru/shell.php?videoid=1",
);
assert.equal(runtime.safeHttpsUrl("http://kodikplayer.com/video", ["kodikplayer.com"]), null);
assert.equal(runtime.safeHttpsUrl("https://kodikplayer.com.evil.test/video", ["kodikplayer.com"]), null);
assert.equal(runtime.safeHttpsUrl("https://user@example.com/video", ["example.com"]), null);
assert.equal(
  runtime.safeHttpsUrl("//kodikplayer.com/seria/1/token/720p", ["kodikplayer.com"]),
  "https://kodikplayer.com/seria/1/token/720p",
);

assert.equal(runtime.normalizeTranslationKey("Dreamcast"), "dream cast");
assert.equal(runtime.normalizeTranslationKey("Dream Cast"), "dream cast");
assert.equal(runtime.normalizeTranslationKey("Озвучка Dream Cast"), "dream cast");

const previousDreamCast = {
  id: 100,
  translation_id: 10,
  translation_title: "Dreamcast",
  provider_title: "Kodik",
  embed_host: "kodikplayer.com",
};
const nextEpisodeSources = [
  {
    id: 201,
    translation_id: 20,
    translation_title: "AniLibria",
    provider_title: "Kodik",
    embed_host: "kodikplayer.com",
  },
  {
    id: 202,
    translation_id: 21,
    translation_title: "Озвучка Dream Cast",
    provider_title: "Sibnet",
    embed_host: "video.sibnet.ru",
  },
  {
    id: 203,
    translation_id: 21,
    translation_title: "Dream Cast",
    provider_title: "Kodik",
    embed_host: "kodikplayer.com",
  },
];
const dreamCastPreference = runtime.sourcePreference(previousDreamCast);
assert.deepEqual(dreamCastPreference, {
  translationKey: "dream cast",
  providerTitleKey: "kodik",
  providerHost: "kodikplayer.com",
});
assert.equal(runtime.selectPreferredSource(nextEpisodeSources, dreamCastPreference).id, 203);
assert.equal(runtime.selectSourceForEpisode(nextEpisodeSources, {
  preference: dreamCastPreference,
}).id, 203);

const providerFallbackSources = [
  {
    id: 301,
    translation_id: 31,
    translation_title: "Dream Cast",
    provider_title: "AniBoom",
    embed_host: "aniboom.one",
  },
  {
    id: 302,
    translation_id: 31,
    translation_title: "Dream Cast",
    provider_title: "Kodik Mirror",
    embed_host: "kodikplayer.com",
  },
];
assert.equal(runtime.selectPreferredSource(providerFallbackSources, dreamCastPreference).id, 302);

const unavailableProviderSources = [
  {
    id: 401,
    translation_id: 41,
    translation_title: "Dream Cast",
    provider_title: "AniBoom",
    embed_host: "aniboom.one",
  },
  {
    id: 402,
    translation_id: 41,
    translation_title: "Dream Cast",
    provider_title: "Sibnet",
    embed_host: "video.sibnet.ru",
  },
];
assert.equal(runtime.selectPreferredSource(unavailableProviderSources, dreamCastPreference).id, 401);
assert.equal(runtime.selectPreferredSource(nextEpisodeSources, null).id, 201);
assert.equal(runtime.selectPreferredSource(nextEpisodeSources, {
  translationKey: "Missing Dub",
  providerTitleKey: "Missing Player",
}).id, 201);
assert.equal(runtime.selectPreferredSource([], dreamCastPreference), null);

assert.equal(runtime.selectSourceForEpisode(nextEpisodeSources, {
  selectedSourceId: "202",
  preference: dreamCastPreference,
}).id, 202);
assert.equal(runtime.selectSourceForEpisode(nextEpisodeSources, {
  selectedTranslationId: "21",
  preference: dreamCastPreference,
}).id, 203);

const sourceSwitchEpisodes = [
  { id: 45744, number: "1" },
  { id: 45887, number: "2" },
  { id: 46001, number: "3" },
];
assert.equal(
  runtime.nearestAvailableEpisodeId(sourceSwitchEpisodes, [45744], 45887),
  45744,
);
assert.equal(
  runtime.nearestAvailableEpisodeId(sourceSwitchEpisodes, [45744, 46001], 45887),
  45744,
);
assert.equal(
  runtime.nearestAvailableEpisodeId(sourceSwitchEpisodes, [45887], 45887),
  45887,
);
assert.equal(runtime.nearestAvailableEpisodeId(sourceSwitchEpisodes, [], 45887), null);

const semanticTranslationGroups = runtime.groupSourcesByTranslation([
  { id: 1, translation_id: 10, translation_title: "Akari Group" },
  { id: 2, translation_id: 11, translation_title: "Akari GROUP" },
  { id: 3, translation_id: 12, translation_title: "Dream Cast" },
]);
assert.equal(semanticTranslationGroups.length, 2);
assert.deepEqual(semanticTranslationGroups[0].sources.map(source => source.id), [1, 2]);

const springBefore = new Date(2026, 2, 28, 23, 30);
const springAfter = new Date(2026, 2, 29, 23, 30);
assert.equal(runtime.localCalendarDayDifference(springAfter, springBefore), 1);

assert.equal(runtime.boundedElapsedSeconds(0, 30_000, 300), 30);
assert.equal(runtime.boundedElapsedSeconds(0, 400_000, 300), 300);
assert.equal(runtime.boundedElapsedSeconds(10_000, 5_000, 300), 0);
assert.equal(runtime.hasPlaybackEvidence({ playerFocused: true, evidenceExpiresAt: 20, now: 10 }), true);
assert.equal(runtime.hasPlaybackEvidence({ playerFocused: true, evidenceExpiresAt: 5, now: 10 }), false);
assert.equal(runtime.hasPlaybackEvidence({ fullscreen: true, evidenceExpiresAt: 20, now: 10 }), true);
assert.equal(runtime.hasPlaybackEvidence({ pageHidden: true, playerFocused: true, evidenceExpiresAt: 20, now: 10 }), false);

assert.equal(runtime.effectiveWatchStatus({ watched: true }), "completed");
assert.equal(runtime.effectiveWatchStatus({ progress_episode_number: 3 }), "watching");
assert.equal(runtime.effectiveWatchStatus({ last_watch: { progress_episode_number: 4 } }), "watching");
assert.equal(runtime.effectiveWatchStatus({ watched: true, watch_status: "dropped" }), "dropped");
assert.equal(runtime.effectiveWatchStatus({}), "");
assert.equal(runtime.watchStatusLabel("planned"), "буду смотреть");
assert.equal(runtime.watchStatusLabel("paused"), "на паузе");
assert.equal(runtime.patchChanges({ updated_at: "old" }, { updated_at: "new" }), true);
assert.equal(
  runtime.patchChanges(
    { watch_status: "watching", updated_at: "old" },
    { watch_status: "watching", updated_at: "new" },
    ["watch_status", "watched", "progress_episode_number"],
  ),
  false,
);
assert.equal(
  runtime.patchChanges(
    { watch_status: "paused" },
    { watch_status: "watching", updated_at: "new" },
    ["watch_status"],
  ),
  true,
);

let meaningfulPlaybackSeconds = 0;
for (let heartbeat = 0; heartbeat < 11; heartbeat += 1) {
  meaningfulPlaybackSeconds += runtime.boundedElapsedSeconds(heartbeat * 30_000, (heartbeat + 1) * 30_000, 300);
}
assert.equal(meaningfulPlaybackSeconds, 330);

async function testKeyedQueue() {
  const events = [];
  const releases = new Map();
  const queue = runtime.createKeyedSerialQueue(async (key, value) => {
    events.push(`start:${key}:${value}`);
    await new Promise(resolve => releases.set(`${key}:${value}`, resolve));
    events.push(`end:${key}:${value}`);
    return value;
  });

  const a = queue.enqueue("anime-1", "A");
  const b = queue.enqueue("anime-1", "B");
  const c = queue.enqueue("anime-2", "C");
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(events, ["start:anime-1:A", "start:anime-2:C"]);
  assert.equal(queue.pending(), 3);

  releases.get("anime-1:A")();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(events.slice(-2), ["end:anime-1:A", "start:anime-1:B"]);

  releases.get("anime-1:B")();
  releases.get("anime-2:C")();
  assert.deepEqual(await Promise.all([a, b, c]), ["A", "B", "C"]);
  assert.equal(queue.pending(), 0);
}

Promise.all([testClientErrorReporter(), testKeyedQueue()]).then(() => {
  console.log("frontend_runtime tests passed");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});

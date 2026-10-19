import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

// Exercise the actual scanner orchestration and checkpoint persistence. Only
// browser APIs and collection at the upstream boundary are substituted.
async function scannerHarness({ stored = null, fetch, discovery = false, legacy = false, retired = false, sendMessage = async () => {}, permission = true, fastTimers = false }) {
  const element = () => ({
    children: [], style: {},
    append(...items) { this.children.push(...items); },
    prepend(...items) { this.children.unshift(...items); },
    setAttribute() {}, addEventListener() {},
  });
  const elements = new Map();
  const payload = { job_id: "1", origin: "http://127.0.0.1:8765", token: "fixture-token", mode: "full", tasks: [{ anime_id: 100, title: "Fixture", url: "https://animego.me/anime/fixture-100" }] };
  if (legacy) delete payload.tasks[0].url;
  if (retired) { payload.tasks[0].source_retired = true; payload.tasks[0].url = null; }
  if (discovery) { payload.discovery = true; payload.tasks = []; }
  let storage = stored || {
    payload, checkpoint: { job_id: "1", status: "paused" },
  };
  const browser = {
    permissions: { contains: async () => permission },
    runtime: { sendMessage, onMessage: { addListener() {} } },
    storage: { local: {
      get: async () => ({ animegoScannerSession: structuredClone(storage) }),
      set: async value => { storage = structuredClone(value.animegoScannerSession); },
    } },
  };
  const context = vm.createContext({
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }, createElement: element },
    resolveExtensionApi: () => browser, fetch, URL, AbortController, DOMException,
    setTimeout: fastTimers ? (fn, ms) => setTimeout(fn, ms >= 15000 ? 1 : ms) : setTimeout,
    clearTimeout, queueMicrotask, console, shouldRestartAfterReload: () => false,
  });
  const source = fs.readFileSync(new URL("scanner.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?from "[^"\n]+";\n/gm, "");
  vm.runInContext(source + `
    globalThis.driver = {
      get state() { return checkpoint; },
      collect(fn) { collectTitle = fn; },
      upstream(fn) { upstreamJson = fn; },
      upstreamError(message, status) { return new UpstreamError(message, { status }); },
      async run() { checkpoint.status = "running"; await runScan(); },
      stop: stopScan,
      start: startNewScan,
    };
  `, context, { filename: "scanner.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { driver: context.driver, elements, snapshot: () => structuredClone(storage) };
}

function response(job) {
  return { ok: true, status: 200, text: async () => JSON.stringify({ job }) };
}

test("retired AnimeGO source goes directly to recovery without requesting deleted URLs", async () => {
  let body;
  const h = await scannerHarness({ retired: true, fetch: async (url, options) => {
    assert.equal(url.origin, "http://127.0.0.1:8765");
    if (url.pathname.endsWith("/results")) body = JSON.parse(options.body);
    return { ok: true, text: async () => JSON.stringify({ job: { checked_items: 1 }, result: { recovered_source: "YummyAnime" } }) };
  } });
  await h.driver.run();
  assert.equal(body.browser_recovery, true);
  assert.equal(body.upstream_status, 404);
  assert.equal(h.driver.state.status, "completed");
});

test("404 recovery uses browser API and preserves source response for server validation", async () => {
  let posted;
  const h = await scannerHarness({ fetch: async (url, options) => {
    if (url.origin === "https://api.yani.tv") return { ok: true, json: async () => ({ response: { anime_id: 26525 } }) };
    const body = JSON.parse(options.body);
    if (url.pathname.endsWith("/results") && !body.recovery_response) return {
      ok: true, text: async () => JSON.stringify({ recovery_task: { anime_id: 20026525, url: "https://api.yani.tv/anime/fixture?need_videos=true" } }),
    };
    if (url.pathname.endsWith("/results")) {
      posted = body;
      return { ok: true, text: async () => JSON.stringify({ job: { checked_items: 1, error_count: 0 }, result: { recovered_source: "YummyAnime" } }) };
    }
    return response({ checked_items: 1, error_count: 0 });
  } });
  h.driver.collect(async () => { throw h.driver.upstreamError("Missing", 404); });
  await h.driver.run();
  assert.equal(h.driver.state.status, "completed");
  assert.equal(posted.browser_recovery, true);
  assert.equal(posted.recovery_response.response.anime_id, 26525);
  assert.equal(h.driver.state.errors.length, 0);
});

test("missing recovery permission pauses without losing the original error; reload resumes it", async () => {
  const first = await scannerHarness({ permission: false, fetch: async () => ({
    ok: true, text: async () => JSON.stringify({ recovery_task: { anime_id: 20026525, url: "https://api.yani.tv/anime/fixture" } }),
  }) });
  first.driver.collect(async () => { throw first.driver.upstreamError("Missing", 404); });
  await first.driver.run();
  assert.equal(first.driver.state.status, "permission");
  assert.equal(first.driver.state.next_index, 0);
  assert.equal(first.snapshot().checkpoint.pending_result.upstream_status, 404);
  const resumed = await scannerHarness({ stored: first.snapshot(), fetch: async () => ({
    ok: true, text: async () => JSON.stringify({ job: { checked_items: 1 }, result: { recovered_source: "YummyAnime" } }),
  }) });
  resumed.driver.collect(async () => { throw new Error("Should replay pending error"); });
  await resumed.driver.run();
  assert.equal(resumed.driver.state.status, "completed");
  assert.equal(resumed.driver.state.errors.length, 0);
});

test("server timeout retains collected result and permits retry instead of hanging", async () => {
  const h = await scannerHarness({ fastTimers: true, fetch: async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  }) });
  h.driver.collect(async () => []);
  h.driver.upstream(async () => "<h1>Title</h1>");
  await h.driver.run();
  assert.equal(h.driver.state.status, "error");
  assert.equal(h.driver.state.next_index, 0);
  assert.equal(h.snapshot().checkpoint.pending_result.anime_id, 100);
});

test("recovery timeout reports a failed check and continues instead of claiming success", async () => {
  let reported = false;
  const h = await scannerHarness({ fastTimers: true, fetch: async (url, options) => {
    if (url.origin === "https://api.yani.tv") return new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    });
    const body = JSON.parse(options.body);
    if (url.pathname.endsWith("/results") && !body.recovery_error) return {
      ok: true, text: async () => JSON.stringify({ recovery_task: { url: "https://api.yani.tv/anime/fixture" } }),
    };
    if (body.recovery_error) reported = true;
    return response({ checked_items: 1, error_count: 1 });
  } });
  h.driver.collect(async () => { throw h.driver.upstreamError("Missing", 404); });
  await h.driver.run();
  assert.equal(reported, true);
  assert.equal(h.driver.state.status, "completed");
  assert.equal(h.driver.state.error_count, 1);
  assert.equal(h.driver.state.errors.length, 1);
});

test("new scans are blocked while paused and available after completion, with duplicate clicks suppressed", async () => {
  let requests = 0;
  let resolve;
  const harness = await scannerHarness({ fetch: async () => response({}), sendMessage: message => {
    if (message.type !== "animego-scanner-create") return Promise.resolve();
    requests++;
    return new Promise(done => { resolve = done; });
  } });
  await harness.driver.start("partial");
  assert.equal(requests, 0);
  harness.driver.state.status = "completed";
  const pending = harness.driver.start("full");
  assert.equal(harness.elements.get("start-partial").disabled, true);
  await harness.driver.start("partial");
  assert.equal(requests, 1);
  resolve({ ok: true, empty: true, message: "Каталог актуален" });
  await pending;
  assert.equal(harness.elements.get("start-full").disabled, false);
  assert.equal(harness.elements.get("launch-status").textContent, "Каталог актуален");
});

test("a verified alternate-source recovery is not reported as a failed title", async () => {
  let posted;
  const harness = await scannerHarness({fetch: async (url, options) => {
    if (url.pathname.endsWith('/results')) {
      posted = JSON.parse(options.body);
      return {ok: true, status: 200, text: async () => JSON.stringify({job: {checked_items: 1, error_count: 0}, result: {recovered_source: 'YummyAnime'}})};
    }
    return response({checked_items: 1, error_count: 0});
  }});
  harness.driver.collect(async () => { throw harness.driver.upstreamError('Missing source', 404); });
  await harness.driver.run();
  assert.equal(posted.upstream_status, 404);
  assert.equal(harness.driver.state.status, 'completed');
  assert.equal(harness.driver.state.errors.length, 0);
  assert.equal(harness.driver.state.error_count, 0);
});

test("discovered tasks and metadata survive reload after an uncertain delivery", async () => {
  const task = { anime_id: 701, title: "New title", url: "https://animego.me/anime/new-701", known_episode_ids: [], selection_reason: "discovery" };
  let posted;
  const first = await scannerHarness({ discovery: true, fetch: async (url, options) => {
    if (url.pathname.endsWith("/discover")) return { ok: true, text: async () => JSON.stringify({ tasks: [task], done: true }) };
    posted = JSON.parse(options.body);
    throw new TypeError("Uncertain delivery");
  } });
  first.driver.upstream(async path => path === "/anime" ? "listing HTML" : "<h1>New title</h1>");
  first.driver.collect(async () => [{ episode: { id: 70101 }, providers: [{ provider_id: "fixture" }] }]);
  await first.driver.run();
  assert.equal(first.driver.state.status, "error");
  assert.equal(first.snapshot().payload.tasks[0].anime_id, 701);
  assert.equal(first.snapshot().checkpoint.pending_result.detail_html, "<h1>New title</h1>");
  const paths = [];
  const resumed = await scannerHarness({ stored: first.snapshot(), fetch: async (url, options) => {
    paths.push(url.pathname);
    if (url.pathname.endsWith("/results")) assert.deepEqual(JSON.parse(options.body), posted);
    return response({ checked_items: 1, new_episode_count: 1 });
  } });
  resumed.driver.collect(async () => { throw new Error("Must replay"); });
  await resumed.driver.run();
  assert.equal(resumed.driver.state.status, "completed");
  assert.deepEqual(paths, ["/api/animego-scans/1/results", "/api/animego-scans/1/complete"]);
});

test("a failed delivery survives reload and retries the exact collected result", async () => {
  const bodies = [];
  const first = await scannerHarness({ fetch: async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    throw new TypeError("Failed to fetch");
  } });
  first.driver.collect(async () => [{ episode: { id: 10001 }, providers: [{ provider_id: "fixture" }] }]);
  first.driver.upstream(async () => "<h1>Fixture</h1>");
  await first.driver.run();
  assert.equal(first.driver.state.status, "error");
  assert.equal(first.driver.state.next_index, 0);
  assert.equal(first.driver.state.error_count, 0);
  assert.equal(bodies.length, 1);
  assert.equal(first.snapshot().checkpoint.pending_result.episodes[0].episode.id, 10001);

  const paths = [];
  const resumed = await scannerHarness({ stored: first.snapshot(), fetch: async (url, options) => {
    paths.push(url.pathname);
    if (url.pathname.endsWith("/results")) {
      assert.deepEqual(JSON.parse(options.body), bodies[0]);
    }
    return response({ checked_items: 1, new_episode_count: 1, new_provider_count: 1, error_count: 0 });
  } });
  resumed.driver.collect(async () => { throw new Error("Must not scrape again"); });
  await resumed.driver.run();
  assert.equal(resumed.driver.state.status, "completed");
  assert.equal(resumed.driver.state.checked_items, 1);
  assert.equal(resumed.driver.state.pending_result, null);
  assert.deepEqual(paths, ["/api/animego-scans/1/results", "/api/animego-scans/1/complete"]);
});

test("existing titles send metadata even without new episodes", async () => {
  let body;
  const harness = await scannerHarness({ fetch: async (url, options) => {
    if (url.pathname.endsWith("/results")) body = JSON.parse(options.body);
    return response({ checked_items: 1 });
  } });
  harness.driver.collect(async () => []);
  harness.driver.upstream(async () => "<h1>Completed title</h1>");
  await harness.driver.run();
  assert.equal(harness.driver.state.status, "completed");
  assert.equal(body.detail_html, "<h1>Completed title</h1>");
  assert.deepEqual(body.episodes, []);
});

test("legacy server tasks without a card URL still deliver collected episodes", async () => {
  let body;
  const harness = await scannerHarness({ legacy: true, fetch: async (url, options) => {
    if (url.pathname.endsWith("/results")) body = JSON.parse(options.body);
    return response({ checked_items: 1, new_episode_count: 1, error_count: 0 });
  } });
  harness.driver.collect(async () => [{ episode: { id: 10001 }, providers: [{ provider_id: "fixture" }] }]);
  harness.driver.upstream(async () => { throw new Error("No card URL supplied"); });
  await harness.driver.run();
  assert.equal(harness.driver.state.status, "completed");
  assert.equal(harness.driver.state.error_count, 0);
  assert.equal(body.episodes[0].episode.id, 10001);
  assert.equal(body.error, undefined);
  assert.equal(body.detail_html, undefined);
});

test("completed scans with failed titles do not claim the catalog was updated", async () => {
  const harness = await scannerHarness({ fetch: async () => response({ checked_items: 1, error_count: 1 }) });
  harness.driver.collect(async () => { throw new Error("AnimeGo ответил HTTP 404."); });
  await harness.driver.run();
  assert.equal(harness.driver.state.status, "completed");
  assert.match(harness.elements.get("subtitle").textContent, /с ошибками: 1 из 1/);
  assert.doesNotMatch(harness.elements.get("subtitle").textContent, /Каталог обновлён/);
  const reopened = await scannerHarness({ stored: harness.snapshot(), fetch: async () => { throw new Error("Completed job must not restart"); } });
  const messages = reopened.elements.get("log").children.map(item => item.children[1].textContent);
  assert.ok(messages.some(message => message.includes("Fixture: AnimeGo ответил HTTP 404.")));
});

test("a failed stop can be retried without reloading the extension", async () => {
  let requests = 0;
  const { driver } = await scannerHarness({ fetch: async () => {
    requests += 1;
    if (requests === 1) throw new TypeError("Failed to fetch");
    return response({ error_count: 0 });
  } });
  await driver.stop();
  assert.equal(driver.state.status, "error");
  await driver.stop();
  assert.equal(requests, 2);
  assert.equal(driver.state.status, "stopped");
});

test("reloading during stop leaves a retryable state", async () => {
  const first = await scannerHarness({ fetch: async () => response({}) });
  const stored = first.snapshot();
  stored.checkpoint.status = "stopping";
  const { driver } = await scannerHarness({ stored, fetch: async () => response({}) });
  assert.equal(driver.state.status, "error");
  await driver.stop();
  assert.equal(driver.state.status, "stopped");
});

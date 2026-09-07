import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

// Exercise the actual scanner orchestration and checkpoint persistence. Only
// browser APIs and collection at the upstream boundary are substituted.
async function scannerHarness({ stored = null, fetch }) {
  const element = () => ({
    children: [], style: {}, append() {}, prepend() {}, setAttribute() {}, addEventListener() {},
  });
  const payload = { job_id: "1", origin: "http://127.0.0.1:8765", token: "fixture-token", mode: "full", tasks: [{ anime_id: 100, title: "Fixture" }] };
  let storage = stored || {
    payload, checkpoint: { job_id: "1", status: "paused" },
  };
  const browser = {
    runtime: { sendMessage: async () => {}, onMessage: { addListener() {} } },
    storage: { local: {
      get: async () => ({ animegoScannerSession: structuredClone(storage) }),
      set: async value => { storage = structuredClone(value.animegoScannerSession); },
    } },
  };
  const context = vm.createContext({
    document: { getElementById: element, createElement: element },
    resolveExtensionApi: () => browser, fetch, URL, AbortController, DOMException,
    setTimeout, queueMicrotask, console, shouldRestartAfterReload: () => false,
  });
  const source = fs.readFileSync(new URL("scanner.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?from "[^"\n]+";\n/gm, "");
  vm.runInContext(source + `
    globalThis.driver = {
      get state() { return checkpoint; },
      collect(fn) { collectTitle = fn; },
      async run() { checkpoint.status = "running"; await runScan(); },
      stop: stopScan,
    };
  `, context, { filename: "scanner.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { driver: context.driver, snapshot: () => structuredClone(storage) };
}

function response(job) {
  return { ok: true, status: 200, text: async () => JSON.stringify({ job }) };
}

test("a failed delivery survives reload and retries the exact collected result", async () => {
  const bodies = [];
  const first = await scannerHarness({ fetch: async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    throw new TypeError("Failed to fetch");
  } });
  first.driver.collect(async () => [{ episode: { id: 10001 }, providers: [{ provider_id: "fixture" }] }]);
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

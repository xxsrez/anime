import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fetchKinogooHtml, kinogooPlayer, kinogooEpisodes, KINOGOO_ORIGINS } from "./kinogoo.js";
import { shouldRestartAfterReload } from "./scan-state.js";

// Exercise the actual scanner orchestration and checkpoint persistence. Only
// browser APIs and collection at the upstream boundary are substituted.
async function scannerHarness({ stored = null, storedTheme, fetch, discovery = false, legacy = false, sendMessage = async () => {}, permission = true, fastTimers = false, saveCheckpoint = async () => ({ ok: true }), beforeGet = async () => {}, requestPermission = async () => true }) {
  const element = () => ({
    children: [], style: {}, attributes: {}, listeners: {},
    append(...items) { this.children.push(...items); },
    prepend(...items) { this.children.unshift(...items); },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
  });
  const elements = new Map();
  const payload = { job_id: "1", origin: "http://127.0.0.1:8765", token: "fixture-token", mode: "full", tasks: [{ anime_id: 100, title: "Fixture", url: "https://animego.me/anime/fixture-100" }] };
  if (legacy) delete payload.tasks[0].url;
  if (discovery) { payload.discovery = true; payload.tasks = []; }
  let storage = stored || {
    payload, checkpoint: { job_id: "1", status: "paused" },
  };
  let onStorageChanged;
  const browser = {
    permissions: { contains: async () => permission, request: requestPermission },
    runtime: { sendMessage: async message => {
      if (message.type === "animego-scanner-save-checkpoint") {
        const result = await saveCheckpoint();
        if (!result?.ok) return result;
        if (String(storage.payload.job_id) !== String(message.payload.job_id)) return { ok: true, stale: true };
        storage = structuredClone({ ...storage, payload: message.payload, checkpoint: message.checkpoint });
        return { ok: true };
      }
      return sendMessage(message);
    }, onMessage: { addListener() {} } },
    storage: { onChanged: { addListener(listener) { onStorageChanged = listener; } }, local: {
      get: async () => { await beforeGet(); return { animegoScannerSession: structuredClone(storage),
        ...(storedTheme !== undefined ? { animegoScannerTheme: storedTheme } : {}) }; },
      set: async value => { storage = structuredClone(value.animegoScannerSession); },
    } },
  };
  const context = vm.createContext({
    document: { documentElement: { dataset: {} }, getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }, createElement: element },
    resolveExtensionApi: () => browser, fetch, URL, AbortController, DOMException,
    KINOGOO_ORIGINS, fetchKinogooHtml, kinogooPlayer, kinogooEpisodes,
    hasAnimeGoHostPermission: async () => permission, requestAnimeGoHostPermission: requestPermission,
    setTimeout: fastTimers ? (fn, ms) => setTimeout(fn, ms >= 15000 ? 1 : ms) : setTimeout,
    clearTimeout, queueMicrotask, console, shouldRestartAfterReload,
  });
  const source = fs.readFileSync(new URL("scanner.js", import.meta.url), "utf8")
    .replace(/^import[\s\S]*?from "[^"\n]+";\n/gm, "");
  vm.runInContext(source + `
    globalThis.driver = {
      get state() { return checkpoint; },
      collect(fn) { collectTitle = fn; },
      upstream(fn) { upstreamJson = fn; },
      upstreamError(message, status) { return new UpstreamError(message, { status }); },
      apiError(message, status) { return new ApiError(message, status); },
      upstreamReal(path) { interruptibleDelay = async () => {}; respectUpstreamDelay = async () => {}; return upstreamJson(path, generation); },
      fastUpstream() { respectUpstreamDelay = async () => {}; },
      waitForUpstream() { return respectUpstreamDelay(generation); },
      render,
      reload: loadSession,
      async run() { checkpoint.status = "running"; await runScan(); },
      stop: stopScan,
      grant: grantAnimeGoAccess,
      start: startNewScan,
    };
  `, context, { filename: "scanner.js" });
  await new Promise(resolve => setImmediate(resolve));
  return { driver: context.driver, elements, root: context.document.documentElement,
    changeTheme: theme => onStorageChanged({ animegoScannerTheme: { newValue: theme } }, "local"),
    replaceSession: session => { storage = structuredClone(session); },
    snapshot: () => structuredClone(storage) };
}

test("scanner uses the catalog theme and falls back to the system for older sessions", async () => {
  const original = await scannerHarness({});
  for (const theme of ["light", "dark", "unexpected"]) {
    const stored = original.snapshot();
    stored.payload.theme = theme;
    const scanner = await scannerHarness({ stored });
    assert.equal(scanner.root.dataset.theme, theme === "unexpected" ? undefined : theme);
  }
  const stored = original.snapshot();
  stored.payload.theme = "dark";
  const reopened = await scannerHarness({ stored, storedTheme: "light" });
  assert.equal(reopened.root.dataset.theme, "light");
  reopened.changeTheme("dark");
  assert.equal(reopened.root.dataset.theme, "dark");
  const system = await scannerHarness({ stored, storedTheme: null });
  assert.equal(system.root.dataset.theme, undefined);
});

test("full scan discovery remains indeterminate after known titles are checked", async () => {
  const h = await scannerHarness({ discovery: true });
  h.driver.state.status = "running";
  h.driver.render();
  assert.equal(h.elements.get("progress-track").attributes["data-indeterminate"], "true");
  assert.equal(h.elements.get("progress-track").attributes["aria-valuenow"], undefined);
  assert.match(h.elements.get("progress-text").textContent, /поиск/);
  assert.match(h.elements.get("current-title").textContent, /Ищем новые тайтлы/);
  h.driver.state.status = "paused";
  h.driver.render();
  assert.equal(h.elements.get("progress-track").attributes["data-indeterminate"], "true");
  assert.equal(h.elements.get("progress-track").attributes["data-active"], undefined);
  h.driver.state.discovery_done = true;
  h.driver.render();
  assert.equal(h.elements.get("progress-track").attributes["data-indeterminate"], undefined);
  assert.equal(h.elements.get("progress-track").attributes["aria-valuenow"], "0");
});

test("reopening the same job does not duplicate its log entries", async () => {
  const h = await scannerHarness({});
  const initialCount = h.elements.get("log").children.length;
  await h.driver.reload();
  await h.driver.reload();
  assert.equal(h.elements.get("log").children.length, initialCount);
});

test("paused and stopped scans do not claim the extension is checking titles", async () => {
  const h = await scannerHarness({});
  h.driver.state.status = "paused";
  h.driver.render();
  assert.match(h.elements.get("subtitle").textContent, /пауз/);
  h.driver.state.status = "stopped";
  h.driver.render();
  assert.match(h.elements.get("subtitle").textContent, /остановлено/);
});

test("control save failures are visible and leave retryable state", async () => {
  const h = await scannerHarness({ saveCheckpoint: async () => ({ ok: false, error: "Сохранение недоступно" }) });
  h.elements.get("pause").listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.driver.state.status, "paused");
  assert.equal(h.elements.get("status").textContent, "Сохранение недоступно");
  assert.equal(h.elements.get("status").className, "status error");
  h.elements.get("stop").listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.driver.state.status, "error");
  assert.equal(h.elements.get("stop").disabled, false);
  assert.equal(h.elements.get("status").textContent, "Сохранение недоступно");
});

test("upstream requests wait while a scan is paused", async () => {
  const h = await scannerHarness({});
  let finished = false;
  const waiting = h.driver.waitForUpstream().then(() => { finished = true; });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(finished, false);
  h.driver.state.status = "running";
  await waiting;
  assert.equal(finished, true);
});

test("an old stop cannot stop or corrupt a newly loaded job", async () => {
  let releaseSave;
  let notifySave;
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  let apiCalls = 0;
  const h = await scannerHarness({
    saveCheckpoint: async () => { notifySave(); await saveGate; return { ok: true }; },
    fetch: async () => { apiCalls += 1; return response({}); },
  });
  const oldStop = h.driver.stop();
  await saveStarted;
  const next = h.snapshot();
  next.payload.job_id = "2";
  next.checkpoint = { job_id: "2", status: "paused" };
  h.replaceSession(next);
  await h.driver.reload();
  releaseSave();
  await oldStop;
  assert.equal(h.driver.state.job_id, "2");
  assert.equal(h.driver.state.status, "paused");
  assert.equal(apiCalls, 0);
});

test("a late final checkpoint cannot report an error against a replacement job", async () => {
  let releaseSave;
  let notifySave;
  let saves = 0;
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => {
      saves += 1;
      if (saves === 2) { notifySave(); await saveGate; }
      return { ok: true };
    },
    fetch: async () => response({}),
  });
  const oldStop = h.driver.stop();
  await saveStarted;
  const next = h.snapshot();
  next.payload.job_id = "2";
  next.checkpoint = { job_id: "2", status: "paused" };
  h.replaceSession(next);
  await h.driver.reload();
  releaseSave();
  await oldStop;
  assert.equal(h.driver.state.job_id, "2");
  assert.equal(h.driver.state.status, "paused");
  assert.notEqual(h.elements.get("status").className, "status error");
});

test("a replacement job does not start using data from the previous job while storage is loading", async () => {
  let blockGet = false;
  let releaseGet;
  let releaseOldTask;
  let notifyOldTask;
  let notifyComplete;
  const getGate = new Promise(resolve => { releaseGet = resolve; });
  const oldTaskGate = new Promise(resolve => { releaseOldTask = resolve; });
  const oldTaskStarted = new Promise(resolve => { notifyOldTask = resolve; });
  const completed = new Promise(resolve => { notifyComplete = resolve; });
  const collected = [];
  const h = await scannerHarness({
    beforeGet: async () => { if (blockGet) await getGate; },
    fetch: async (url) => {
      if (url.pathname.endsWith("/complete")) notifyComplete();
      return response({ checked_items: 1 });
    },
  });
  h.driver.collect(async task => {
    collected.push(task.anime_id);
    if (task.anime_id === 100) { notifyOldTask(); await oldTaskGate; }
    return [];
  });
  h.driver.upstream(async () => "<h1>Fixture</h1>");
  const oldRun = h.driver.run();
  await oldTaskStarted;
  const next = h.snapshot();
  next.payload.job_id = "2";
  next.payload.tasks[0].anime_id = 200;
  next.checkpoint = { job_id: "2", status: "running" };
  h.replaceSession(next);
  blockGet = true;
  const reload = h.driver.reload();
  releaseOldTask();
  await oldRun;
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(collected, [100]);
  releaseGet();
  await reload;
  await completed;
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(collected, [100, 200]);
  assert.equal(h.driver.state.status, "completed");
});

test("controls do not act on the previous job while a replacement is loading", async () => {
  let blockGet = false;
  let releaseGet;
  let saves = 0;
  const gate = new Promise(resolve => { releaseGet = resolve; });
  const h = await scannerHarness({
    beforeGet: async () => { if (blockGet) await gate; },
    saveCheckpoint: async () => { saves += 1; return { ok: true }; },
  });
  const next = h.snapshot();
  next.payload.job_id = "2";
  next.checkpoint = { job_id: "2", status: "paused" };
  h.replaceSession(next);
  blockGet = true;
  const reload = h.driver.reload();
  assert.equal(h.elements.get("pause").disabled, true);
  assert.equal(h.elements.get("stop").disabled, true);
  assert.equal(h.elements.get("start-partial").disabled, true);
  h.elements.get("pause").listeners.click();
  await h.driver.stop();
  await h.driver.grant();
  assert.equal(saves, 0);
  releaseGet();
  await reload;
  assert.equal(h.driver.state.job_id, "2");
  assert.equal(h.driver.state.status, "paused");
});

test("permission checkpoint failure keeps the scan retryable", async () => {
  const h = await scannerHarness({ saveCheckpoint: async () => ({ ok: false, error: "Сохранение недоступно" }) });
  h.driver.state.status = "permission";
  h.driver.render();
  await h.driver.grant();
  assert.equal(h.driver.state.status, "permission");
  assert.equal(h.elements.get("grant-access").disabled, false);
  assert.equal(h.elements.get("status").textContent, "Сохранение недоступно");
});

test("late permission approval preserves the stopped status", async () => {
  let approve;
  let notifyRequest;
  const requested = new Promise(resolve => { notifyRequest = resolve; });
  const gate = new Promise(resolve => { approve = resolve; });
  const h = await scannerHarness({
    requestPermission: async () => { notifyRequest(); await gate; return true; },
    fetch: async () => response({}),
  });
  h.driver.state.status = "permission";
  h.driver.render();
  const granting = h.driver.grant();
  await requested;
  await h.driver.stop();
  approve();
  await granting;
  assert.equal(h.driver.state.status, "stopped");
  assert.match(h.elements.get("status").textContent, /остановлено/);
});

test("late permission denial preserves the stopped status", async () => {
  let respond;
  let notifyRequest;
  const requested = new Promise(resolve => { notifyRequest = resolve; });
  const gate = new Promise(resolve => { respond = resolve; });
  const h = await scannerHarness({
    requestPermission: async () => { notifyRequest(); await gate; return false; },
    fetch: async () => response({}),
  });
  h.driver.state.status = "permission";
  const granting = h.driver.grant();
  await requested;
  await h.driver.stop();
  respond();
  await granting;
  assert.equal(h.driver.state.status, "stopped");
  assert.match(h.elements.get("status").textContent, /остановлено/);
});

test("a failed permission save cannot undo a later stop", async () => {
  let release;
  let notifySave;
  let saves = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { notifySave = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => { if (++saves === 1) { notifySave(); await gate; return { ok: false, error: "Не сохранено" }; } return { ok: true }; },
    fetch: async () => response({}),
  });
  h.driver.state.status = "permission";
  const granting = h.driver.grant();
  await started;
  await h.driver.stop();
  release();
  await granting;
  assert.equal(h.driver.state.status, "stopped");
  assert.match(h.elements.get("status").textContent, /остановлено/);
});

test("a failed resume save cannot undo a later stop", async () => {
  let release;
  let notifySave;
  let saves = 0;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { notifySave = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => { if (++saves === 1) { notifySave(); await gate; return { ok: false, error: "Не сохранено" }; } return { ok: true }; },
    fetch: async () => response({}),
  });
  h.elements.get("pause").listeners.click();
  await started;
  await h.driver.stop();
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.driver.state.status, "stopped");
  assert.match(h.elements.get("status").textContent, /остановлено/);
});

test("a failed pause save cannot overwrite a newer scan error", async () => {
  let failTask;
  let notifyTask;
  let releaseSave;
  let notifySave;
  let saves = 0;
  const taskStarted = new Promise(resolve => { notifyTask = resolve; });
  const taskGate = new Promise((_resolve, reject) => { failTask = reject; });
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => { if (++saves === 1) { notifySave(); await saveGate; return { ok: false, error: "Не сохранено" }; } return { ok: true }; },
  });
  h.driver.collect(async () => { notifyTask(); return taskGate; });
  const running = h.driver.run();
  await taskStarted;
  h.elements.get("pause").listeners.click();
  await saveStarted;
  failTask(h.driver.apiError("Сервер недоступен", 503));
  await running;
  releaseSave();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.driver.state.status, "error");
  assert.equal(h.elements.get("status").textContent, "Сервер недоступен");
});

test("starting another scan first retries an unsaved terminal checkpoint", async () => {
  let failSave = true;
  let createCalls = 0;
  let h;
  h = await scannerHarness({
    saveCheckpoint: async () => failSave ? { ok: false, error: "Хранилище недоступно" } : { ok: true },
    sendMessage: async message => {
      if (message.type === "animego-scanner-create") {
        createCalls += 1;
        assert.equal(h.snapshot().checkpoint.status, "expired");
        return { ok: true, empty: true, message: "Нет заданий" };
      }
    },
  });
  h.driver.state.status = "expired";
  h.driver.render();
  await h.driver.start("partial");
  assert.equal(createCalls, 0);
  assert.equal(h.elements.get("launch-status").textContent, "Хранилище недоступно");
  failSave = false;
  await h.driver.start("partial");
  assert.equal(createCalls, 1);
  assert.equal(h.elements.get("launch-status").textContent, "Нет заданий");
});

test("a persistent checkpoint failure during completion is visible and retryable", async () => {
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.checkpoint = { job_id: "1", status: "paused", next_index: 1, discovery_done: true };
  const h = await scannerHarness({ stored, saveCheckpoint: async () => ({ ok: false, error: "Хранилище недоступно" }) });
  await h.driver.run();
  assert.equal(h.driver.state.status, "error");
  assert.equal(h.elements.get("pause").disabled, false);
  assert.equal(h.elements.get("stop").disabled, false);
  assert.match(h.elements.get("status").textContent, /Хранилище недоступно/);
});

test("a final stop checkpoint failure keeps a failed stop retryable", async () => {
  let saves = 0;
  const h = await scannerHarness({
    saveCheckpoint: async () => ++saves === 2 ? { ok: false, error: "Хранилище недоступно" } : { ok: true },
    fetch: async () => ({ ok: false, status: 503, text: async () => JSON.stringify({ error: "Сервер недоступен" }) }),
  });
  h.elements.get("stop").listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.driver.state.status, "error");
  assert.equal(h.elements.get("stop").disabled, false);
  assert.match(h.elements.get("status").textContent, /Хранилище недоступно/);
});

test("an expired scan remains usable if its checkpoint cannot be saved", async () => {
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.payload.job = { expires_at: "2020-01-01T00:00:00+00:00" };
  const h = await scannerHarness({ stored, saveCheckpoint: async () => ({ ok: false, error: "Хранилище недоступно" }) });
  assert.equal(h.driver.state.status, "expired");
  assert.equal(h.elements.get("start-partial").disabled, false);
  assert.match(h.elements.get("status").textContent, /Хранилище недоступно/);
});

test("stopping while a result checkpoint is saving does not send that result", async () => {
  let releaseSave;
  let notifySave;
  let saves = 0;
  let results = 0;
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => { if (++saves === 1) { notifySave(); await saveGate; } return { ok: true }; },
    fetch: async url => { if (url.pathname.endsWith("/results")) results += 1; return response({}); },
  });
  h.driver.collect(async () => []);
  h.driver.upstream(async () => "<h1>Fixture</h1>");
  const running = h.driver.run();
  await saveStarted;
  await h.driver.stop();
  releaseSave();
  await running;
  assert.equal(results, 0);
  assert.equal(h.driver.state.status, "stopped");
});

test("a completion checkpoint from an old job cannot complete its replacement", async () => {
  let releaseSave;
  let notifySave;
  let completeCalls = 0;
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.checkpoint = { job_id: "1", status: "paused", next_index: 1, discovery_done: true };
  const h = await scannerHarness({
    stored,
    saveCheckpoint: async () => { notifySave(); await saveGate; return { ok: true }; },
    fetch: async url => { if (url.pathname.endsWith("/complete")) completeCalls += 1; return response({}); },
  });
  const oldRun = h.driver.run();
  await saveStarted;
  const next = h.snapshot();
  next.payload.job_id = "2";
  next.checkpoint = { job_id: "2", status: "paused" };
  h.replaceSession(next);
  await h.driver.reload();
  releaseSave();
  await oldRun;
  assert.equal(completeCalls, 0);
  assert.equal(h.driver.state.job_id, "2");
  assert.equal(h.driver.state.status, "paused");
});

test("resume during an error checkpoint restarts after the old loop exits", async () => {
  let releaseSave;
  let notifySave;
  let notifyComplete;
  let saves = 0;
  let calls = 0;
  const saveStarted = new Promise(resolve => { notifySave = resolve; });
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  const completed = new Promise(resolve => { notifyComplete = resolve; });
  const h = await scannerHarness({
    saveCheckpoint: async () => { if (++saves === 1) { notifySave(); await saveGate; } return { ok: true }; },
    fetch: async url => { if (url.pathname.endsWith("/complete")) notifyComplete(); return response({ checked_items: 1 }); },
  });
  h.driver.collect(async () => { if (++calls === 1) throw h.driver.apiError("Temporary", 503); return []; });
  h.driver.upstream(async () => "<h1>Fixture</h1>");
  const oldRun = h.driver.run();
  await saveStarted;
  h.elements.get("pause").listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  releaseSave();
  await oldRun;
  await completed;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 2);
  assert.equal(h.driver.state.status, "completed");
});

function response(job) {
  return { ok: true, status: 200, text: async () => JSON.stringify({ job }) };
}

test("loading an old error checkpoint recognizes expiry without retrying or losing progress", async () => {
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.payload.job = { expires_at: "2020-01-01T00:00:00+00:00" };
  stored.checkpoint = { job_id: "1", status: "error", checked_items: 1, new_episode_count: 2 };
  const h = await scannerHarness({ stored, fetch: () => { throw new Error("Must not send a request"); } });
  assert.equal(h.driver.state.status, "expired");
  assert.equal(h.snapshot().checkpoint.status, "expired");
  assert.equal(h.driver.state.checked_items, 1);
  assert.equal(h.driver.state.new_episode_count, 2);
  assert.equal(h.elements.get("start-partial").disabled, false);
  assert.match(h.elements.get("status").textContent, /Срок задания истёк/);
  stored.checkpoint.status = "completed";
  const completed = await scannerHarness({ stored });
  assert.equal(completed.driver.state.status, "completed");
});

for (const phase of ["result", "error-result", "complete", "discovery", "stop"]) {
  test(`expired ${phase} unlocks a new scan and preserves the explanation across reload`, async () => {
    const h = await scannerHarness({ discovery: phase === "discovery", fetch: async url => {
      if (phase === "complete" && url.pathname.endsWith("/results")) return response({ checked_items: 1 });
      return { ok: false, status: 410, text: async () => JSON.stringify({ error: "scan expired" }) };
    } });
    h.driver.collect(async () => {
      if (phase === "error-result") throw h.driver.upstreamError("Missing", 404);
      return [];
    });
    h.driver.upstream(async () => "<h1>Fixture</h1>");
    if (phase === "stop") await h.driver.stop();
    else await h.driver.run();
    assert.equal(h.driver.state.status, "expired");
    assert.equal(h.elements.get("start-partial").disabled, false);
    assert.equal(h.elements.get("pause").disabled, true);
    assert.equal(h.elements.get("stop").disabled, true);
    assert.match(h.elements.get("status").textContent, /Срок задания истёк/);
    const reloaded = await scannerHarness({ stored: h.snapshot(), fetch: async () => { throw new Error("No retry expected"); } });
    assert.equal(reloaded.driver.state.status, "expired");
    assert.match(reloaded.elements.get("status").textContent, /Срок задания истёк/);
    assert.ok(reloaded.elements.get("log").children.some(item => item.children.some(child => /Срок задания истёк/.test(child.textContent))));
  });
}

test("temporary API failure keeps retry available and explains the error after reload", async () => {
  const h = await scannerHarness({ fetch: async () => ({ ok: false, status: 503, text: async () => JSON.stringify({ error: "Temporarily unavailable" }) }) });
  h.driver.collect(async () => []);
  h.driver.upstream(async () => "<h1>Fixture</h1>");
  await h.driver.run();
  const reloaded = await scannerHarness({ stored: h.snapshot() });
  assert.equal(reloaded.driver.state.status, "error");
  assert.equal(reloaded.elements.get("pause").disabled, false);
  assert.equal(reloaded.elements.get("start-partial").disabled, true);
  assert.equal(reloaded.elements.get("status").textContent, "Temporarily unavailable");
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

test("AnimeGo timeout retries and reports an ordinary upstream error", async () => {
  let attempts = 0;
  const h = await scannerHarness({ fastTimers: true, fetch: async (_url, options) => {
    attempts += 1;
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
  } });
  await assert.rejects(h.driver.upstreamReal("/player/100"), /AnimeGo не ответил вовремя/);
  assert.equal(attempts, 3);
});

test("Kinogoo timeout records the failed title and completes the scan", async () => {
  let posted;
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.payload.tasks[0] = { anime_id: 100, source: "kinogoo", url: "https://kinogoo.fm/123-title.html" };
  const h = await scannerHarness({ stored, fastTimers: true, fetch: async (address, options) => {
    const url = new URL(address);
    if (url.hostname === "kinogoo.fm") return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
    if (url.pathname.endsWith("/results")) posted = JSON.parse(options.body);
    return response({ checked_items: 1, error_count: 1 });
  } });
  h.driver.fastUpstream();
  await h.driver.run();
  assert.match(posted.error, /Kinogoo не ответил вовремя/);
  assert.equal(h.driver.state.next_index, 1);
  assert.equal(h.driver.state.status, "completed");
});

test("stopping during a Kinogoo request does not report a timeout", async () => {
  let requestStarted;
  const started = new Promise(resolve => { requestStarted = resolve; });
  let results = 0;
  const original = await scannerHarness({});
  const stored = original.snapshot();
  stored.payload.tasks[0] = { anime_id: 100, source: "kinogoo", url: "https://kinogoo.fm/123-title.html" };
  const h = await scannerHarness({ stored, fetch: async (address, options) => {
    const url = new URL(address);
    if (url.hostname === "kinogoo.fm") {
      requestStarted();
      return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
    }
    if (url.pathname.endsWith("/results")) results += 1;
    return response({});
  } });
  h.driver.fastUpstream();
  const running = h.driver.run();
  await started;
  await h.driver.stop();
  await running;
  assert.equal(h.driver.state.status, "stopped");
  assert.equal(results, 0);
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
  await new Promise(done => setImmediate(done));
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

test("metadata failure does not discard episodes for an existing title", async () => {
  let body;
  const h = await scannerHarness({ fetch: async (url, options) => {
    if (url.pathname.endsWith("/results")) body = JSON.parse(options.body);
    return response({ checked_items: 1, new_episode_count: 1 });
  } });
  h.driver.collect(async () => [{ episode: { id: 10001 }, providers: [{ provider_id: "fixture" }] }]);
  h.driver.upstream(async () => { throw h.driver.upstreamError("Карточка недоступна", 404); });
  await h.driver.run();
  assert.equal(body.episodes[0].episode.id, 10001);
  assert.equal(body.detail_html, undefined);
  assert.equal(h.driver.state.status, "completed");
  assert.ok(h.elements.get("log").children.some(item => item.children.some(child => /Не удалось обновить карточку/.test(child.textContent))));
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

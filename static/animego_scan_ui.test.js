const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const appSource = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const indexSource = fs.readFileSync(`${__dirname}/index.html`, "utf8");
const scannerStart = appSource.indexOf("function setAnimeGoScanMenuOpen");
const scannerEnd = appSource.indexOf("function searchText", scannerStart);
assert.ok(scannerStart >= 0 && scannerEnd > scannerStart, "scanner controller block exists");
const scannerSource = appSource.slice(scannerStart, scannerEnd);

function element(extra = {}) {
  const attributes = new Map();
  return {
    hidden: false,
    disabled: false,
    open: false,
    textContent: "",
    title: "",
    dataset: {},
    setAttribute(name, value) {
      attributes.set(name, String(value));
      if (name === "open") this.open = true;
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    removeAttribute(name) {
      attributes.delete(name);
      if (name === "open") this.open = false;
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
    focus() {},
    contains() {
      return false;
    },
    ...extra,
  };
}

function scannerHarness({
  ready = true,
  upstreamReady = true,
  permissionResult,
  apiResult,
  apiError,
  apiHandler,
} = {}) {
  const events = [];
  const requests = [];
  const statuses = [];
  const timers = new Map();
  const documentListeners = new Map();
  let nextTimerId = 1;
  const el = {
    animeGoScanControl: element(),
    animeGoScanSplit: element(),
    animeGoScanButton: element(),
    animeGoScanMenuToggle: element(),
    animeGoScanMenu: element({ hidden: true }),
    animeGoScanMenuItems: [],
    animeGoScanState: element(),
    animeGoScanDialog: element(),
    animeGoScanDialogTitle: element(),
    animeGoScanDialogMessage: element(),
    animeGoScanSetupLink: element(),
    animeGoScanDialogCancel: element(),
    animeGoScanDialogConfirm: element(),
  };
  const state = {
    animeGoScannerReady: ready,
    animeGoScannerVersion: null,
    animeGoScannerUpstreamReady: upstreamReady,
    animeGoScanPhase: "idle",
    animeGoScanJobId: null,
    animeGoScanMode: null,
    selectedAnimeId: 42,
    selectedEpisodeId: null,
    selectedContentSource: null,
    selectedTranslation: null,
    selectedSourceId: null,
    detail: null,
    anime: [],
    user: { id: 7 },
  };
  class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }
  const context = vm.createContext({
    ANIMEGO_SCAN_ENDPOINT: "/api/animego-scans",
    ANIMEGO_SCAN_POLL_INTERVAL_MS: 2000,
    ANIMEGO_SCANNER_PREPARE_TIMEOUT_MS: 2500,
    CustomEvent,
    animeGoScanDialogResolve: null,
    animeGoScanPollTimer: 0,
    animeGoScanPollGeneration: 0,
    pendingAnimeGoScan: null,
    document: {
      activeElement: null,
      addEventListener(type, listener) {
        const listeners = documentListeners.get(type) || new Set();
        listeners.add(listener);
        documentListeners.set(type, listeners);
      },
      removeEventListener(type, listener) {
        documentListeners.get(type)?.delete(listener);
      },
      dispatchEvent(event) {
        events.push(event);
        for (const listener of documentListeners.get(event.type) || []) {
          listener(event);
        }
        if (event.type === "animego-scanner-prepare") {
          Promise.resolve().then(() => {
            const result = typeof permissionResult === "function"
              ? permissionResult()
              : permissionResult;
            this.dispatchEvent(new CustomEvent("animego-scanner-permission-state", {
              detail: result ?? { granted: upstreamReady },
            }));
          });
        }
        return true;
      },
    },
    el,
    state,
    window: {
      location: { origin: "https://anime.test" },
      setTimeout(callback) {
        const id = nextTimerId;
        nextTimerId += 1;
        timers.set(id, callback);
        return id;
      },
      clearTimeout(id) {
        timers.delete(id);
      },
    },
    api: async (path, options) => {
      requests.push({ path, options });
      if (path === "/api/anime") return { items: [] };
      if (apiHandler) return apiHandler(path, options);
      if (apiError) throw apiError;
      return apiResult;
    },
    applyLoadedSearchFields() {},
    applyFilter() {},
    resetContentUpdatesForQuery() {},
    invalidateRecommendations() {},
    isUpdatesView() { return false; },
    isRecommendationView() { return false; },
    loadContentUpdatesForView() {},
    loadRecommendationsForView() {},
    async selectAnime() {},
    reportClientError() {},
    showAppStatus(message, tone) {
      statuses.push({ message, tone });
    },
  });
  vm.runInContext(scannerSource, context, { filename: "app.js#animego-scan-ui" });
  return {
    context,
    el,
    events,
    requests,
    state,
    statuses,
    async runNextTimer() {
      const next = timers.entries().next().value;
      assert.ok(next, "a scan status poll is scheduled");
      const [id, callback] = next;
      timers.delete(id);
      await callback();
    },
  };
}

async function testPartialScanDispatch() {
  const harness = scannerHarness({
    apiResult: {
      job: { id: 17, status: "running", total_items: 2 },
      token: "job-token",
      tasks: [{ anime_id: 1 }, { anime_id: 2 }],
      origin: "https://forged.example",
    },
  });
  await harness.context.startAnimeGoScan("partial");
  assert.equal(harness.requests.length, 1);
  assert.deepEqual(JSON.parse(harness.requests[0].options.body), {
    mode: "partial",
    current_anime_id: 42,
  });
  const start = harness.events.find(event => event.type === "animego-scan-start");
  assert.ok(start, "page dispatches a scan-start event");
  assert.equal(start.detail.job_id, 17);
  assert.equal(start.detail.token, "job-token");
  assert.equal(start.detail.tasks.length, 2);
  assert.equal(start.detail.origin, "https://anime.test");
  assert.equal(harness.state.animeGoScanPhase, "active");
  assert.equal(harness.el.animeGoScanButton.disabled, true);

  harness.context.handleAnimeGoScanComplete({
    detail: { job_id: 17, checked_items: 2, total_items: 2, new_episode_count: 1 },
  });
  assert.equal(harness.state.animeGoScanPhase, "idle");
  assert.match(harness.el.animeGoScanState.textContent, /добавлено серий: 1/);
  await Promise.resolve();
  assert.ok(harness.requests.some(request => request.path === "/api/anime"));
}

async function testNoExtensionShowsSetup() {
  const harness = scannerHarness({ ready: false });
  await harness.context.startAnimeGoScan("partial");
  assert.ok(harness.events.some(event => event.type === "animego-scanner-ping"));
  assert.equal(harness.requests.length, 0);
  assert.equal(harness.el.animeGoScanDialog.open, true);
  assert.equal(harness.el.animeGoScanSetupLink.hidden, false);
}

async function testPermissionIsGrantedBeforeCreatingJob() {
  let permissionGranted = false;
  const harness = scannerHarness({
    upstreamReady: false,
    permissionResult: () => ({ granted: permissionGranted }),
    apiResult: {
      job: { id: 20, status: "running", total_items: 1 },
      token: "job-token",
      tasks: [{ anime_id: 1 }],
    },
  });

  await harness.context.startAnimeGoScan("partial");
  assert.equal(harness.requests.length, 0, "permission denial does not create a server job");
  assert.ok(harness.events.some(event => event.type === "animego-scanner-prepare"));
  assert.match(harness.el.animeGoScanState.textContent, /Разрешите доступ/);

  permissionGranted = true;
  await harness.context.handleAnimeGoScannerPermissionGranted();
  assert.equal(harness.requests[0].path, "/api/animego-scans");
  assert.equal(harness.state.animeGoScanJobId, 20);
}

async function testPermissionIsRecheckedBeforeEveryJob() {
  const harness = scannerHarness({
    upstreamReady: true,
    permissionResult: { granted: false, error: "Доступ отозван" },
    apiResult: {
      job: { id: 23, status: "running", total_items: 1 },
      token: "job-token",
      tasks: [{ anime_id: 1 }],
    },
  });

  await harness.context.startAnimeGoScan("partial");

  assert.equal(harness.requests.length, 0, "revoked permission does not create a server job");
  assert.equal(harness.state.animeGoScannerUpstreamReady, false);
  assert.ok(harness.events.some(event => event.type === "animego-scanner-prepare"));
}

async function testGrantedPreflightClearsStalePendingScan() {
  const harness = scannerHarness({
    upstreamReady: true,
    permissionResult: { granted: true },
    apiResult: {
      job: { id: 24, status: "running", total_items: 1 },
      token: "job-token",
      tasks: [{ anime_id: 1 }],
    },
  });
  harness.context.pendingAnimeGoScan = { mode: "full", fullConfirmed: true };

  await harness.context.startAnimeGoScan("partial");
  await harness.context.handleAnimeGoScannerPermissionGranted();

  assert.equal(
    harness.requests.filter(request => request.path === "/api/animego-scans").length,
    1,
    "a stale permission event cannot launch a second scan",
  );
  assert.equal(JSON.parse(harness.requests[0].options.body).mode, "partial");
}

async function testFullScanConfirmationAndNoWork() {
  const harness = scannerHarness({
    apiResult: {
      job: { id: 18, status: "completed", total_items: 0 },
      token: "job-token",
      tasks: [],
    },
  });
  const request = harness.context.startAnimeGoScan("full");
  assert.equal(harness.el.animeGoScanDialog.open, true);
  harness.context.settleAnimeGoScanDialog(true);
  await request;
  assert.equal(JSON.parse(harness.requests[0].options.body).mode, "full");
  assert.equal(harness.events.some(event => event.type === "animego-scan-start"), false);
  assert.match(harness.el.animeGoScanState.textContent, /каталог уже актуален/);
}

async function testOwnBusyScanReopensExtension() {
  const error = new Error("scan already in progress");
  error.status = 409;
  error.payload = { job: { id: 19, user_id: 7, status: "running" } };
  const harness = scannerHarness({ apiError: error });
  await harness.context.startAnimeGoScan("partial");
  assert.ok(harness.events.some(event => event.type === "animego-scanner-open"));
  assert.match(harness.el.animeGoScanState.textContent, /открываем сканер/);
}

async function testServerStatusSettlesMissedCompletionEvent() {
  const harness = scannerHarness({
    apiHandler(path) {
      if (path === "/api/animego-scans") {
        return {
          job: { id: 21, status: "running", total_items: 2 },
          token: "job-token-for-polling",
          tasks: [{ anime_id: 1 }, { anime_id: 2 }],
        };
      }
      assert.equal(path, "/api/animego-scans/21");
      return {
        job: {
          id: 21,
          status: "completed",
          checked_items: 2,
          total_items: 2,
          new_episode_count: 1,
        },
      };
    },
  });
  await harness.context.startAnimeGoScan("partial");
  assert.equal(harness.state.animeGoScanPhase, "active");
  assert.equal(harness.el.animeGoScanButton.textContent, "Scanning…");

  await harness.runNextTimer();

  assert.equal(harness.state.animeGoScanPhase, "idle");
  assert.equal(harness.el.animeGoScanButton.textContent, "Scan");
  assert.equal(harness.el.animeGoScanButton.disabled, false);
  assert.match(harness.el.animeGoScanState.textContent, /добавлено серий: 1/);
  assert.equal(
    harness.requests.find(request => request.path === "/api/animego-scans/21").options.headers.Authorization,
    "Bearer job-token-for-polling",
  );
}

async function testPermissionLossKeepsRunningJobAttached() {
  const harness = scannerHarness({
    apiResult: {
      job: { id: 22, status: "running", total_items: 1 },
      token: "job-token",
      tasks: [{ anime_id: 1 }],
    },
  });
  await harness.context.startAnimeGoScan("partial");

  harness.context.handleAnimeGoScanError({
    detail: { job_id: 22, permission: true, error: "Разрешите animego.me" },
  });

  assert.equal(harness.state.animeGoScanPhase, "active");
  assert.equal(harness.state.animeGoScanJobId, 22);
  assert.equal(harness.state.animeGoScannerUpstreamReady, false);
  assert.match(harness.el.animeGoScanState.textContent, /Разрешите animego\.me/);
}

assert.match(indexSource, /id="animego-scan-button"/);
assert.match(indexSource, /role="menu"/);
assert.match(indexSource, /data-scan-mode="partial"/);
assert.match(indexSource, /data-scan-mode="full"/);
assert.match(indexSource, /href="\/scanner-setup"/);

Promise.resolve()
  .then(testPartialScanDispatch)
  .then(testNoExtensionShowsSetup)
  .then(testPermissionIsGrantedBeforeCreatingJob)
  .then(testPermissionIsRecheckedBeforeEveryJob)
  .then(testGrantedPreflightClearsStalePendingScan)
  .then(testFullScanConfirmationAndNoWork)
  .then(testOwnBusyScanReopensExtension)
  .then(testServerStatusSettlesMissedCompletionEvent)
  .then(testPermissionLossKeepsRunningJobAttached)
  .then(() => console.log("animego scan UI tests passed"));

import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = fs.readFileSync(new URL("content-script.js", import.meta.url), "utf8");
const origin = "https://anime-srez.up.railway.app";

function bridge(globals) {
  const listeners = new Map();
  const events = [];
  class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }
  const document = {
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    dispatchEvent(event) {
      events.push(event);
      listeners.get(event.type)?.(event);
    },
  };
  vm.runInNewContext(source, {
    ...globals, document, CustomEvent, location: { origin },
    window: { addEventListener() {} },
  });
  return {
    events,
    async emit(type, detail) {
      document.dispatchEvent(new CustomEvent(type, { detail }));
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

function runtime(sendMessage = async () => ({ ok: true, granted: true })) {
  return {
    getManifest: () => ({ version: "0.4.1" }),
    sendMessage,
    onMessage: { addListener() {} },
  };
}

test("Chrome runtime is used when a browser global has no extension API", async () => {
  const page = bridge({ browser: {}, chrome: { runtime: runtime() } });
  await page.emit("animego-scanner-prepare");
  assert.equal(page.events.find(e => e.type === "animego-scanner-ready").detail.version, "0.4.1");
  assert.equal(page.events.find(e => e.type === "animego-scanner-permission-state").detail.granted, true);
});

for (const failure of ["missing runtime", "invalidated context", "sync failure", "async failure"]) {
  test(`all page commands report ${failure} without throwing or timing out`, async () => {
    const chrome = { runtime: runtime() };
    const page = bridge({ chrome });
    page.events.length = 0;
    if (failure === "missing runtime") delete chrome.runtime;
    if (failure === "invalidated context") {
      chrome.runtime.getManifest = () => { throw new Error("Extension context invalidated."); };
    }
    if (failure === "sync failure") {
      chrome.runtime.sendMessage = () => { throw new Error("Extension context invalidated."); };
    }
    if (failure === "async failure") {
      chrome.runtime.sendMessage = async () => { throw new Error("Service worker unavailable"); };
    }
    await page.emit("animego-scanner-ping");
    if (["missing runtime", "invalidated context"].includes(failure)) {
      assert.ok(!page.events.some(e => e.type === "animego-scanner-ready"));
    }
    for (const command of ["animego-scanner-prepare", "animego-scanner-open", "animego-scan-start"]) {
      page.events.length = 0;
      await page.emit(command, { origin });
      const response = page.events.find(e => e.type === (
        command === "animego-scanner-prepare" ? "animego-scanner-permission-state" : "animego-scan-error"
      ));
      assert.ok(response, command);
      assert.match(response.detail.error, failure === "async failure" ? /Service worker unavailable/ : /Обновите страницу/);
    }
  });
}

test("missing extension API at injection does not announce readiness", async () => {
  const page = bridge({});
  await page.emit("animego-scanner-ping");
  assert.ok(!page.events.some(e => e.type === "animego-scanner-ready"));
  await page.emit("animego-scanner-prepare");
  assert.match(page.events.find(e => e.type === "animego-scanner-permission-state").detail.error, /Обновите страницу/);
});

for (const mode of ["partial", "full"]) {
  test(`extension creates ${mode} job with the catalog session`, async () => {
    let listener;
    let request;
    const api = runtime();
    api.onMessage.addListener = fn => { listener = fn; };
    bridge({ chrome: { runtime: api }, fetch: async (url, options) => {
      request = { url, options };
      return { ok: true, json: async () => ({
        job: { id: 42 }, token: "fixture", tasks: [{ anime_id: 1 }], discovery: mode === "full",
      }) };
    } });
    const result = await new Promise(resolve => {
      assert.equal(listener({ type: "animego-scanner-create-job", mode }, {}, resolve), true);
    });
    assert.equal(request.url, "/api/animego-scans");
    assert.equal(request.options.credentials, "same-origin");
    assert.equal(JSON.parse(request.options.body).mode, mode);
    assert.equal(result.detail.job_id, 42);
    assert.equal(result.detail.origin, origin);
    assert.equal(result.detail.mode, mode);
  });
}

test("catalog authentication failure is reported to the scanner", async () => {
  let listener;
  const api = runtime();
  api.onMessage.addListener = fn => { listener = fn; };
  bridge({ chrome: { runtime: api }, fetch: async () => ({
    ok: false, status: 401, json: async () => ({ error: "Войдите в аккаунт" }),
  }) });
  const result = await new Promise(resolve => listener({ type: "animego-scanner-create-job", mode: "partial" }, {}, resolve));
  assert.equal(result.ok, false);
  assert.match(result.error, /Войдите/);
});

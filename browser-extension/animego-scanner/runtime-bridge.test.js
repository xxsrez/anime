import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const APP_URL = "https://anime-srez.up.railway.app/";
const SCANNER_URL = "safari-web-extension://scanner/scanner.html";

function storageMock() {
  const values = {};
  return {
    values,
    area: {
      async get(keys) {
        const names = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(names.filter(name => name in values).map(name => [name, values[name]]));
      },
      async set(next) {
        Object.assign(values, next);
      },
      async remove(key) {
        delete values[key];
      },
    },
  };
}

test("Safari background bridge prepares optional access without windows API", async () => {
  const source = fs.readFileSync(new URL("background.js", import.meta.url), "utf8");
  const storage = storageMock();
  const tabMessages = [];
  let runtimeListener;
  const browser = {
    action: { onClicked: { addListener() {} } },
    permissions: { async contains() { return false; } },
    runtime: {
      getURL(path) {
        assert.equal(path, "scanner.html");
        return SCANNER_URL;
      },
      onMessage: { addListener(listener) { runtimeListener = listener; } },
    },
    storage: { local: storage.area },
    tabs: {
      async query() {
        return [{ id: 99, windowId: 5, url: SCANNER_URL }];
      },
      async update() {},
      async sendMessage(tabId, message) {
        tabMessages.push({ tabId, message });
      },
      async reload() {},
      async create() {},
    },
  };

  vm.runInNewContext(source, { browser, URL }, { filename: "background.js" });
  assert.equal(typeof runtimeListener, "function");

  const prepared = await new Promise(resolve => {
    assert.equal(
      runtimeListener(
        { type: "animego-scanner-prepare" },
        { tab: { id: 7, url: APP_URL }, url: APP_URL },
        resolve,
      ),
      true,
    );
  });
  assert.equal(prepared.ok, true);
  assert.equal(prepared.granted, false);
  assert.equal(storage.values.animegoScannerPermissionSource.sourceTabId, 7);

  const granted = await new Promise(resolve => {
    assert.equal(
      runtimeListener(
        { type: "animego-scanner-permission-granted" },
        { url: SCANNER_URL },
        resolve,
      ),
      true,
    );
  });
  assert.equal(granted.ok, true);
  assert.ok(
    tabMessages.some(
      ({ tabId, message }) => tabId === 7 && message.type === "animego-scanner-permission-granted",
    ),
  );
});

test("Safari content bridge reports permission state to the catalog page", async () => {
  const source = fs.readFileSync(new URL("content-script.js", import.meta.url), "utf8");
  const listeners = new Map();
  const events = [];
  let runtimeListener;
  class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }
  const document = {
    addEventListener(type, listener) {
      const group = listeners.get(type) || [];
      group.push(listener);
      listeners.set(type, group);
    },
    dispatchEvent(event) {
      events.push(event);
      for (const listener of listeners.get(event.type) || []) listener(event);
    },
  };
  const browser = {
    runtime: {
      getManifest() {
        return { version: "0.2.0" };
      },
      async sendMessage(message) {
        assert.equal(message.type, "animego-scanner-prepare");
        return { ok: true, granted: false };
      },
      onMessage: { addListener(listener) { runtimeListener = listener; } },
    },
  };
  const window = { addEventListener() {} };

  vm.runInNewContext(
    source,
    {
      browser,
      CustomEvent,
      document,
      location: { origin: "https://anime-srez.up.railway.app" },
      window,
    },
    { filename: "content-script.js" },
  );
  document.dispatchEvent(new CustomEvent("animego-scanner-prepare"));
  await Promise.resolve();
  await Promise.resolve();

  const state = events.find(event => event.type === "animego-scanner-permission-state");
  assert.equal(state?.detail?.granted, false);
  assert.equal(typeof runtimeListener, "function");
  assert.ok(events.some(event => event.type === "animego-scanner-ready"));
});

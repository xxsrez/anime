import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const origin = "https://anime-srez.up.railway.app";
const scannerUrl = "chrome-extension://fixture/scanner.html";

function background({ status = "completed", tabs = [{ id: 7, url: origin }], reply } = {}) {
  const storage = { animegoScannerSession: {
    payload: { origin, job_id: "1" }, sourceTabId: 7, checkpoint: { status },
  } };
  const sent = [];
  const context = vm.createContext({ URL, chrome: {
    runtime: { getURL: () => scannerUrl, onMessage: { addListener() {} } },
    action: { onClicked: { addListener() {} } },
    storage: { local: {
      get: async () => storage,
      set: async value => Object.assign(storage, value),
    } },
    tabs: {
      query: async query => query.url ? [{ id: 8 }] : tabs,
      update: async () => {},
      sendMessage: async (id, message) => {
        sent.push({ id, message });
        return reply || { ok: true, detail: {
          origin, job_id: "2", token: "fixture-token-long-enough",
          tasks: [], discovery: true, mode: message.mode,
        } };
      },
    },
  } });
  vm.runInContext(fs.readFileSync(new URL("background.js", import.meta.url), "utf8"), context);
  return { create: vm.runInContext("createScan", context), save: vm.runInContext("saveScanCheckpoint", context), reopen: vm.runInContext("reopenFromApp", context), storage, sent };
}

test("reopening the scanner updates its theme without losing the job", async () => {
  const h = background({ status: "paused" });
  await h.reopen({ tab: { id: 9, url: origin } }, "light");
  assert.equal(h.storage.animegoScannerTheme, "light");
  assert.equal(h.storage.animegoScannerSession.sourceTabId, 9);
  assert.equal(h.storage.animegoScannerSession.checkpoint.status, "paused");
  await h.reopen({ tab: { id: 9, url: origin } }, "unexpected");
  assert.equal(h.storage.animegoScannerTheme, null);
});

for (const mode of ["partial", "full"]) {
  test(`scanner starts a fresh ${mode} job in the existing tab`, async () => {
    const h = background();
    assert.equal((await h.create({ mode }, { url: scannerUrl })).ok, true);
    assert.equal(h.storage.animegoScannerSession.payload.job_id, "2");
    assert.equal(h.storage.animegoScannerSession.payload.mode, mode);
    assert.equal(h.storage.animegoScannerSession.checkpoint, null);
    assert.equal(h.sent[0].id, 7);
    assert.equal(h.sent[1].message.type, "animego-scanner-reload");
  });
}

test("rejects foreign senders, active jobs and missing catalog without overwriting progress", async () => {
  const h = background({ status: "running" });
  await assert.rejects(h.create({ mode: "full" }, { url: origin }), /источник/);
  await assert.rejects(h.create({ mode: "full" }, { url: scannerUrl }), /остановите/);
  assert.equal(h.sent.length, 0);
  const closed = background({ tabs: [] });
  await assert.rejects(closed.create({ mode: "partial" }, { url: scannerUrl }), /Откройте/);
  assert.equal(closed.storage.animegoScannerSession.payload.job_id, "1");
});

test("an expired job can be replaced without trying to stop it on the server", async () => {
  const h = background({ status: "expired" });
  assert.equal((await h.create({ mode: "partial" }, { url: scannerUrl })).ok, true);
  assert.equal(h.storage.animegoScannerSession.payload.job_id, "2");
  assert.equal(h.sent[0].message.type, "animego-scanner-create-job");
});

test("late checkpoint from the old scanner cannot overwrite a newly created job", async () => {
  const h = background();
  const old = structuredClone(h.storage.animegoScannerSession);
  await h.create({ mode: "partial" }, { url: scannerUrl });
  const reply = await h.save({ payload: old.payload, checkpoint: { status: "error" } }, { url: scannerUrl });
  assert.equal(reply.stale, true);
  assert.equal(h.storage.animegoScannerSession.payload.job_id, "2");
  assert.equal(h.storage.animegoScannerSession.checkpoint, null);
});

test("checkpoint writes and new job creation are serialized", async () => {
  const h = background();
  const old = structuredClone(h.storage.animegoScannerSession);
  await Promise.all([
    h.save({ payload: old.payload, checkpoint: { status: "completed" } }, { url: scannerUrl }),
    h.create({ mode: "partial" }, { url: scannerUrl }),
  ]);
  assert.equal(h.storage.animegoScannerSession.payload.job_id, "2");
  await assert.rejects(h.save({ payload: old.payload }, { url: origin }), /источник/);
});

test("empty work and server conflicts preserve the previous result", async () => {
  const empty = background({ reply: { ok: true, empty: true } });
  assert.equal((await empty.create({ mode: "partial" }, { url: scannerUrl })).empty, true);
  assert.equal(empty.storage.animegoScannerSession.payload.job_id, "1");
  const conflict = background({ reply: { ok: false, error: "Скан уже идёт" } });
  await assert.rejects(conflict.create({ mode: "full" }, { url: scannerUrl }), /уже идёт/);
  assert.equal(conflict.storage.animegoScannerSession.payload.job_id, "1");
});

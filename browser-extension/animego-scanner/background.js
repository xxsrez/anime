const STORAGE_KEY = "animegoScannerSession";
const PERMISSION_SOURCE_KEY = "animegoScannerPermissionSource";
const ANIMEGO_HOST_PERMISSION = "https://animego.me/*";
const extensionApi = globalThis.browser ?? globalThis.chrome;
const APP_ORIGINS = new Set([
  "http://127.0.0.1:8765",
  "https://anime-srez.up.railway.app",
]);
const FORWARDED_EVENTS = new Set([
  "animego-scan-progress",
  "animego-scan-complete",
  "animego-scan-error",
]);

function senderOrigin(sender) {
  try {
    return new URL(sender?.tab?.url || sender?.url || "").origin;
  } catch (_error) {
    return null;
  }
}

function jobId(detail) {
  const candidate = detail?.job_id ?? detail?.job?.id ?? detail?.job;
  if (typeof candidate === "number" && Number.isSafeInteger(candidate) && candidate > 0) {
    return String(candidate);
  }
  if (typeof candidate === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(candidate)) {
    return candidate;
  }
  return null;
}

function validateStart(detail, origin) {
  if (!detail || typeof detail !== "object" || detail.origin !== origin) {
    throw new Error("Источник задания не совпадает со страницей Anime Catalog.");
  }
  if (!jobId(detail)) {
    throw new Error("Задание сканирования не содержит корректный id.");
  }
  if (typeof detail.token !== "string" || detail.token.length < 16 || detail.token.length > 8192) {
    throw new Error("Задание сканирования не содержит корректный token.");
  }
  if (!Array.isArray(detail.tasks) || detail.tasks.length > 2000) {
    throw new Error("Некорректный список тайтлов для сканирования.");
  }
  for (const task of detail.tasks) {
    const animeId = task?.anime_id;
    if (!Number.isSafeInteger(animeId) || animeId <= 0 || animeId >= 10_000_000) {
      throw new Error("В задании найден некорректный AnimeGo id.");
    }
    if (!Array.isArray(task.known_episode_ids) || task.known_episode_ids.length > 10000) {
      throw new Error(`Некорректный список известных серий для AnimeGo ${animeId}.`);
    }
  }
}

async function openScanner() {
  const scannerUrl = extensionApi.runtime.getURL("scanner.html");
  const tabs = await extensionApi.tabs.query({ url: scannerUrl });
  if (tabs.length > 0 && tabs[0].id != null) {
    await extensionApi.tabs.update(tabs[0].id, { active: true });
    if (tabs[0].windowId != null && extensionApi.windows?.update) {
      try {
        await extensionApi.windows.update(tabs[0].windowId, { focused: true });
      } catch (_error) {
        // Window focus is best-effort and is unavailable in Safari on iOS.
      }
    }
    try {
      await extensionApi.tabs.sendMessage(tabs[0].id, { type: "animego-scanner-reload" });
    } catch (_error) {
      await extensionApi.tabs.reload(tabs[0].id);
    }
    return;
  }
  await extensionApi.tabs.create({ url: scannerUrl, active: true });
}

async function startScan(message, sender) {
  const origin = senderOrigin(sender);
  if (!origin || !APP_ORIGINS.has(origin)) {
    throw new Error("Эта страница не может запускать сканер AnimeGo.");
  }
  validateStart(message.detail, origin);
  const payload = {
    ...message.detail,
    job_id: jobId(message.detail),
    origin,
  };
  await extensionApi.storage.local.set({
    [STORAGE_KEY]: {
      payload,
      sourceTabId: sender.tab?.id ?? null,
      checkpoint: null,
      savedAt: new Date().toISOString(),
    },
  });
  await openScanner();
  return { ok: true };
}

async function forwardToApp(message) {
  const stored = await extensionApi.storage.local.get(STORAGE_KEY);
  const tabId = stored[STORAGE_KEY]?.sourceTabId;
  if (tabId == null) {
    return;
  }
  try {
    await extensionApi.tabs.sendMessage(tabId, {
      type: message.type,
      detail: message.detail || {},
    });
  } catch (_error) {
    // The app tab may have been closed. The visible scanner tab remains authoritative.
  }
}

async function reopenFromApp(sender) {
  const stored = await extensionApi.storage.local.get(STORAGE_KEY);
  const current = stored[STORAGE_KEY];
  if (current && sender.tab?.id != null) {
    await extensionApi.storage.local.set({
      [STORAGE_KEY]: {
        ...current,
        sourceTabId: sender.tab.id,
        savedAt: new Date().toISOString(),
      },
    });
  }
  await openScanner();
}

async function prepareAnimeGoAccess(sender) {
  const origin = senderOrigin(sender);
  if (!origin || !APP_ORIGINS.has(origin)) {
    throw new Error("Эта страница не может запрашивать доступ к AnimeGo.");
  }
  const granted = Boolean(
    await extensionApi.permissions?.contains?.({ origins: [ANIMEGO_HOST_PERMISSION] }),
  );
  if (granted) {
    return { ok: true, granted: true };
  }
  if (sender.tab?.id != null) {
    await extensionApi.storage.local.set({
      [PERMISSION_SOURCE_KEY]: {
        sourceTabId: sender.tab.id,
        origin,
        savedAt: new Date().toISOString(),
      },
    });
  }
  await openScanner();
  return { ok: true, granted: false };
}

function isScannerPage(sender) {
  const scannerUrl = extensionApi.runtime.getURL("scanner.html");
  return sender?.url === scannerUrl;
}

async function forwardPermissionGranted(sender) {
  if (!isScannerPage(sender)) {
    throw new Error("Некорректный источник подтверждения доступа.");
  }
  const stored = await extensionApi.storage.local.get([PERMISSION_SOURCE_KEY, STORAGE_KEY]);
  const tabId =
    stored[PERMISSION_SOURCE_KEY]?.sourceTabId ?? stored[STORAGE_KEY]?.sourceTabId ?? null;
  if (tabId != null) {
    try {
      await extensionApi.tabs.sendMessage(tabId, {
        type: "animego-scanner-permission-granted",
        detail: { granted: true },
      });
    } catch (_error) {
      // The source tab may have been closed; the scanner can still resume locally.
    }
  }
  await extensionApi.storage.local.remove(PERMISSION_SOURCE_KEY);
  return { ok: true };
}

extensionApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "animego-scanner-prepare") {
    prepareAnimeGoAccess(sender)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error?.message || String(error) }));
    return true;
  }
  if (message?.type === "animego-scanner-permission-granted") {
    forwardPermissionGranted(sender)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error?.message || String(error) }));
    return true;
  }
  if (message?.type === "animego-scan-start") {
    startScan(message, sender)
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error?.message || String(error) }));
    return true;
  }
  if (message?.type === "animego-scanner-open") {
    const origin = senderOrigin(sender);
    if (!origin || !APP_ORIGINS.has(origin)) {
      sendResponse({ ok: false, error: "Эта страница не может открывать сканер AnimeGo." });
      return false;
    }
    reopenFromApp(sender)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error?.message || String(error) }));
    return true;
  }
  if (FORWARDED_EVENTS.has(message?.type)) {
    forwardToApp(message).catch(() => {});
  }
  return false;
});

extensionApi.action.onClicked.addListener(() => {
  openScanner().catch(() => {});
});

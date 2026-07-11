const STORAGE_KEY = "animegoScannerSession";
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
  const scannerUrl = chrome.runtime.getURL("scanner.html");
  const tabs = await chrome.tabs.query({ url: scannerUrl });
  if (tabs.length > 0 && tabs[0].id != null) {
    await chrome.tabs.update(tabs[0].id, { active: true });
    if (tabs[0].windowId != null) {
      await chrome.windows.update(tabs[0].windowId, { focused: true });
    }
    try {
      await chrome.tabs.sendMessage(tabs[0].id, { type: "animego-scanner-reload" });
    } catch (_error) {
      await chrome.tabs.reload(tabs[0].id);
    }
    return;
  }
  await chrome.tabs.create({ url: scannerUrl, active: true });
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
  await chrome.storage.local.set({
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
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const tabId = stored[STORAGE_KEY]?.sourceTabId;
  if (tabId == null) {
    return;
  }
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: message.type,
      detail: message.detail || {},
    });
  } catch (_error) {
    // The app tab may have been closed. The visible scanner tab remains authoritative.
  }
}

async function reopenFromApp(sender) {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const current = stored[STORAGE_KEY];
  if (current && sender.tab?.id != null) {
    await chrome.storage.local.set({
      [STORAGE_KEY]: {
        ...current,
        sourceTabId: sender.tab.id,
        savedAt: new Date().toISOString(),
      },
    });
  }
  await openScanner();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
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

chrome.action.onClicked.addListener(() => {
  openScanner().catch(() => {});
});

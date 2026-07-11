import {
  looksLikeChallenge,
  parsePlayerContent,
  parseUnavailableReason,
  shouldUseInitialProviders,
  syntheticEpisode,
  unknownEpisodes,
} from "./parser.js";
import { shouldRestartAfterReload } from "./scan-state.js";

const STORAGE_KEY = "animegoScannerSession";
const APP_ORIGINS = new Set([
  "http://127.0.0.1:8765",
  "https://anime-srez.up.railway.app",
]);
const UPSTREAM_BASE = "https://animego.me";
const MAX_LOG_ENTRIES = 200;

class BlockedError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = "BlockedError";
    this.status = status;
  }
}

class ApiError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

class UpstreamError extends Error {
  constructor(message, { retryable = false, status = null } = {}) {
    super(message);
    this.name = "UpstreamError";
    this.retryable = retryable;
    this.status = status;
  }
}

const elements = Object.fromEntries(
  [
    "subtitle",
    "mode",
    "current-title",
    "progress-text",
    "progress-track",
    "progress-bar",
    "checked",
    "episodes",
    "providers",
    "errors",
    "pause",
    "stop",
    "status",
    "job-label",
    "log",
  ].map((id) => [id, document.getElementById(id)]),
);

let session = null;
let checkpoint = null;
let activeRun = false;
let currentRequest = null;
let stopFinalization = null;
let generation = 0;
let lastUpstreamRequestAt = 0;

function nowTime() {
  return new Intl.DateTimeFormat("ru", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

function log(message, kind = "") {
  const item = document.createElement("li");
  const time = document.createElement("time");
  const text = document.createElement("span");
  time.textContent = nowTime();
  text.textContent = String(message);
  if (kind) {
    text.className = kind;
  }
  item.append(time, text);
  elements.log.prepend(item);
  while (elements.log.children.length > MAX_LOG_ENTRIES) {
    elements.log.lastElementChild?.remove();
  }
}

function modeLabel(mode) {
  return mode === "full" ? "Полный скан" : "Быстрый скан";
}

function taskTitle(task) {
  return task?.title || task?.anime_title || `AnimeGo #${task?.anime_id ?? "?"}`;
}

function defaultCheckpoint(payload) {
  return {
    job_id: String(payload.job_id),
    status: "running",
    next_index: 0,
    checked_items: 0,
    total_items: payload.tasks.length,
    new_episode_count: 0,
    new_provider_count: 0,
    error_count: 0,
    errors: [],
    current: null,
  };
}

function normalizeCheckpoint(payload, stored) {
  if (!stored || String(stored.job_id) !== String(payload.job_id)) {
    return defaultCheckpoint(payload);
  }
  const total = payload.tasks.length;
  const status = stored.status === "completing" ? "running" : stored.status;
  return {
    ...defaultCheckpoint(payload),
    ...stored,
    status,
    job_id: String(payload.job_id),
    total_items: total,
    next_index: Math.max(0, Math.min(total, Number(stored.next_index) || 0)),
    checked_items: Math.max(0, Number(stored.checked_items) || 0),
    new_episode_count: Math.max(0, Number(stored.new_episode_count) || 0),
    new_provider_count: Math.max(0, Number(stored.new_provider_count) || 0),
    error_count: Math.max(0, Number(stored.error_count) || 0),
    errors: Array.isArray(stored.errors) ? stored.errors.slice(-100) : [],
  };
}

function statusText(status) {
  return (
    {
      running: "Сканирование идёт",
      paused: "Сканирование на паузе",
      blocked: "AnimeGo запросил проверку — сканирование остановлено",
      error: "Сканирование прервано ошибкой",
      stopping: "Останавливаем…",
      stopped: "Сканирование остановлено",
      completing: "Сохраняем итог…",
      completed: "Готово",
    }[status] || "Ожидание"
  );
}

function render() {
  if (!session || !checkpoint) {
    return;
  }
  const { payload } = session;
  const total = payload.tasks.length;
  const checked = Math.min(total, checkpoint.checked_items);
  const percent =
    checkpoint.status === "completed"
      ? 100
      : total === 0
        ? 0
        : (checked / total) * 100;

  elements.subtitle.textContent =
    checkpoint.status === "completed"
      ? "Каталог обновлён. Результаты уже доступны всем пользователям."
      : "Расширение проверяет AnimeGo через ваш браузер. Можно оставить эту вкладку в фоне.";
  elements.mode.textContent = modeLabel(payload.mode);
  elements["current-title"].textContent = checkpoint.current?.title || statusText(checkpoint.status);
  elements["progress-text"].textContent = `${checked} / ${total}`;
  elements["progress-track"].setAttribute("aria-valuemax", String(total));
  elements["progress-track"].setAttribute("aria-valuenow", String(checked));
  elements["progress-bar"].style.width = `${Math.max(0, Math.min(100, percent))}%`;
  elements.checked.textContent = String(checked);
  elements.episodes.textContent = String(checkpoint.new_episode_count);
  elements.providers.textContent = String(checkpoint.new_provider_count);
  elements.errors.textContent = String(checkpoint.error_count);
  elements["job-label"].textContent = `job ${payload.job_id}`;

  const controllable = ["running", "paused", "blocked", "error"].includes(checkpoint.status);
  elements.pause.disabled = !controllable;
  elements.pause.textContent = checkpoint.status === "running" ? "Пауза" : "Продолжить";
  elements.stop.disabled = !controllable;
  elements.status.textContent = statusText(checkpoint.status);
  elements.status.className = `status ${
    ["blocked", "error"].includes(checkpoint.status)
      ? "error"
      : checkpoint.status === "completed"
        ? "success"
        : ""
  }`;
}

function eventDetail(message = null) {
  return {
    job_id: session?.payload?.job_id ?? null,
    status: checkpoint?.status ?? "error",
    checked_items: checkpoint?.checked_items ?? 0,
    total_items: checkpoint?.total_items ?? 0,
    new_episode_count: checkpoint?.new_episode_count ?? 0,
    new_provider_count: checkpoint?.new_provider_count ?? 0,
    error_count: checkpoint?.error_count ?? 0,
    current: checkpoint?.current ?? null,
    checked: checkpoint?.checked_items ?? 0,
    total: checkpoint?.total_items ?? 0,
    added: checkpoint?.new_episode_count ?? 0,
    message,
  };
}

function notifyApp(type, extra = {}) {
  chrome.runtime
    .sendMessage({ type, detail: { ...eventDetail(), ...extra } })
    .catch(() => {});
}

async function saveCheckpoint() {
  if (!session || !checkpoint) {
    return;
  }
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const current = stored[STORAGE_KEY];
  if (!current || String(current.payload?.job_id) !== String(session.payload.job_id)) {
    return;
  }
  session = {
    ...current,
    checkpoint: { ...checkpoint },
    savedAt: new Date().toISOString(),
  };
  await chrome.storage.local.set({ [STORAGE_KEY]: session });
}

function validPayload(payload) {
  return Boolean(
    payload &&
      APP_ORIGINS.has(payload.origin) &&
      payload.job_id != null &&
      typeof payload.token === "string" &&
      Array.isArray(payload.tasks),
  );
}

async function loadSession({ announce = true } = {}) {
  generation += 1;
  currentRequest?.abort();
  currentRequest = null;
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  const nextSession = stored[STORAGE_KEY];
  if (!validPayload(nextSession?.payload)) {
    elements.status.textContent = "Откройте Anime Catalog и запустите скан оттуда.";
    elements.status.className = "status error";
    elements["current-title"].textContent = "Нет активного задания";
    log("Активное задание не найдено. Вернитесь в Anime Catalog.", "error");
    return;
  }
  session = nextSession;
  checkpoint = normalizeCheckpoint(session.payload, session.checkpoint);
  stopFinalization = null;
  render();
  if (announce) {
    log(`Загружено задание: ${session.payload.tasks.length} тайтлов.`);
  }
  if (checkpoint.status === "running") {
    runScan();
  }
}

async function interruptibleDelay(milliseconds, runGeneration) {
  const end = Date.now() + milliseconds;
  do {
    if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
      throw new DOMException("Остановлено", "AbortError");
    }
    while (checkpoint.status === "paused") {
      await new Promise((resolve) => setTimeout(resolve, 200));
      if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
        throw new DOMException("Остановлено", "AbortError");
      }
    }
    if (Date.now() < end) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(200, end - Date.now())));
    }
  } while (Date.now() < end);
}

async function respectUpstreamDelay(runGeneration) {
  const randomizedDelay = 550 + Math.floor(Math.random() * 450);
  const remaining = lastUpstreamRequestAt + randomizedDelay - Date.now();
  if (remaining > 0) {
    await interruptibleDelay(remaining, runGeneration);
  }
  lastUpstreamRequestAt = Date.now();
}

async function upstreamJson(path, runGeneration) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (attempt > 0) {
      const backoff = 1200 * 2 ** (attempt - 1) + Math.floor(Math.random() * 700);
      log(`Повтор через ${(backoff / 1000).toFixed(1)} с…`);
      await interruptibleDelay(backoff, runGeneration);
    }
    await respectUpstreamDelay(runGeneration);
    const controller = new AbortController();
    currentRequest = controller;
    try {
      const response = await fetch(new URL(path, UPSTREAM_BASE), {
        method: "GET",
        headers: {
          Accept: "application/json, text/plain, */*",
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "include",
        cache: "no-store",
        signal: controller.signal,
      });
      const body = await response.text();
      if (response.status === 403 || response.status === 429 || looksLikeChallenge(body)) {
        throw new BlockedError(
          response.status === 429
            ? "AnimeGo ограничил частоту запросов (429). Попробуйте позже."
            : "AnimeGo запросил CAPTCHA или проверку браузера.",
          response.status,
        );
      }
      if (!response.ok) {
        throw new UpstreamError(`AnimeGo ответил HTTP ${response.status}.`, {
          retryable: response.status === 408 || response.status >= 500,
          status: response.status,
        });
      }
      let parsed;
      try {
        parsed = JSON.parse(body);
      } catch (_error) {
        throw new UpstreamError("AnimeGo вернул ответ в неожиданном формате.");
      }
      if (!parsed || typeof parsed !== "object") {
        throw new UpstreamError("AnimeGo вернул пустой ответ.");
      }
      return parsed;
    } catch (error) {
      if (error?.name === "AbortError" || error instanceof BlockedError) {
        throw error;
      }
      lastError =
        error instanceof UpstreamError
          ? error
          : new UpstreamError(error?.message || "Сетевая ошибка AnimeGo.", { retryable: true });
      if (!lastError.retryable) {
        throw lastError;
      }
    } finally {
      if (currentRequest === controller) {
        currentRequest = null;
      }
    }
  }
  throw lastError || new UpstreamError("AnimeGo временно недоступен.");
}

function responseContent(response, label) {
  const content = response?.data?.content;
  if (typeof content !== "string") {
    throw new UpstreamError(`${label}: в ответе нет HTML плеера.`);
  }
  return content;
}

async function collectTitle(task, runGeneration) {
  const animeId = task.anime_id;
  const initialResponse = await upstreamJson(`/player/${animeId}`, runGeneration);
  const initialContent = responseContent(initialResponse, `AnimeGo #${animeId}`);
  const initial = parsePlayerContent(initialContent);
  let episodes = initial.episodes;
  if (episodes.length === 0 && initial.providers.length > 0) {
    episodes = [syntheticEpisode(animeId, taskTitle(task))];
  }
  const additions = unknownEpisodes(episodes, task.known_episode_ids);
  const collected = [];

  for (const episode of additions) {
    let providers;
    let unavailableReason = null;
    if (
      initial.providers.length > 0 &&
      shouldUseInitialProviders(episode, initial.selectedEpisodeId, episodes.length, animeId)
    ) {
      providers = initial.providers;
    } else {
      const videoResponse = await upstreamJson(`/player/videos/${episode.id}`, runGeneration);
      const videoContent = responseContent(videoResponse, `Серия #${episode.id}`);
      const video = parsePlayerContent(videoContent);
      providers = video.providers;
      unavailableReason = parseUnavailableReason(videoResponse?.data?.content_online || "");
    }
    if (providers.length === 0) {
      log(
        `Серия ${episode.number || episode.id} найдена, но playable-плеер пока недоступен${
          unavailableReason ? `: ${unavailableReason}` : "."
        }`,
      );
      continue;
    }
    collected.push({ episode, providers, unavailable_reason: null });
  }
  return collected;
}

async function apiPost(path, body) {
  const controller = new AbortController();
  currentRequest = controller;
  try {
    const response = await fetch(new URL(path, session.payload.origin), {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.payload.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
      signal: controller.signal,
    });
    const text = await response.text();
    let parsed = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch (_error) {
        throw new ApiError("Anime Catalog вернул ответ в неожиданном формате.", response.status);
      }
    }
    if (!response.ok) {
      throw new ApiError(
        parsed?.error || parsed?.message || `Anime Catalog ответил HTTP ${response.status}.`,
        response.status,
      );
    }
    return parsed;
  } finally {
    if (currentRequest === controller) {
      currentRequest = null;
    }
  }
}

function updateCountersFromResponse(response, localEpisodes) {
  const job = response?.job;
  const localProviderCount = localEpisodes.reduce((sum, item) => sum + item.providers.length, 0);
  if (job && typeof job === "object") {
    checkpoint.checked_items = Math.max(checkpoint.checked_items + 1, Number(job.checked_items) || 0);
    checkpoint.new_episode_count = Math.max(
      checkpoint.new_episode_count,
      Number(job.new_episode_count) || 0,
    );
    checkpoint.new_provider_count = Math.max(
      checkpoint.new_provider_count,
      Number(job.new_provider_count) || 0,
    );
    checkpoint.error_count = Math.max(checkpoint.error_count, Number(job.error_count) || 0);
    return;
  }
  checkpoint.checked_items += 1;
  checkpoint.new_episode_count += Number(response?.new_episode_count ?? localEpisodes.length) || 0;
  checkpoint.new_provider_count += Number(response?.new_provider_count ?? localProviderCount) || 0;
}

async function processTask(task, index, runGeneration) {
  checkpoint.current = {
    anime_id: task.anime_id,
    title: taskTitle(task),
    index: index + 1,
  };
  render();
  notifyApp("animego-scan-progress", { message: `Проверяем ${taskTitle(task)}` });
  log(`[${index + 1}/${session.payload.tasks.length}] ${taskTitle(task)}`);

  const episodes = await collectTitle(task, runGeneration);
  const response = await apiPost(`/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/results`, {
    anime_id: task.anime_id,
    episodes,
    selection_reason: task.selection_reason || null,
  });
  updateCountersFromResponse(response, episodes);
  checkpoint.next_index = index + 1;
  checkpoint.current = null;
  if (episodes.length > 0) {
    log(`Добавлено новых серий: ${episodes.length}.`, "success");
  } else {
    log("Новых playable-серий нет.");
  }
  await saveCheckpoint();
  render();
  notifyApp("animego-scan-progress", {
    message: episodes.length > 0 ? `Добавлено серий: ${episodes.length}` : "Без изменений",
  });
}

async function recordOrdinaryError(task, index, error) {
  const message = error?.message || String(error);
  const response = await apiPost(
    `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/results`,
    {
      anime_id: task.anime_id,
      episodes: [],
      error: message.slice(0, 1000),
      selection_reason: task.selection_reason || null,
    },
  );
  updateCountersFromResponse(response, []);
  checkpoint.next_index = index + 1;
  checkpoint.current = null;
  checkpoint.errors.push({
    anime_id: task.anime_id,
    message: message.slice(0, 1000),
  });
  checkpoint.errors = checkpoint.errors.slice(-100);
  log(`${taskTitle(task)}: ${message}`, "error");
}

async function finishScan() {
  checkpoint.status = "completing";
  checkpoint.current = null;
  render();
  await saveCheckpoint();
  const response = await apiPost(
    `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/complete`,
    { errors: checkpoint.errors },
  );
  const job = response?.job;
  if (job) {
    checkpoint.checked_items = Number(job.checked_items) || checkpoint.checked_items;
    checkpoint.new_episode_count = Number(job.new_episode_count) || checkpoint.new_episode_count;
    checkpoint.new_provider_count = Number(job.new_provider_count) || checkpoint.new_provider_count;
    checkpoint.error_count = Number(job.error_count) || checkpoint.error_count;
  }
  checkpoint.status = "completed";
  checkpoint.current = null;
  await saveCheckpoint();
  render();
  log(
    `Готово: новых серий ${checkpoint.new_episode_count}, ошибок ${checkpoint.error_count}.`,
    "success",
  );
  notifyApp("animego-scan-complete", { message: "Сканирование завершено" });
}

async function runScan() {
  if (activeRun || !session || !checkpoint || checkpoint.status !== "running") {
    return;
  }
  activeRun = true;
  const runGeneration = generation;
  try {
    for (let index = checkpoint.next_index; index < session.payload.tasks.length; index += 1) {
      await interruptibleDelay(0, runGeneration);
      if (checkpoint.status !== "running") {
        return;
      }
      const task = session.payload.tasks[index];
      try {
        await processTask(task, index, runGeneration);
      } catch (error) {
        if (error?.name === "AbortError") {
          if (["stopping", "stopped"].includes(checkpoint.status) || runGeneration !== generation) {
            return;
          }
          throw error;
        }
        if (error instanceof BlockedError) {
          checkpoint.status = "blocked";
          checkpoint.current = null;
          await saveCheckpoint();
          render();
          log(error.message, "error");
          notifyApp("animego-scan-error", { error: error.message, blocked: true });
          return;
        }
        if (error instanceof ApiError) {
          checkpoint.status = "error";
          checkpoint.current = null;
          await saveCheckpoint();
          render();
          log(`Anime Catalog: ${error.message}`, "error");
          notifyApp("animego-scan-error", { error: error.message, blocked: false });
          return;
        }
        try {
          await recordOrdinaryError(task, index, error);
          await saveCheckpoint();
          render();
          notifyApp("animego-scan-progress", {
            message: `Ошибка: ${error?.message || String(error)}`,
          });
        } catch (submitError) {
          checkpoint.status = "error";
          checkpoint.current = null;
          await saveCheckpoint();
          render();
          log(`Не удалось сохранить ошибку title: ${submitError?.message || String(submitError)}`, "error");
          notifyApp("animego-scan-error", {
            error: submitError?.message || String(submitError),
            blocked: false,
          });
          return;
        }
      }
    }
    if (checkpoint.status === "running") {
      try {
        await finishScan();
      } catch (error) {
        if (error?.name === "AbortError" && ["stopping", "stopped"].includes(checkpoint.status)) {
          return;
        }
        checkpoint.status = "error";
        await saveCheckpoint();
        render();
        log(`Не удалось завершить job: ${error?.message || String(error)}`, "error");
        notifyApp("animego-scan-error", {
          error: error?.message || String(error),
          blocked: false,
        });
      }
    }
  } finally {
    activeRun = false;
    if (shouldRestartAfterReload(runGeneration, generation, checkpoint?.status)) {
      queueMicrotask(() => runScan());
    }
  }
}

async function pauseOrResume() {
  if (!checkpoint) {
    return;
  }
  if (checkpoint.status === "running") {
    checkpoint.status = "paused";
    await saveCheckpoint();
    render();
    log("Сканирование поставлено на паузу.");
    notifyApp("animego-scan-progress", { message: "Пауза" });
    return;
  }
  if (["paused", "blocked", "error"].includes(checkpoint.status)) {
    checkpoint.status = "running";
    await saveCheckpoint();
    render();
    log("Сканирование продолжено.");
    notifyApp("animego-scan-progress", { message: "Продолжаем сканирование" });
    runScan();
  }
}

async function stopScan() {
  if (!checkpoint || !["running", "paused", "blocked", "error"].includes(checkpoint.status)) {
    return;
  }
  if (stopFinalization) {
    return stopFinalization;
  }
  stopFinalization = (async () => {
    checkpoint.status = "stopping";
    checkpoint.current = null;
    currentRequest?.abort();
    render();
    await saveCheckpoint();
    try {
      const response = await apiPost(
        `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/complete`,
        { stopped: true, errors: checkpoint.errors },
      );
      checkpoint.status = "stopped";
      if (response?.job) {
        checkpoint.error_count = Number(response.job.error_count) || checkpoint.error_count;
      }
      log("Сканирование остановлено. Прогресс до последнего тайтла сохранён.");
      notifyApp("animego-scan-complete", { stopped: true, message: "Сканирование остановлено" });
    } catch (error) {
      checkpoint.status = "error";
      log(`Не удалось остановить job на сервере: ${error?.message || String(error)}`, "error");
      notifyApp("animego-scan-error", {
        error: error?.message || String(error),
        blocked: false,
      });
    }
    await saveCheckpoint();
    render();
  })();
  return stopFinalization;
}

elements.pause.addEventListener("click", () => {
  pauseOrResume().catch((error) => log(error?.message || String(error), "error"));
});
elements.stop.addEventListener("click", () => {
  stopScan().catch((error) => log(error?.message || String(error), "error"));
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "animego-scanner-reload") {
    loadSession().catch((error) => log(error?.message || String(error), "error"));
  }
});

loadSession().catch((error) => {
  elements.status.textContent = error?.message || String(error);
  elements.status.className = "status error";
  log(error?.message || String(error), "error");
});

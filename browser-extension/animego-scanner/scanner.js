import {
  looksLikeChallenge,
  parsePlayerContent,
  parseUnavailableReason,
  shouldUseInitialProviders,
  syntheticEpisode,
  unknownEpisodes,
} from "./parser.js";
import {
  hasAnimeGoHostPermission,
  requestAnimeGoHostPermission,
  resolveExtensionApi,
} from "./permissions.js";
import { shouldRestartAfterReload } from "./scan-state.js";
import { upstreamHttpError } from "./upstream-errors.js";
import { KINOGOO_ORIGINS, fetchKinogooHtml, kinogooPlayer, kinogooEpisodes } from "./kinogoo.js";

const extensionApi = resolveExtensionApi();
const STORAGE_KEY = "animegoScannerSession";
const THEME_KEY = "animegoScannerTheme";
const APP_ORIGINS = new Set([
  "http://127.0.0.1:8765",
  "https://anime-srez.up.railway.app",
]);
const UPSTREAM_BASE = "https://animego.me";
const MAX_LOG_ENTRIES = 200;
const YUMMY_ORIGIN = "https://api.yani.tv/*";
const API_TIMEOUT_MS = 45000;
const RECOVERY_TIMEOUT_MS = 15000;
const UPSTREAM_TIMEOUT_MS = 30000;

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

class PermissionError extends Error {
  constructor(message) {
    super(message);
    this.name = "PermissionError";
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
    "grant-access",
    "stop",
    "status",
    "job-label",
    "log",
    "start-partial",
    "start-full",
    "launch-status",
  ].map((id) => [id, document.getElementById(id)]),
);

let session = null;
let checkpoint = null;
let activeRun = false;
let currentRequest = null;
let stopFinalization = null;
let generation = 0;
let loadedGeneration = null;
let restartRequested = null;
let lastUpstreamRequestAt = 0;
let creatingScan = false;
let announcedJobId = null;
let announcedNoSession = false;
let themeRevision = 0;

function applyTheme(theme) {
  if (!document.documentElement) return;
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

extensionApi.storage.onChanged?.addListener((changes, area) => {
  if (area !== "local" || !Object.hasOwn(changes, THEME_KEY)) return;
  themeRevision += 1;
  applyTheme(changes[THEME_KEY].newValue);
});

function renderLaunch() {
  const busy = loadedGeneration !== generation || creatingScan || (checkpoint && !["completed", "stopped", "expired"].includes(checkpoint.status));
  elements["start-partial"].disabled = Boolean(busy);
  elements["start-full"].disabled = Boolean(busy);
}

async function startNewScan(mode) {
  if (loadedGeneration !== generation || creatingScan || (checkpoint && !["completed", "stopped", "expired"].includes(checkpoint.status))) return;
  const actionGeneration = generation;
  creatingScan = true;
  renderLaunch();
  elements["launch-status"].textContent = "Готовим задание…";
  try {
    // A terminal state may have been reached even though its last save failed.
    // The background reads storage before it permits creating another job.
    if (checkpoint) {
      await saveCheckpoint();
      if (actionGeneration !== generation) return;
    }
    const response = await extensionApi.runtime.sendMessage({ type: "animego-scanner-create", mode });
    if (!response?.ok) throw new Error(response?.error || "Не удалось запустить скан.");
    elements["launch-status"].textContent = response.empty ? response.message : "";
  } catch (error) {
    elements["launch-status"].textContent = error.message;
  } finally {
    creatingScan = false;
    renderLaunch();
  }
}

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

function reportControlError(error) {
  const message = error?.message || String(error);
  elements.status.textContent = message;
  elements.status.className = "status error";
  log(message, "error");
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
    pending_result: null,
    discovery_page: 1,
    discovery_done: !payload.discovery,
  };
}

function normalizeCheckpoint(payload, stored) {
  if (!stored || String(stored.job_id) !== String(payload.job_id)) {
    return defaultCheckpoint(payload);
  }
  const total = payload.tasks.length;
  const status = stored.status === "completing" ? "running"
    : stored.status === "stopping" ? "error" : stored.status;
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
      permission: "Нужен доступ к источнику",
      blocked: "AnimeGo запросил проверку — сканирование остановлено",
      error: "Сканирование прервано ошибкой",
      expired: "Срок задания истёк. Запустите новый скан; уже сохранённые результаты не потеряны.",
      stopping: "Останавливаем…",
      stopped: "Сканирование остановлено",
      completing: "Сохраняем итог…",
      completed: "Готово",
    }[status] || "Ожидание"
  );
}

function render() {
  renderLaunch();
  if (!session || !checkpoint) {
    return;
  }
  const { payload } = session;
  const total = payload.tasks.length;
  const checked = Math.min(total, checkpoint.checked_items);
  const discovering = payload.discovery && !checkpoint.discovery_done
    && checkpoint.next_index >= total
    && !["completed", "stopped", "expired"].includes(checkpoint.status);
  const percent =
    checkpoint.status === "completed"
      ? 100
      : total === 0
        ? 0
        : (checked / total) * 100;

  elements.subtitle.textContent = checkpoint.status === "completed"
    ? checkpoint.error_count > 0
      ? `Сканирование завершено с ошибками: ${checkpoint.error_count} из ${total} тайтлов. Подробности ниже.`
      : "Сканирование завершено. Результаты проверки сохранены."
    : ({
      paused: "Сканирование на паузе. Можно продолжить или остановить его.",
      permission: "Сканирование приостановлено до разрешения доступа к источнику.",
      blocked: "Проверка приостановлена. Подробности и дальнейшие действия указаны ниже.",
      error: "Проверка прервана. Подробности и дальнейшие действия указаны ниже.",
      expired: "Срок задания истёк. Сохранённые результаты остаются в каталоге.",
      stopping: "Останавливаем сканирование и сохраняем результаты.",
      stopped: "Сканирование остановлено. Сохранённые результаты остаются в каталоге.",
      completing: "Сохраняем итог сканирования.",
    }[checkpoint.status] || "Расширение проверяет источники через ваш браузер. Можно оставить эту вкладку в фоне.");
  elements.mode.textContent = modeLabel(payload.mode);
  elements["current-title"].textContent = checkpoint.current?.title
    || (discovering && checkpoint.status === "running"
      ? `Ищем новые тайтлы · страница ${checkpoint.discovery_page}`
      : statusText(checkpoint.status));
  elements["progress-text"].textContent = `${checked} / ${total}${discovering ? " · поиск" : ""}`;
  elements["progress-track"].setAttribute("aria-valuemax", String(Math.max(total, 1)));
  if (discovering) {
    elements["progress-track"].removeAttribute("aria-valuenow");
    elements["progress-track"].setAttribute("aria-valuetext", "Поиск новых тайтлов не завершён");
    elements["progress-track"].setAttribute("data-indeterminate", "true");
    if (checkpoint.status === "running") elements["progress-track"].setAttribute("data-active", "true");
    else elements["progress-track"].removeAttribute("data-active");
  } else {
    elements["progress-track"].setAttribute("aria-valuenow", String(checked));
    elements["progress-track"].removeAttribute("aria-valuetext");
    elements["progress-track"].removeAttribute("data-indeterminate");
    elements["progress-track"].removeAttribute("data-active");
  }
  elements["progress-bar"].style.width = discovering ? "32%" : `${Math.max(0, Math.min(100, percent))}%`;
  elements.checked.textContent = String(checked);
  elements.episodes.textContent = String(checkpoint.new_episode_count);
  elements.providers.textContent = String(checkpoint.new_provider_count);
  elements.errors.textContent = String(checkpoint.error_count);
  elements["job-label"].textContent = `job ${payload.job_id}`;

  const canPause = ["running", "paused", "blocked", "error"].includes(checkpoint.status);
  const canStop = ["running", "paused", "permission", "blocked", "error"].includes(
    checkpoint.status,
  );
  elements.pause.disabled = !canPause;
  elements.pause.textContent = checkpoint.status === "running" ? "Пауза" : "Продолжить";
  elements["grant-access"].hidden = checkpoint.status !== "permission";
  elements["grant-access"].disabled = checkpoint.status !== "permission";
  elements.stop.disabled = !canStop;
  elements.status.textContent = checkpoint.last_error || statusText(checkpoint.status);
  elements.status.className = `status ${
    ["permission", "blocked", "error", "expired"].includes(checkpoint.status)
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
  extensionApi.runtime
    .sendMessage({ type, detail: { ...eventDetail(), ...extra } })
    .catch(() => {});
}

async function saveCheckpoint() {
  if (!session || !checkpoint) {
    return;
  }
  const savedGeneration = generation;
  const response = await extensionApi.runtime.sendMessage({
    type: "animego-scanner-save-checkpoint", payload: session.payload,
    checkpoint: { ...checkpoint },
  });
  if (!response?.ok) throw new Error(response?.error || "Не удалось сохранить прогресс скана. Обновите расширение и вкладку сканера.");
  if (response.stale) {
    if (savedGeneration === generation) {
      generation += 1;
      currentRequest?.abort();
    }
    throw new DOMException("Задание заменено новым", "AbortError");
  }
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
  const loadGeneration = generation;
  loadedGeneration = null;
  renderLaunch();
  elements.pause.disabled = true;
  elements.stop.disabled = true;
  elements["grant-access"].disabled = true;
  currentRequest?.abort();
  currentRequest = null;
  const themeRevisionAtStart = themeRevision;
  const stored = await extensionApi.storage.local.get([STORAGE_KEY, THEME_KEY]);
  if (loadGeneration !== generation) return;
  const nextSession = stored[STORAGE_KEY];
  if (themeRevisionAtStart === themeRevision) {
    const theme = Object.hasOwn(stored, THEME_KEY) ? stored[THEME_KEY] : nextSession?.payload?.theme;
    applyTheme(theme);
  }
  if (!validPayload(nextSession?.payload)) {
    session = null;
    checkpoint = null;
    renderLaunch();
    const granted = await hasAnimeGoHostPermission(extensionApi);
    if (loadGeneration !== generation) return;
    loadedGeneration = loadGeneration;
    renderLaunch();
    elements.subtitle.textContent = granted
      ? "Выберите быстрый или полный скан. Вкладка Anime Catalog должна быть открыта."
      : "Сначала разрешите узкий доступ только к AnimeGo.";
    elements["grant-access"].hidden = granted;
    elements["grant-access"].disabled = granted;
    elements.status.textContent = granted
      ? "Готово к запуску"
      : "Разрешите доступ к источнику, чтобы продолжить.";
    elements.status.className = granted ? "status success" : "status error";
    elements["current-title"].textContent = granted
      ? "Доступ к AnimeGo выдан"
      : "Нужен доступ к AnimeGo";
    if (!announcedNoSession) {
      log(
        granted
          ? "Доступ к AnimeGo уже выдан. Можно запустить скан здесь."
          : "Нажмите «Разрешить доступ к источнику». Браузер запросит доступ только к animego.me.",
        granted ? "success" : "",
      );
      announcedNoSession = true;
    }
    if (granted) {
      extensionApi.runtime.sendMessage({ type: "animego-scanner-permission-granted" }).catch(() => {});
    }
    return;
  }
  session = nextSession;
  checkpoint = normalizeCheckpoint(session.payload, session.checkpoint);
  const expiresAt = Date.parse(session.payload.job?.expires_at || session.payload.expires_at || "");
  if (!["completed", "stopped", "expired"].includes(checkpoint.status)
      && Number.isFinite(expiresAt) && expiresAt <= Date.now()) {
    checkpoint.status = "expired";
    checkpoint.current = null;
    checkpoint.last_error = statusText("expired");
    try {
      await saveCheckpoint();
    } catch (error) {
      if (loadGeneration !== generation) return;
      checkpoint.last_error = `${statusText("expired")} Не удалось сохранить состояние: ${error?.message || String(error)}`;
    }
    if (loadGeneration !== generation) return;
  }
  loadedGeneration = loadGeneration;
  stopFinalization = null;
  render();
  const announcementKey = `${session.payload.origin}/${session.payload.job_id}`;
  if (announce && announcedJobId !== announcementKey) {
    announcedJobId = announcementKey;
    log(`Загружено задание: ${session.payload.tasks.length} тайтлов.`);
    if (checkpoint.last_error) log(checkpoint.last_error, "error");
    for (const error of checkpoint.errors) {
      const task = session.payload.tasks.find(item => item.anime_id === error.anime_id);
      log(`${taskTitle(task || error)}: ${error.message}`, "error");
    }
  }
  if (checkpoint.status === "running") {
    launchScan();
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
  await interruptibleDelay(0, runGeneration);
  const randomizedDelay = 550 + Math.floor(Math.random() * 450);
  const remaining = lastUpstreamRequestAt + randomizedDelay - Date.now();
  if (remaining > 0) {
    await interruptibleDelay(remaining, runGeneration);
  }
  lastUpstreamRequestAt = Date.now();
}

async function ensureAnimeGoAccess() {
  if (!(await hasAnimeGoHostPermission(extensionApi))) {
    throw new PermissionError(
      "Разрешите расширению доступ к animego.me, затем нажмите «Разрешить доступ к источнику» ещё раз.",
    );
  }
}

async function upstreamJson(path, runGeneration, { html = false } = {}) {
  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (attempt > 0) {
      const backoff = 1200 * 2 ** (attempt - 1) + Math.floor(Math.random() * 700);
      log(`Повтор через ${(backoff / 1000).toFixed(1)} с…`);
      await interruptibleDelay(backoff, runGeneration);
    }
    await respectUpstreamDelay(runGeneration);
    await ensureAnimeGoAccess();
    await interruptibleDelay(0, runGeneration);
    const controller = new AbortController();
    currentRequest = controller;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, UPSTREAM_TIMEOUT_MS);
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
        throw new UpstreamError(upstreamHttpError(path, response.status), {
          retryable: response.status === 408 || response.status >= 500,
          status: response.status,
        });
      }
      if (html) return body;
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
      if (timedOut && error?.name === "AbortError" && runGeneration === generation
          && !["stopping", "stopped"].includes(checkpoint.status)) {
        error = new UpstreamError("AnimeGo не ответил вовремя.", { retryable: true });
      }
      if (
        error?.name === "AbortError" ||
        error instanceof BlockedError ||
        error instanceof PermissionError
      ) {
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
      clearTimeout(timer);
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
  if (task.source === "kinogoo") {
    if (!(await extensionApi.permissions.contains({ origins: KINOGOO_ORIGINS }))) {
      throw new PermissionError("Нужен доступ к kinogoo.fm и api.nextembed.ws");
    }
    async function read(url) {
      await respectUpstreamDelay(runGeneration);
      const controller = new AbortController();
      currentRequest = controller;
      let timedOut = false;
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, UPSTREAM_TIMEOUT_MS);
      try {
        return await fetchKinogooHtml(url, controller.signal, fetch);
      } catch (error) {
        if (timedOut && error?.name === "AbortError" && runGeneration === generation
            && !["stopping", "stopped"].includes(checkpoint.status)) {
          throw new Error("Kinogoo не ответил вовремя.");
        }
        throw error;
      } finally {
        clearTimeout(timer);
        if (currentRequest === controller) currentRequest = null;
      }
    }
    const embed = kinogooPlayer(await read(task.url), task.url);
    const episodes = kinogooEpisodes(await read(embed), embed, task.season);
    const known = new Set(task.known_episode_ids || []);
    return episodes.filter(snapshot => !known.has(snapshot.episode.id));
  }
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
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, API_TIMEOUT_MS);
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
        response.status === 410 ? statusText("expired")
          : parsed?.error || parsed?.message || `Anime Catalog ответил HTTP ${response.status}.`,
        response.status,
      );
    }
    return parsed;
  } catch (error) {
    if (timedOut) throw new ApiError("Сервер не ответил за 45 секунд. Результат сохранён; нажмите «Продолжить» для повторной отправки.");
    if (error instanceof ApiError || error?.name === "AbortError") throw error;
    throw new ApiError(error?.message || "Не удалось отправить результат в Anime Catalog.");
  } finally {
    clearTimeout(timer);
    if (currentRequest === controller) {
      currentRequest = null;
    }
  }
}

function updateCountersFromResponse(response, localEpisodes) {
  const job = response?.job;
  const localProviderCount = localEpisodes.reduce((sum, item) => sum + item.providers.length, 0);
  if (job && typeof job === "object") {
    checkpoint.checked_items = Math.max(checkpoint.checked_items, Number(job.checked_items) || 0);
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

  let body = checkpoint.pending_result;
  if (!body || body.anime_id !== task.anime_id) {
    const episodes = await collectTitle(task, runGeneration);
    if (runGeneration !== generation) throw new DOMException("Остановлено", "AbortError");
    body = { anime_id: task.anime_id, episodes, selection_reason: task.selection_reason || null };
    if (task.source === "kinogoo") {
      // Kinogoo metadata is imported explicitly; AnimeGO HTML parsing does not apply.
    } else if (!task.url && task.selection_reason !== "discovery") {
      // Older servers omit card URLs. Keep episode delivery working while
      // metadata refresh waits for the server contract update.
      log("Сервер не передал адрес карточки: отправляем результат проверки серий без обновления метаданных.");
    } else if (task.selection_reason !== "discovery" || episodes.length) {
      const url = new URL(task.url);
      if (url.origin !== UPSTREAM_BASE || !url.pathname.startsWith("/anime/")) {
        throw new UpstreamError("Некорректный адрес карточки AnimeGo.");
      }
      try {
        body.detail_html = await upstreamJson(url.pathname, runGeneration, { html: true });
      } catch (error) {
        if (task.selection_reason === "discovery" || !(error instanceof UpstreamError)
            || runGeneration !== generation) throw error;
        log(`Не удалось обновить карточку: ${error?.message || String(error)}. Найденные серии будут сохранены.`, "error");
      }
    }
  }
  const response = await submitTaskResult(body, index, runGeneration);
  if (body.error) {
    reportErrorResult(task, body.error, response);
    render();
    return;
  }
  const episodes = body.episodes;
  if (episodes.length > 0) {
    log(`Добавлено новых серий: ${episodes.length}.`, "success");
  } else {
    log("Новых playable-серий нет.");
  }
  render();
  notifyApp("animego-scan-progress", {
    message: episodes.length > 0 ? `Добавлено серий: ${episodes.length}` : "Без изменений",
  });
}

async function submitTaskResult(body, index, runGeneration = generation) {
  // Persist before delivery: an interrupted request may already have committed
  // on the server. Replaying the exact result is safe and needs no new scrape.
  checkpoint.pending_result = body;
  await saveCheckpoint();
  if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
    throw new DOMException("Остановлено", "AbortError");
  }
  let response = await apiPost(
    `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/results`, body,
  );
  if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
    throw new DOMException("Остановлено", "AbortError");
  }
  if (response?.recovery_task) {
    checkpoint.recovery_task = response.recovery_task;
    await saveCheckpoint();
    if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
      throw new DOMException("Остановлено", "AbortError");
    }
    const url = new URL(response.recovery_task.url);
    if (url.origin !== "https://api.yani.tv" || !url.pathname.startsWith("/anime/")) {
      throw new ApiError("Сервер вернул недопустимый адрес запасного источника.");
    }
    if (!(await extensionApi.permissions.contains({ origins: [YUMMY_ORIGIN] }))) {
      throw new PermissionError("Для проверки запасного источника разрешите доступ к api.yani.tv.");
    }
    if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
      throw new DOMException("Остановлено", "AbortError");
    }
    log("AnimeGO вернул 404. Проверяем связанный тайтл на YummyAnime…");
    elements.status.textContent = "Проверяем YummyAnime · до 15 секунд";
    const controller = new AbortController();
    currentRequest = controller;
    const timer = setTimeout(() => controller.abort(), RECOVERY_TIMEOUT_MS);
    try {
      const upstream = await fetch(url, { signal: controller.signal, credentials: "omit", redirect: "error" });
      if (!upstream.ok) throw new Error(`YummyAnime HTTP ${upstream.status}`);
      body.recovery_response = await upstream.json();
    } catch (error) {
      if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) throw error;
      body.recovery_error = true;
      log("YummyAnime не ответил или вернул ошибку. Этот тайтл будет отмечен как непроверенный.", "error");
    } finally {
      clearTimeout(timer);
      if (currentRequest === controller) currentRequest = null;
    }
    checkpoint.pending_result = body;
    await saveCheckpoint();
    if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
      throw new DOMException("Остановлено", "AbortError");
    }
    response = await apiPost(`/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/results`, body);
  }
  if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
    throw new DOMException("Остановлено", "AbortError");
  }
  updateCountersFromResponse(response, body.episodes);
  checkpoint.next_index = index + 1;
  checkpoint.current = null;
  checkpoint.pending_result = null;
  checkpoint.recovery_task = null;
  await saveCheckpoint();
  if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
    throw new DOMException("Остановлено", "AbortError");
  }
  return response;
}

async function recordOrdinaryError(task, index, error) {
  const message = error?.message || String(error);
  const response = await submitTaskResult(
    {
      anime_id: task.anime_id,
      episodes: [],
      error: message.slice(0, 1000),
      upstream_status: error instanceof UpstreamError ? error.status : null,
      browser_recovery: true,
      selection_reason: task.selection_reason || null,
    }, index,
  );
  reportErrorResult(task, message, response);
}

function reportErrorResult(task, message, response) {
  if (response?.result?.recovered_source) {
    log(`${taskTitle(task)}: AnimeGO недоступен; проверено и обновлено через ${response.result.recovered_source}.`, "success");
    return;
  }
  checkpoint.errors.push({
    anime_id: task.anime_id,
    message: message.slice(0, 1000),
  });
  checkpoint.errors = checkpoint.errors.slice(-100);
  log(`${taskTitle(task)}: ${message}`, "error");
}

async function finishScan(runGeneration) {
  checkpoint.status = "completing";
  checkpoint.current = null;
  render();
  await saveCheckpoint();
  if (runGeneration !== generation) throw new DOMException("Остановлено", "AbortError");
  const response = await apiPost(
    `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/complete`,
    { errors: checkpoint.errors },
  );
  if (runGeneration !== generation) throw new DOMException("Остановлено", "AbortError");
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
  if (runGeneration !== generation) throw new DOMException("Остановлено", "AbortError");
  render();
  log(
    `Готово: новых серий ${checkpoint.new_episode_count}, ошибок ${checkpoint.error_count}.`,
    checkpoint.error_count > 0 ? "error" : "success",
  );
  notifyApp("animego-scan-complete", { message: "Сканирование завершено" });
}

async function runScan() {
  if (!session || !checkpoint || checkpoint.status !== "running" || loadedGeneration !== generation) {
    return;
  }
  if (activeRun) {
    restartRequested = generation;
    return;
  }
  restartRequested = null;
  activeRun = true;
  checkpoint.last_error = null;
  const runGeneration = generation;
  try {
    for (let index = checkpoint.next_index; ; index += 1) {
      await interruptibleDelay(0, runGeneration);
      if (checkpoint.status !== "running") {
        return;
      }
      if (index >= session.payload.tasks.length) {
        if (checkpoint.discovery_done) break;
        const page = checkpoint.discovery_page;
        log(`Ищем новые тайтлы: страница ${page}…`);
        let html = null;
        let notFound = false;
        try {
          html = await upstreamJson(page === 1 ? "/anime" : `/anime/${page}`, runGeneration, { html: true });
        } catch (error) {
          if (error.status === 404 && page > 1) notFound = true;
          else throw error;
        }
        if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) return;
        const discovered = await apiPost(`/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/discover`, { page, html, not_found: notFound });
        if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) return;
        const known = new Set(session.payload.tasks.map(task => task.anime_id));
        session.payload.tasks.push(...discovered.tasks.filter(task => !known.has(task.anime_id)));
        checkpoint.total_items = session.payload.tasks.length;
        checkpoint.discovery_page = page + 1;
        checkpoint.discovery_done = discovered.done;
        await saveCheckpoint();
        render();
        index -= 1;
        continue;
      }
      const task = session.payload.tasks[index];
      try {
        await processTask(task, index, runGeneration);
      } catch (error) {
        if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) return;
        if (error?.name === "AbortError") {
          if (["stopping", "stopped"].includes(checkpoint.status) || runGeneration !== generation) {
            return;
          }
          throw error;
        }
        if (error instanceof BlockedError) {
          checkpoint.status = "blocked";
          checkpoint.last_error = error.message;
          checkpoint.current = null;
          await saveCheckpoint();
          render();
          log(error.message, "error");
          notifyApp("animego-scan-error", { error: error.message, blocked: true });
          return;
        }
        if (error instanceof PermissionError) {
          checkpoint.status = "permission";
          checkpoint.last_error = error.message;
          checkpoint.current = null;
          await saveCheckpoint();
          render();
          log(error.message, "error");
          notifyApp("animego-scan-error", {
            error: error.message,
            permission: true,
          });
          return;
        }
        if (error instanceof ApiError) {
          checkpoint.status = error.status === 410 ? "expired" : "error";
          checkpoint.last_error = error.message;
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
            message: "Проверка тайтла завершена; подробности в журнале сканера",
          });
        } catch (submitError) {
          if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) return;
          checkpoint.status = submitError instanceof PermissionError ? "permission"
            : submitError instanceof ApiError && submitError.status === 410 ? "expired" : "error";
          checkpoint.last_error = submitError?.message || String(submitError);
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
        await finishScan(runGeneration);
      } catch (error) {
        if (runGeneration !== generation || ["stopping", "stopped"].includes(checkpoint.status)) {
          return;
        }
        checkpoint.status = error instanceof ApiError && error.status === 410 ? "expired" : "error";
        checkpoint.last_error = error?.message || String(error);
        await saveCheckpoint();
        render();
        log(`Не удалось завершить job: ${error?.message || String(error)}`, "error");
        notifyApp("animego-scan-error", {
          error: error?.message || String(error),
          blocked: false,
        });
      }
    }
  } catch (error) {
    if (runGeneration !== generation || ["stopping", "stopped", "paused"].includes(checkpoint.status)) return;
    checkpoint.status = error instanceof BlockedError ? "blocked" : error instanceof PermissionError ? "permission"
      : error instanceof ApiError && error.status === 410 ? "expired" : "error";
    checkpoint.last_error = error?.message || String(error);
    try {
      await saveCheckpoint();
    } catch (saveError) {
      if (runGeneration !== generation) return;
      checkpoint.last_error = `Не удалось сохранить состояние скана: ${saveError?.message || String(saveError)}`;
    }
    if (runGeneration !== generation) return;
    render();
    log(checkpoint.last_error, "error");
    notifyApp("animego-scan-error", { error: checkpoint.last_error });
  } finally {
    activeRun = false;
    if (loadedGeneration === generation && checkpoint?.status === "running"
        && (restartRequested === generation || shouldRestartAfterReload(runGeneration, generation, checkpoint.status))) {
      restartRequested = null;
      queueMicrotask(launchScan);
    }
  }
}

function launchScan() {
  const runGeneration = generation;
  runScan().catch(error => {
    if (runGeneration !== generation) return;
    if (checkpoint && !["stopping", "stopped", "expired", "completed"].includes(checkpoint.status)) {
      checkpoint.status = "error";
      checkpoint.last_error = error?.message || String(error);
      render();
    }
    reportControlError(error);
  });
}

async function pauseOrResume() {
  if (!checkpoint || loadedGeneration !== generation) {
    return;
  }
  const actionGeneration = generation;
  if (checkpoint.status === "running") {
    checkpoint.status = "paused";
    try {
      await saveCheckpoint();
    } catch (error) {
      if (actionGeneration !== generation) return;
      if (checkpoint.status !== "paused") return;
      checkpoint.status = "running";
      render();
      throw error;
    }
    if (actionGeneration !== generation || checkpoint.status !== "paused") return;
    render();
    log("Сканирование поставлено на паузу.");
    notifyApp("animego-scan-progress", { message: "Пауза" });
    return;
  }
  if (["paused", "blocked", "error"].includes(checkpoint.status)) {
    const previousStatus = checkpoint.status;
    const previousError = checkpoint.last_error;
    checkpoint.status = "running";
    checkpoint.last_error = null;
    try {
      await saveCheckpoint();
    } catch (error) {
      if (actionGeneration !== generation) return;
      if (checkpoint.status !== "running") return;
      checkpoint.status = previousStatus;
      checkpoint.last_error = previousError;
      render();
      throw error;
    }
    if (actionGeneration !== generation || checkpoint.status !== "running") return;
    render();
    log("Сканирование продолжено.");
    notifyApp("animego-scan-progress", { message: "Продолжаем сканирование" });
    launchScan();
  }
}

async function grantAnimeGoAccess() {
  if (loadedGeneration !== generation) return;
  const actionGeneration = generation;
  elements["grant-access"].disabled = true;
  elements.status.textContent = "Ожидаем разрешение браузера…";
  try {
    const task = session?.payload?.tasks?.[checkpoint?.next_index || 0];
    const granted = checkpoint?.recovery_task
      ? await extensionApi.permissions.request({ origins: [YUMMY_ORIGIN] })
      : task?.source === "kinogoo"
      ? await extensionApi.permissions.request({ origins: KINOGOO_ORIGINS })
      : await requestAnimeGoHostPermission(extensionApi);
    if (actionGeneration !== generation) return;
    if (session && checkpoint?.status !== "permission") {
      render();
      return;
    }
    if (!granted) {
      elements["grant-access"].disabled = false;
      elements.status.textContent = "Доступ не выдан. Можно повторить или остановить скан.";
      elements.status.className = "status error";
      log("Браузер не выдал доступ к источнику.", "error");
      return;
    }

    log("Доступ к источнику выдан.", "success");
    await extensionApi.runtime.sendMessage({ type: "animego-scanner-permission-granted" });
    if (actionGeneration !== generation) return;
    if (session && checkpoint?.status === "permission") {
      const previousError = checkpoint.last_error;
      checkpoint.status = "running";
      checkpoint.last_error = null;
      try {
        await saveCheckpoint();
      } catch (error) {
        if (actionGeneration !== generation) return;
        if (checkpoint.status !== "running") return;
        checkpoint.status = "permission";
        checkpoint.last_error = previousError;
        render();
        throw error;
      }
      if (actionGeneration !== generation || checkpoint.status !== "running") return;
      render();
      notifyApp("animego-scan-progress", { message: "Доступ выдан, продолжаем сканирование" });
      launchScan();
      return;
    }
    if (session) {
      render();
      return;
    }
    elements["grant-access"].hidden = true;
    elements.status.textContent = "Доступ выдан. Если скан не начался, выберите режим проверки.";
    elements.status.className = "status success";
    elements["current-title"].textContent = "Доступ к AnimeGo выдан";
  } catch (error) {
    if (actionGeneration !== generation) return;
    if (session && ["stopping", "stopped", "expired", "completed"].includes(checkpoint?.status)) {
      render();
      return;
    }
    elements["grant-access"].disabled = false;
    elements.status.textContent = error?.message || "Не удалось запросить доступ к AnimeGo.";
    elements.status.className = "status error";
    log(error?.message || String(error), "error");
  }
}

async function stopScan() {
  if (
    loadedGeneration !== generation ||
    !checkpoint ||
    !["running", "paused", "permission", "blocked", "error"].includes(checkpoint.status)
  ) {
    return;
  }
  if (stopFinalization) {
    return stopFinalization;
  }
  const actionGeneration = generation;
  const finalization = (async () => {
    checkpoint.status = "stopping";
    checkpoint.last_error = null;
    checkpoint.current = null;
    currentRequest?.abort();
    render();
    try {
      await saveCheckpoint();
    } catch (error) {
      if (actionGeneration !== generation) return;
      checkpoint.status = "error";
      checkpoint.last_error = error?.message || String(error);
      render();
      throw error;
    }
    if (actionGeneration !== generation) return;
    try {
      const response = await apiPost(
        `/api/animego-scans/${encodeURIComponent(session.payload.job_id)}/complete`,
        { stopped: true, errors: checkpoint.errors },
      );
      if (actionGeneration !== generation) return;
      checkpoint.status = "stopped";
      if (response?.job) {
        checkpoint.error_count = Number(response.job.error_count) || checkpoint.error_count;
      }
      log("Сканирование остановлено. Прогресс до последнего тайтла сохранён.");
      notifyApp("animego-scan-complete", { stopped: true, message: "Сканирование остановлено" });
    } catch (error) {
      if (actionGeneration !== generation) return;
      checkpoint.status = error instanceof ApiError && error.status === 410 ? "expired" : "error";
      checkpoint.last_error = error?.message || String(error);
      log(`Не удалось остановить job на сервере: ${error?.message || String(error)}`, "error");
      notifyApp("animego-scan-error", {
        error: error?.message || String(error),
        blocked: false,
      });
    }
    try {
      await saveCheckpoint();
    } catch (error) {
      if (actionGeneration !== generation) return;
      render();
      throw error;
    }
    if (actionGeneration !== generation) return;
    render();
  })();
  stopFinalization = finalization;
  try {
    return await finalization;
  } finally {
    if (stopFinalization === finalization) stopFinalization = null;
  }
}

for (const mode of ["partial", "full"]) {
  elements[`start-${mode}`].addEventListener("click", () => startNewScan(mode));
}

elements.pause.addEventListener("click", () => {
  pauseOrResume().catch(reportControlError);
});
elements["grant-access"].addEventListener("click", () => {
  grantAnimeGoAccess().catch(reportControlError);
});
elements.stop.addEventListener("click", () => {
  stopScan().catch(reportControlError);
});

function launchSessionLoad() {
  const pending = loadSession();
  const loadGeneration = generation;
  pending.catch(error => {
    if (loadGeneration === generation) reportControlError(error);
  });
}

extensionApi.runtime.onMessage.addListener((message) => {
  if (message?.type === "animego-scanner-reload") {
    launchSessionLoad();
  }
});

launchSessionLoad();

const DEFAULT_FILTERS = {
  genre: "",
  year: "",
  kind: "",
  status: "",
  source: "",
  video: "any",
};
const DEFAULT_SORT_BY = "rating_best";
const DEFAULT_SORT_DIR = "desc";
const VIEW_SORT_DEFAULTS = {
  all: { by: DEFAULT_SORT_BY, dir: DEFAULT_SORT_DIR },
  favorites: { by: "favorite_added", dir: "desc" },
  progress: { by: "watch_recent", dir: "desc" },
};
const USER_STATE_RESPONSE_FIELDS = [
  "is_favorite",
  "watched",
  "progress_episode_number",
  "watch_status",
  "not_interested",
  "updated_at",
  "favorite_updated_at",
  "watch_status_updated_at",
  "not_interested_updated_at",
];
const USER_STATE_TRANSPORT_FIELDS = new Set(["video_source_id"]);
const RECOMMENDATION_SEMANTIC_FIELDS = [
  "is_favorite",
  "watched",
  "progress_episode_number",
  "watch_status",
  "not_interested",
];
const RECOMMENDATION_LIMIT = 20;
const CONTENT_UPDATE_LIMIT = 220;
const CONTENT_UPDATE_DEFAULT_DAYS = "7";
const INITIAL_RENDER_LIMIT = 40;
const RENDER_BATCH_SIZE = 80;
const LINK_PARAM_KEYS = ["episode", "source", "translation", "provider"];
const PLAYER_IFRAME_ALLOW = "autoplay; fullscreen; picture-in-picture; encrypted-media; web-share; screen-wake-lock";
const PLAYER_IFRAME_SANDBOX = "allow-scripts allow-same-origin allow-forms allow-presentation allow-popups";
let playerHosts = [];
const WATCH_ENDPOINT = "/api/watch-events";
const CONTENT_UPDATE_ENDPOINT = "/api/content-updates";
const WATCH_HEARTBEAT_MS = 30000;
const WATCH_MAX_DELTA_SECONDS = 300;
const WATCH_EVIDENCE_MAX_AGE_MS = 4 * 60 * 60 * 1000;
const SEARCH_INPUT_DEBOUNCE_MS = 140;
const reportClientError = window.reportClientError || (() => {});
const clientActionError = window.reportActionError || (() => error => console.error(error));
const PERFORMANCE_ENDPOINT = "/api/performance";
const MAX_PERFORMANCE_API_REQUESTS = 40;
const MAX_PERFORMANCE_RESOURCES = 24;
const catalogSearch = window.AnimeSearch;
const frontendRuntime = window.AnimeFrontendRuntime;
const CONTENT_UPDATE_TYPES = [
  { id: "all", label: "Все" },
  { id: "new_title", label: "Тайтлы" },
  { id: "new_episode", label: "Серии" },
  { id: "new_translation", label: "Озвучки" },
  { id: "new_provider", label: "Плееры" },
];
const CONTENT_UPDATE_PERIODS = [
  { id: "3", label: "3 дня" },
  { id: "7", label: "7 дней" },
  { id: "30", label: "30 дней" },
  { id: "all", label: "Лента" },
];

if (!catalogSearch || !frontendRuntime) {
  throw new Error("Frontend dependencies are not loaded");
}

function performanceNow() {
  return window.performance?.now ? window.performance.now() : Date.now();
}

function roundMetric(value) {
  return Number.isFinite(value) ? Math.round(value * 10) / 10 : null;
}

const pagePerformance = {
  bootStartedAt: performanceNow(),
  checkpoints: [],
  apiRequests: [],
  reported: false,
};

const state = {
  user: null,
  anime: [],
  recommendations: [],
  recommendationProfile: null,
  recommendationsLoaded: false,
  recommendationsLoading: null,
  recommendationsError: null,
  recommendationsRequestId: 0,
  recommendationsQueryKey: null,
  recommendationsDirtyConfirmed: false,
  contentUpdates: null,
  contentUpdatesLoaded: false,
  contentUpdatesLoading: null,
  contentUpdatesLoadingMore: null,
  contentUpdatesError: null,
  contentUpdatesPageError: null,
  contentUpdatesRequestId: 0,
  contentUpdateDays: CONTENT_UPDATE_DEFAULT_DAYS,
  contentUpdateType: "all",
  continueWatching: null,
  searchFieldsLoaded: false,
  searchFieldsLoading: null,
  searchFieldsError: null,
  searchFieldsById: null,
  filtered: [],
  selectedAnimeId: null,
  detail: null,
  selectedEpisodeId: null,
  selectedContentSource: null,
  selectedTranslation: null,
  selectedSourceId: null,
  sourceSelectionPreference: null,
  viewMode: "all",
  filters: { ...DEFAULT_FILTERS },
  activeFilterIds: [],
  sortBy: DEFAULT_SORT_BY,
  sortDir: DEFAULT_SORT_DIR,
  viewSorts: Object.fromEntries(
    Object.entries(VIEW_SORT_DEFAULTS).map(([mode, value]) => [mode, { ...value }])
  ),
  renderLimit: INITIAL_RENDER_LIMIT,
  filterControls: {},
  userStateRevision: 0,
  userStateFieldRevisions: new Map(),
  detailRequestId: 0,
  detailRequestController: null,
  descriptionExpanded: false,
  descriptionCanExpand: false,
  descriptionMeasureId: 0,
  urlSyncSuspended: false,
  watchSession: null,
  watchHeartbeatTimer: null,
  watchFullscreenActive: false,
};

const el = {
  count: document.getElementById("catalog-count"),
  accountRow: document.getElementById("account-row"),
  accountAvatar: document.getElementById("account-avatar"),
  accountName: document.getElementById("account-name"),
  accountEmail: document.getElementById("account-email"),
  adminLink: null,
  logoutButton: document.getElementById("logout-button"),
  search: document.getElementById("search"),
  filterGrid: document.getElementById("filter-grid"),
  activeFilters: document.getElementById("active-filters"),
  sortBy: document.getElementById("sort-by"),
  sortDirToggle: document.getElementById("sort-dir-toggle"),
  addFilter: document.getElementById("add-filter"),
  resetFilters: document.getElementById("reset-filters"),
  viewTabs: document.querySelectorAll(".view-tabs button"),
  recommendationMeta: document.getElementById("recommendation-meta"),
  list: document.getElementById("anime-list"),
  updatesView: document.getElementById("updates-view"),
  titleDetailView: document.getElementById("title-detail-view"),
  poster: document.getElementById("poster"),
  meta: document.getElementById("meta-line"),
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  favoriteToggle: document.getElementById("favorite-toggle"),
  notWatchingButton: document.getElementById("not-watching-button"),
  notInterestedButton: document.getElementById("not-interested-button"),
  watchedToggle: document.getElementById("watched-toggle"),
  recommendationContext: document.getElementById("recommendation-context"),
  recentUpdates: document.getElementById("recent-updates"),
  genres: document.getElementById("genres"),
  description: document.getElementById("description"),
  descriptionToggle: document.getElementById("description-toggle"),
  fields: document.getElementById("fields"),
  episodes: document.getElementById("episodes"),
  contentSource: document.getElementById("content-source"),
  translation: document.getElementById("translation"),
  provider: document.getElementById("provider"),
  fullscreenToggle: document.getElementById("fullscreen-toggle"),
  pipToggle: document.getElementById("pip-toggle"),
  playerActionState: document.getElementById("player-action-state"),
  player: document.getElementById("player"),
  wrap: document.getElementById("iframe-wrap"),
  empty: document.getElementById("empty-player"),
  host: document.getElementById("host"),
  episodeState: document.getElementById("episode-state"),
  appStatus: document.getElementById("app-status"),
};

let listImageObserver = null;
let titleTooltip = null;
let titleTooltipTarget = null;
let appStatusTimer = 0;
let searchInputTimer = 0;

function isAbortError(error) {
  return error?.name === "AbortError";
}

function showAppStatus(message, tone = "warn", timeoutMs = 6000) {
  if (!el.appStatus) return;
  if (appStatusTimer) window.clearTimeout(appStatusTimer);
  el.appStatus.textContent = message || "";
  el.appStatus.dataset.tone = tone;
  el.appStatus.hidden = !message;
  appStatusTimer = message && timeoutMs > 0
    ? window.setTimeout(() => {
      el.appStatus.hidden = true;
      appStatusTimer = 0;
    }, timeoutMs)
    : 0;
}

const reportActionError = (action, context = {}) => {
  const report = clientActionError(action, context);
  return error => {
    if (isAbortError(error)) return;
    report(error);
    showAppStatus(error?.message || "Не удалось выполнить действие");
  };
};

function sameOriginPath(value) {
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin) return null;
    return `${url.pathname}${url.search}`;
  } catch (error) {
    return null;
  }
}

function markPerformanceCheckpoint(name, context = {}) {
  pagePerformance.checkpoints.push({
    name,
    at_ms: roundMetric(performanceNow() - pagePerformance.bootStartedAt),
    ...context,
  });
}

function recordApiPerformance(path, details) {
  const normalizedPath = sameOriginPath(path);
  if (!normalizedPath || normalizedPath === PERFORMANCE_ENDPOINT || !normalizedPath.startsWith("/api/")) {
    return;
  }
  pagePerformance.apiRequests.push({
    path: normalizedPath,
    ...details,
  });
  if (pagePerformance.apiRequests.length > MAX_PERFORMANCE_API_REQUESTS) {
    pagePerformance.apiRequests.splice(0, pagePerformance.apiRequests.length - MAX_PERFORMANCE_API_REQUESTS);
  }
}

function navigationPerformance() {
  const entry = window.performance?.getEntriesByType?.("navigation")?.[0];
  if (!entry) return null;
  return {
    type: entry.type || "",
    response_start_ms: roundMetric(entry.responseStart),
    response_end_ms: roundMetric(entry.responseEnd),
    dom_interactive_ms: roundMetric(entry.domInteractive),
    dom_content_loaded_ms: roundMetric(entry.domContentLoadedEventEnd),
    dom_complete_ms: roundMetric(entry.domComplete),
    load_event_end_ms: roundMetric(entry.loadEventEnd),
    duration_ms: roundMetric(entry.duration),
    transfer_size: entry.transferSize || 0,
    encoded_body_size: entry.encodedBodySize || 0,
    decoded_body_size: entry.decodedBodySize || 0,
  };
}

function resourcePerformance() {
  const entries = window.performance?.getEntriesByType?.("resource") || [];
  return entries
    .map(entry => {
      const path = sameOriginPath(entry.name);
      if (!path || path === PERFORMANCE_ENDPOINT) return null;
      return {
        path,
        initiator_type: entry.initiatorType || "",
        start_ms: roundMetric(entry.startTime),
        duration_ms: roundMetric(entry.duration),
        response_end_ms: roundMetric(entry.responseEnd),
        transfer_size: entry.transferSize || 0,
        encoded_body_size: entry.encodedBodySize || 0,
        decoded_body_size: entry.decodedBodySize || 0,
      };
    })
    .filter(Boolean)
    .sort((left, right) => (right.duration_ms || 0) - (left.duration_ms || 0))
    .slice(0, MAX_PERFORMANCE_RESOURCES);
}

function connectionPerformance() {
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if (!connection) return null;
  return {
    effective_type: connection.effectiveType || "",
    downlink: connection.downlink || null,
    rtt: connection.rtt || null,
    save_data: Boolean(connection.saveData),
  };
}

function reportHomePerformance(result, context = {}) {
  if (pagePerformance.reported) return;
  pagePerformance.reported = true;
  const payload = {
    event: "home_boot",
    source: "static/app.js",
    result,
    timestamp: new Date().toISOString(),
    path: `${window.location.pathname}${window.location.search}${window.location.hash}`,
    duration_ms: roundMetric(performanceNow() - pagePerformance.bootStartedAt),
    navigation: navigationPerformance(),
    resources: resourcePerformance(),
    api_requests: [...pagePerformance.apiRequests],
    checkpoints: [...pagePerformance.checkpoints],
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      device_pixel_ratio: window.devicePixelRatio || 1,
    },
    connection: connectionPerformance(),
    catalog: {
      items: state.anime.length,
      filtered: state.filtered.length,
      recommendations: state.recommendations.length,
      selected_anime_id: state.selectedAnimeId,
      authenticated: Boolean(state.user),
    },
    context,
  };
  window.setTimeout(() => {
    fetch(PERFORMANCE_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }, 0);
}

async function api(path, options = {}) {
  const startedAt = performanceNow();
  const startFromBoot = startedAt - pagePerformance.bootStartedAt;
  let status = 0;
  let ok = false;
  try {
    const response = await fetch(path, options);
    status = response.status;
    ok = response.ok;
    if (response.status === 401) {
      const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      window.location.replace(`/login?next=${encodeURIComponent(next)}`);
      throw new Error("authentication required");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload?.error || `${response.status} ${response.statusText}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  } finally {
    recordApiPerformance(path, {
      method: options.method || "GET",
      status,
      ok,
      start_ms: roundMetric(startFromBoot),
      duration_ms: roundMetric(performanceNow() - startedAt),
    });
  }
}

function text(value, fallback = "") {
  return value == null || value === "" ? fallback : String(value);
}

function userInitials(user) {
  const source = user?.name || user?.email || "";
  const parts = source.trim().split(/\s+/).filter(Boolean);
  const letters = parts.length >= 2
    ? [parts[0][0], parts[1][0]]
    : [source.trim()[0] || "?"];
  return letters.join("").toLocaleUpperCase("ru");
}

function renderAccount() {
  if (!state.user || !el.accountRow) return;
  el.accountName.textContent = state.user.name || state.user.email || "Google user";
  el.accountEmail.textContent = state.user.email || "";
  renderAdminLink();
  el.accountRow.hidden = false;
  if (state.user.picture_url) {
    el.accountAvatar.textContent = "";
    el.accountAvatar.style.backgroundImage = `url(${JSON.stringify(state.user.picture_url)})`;
  } else {
    el.accountAvatar.style.backgroundImage = "";
    el.accountAvatar.textContent = userInitials(state.user);
  }
}

function renderAdminLink() {
  if (!state.user?.is_admin) {
    el.adminLink?.remove();
    el.adminLink = null;
    return;
  }
  if (!el.adminLink) {
    const link = document.createElement("a");
    link.id = "admin-link";
    link.className = "icon-button icon-link admin-link";
    link.href = "/admin";
    link.setAttribute("aria-label", "Админка");
    link.title = "Админка";
    link.textContent = "⚙";
    el.logoutButton.before(link);
    el.adminLink = link;
  }
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  window.location.replace("/login");
}

function searchText(value) {
  return catalogSearch.searchText(value);
}

function clearSearchIndex(item) {
  if (item && Object.prototype.hasOwnProperty.call(item, "_searchIndex")) {
    delete item._searchIndex;
  }
}

function setItemSearchFields(item, searchFields) {
  if (!item) return;
  item.search_fields = Array.isArray(searchFields) ? searchFields : [];
  clearSearchIndex(item);
}

function isMobileLayout() {
  return window.matchMedia?.("(max-width: 980px)").matches;
}

function scrollDetailIntoViewForMobile() {
  if (!isMobileLayout()) return;
  requestAnimationFrame(() => {
    document.querySelector(".detail")?.scrollIntoView({ block: "start" });
  });
}

function ensureTitleTooltip() {
  if (titleTooltip) return titleTooltip;
  titleTooltip = document.createElement("div");
  titleTooltip.id = "title-tooltip";
  titleTooltip.className = "title-tooltip";
  titleTooltip.setAttribute("role", "tooltip");
  titleTooltip.setAttribute("aria-hidden", "true");
  document.body.append(titleTooltip);
  return titleTooltip;
}

function isTitleOverflowing(node) {
  if (!node) return false;
  return node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1;
}

function placeTitleTooltip(target, tooltip) {
  const rect = target.getBoundingClientRect();
  const gap = 10;
  const viewportPadding = 12;
  const wideMax = Math.min(420, window.innerWidth - viewportPadding * 2);
  tooltip.style.maxWidth = `${Math.max(220, wideMax)}px`;

  const tooltipRect = tooltip.getBoundingClientRect();
  const enoughRightSpace = rect.right + gap + tooltipRect.width <= window.innerWidth - viewportPadding;
  let left = enoughRightSpace ? rect.right + gap : rect.left;
  let top = enoughRightSpace ? rect.top + 6 : rect.bottom + gap;

  if (left + tooltipRect.width > window.innerWidth - viewportPadding) {
    left = window.innerWidth - tooltipRect.width - viewportPadding;
  }
  if (top + tooltipRect.height > window.innerHeight - viewportPadding) {
    top = rect.top - tooltipRect.height - gap;
  }

  tooltip.style.left = `${Math.max(viewportPadding, left)}px`;
  tooltip.style.top = `${Math.max(viewportPadding, top)}px`;
}

function showTitleTooltip(target, titleNode) {
  const fullTitle = target.dataset.fullTitle || titleNode?.textContent?.trim() || "";
  if (!fullTitle || !isTitleOverflowing(titleNode)) return;

  const tooltip = ensureTitleTooltip();
  titleTooltipTarget = target;
  tooltip.textContent = fullTitle;
  tooltip.classList.add("visible");
  tooltip.setAttribute("aria-hidden", "false");
  target.setAttribute("aria-describedby", tooltip.id);

  requestAnimationFrame(() => {
    if (titleTooltipTarget === target) placeTitleTooltip(target, tooltip);
  });
}

function hideTitleTooltip() {
  if (!titleTooltipTarget || !titleTooltip) return;
  titleTooltipTarget.removeAttribute("aria-describedby");
  titleTooltipTarget = null;
  titleTooltip.classList.remove("visible");
  titleTooltip.setAttribute("aria-hidden", "true");
}

function normalizeLinkValue(value) {
  const text = value == null ? "" : String(value).trim();
  return text || null;
}

function titleRefFromPath() {
  let value = "";
  try {
    value = decodeURIComponent(window.location.pathname.replace(/^\/+|\/+$/g, ""));
  } catch (error) {
    value = "";
  }
  if (!value || value.includes("/") || value === "api" || value === "static") return null;
  return normalizeLinkValue(value);
}

function readLinkState() {
  const params = new URLSearchParams(window.location.search);
  return {
    animeId: titleRefFromPath() || normalizeLinkValue(params.get("anime")),
    episodeId: normalizeLinkValue(params.get("episode")),
    contentSource: normalizeLinkValue(params.get("source")),
    translation: normalizeLinkValue(params.get("translation")),
    provider: normalizeLinkValue(params.get("provider")),
  };
}

function setOptionalParam(params, key, value) {
  const normalized = normalizeLinkValue(value);
  if (normalized) {
    params.set(key, normalized);
  } else {
    params.delete(key);
  }
}

function syncUrlFromDetail({ replace = true } = {}) {
  if (state.urlSyncSuspended || !state.selectedAnimeId) return;

  const url = new URL(window.location.href);
  const params = url.searchParams;
  params.delete("anime");
  setOptionalParam(params, "episode", state.selectedEpisodeId);
  setOptionalParam(params, "source", state.selectedContentSource);
  setOptionalParam(params, "translation", state.selectedTranslation);
  setOptionalParam(params, "provider", state.selectedSourceId);
  const slug = state.detail?.slug || state.detail?.internal_id || state.selectedAnimeId;
  url.pathname = slug ? `/${encodeURIComponent(slug)}` : "/";

  const next = `${url.pathname}${url.search}${url.hash}`;
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (next === current) return;

  const statePayload = {
    anime: slug,
    ...Object.fromEntries(LINK_PARAM_KEYS.map(key => [key, params.get(key)])),
  };
  window.history[replace ? "replaceState" : "pushState"](statePayload, "", next);
}

function sourcesForEpisode(episodeId) {
  return allSourcesForEpisode(episodeId)
    .filter(source => !state.selectedContentSource || source.source === state.selectedContentSource);
}

function selectedSourceForEpisode(episodeId = state.selectedEpisodeId) {
  const sources = sourcesForEpisode(episodeId);
  return sources.find(source => String(source.id) === String(state.selectedSourceId)) || sources[0] || null;
}

function activeEpisode() {
  return state.detail?.episodes?.find(episode => String(episode.id) === String(state.selectedEpisodeId)) || null;
}

function titleRefForItem(item) {
  return item?.slug || item?.internal_id || item?.id;
}

function observeListImage(img, src) {
  if (!src) return;
  if (!("IntersectionObserver" in window)) {
    img.src = src;
    return;
  }
  if (!listImageObserver) {
    listImageObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const image = entry.target;
        image.src = image.dataset.src || "";
        image.removeAttribute("data-src");
        listImageObserver.unobserve(image);
      }
    }, {
      root: el.list,
      rootMargin: "180px 0px",
    });
  }
  img.dataset.src = src;
  listImageObserver.observe(img);
}

function resetListImageObserver() {
  if (listImageObserver) listImageObserver.disconnect();
  listImageObserver = null;
}

function selectedEpisodeIndex(episodes) {
  return episodes.findIndex(episode => String(episode.id) === String(state.selectedEpisodeId));
}

function episodeNumberLabel(episode, fallbackIndex) {
  const value = text(episode?.number, fallbackIndex + 1);
  return /^\d+([.,]\d+)?$/.test(value) ? `${value} серия` : value;
}

function episodeTitleText(episode) {
  const candidates = [episode?.title, episode?.release_label];
  return candidates.find(value => value && value !== "---") || "";
}

function episodeOptionText(episode, index) {
  const label = episodeNumberLabel(episode, index);
  const title = episodeTitleText(episode);
  const showTitle = title && searchText(title) !== searchText(label);
  const unavailable = (episode.source_count || 0) <= 0 ? " (нет видео)" : "";
  return `${label}${showTitle ? ` - ${title}` : ""}${unavailable}`;
}

function adjacentAvailableEpisode(episodes, currentIndex, direction) {
  for (let index = currentIndex + direction; index >= 0 && index < episodes.length; index += direction) {
    if ((episodes[index].source_count || 0) > 0) return episodes[index];
  }
  return null;
}

function numberFrom(value) {
  const match = String(value || "").match(/\d+/);
  return match ? Number.parseInt(match[0], 10) : null;
}

function effectiveWatchStatus(item) {
  return frontendRuntime.effectiveWatchStatus(item);
}

function watchStatusLabel(status) {
  return frontendRuntime.watchStatusLabel(status);
}

function progressText(item) {
  const status = effectiveWatchStatus(item);
  const progress = item?.last_watch?.progress_episode_number ?? item?.progress_episode_number;
  const label = watchStatusLabel(status);
  if (progress == null || !["watching", "paused"].includes(status)) return label;
  return `${label} · серия ${progress}`;
}

function effectiveProgressEpisodeNumber(detail) {
  return detail?.last_watch?.progress_episode_number ?? detail?.progress_episode_number;
}

function sourceLabel(source) {
  if (source === "yummyanime") return "YummyAnime";
  if (source === "animego") return "AnimeGO";
  return source ? String(source) : "";
}

function itemSources(item) {
  const values = Array.isArray(item?.sources) && item.sources.length
    ? item.sources
    : [item?.source].filter(Boolean);
  return [...new Set(values.filter(Boolean))];
}

function sourceLabelList(item) {
  return itemSources(item).map(sourceLabel).filter(Boolean).join(" · ");
}

function sourceVariants(detail) {
  const variants = Array.isArray(detail?.source_variants) ? detail.source_variants : [];
  if (variants.length) return variants;
  return detail?.source ? [{ source: detail.source, source_count: detail.source_count || 0 }] : [];
}

function allSourcesForEpisode(episodeId) {
  return (state.detail?.sources_by_episode?.[episodeId] || [])
    .filter(source => source.embed_url);
}

function contentSourceHasEpisode(source, episodeId) {
  if (!source || !episodeId) return false;
  return allSourcesForEpisode(episodeId).some(item => item.source === source);
}

function nearestEpisodeIdForContentSource(source, selectedEpisodeId = state.selectedEpisodeId) {
  if (!source) return selectedEpisodeId;
  const availableEpisodeIds = (state.detail?.episodes || [])
    .filter(episode => contentSourceHasEpisode(source, episode.id))
    .map(episode => episode.id);
  return frontendRuntime.nearestAvailableEpisodeId(
    state.detail?.episodes || [],
    availableEpisodeIds,
    selectedEpisodeId,
  );
}

function preferredContentSource(detail, episodeId = null) {
  const variants = sourceVariants(detail);
  if (episodeId) {
    const withEpisode = variants.find(variant => contentSourceHasEpisode(variant.source, episodeId));
    if (withEpisode) return withEpisode.source;
  }
  const ranked = variants
    .filter(variant => (variant.source_count || 0) > 0)
    .sort((left, right) => (
      (right.available_episode_count || 0) - (left.available_episode_count || 0)
      || (right.source_count || 0) - (left.source_count || 0)
    ));
  return (ranked[0] || variants[0] || {}).source || detail?.source || null;
}

function matchingEpisodeId(episodeId) {
  if (!episodeId) return null;
  const episode = state.detail?.episodes?.find(item => String(item.id) === String(episodeId));
  return episode ? episode.id : null;
}

function episodeIdForProgress(progressEpisodeNumber) {
  const progress = numberFrom(progressEpisodeNumber);
  if (progress == null) return null;
  const episodes = state.detail?.episodes || [];
  const withVideo = episodes.find(episode => (
    (episode.source_count || 0) > 0
    && numberFrom(episode.number) === progress
  ));
  const matching = withVideo || episodes.find(episode => numberFrom(episode.number) === progress);
  return matching ? matching.id : null;
}

function episodeIdForUpdateEvent(event) {
  return matchingEpisodeId(event?.episode_id)
    || episodeIdForProgress(event?.episode_number);
}

function episodeIdForLastWatch(lastWatch) {
  if (!lastWatch) return null;
  return matchingEpisodeId(lastWatch.episode_id)
    || episodeIdForProgress(lastWatch.progress_episode_number || lastWatch.episode_number);
}

function matchingContentSource(source) {
  if (!source) return null;
  return sourceVariants(state.detail).some(variant => variant.source === source) ? source : null;
}

function applyDetailLinkState(linkState = {}) {
  const firstAvailable = state.detail.episodes.find(episode => episode.source_count > 0);
  const lastWatch = state.detail.last_watch || null;
  const lastWatchEpisodeId = !linkState.episodeId ? episodeIdForLastWatch(lastWatch) : null;
  state.selectedEpisodeId = matchingEpisodeId(linkState.episodeId)
    || lastWatchEpisodeId
    || episodeIdForProgress(state.detail.progress_episode_number)
    || (firstAvailable || state.detail.episodes[0] || {}).id
    || null;

  state.selectedContentSource = matchingContentSource(linkState.contentSource)
    || (lastWatchEpisodeId ? matchingContentSource(lastWatch?.source) : null)
    || preferredContentSource(state.detail, state.selectedEpisodeId);

  state.selectedTranslation = linkState.translation || (lastWatchEpisodeId ? lastWatch?.translation_id : null) || null;
  state.selectedSourceId = linkState.provider || (lastWatchEpisodeId ? lastWatch?.video_source_id : null) || null;
  state.sourceSelectionPreference = lastWatchEpisodeId
    ? frontendRuntime.sourcePreference(lastWatch)
    : null;
}

function numericValue(value) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function yearValue(item) {
  return numericValue(item.year) || numericValue(String(item.date_published || "").slice(0, 4));
}

function bestScore(item) {
  return numericValue(item.effective_score)
    ?? numericValue(item.aggregate_score)
    ?? numericValue(item.listing_score);
}

function formatScore(value) {
  const score = numericValue(value);
  if (score == null) return "";
  const rounded = score.toFixed(1).replace(/\.0$/, "");
  return `${rounded}/10`;
}

function ratingSourceLabel(item) {
  const source = item?.effective_score_source;
  if (!source) return "";
  return source === "synthetic" ? "синт." : source;
}

function ratingText(item) {
  const formatted = formatScore(bestScore(item));
  if (!formatted) return "";
  const source = ratingSourceLabel(item);
  return source ? `${formatted} ${source}` : formatted;
}

function formatRecommendationScore(value) {
  const score = numericValue(value);
  if (score == null) return "";
  return `${score.toFixed(1).replace(/\.0$/, "")}/100`;
}

function russianPlural(count, one, few, many) {
  const value = Math.abs(Number.parseInt(count, 10) || 0);
  if (value % 10 === 1 && value % 100 !== 11) return one;
  if (value % 10 >= 2 && value % 10 <= 4 && !(value % 100 >= 12 && value % 100 <= 14)) return few;
  return many;
}

function contentUpdateTypeLabel(type) {
  return CONTENT_UPDATE_TYPES.find(item => item.id === type)?.label || "Обновление";
}

function contentUpdateEvents() {
  return state.contentUpdates?.events || [];
}

function contentUpdateMatchesType(event) {
  return state.contentUpdateType === "all" || event?.event_type === state.contentUpdateType;
}

function filteredContentUpdateEvents() {
  return contentUpdateEvents().filter(contentUpdateMatchesType);
}

function itemContentUpdateEvents(item) {
  if (Array.isArray(item?.events)) return item.events.filter(contentUpdateMatchesType);
  const id = String(item?.id ?? "");
  if (!id || !state.contentUpdatesLoaded) return [];
  return filteredContentUpdateEvents().filter(event => String(event.anime_id) === id);
}

function contentUpdateSummaryFromEvents(events, days = state.contentUpdates?.period?.days) {
  if (!events.length) return null;
  const counts = {};
  for (const event of events) {
    const eventType = event.display_event_type || event.event_type;
    counts[eventType] = (counts[eventType] || 0) + 1;
  }

  let badge = "";
  let label = "";
  if (counts.new_title) {
    badge = counts.new_title === 1 ? "новый" : `+${counts.new_title} тайтла`;
    label = counts.new_title === 1 ? "Новый тайтл" : `Добавлено ${counts.new_title} тайтла`;
  } else if (counts.new_episode) {
    const count = counts.new_episode;
    const word = russianPlural(count, "серия", "серии", "серий");
    badge = `+${count} ${word}`;
    label = `Добавлено ${count} ${word}`;
  } else if (counts.new_translation) {
    const count = counts.new_translation;
    const word = russianPlural(count, "озвучка", "озвучки", "озвучек");
    badge = `+${count} ${word}`;
    label = `Добавлено ${count} ${word}`;
  } else {
    const count = counts.new_provider || events.length;
    const word = russianPlural(count, "плеер", "плеера", "плееров");
    badge = `+${count} ${word}`;
    label = `Добавлено ${count} ${word}`;
  }

  return {
    badge,
    label,
    count: events.length,
    event_counts: counts,
    latest_at: events[0]?.occurred_at,
    days,
  };
}

function recentUpdateSummary(item) {
  if (item?.recent_update_summary) return item.recent_update_summary;
  if (isUpdatesView() && state.contentUpdatesLoaded) {
    return contentUpdateSummaryFromEvents(itemContentUpdateEvents(item));
  }
  return null;
}

function hasRecentUpdates(item) {
  return Boolean(recentUpdateSummary(item)?.count);
}

function recentUpdateBadgeText(item) {
  return recentUpdateSummary(item)?.badge || "";
}

function updateTimeLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const diffDays = frontendRuntime.localCalendarDayDifference(now, date);
  if (diffDays <= 0) return "сегодня";
  if (diffDays === 1) return "вчера";
  return `${diffDays} дн. назад`;
}

function updateEventTitle(event) {
  const eventType = event.display_event_type || event.event_type;
  if (eventType === "new_title") return "Новый тайтл";
  if (eventType === "new_episode") {
    return event.episode_number ? `Добавлена ${event.episode_number} серия` : "Добавлена серия";
  }
  if (eventType === "new_translation") {
    return `Новая озвучка${event.translation_title ? `: ${event.translation_title}` : ""}`;
  }
  if (eventType === "new_provider") {
    return `Новый плеер${event.provider_title ? `: ${event.provider_title}` : ""}`;
  }
  return event.description || "Обновление";
}

function updateEventMeta(event) {
  return [
    event.episode_number ? `серия ${event.episode_number}` : "",
    event.translation_title,
    event.provider_title,
    sourceLabel(event.source),
    updateTimeLabel(event.occurred_at),
  ].filter(Boolean).join(" · ");
}

function isRecommendationView() {
  return state.viewMode === "recommendations";
}

function isUpdatesView() {
  return state.viewMode === "updates";
}

function recommendationFor(id) {
  return state.recommendations.find(item => String(item.id) === String(id)) || null;
}

function invalidateRecommendations() {
  state.recommendationsLoaded = false;
  state.recommendationsQueryKey = null;
  state.recommendationProfile = null;
  state.recommendationsError = null;
  state.recommendationsRequestId += 1;
}

function recommendationFilterParams() {
  const params = new URLSearchParams({ limit: String(RECOMMENDATION_LIMIT) });
  for (const key of ["genre", "year", "kind", "status", "source", "video"]) {
    const value = state.filters[key];
    if (value != null && value !== "" && value !== "any") params.set(key, value);
  }
  return params;
}

function currentRecommendationQueryKey() {
  return recommendationFilterParams().toString();
}

function refreshRecommendationsForCriteria() {
  if (!isRecommendationView()) return;
  loadRecommendationsForView({ force: true, selectFirst: true });
}

function supersedeRecommendationsRequest() {
  state.recommendationsRequestId += 1;
  state.recommendationsLoading = null;
  state.recommendationsError = null;
}

function contentUpdateItemsForView() {
  const catalogById = new Map(state.anime.map(item => [String(item.id), item]));
  return (state.contentUpdates?.items || []).map(update => {
    const current = catalogById.get(String(update.id)) || {};
    const item = {
      ...current,
      ...update,
      events: [...(update.events || [])],
      report: update.report ? { ...update.report } : null,
    };
    for (const field of USER_STATE_RESPONSE_FIELDS) {
      if (Object.prototype.hasOwnProperty.call(current, field)) item[field] = current[field];
    }
    item.is_priority = Boolean(item.is_favorite)
      || ["watching", "paused"].includes(effectiveWatchStatus(item));
    return item;
  });
}

function baseItemsForView() {
  if (isUpdatesView()) return state.contentUpdatesLoaded ? contentUpdateItemsForView() : [];
  if (!isRecommendationView()) return state.anime;
  return state.recommendationsLoaded || (state.recommendationsLoading && state.recommendations.length)
    ? state.recommendations
    : [];
}

function itemMatchesView(item, mode = state.viewMode) {
  if (mode === "recommendations") return true;
  if (mode === "updates") return state.contentUpdatesLoaded && hasRecentUpdates(item);
  if (mode === "favorites") return Boolean(item.is_favorite);
  const status = effectiveWatchStatus(item);
  if (mode === "progress") return status === "watching" || status === "paused";
  return true;
}

function currentShelfTotal() {
  if (isUpdatesView()) {
    return state.contentUpdates?.summary?.updated_title_count
      ?? state.anime.filter(item => itemMatchesView(item)).length;
  }
  return baseItemsForView().filter(item => itemMatchesView(item)).length;
}

function currentShelfCountLabel(total) {
  const labels = {
    favorites: "избранных",
    progress: "в просмотре",
  };
  if (isRecommendationView()) return `${state.filtered.length} из ${total} советов`;
  if (isUpdatesView()) return `${state.filtered.length} из ${total} обновл.`;
  return `${state.filtered.length} из ${total} ${labels[state.viewMode] || "тайтлов"}`;
}

function normalizeOptionValues(value) {
  const values = Array.isArray(value) ? value : [value];
  return values
    .map(item => (item == null ? "" : String(item).trim()))
    .filter(Boolean);
}

function countedOptions(items, extractor, labeler = value => value, sorter = null) {
  const counts = new Map();
  for (const item of items) {
    for (const value of normalizeOptionValues(extractor(item))) {
      const entry = counts.get(value) || { value, label: labeler(value), count: 0 };
      entry.count += 1;
      counts.set(value, entry);
    }
  }
  const options = [...counts.values()].map(option => ({
    ...option,
    label: `${option.label} (${option.count})`,
  }));
  return options.sort(sorter || ((a, b) => b.count - a.count || a.label.localeCompare(b.label, "ru")));
}

const filterDefinitions = [
  {
    id: "genre",
    label: "Жанр",
    allLabel: "Все жанры",
    options: items => countedOptions(items, item => item.genres || []),
    match: (item, value) => !value || (item.genres || []).includes(value),
  },
  {
    id: "year",
    label: "Год",
    allLabel: "Все годы",
    options: items => countedOptions(
      items,
      item => yearValue(item),
      value => value,
      (a, b) => Number(b.value) - Number(a.value)
    ),
    match: (item, value) => !value || String(yearValue(item)) === String(value),
  },
  {
    id: "kind",
    label: "Тип",
    allLabel: "Все типы",
    options: items => countedOptions(items, item => item.kind),
    match: (item, value) => !value || item.kind === value,
  },
  {
    id: "status",
    label: "Статус",
    allLabel: "Все статусы",
    options: items => countedOptions(items, item => item.status),
    match: (item, value) => !value || item.status === value,
  },
  {
    id: "source",
    label: "Источник",
    allLabel: "Все источники",
    options: items => countedOptions(items, item => itemSources(item), sourceLabel),
    match: (item, value) => !value || itemSources(item).includes(value),
  },
  {
    id: "video",
    label: "Видео",
    allLabel: "Любые видео",
    options: () => [
      { value: "with", label: "Есть видео" },
      { value: "missing", label: "Без видео" },
    ],
    match: (item, value) => {
      if (value === "with") return (item.source_count || 0) > 0;
      if (value === "missing") return (item.source_count || 0) === 0;
      return true;
    },
  },
];

const sortDefinitions = [
  {
    id: "favorite_added",
    label: "Добавлено",
    defaultDir: "desc",
    type: "number",
    value: item => dateValue(item.favorite_updated_at || item.updated_at),
  },
  {
    id: "watch_recent",
    label: "Недавно смотрел",
    defaultDir: "desc",
    type: "number",
    value: item => dateValue(
      item.watch_last_seen_at
      || item.last_watch?.last_seen_at
      || item.watch_status_updated_at
      || item.updated_at
    ),
  },
  {
    id: "rating_best",
    label: "Лучший",
    defaultDir: "desc",
    type: "number",
    value: bestScore,
  },
  {
    id: "aggregate_score",
    label: "Сайт",
    defaultDir: "desc",
    type: "number",
    value: item => numericValue(item.aggregate_score),
  },
  {
    id: "listing_score",
    label: "Список",
    defaultDir: "desc",
    type: "number",
    value: item => numericValue(item.listing_score),
  },
  {
    id: "aggregate_count",
    label: "Оценок",
    defaultDir: "desc",
    type: "number",
    value: item => numericValue(item.aggregate_count),
  },
  {
    id: "year",
    label: "Год",
    defaultDir: "desc",
    type: "number",
    value: yearValue,
  },
  {
    id: "videos",
    label: "Видео",
    defaultDir: "desc",
    type: "number",
    value: item => numericValue(item.available_episode_count) ?? numericValue(item.source_count),
  },
  {
    id: "title",
    label: "Название",
    defaultDir: "asc",
    type: "string",
    value: item => item.title || "",
  },
];

function dateValue(value) {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function viewSortDefault(mode = state.viewMode) {
  return VIEW_SORT_DEFAULTS[mode] || VIEW_SORT_DEFAULTS.all;
}

function rememberCurrentViewSort() {
  if (fixedSortForView()) return;
  state.viewSorts[state.viewMode] = { by: state.sortBy, dir: state.sortDir };
}

function restoreViewSort(mode = state.viewMode) {
  const saved = state.viewSorts[mode] || viewSortDefault(mode);
  state.sortBy = saved.by;
  state.sortDir = saved.dir;
}

function sortDefinition(id = state.sortBy) {
  return sortDefinitions.find(item => item.id === id)
    || sortDefinitions.find(item => item.id === DEFAULT_SORT_BY);
}

function sortDefinitionsForView(mode = state.viewMode) {
  const shelfSort = {
    favorites: "favorite_added",
    progress: "watch_recent",
  }[mode];
  const shelfOnly = new Set(["favorite_added", "watch_recent"]);
  return sortDefinitions.filter(definition => definition.id === shelfSort || !shelfOnly.has(definition.id));
}

function compareAnime(left, right) {
  const definition = sortDefinition();
  const leftValue = definition.value(left);
  const rightValue = definition.value(right);
  const leftMissing = leftValue == null || leftValue === "";
  const rightMissing = rightValue == null || rightValue === "";

  if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
  if (!leftMissing) {
    let result = 0;
    if (definition.type === "string") {
      result = String(leftValue).localeCompare(String(rightValue), "ru", { sensitivity: "base" });
    } else {
      result = Number(leftValue) - Number(rightValue);
    }
    if (result !== 0) return state.sortDir === "desc" ? -result : result;
  }

  return String(left.title || "").localeCompare(String(right.title || ""), "ru", { sensitivity: "base" });
}

function compareRecommendations(left, right) {
  const scoreDiff = (numericValue(right.recommendation_score) || 0) - (numericValue(left.recommendation_score) || 0);
  if (scoreDiff !== 0) return scoreDiff;
  const rankDiff = (numericValue(left.recommendation_rank) || 9999) - (numericValue(right.recommendation_rank) || 9999);
  if (rankDiff !== 0) return rankDiff;
  return String(left.title || "").localeCompare(String(right.title || ""), "ru", { sensitivity: "base" });
}

function compareContentUpdates(left, right) {
  const leftPriority = Boolean(left.is_favorite) || ["watching", "paused"].includes(effectiveWatchStatus(left));
  const rightPriority = Boolean(right.is_favorite) || ["watching", "paused"].includes(effectiveWatchStatus(right));
  if (leftPriority !== rightPriority) return leftPriority ? -1 : 1;
  const leftSummary = recentUpdateSummary(left);
  const rightSummary = recentUpdateSummary(right);
  const dateDiff = String(rightSummary?.latest_at || "").localeCompare(String(leftSummary?.latest_at || ""));
  if (dateDiff !== 0) return dateDiff;
  return String(left.title || "").localeCompare(String(right.title || ""), "ru", { sensitivity: "base" });
}

function filterOptionLabel(definition, value) {
  if (!value || value === "any") return "";
  const option = definition.options(state.anime).find(item => String(item.value) === String(value));
  return option ? option.label.replace(/\s+\(\d+\)$/, "") : value;
}

function activeFilterDefinitions() {
  return state.activeFilterIds
    .map(id => filterDefinitions.find(definition => definition.id === id))
    .filter(Boolean);
}

function renderAddFilterControl() {
  const available = filterDefinitions.filter(definition => !state.activeFilterIds.includes(definition.id));
  el.addFilter.replaceChildren();

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = available.length ? "+ добавить" : "Все добавлены";
  el.addFilter.append(placeholder);

  for (const definition of available) {
    const option = document.createElement("option");
    option.value = definition.id;
    option.textContent = definition.label;
    el.addFilter.append(option);
  }

  el.addFilter.value = "";
  el.addFilter.disabled = available.length === 0;
}

function renderFilterControls() {
  state.filterControls = {};
  el.filterGrid.replaceChildren();
  el.filterGrid.hidden = state.activeFilterIds.length === 0;

  for (const definition of activeFilterDefinitions()) {
    const row = document.createElement("div");
    row.className = "filter-control";

    const label = document.createElement("label");
    label.className = "tool-field";

    const caption = document.createElement("span");
    caption.textContent = definition.label;

    const select = document.createElement("select");
    select.dataset.filter = definition.id;

    const all = document.createElement("option");
    all.value = definition.id === "video" ? "any" : "";
    all.textContent = definition.allLabel;
    select.append(all);

    for (const item of definition.options(state.anime)) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      select.append(option);
    }

    select.value = state.filters[definition.id] || all.value;
    select.addEventListener("change", () => {
      state.filters[definition.id] = select.value;
      applyFilter({ selectFirst: true });
      refreshRecommendationsForCriteria();
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-button remove-filter";
    remove.textContent = "×";
    remove.title = `Убрать фильтр: ${definition.label}`;
    remove.setAttribute("aria-label", `Убрать фильтр: ${definition.label}`);
    remove.addEventListener("click", () => {
      state.filters[definition.id] = DEFAULT_FILTERS[definition.id];
      state.activeFilterIds = state.activeFilterIds.filter(id => id !== definition.id);
      renderFilterControls();
      renderAddFilterControl();
      applyFilter({ selectFirst: true });
      refreshRecommendationsForCriteria();
    });

    state.filterControls[definition.id] = select;
    label.append(caption, select);
    row.append(label, remove);
    el.filterGrid.append(row);
  }

  renderAddFilterControl();
}

function renderSortControls() {
  const fixedSort = fixedSortForView();
  if (fixedSort) {
    const option = document.createElement("option");
    option.value = fixedSort.id;
    option.textContent = fixedSort.label;
    el.sortBy.replaceChildren(option);
    el.sortBy.value = fixedSort.id;
    el.sortBy.disabled = true;
    el.sortBy.dataset.sortMode = "fixed";
    el.sortBy.title = fixedSort.description;
    el.sortBy.setAttribute("aria-label", fixedSort.description);
    renderSortDirection();
    return;
  }

  el.sortBy.replaceChildren(...sortDefinitionsForView().map(definition => {
    const option = document.createElement("option");
    option.value = definition.id;
    option.textContent = definition.label;
    return option;
  }));
  el.sortBy.value = state.sortBy;
  el.sortBy.disabled = false;
  delete el.sortBy.dataset.sortMode;
  el.sortBy.removeAttribute("title");
  el.sortBy.setAttribute("aria-label", "Сортировка");
  renderSortDirection();
}

function fixedSortForView() {
  if (isRecommendationView()) {
    return {
      id: "recommendation_rank",
      label: "По рекомендации",
      description: "Фиксированная сортировка: лучшие рекомендации сначала",
    };
  }
  if (isUpdatesView()) {
    return {
      id: "update_time",
      label: "По свежести",
      description: "Фиксированная сортировка: новые обновления сначала",
    };
  }
  return null;
}

function renderSortDirection() {
  const fixedSort = fixedSortForView();
  if (fixedSort) {
    el.sortDirToggle.textContent = "↓";
    el.sortDirToggle.disabled = true;
    el.sortDirToggle.dataset.sortMode = "fixed";
    el.sortDirToggle.title = fixedSort.description;
    el.sortDirToggle.setAttribute("aria-label", fixedSort.description);
    return;
  }

  const isDesc = state.sortDir === "desc";
  const label = isDesc ? "По убыванию" : "По возрастанию";
  el.sortDirToggle.disabled = false;
  delete el.sortDirToggle.dataset.sortMode;
  el.sortDirToggle.textContent = isDesc ? "↓" : "↑";
  el.sortDirToggle.title = label;
  el.sortDirToggle.setAttribute("aria-label", label);
}

function renderActiveFilters() {
  el.activeFilters.hidden = true;
  el.activeFilters.replaceChildren();
}

function hasActiveCatalogTools() {
  const query = el.search.value.trim();
  const filtersChanged = Object.keys(DEFAULT_FILTERS).some(key => state.filters[key] !== DEFAULT_FILTERS[key]);
  const defaultSort = viewSortDefault();
  return Boolean(query)
    || filtersChanged
    || state.activeFilterIds.length > 0
    || (!fixedSortForView() && state.sortBy !== defaultSort.by)
    || (!fixedSortForView() && state.sortDir !== defaultSort.dir);
}

function resetCatalogTools() {
  el.search.value = "";
  state.filters = { ...DEFAULT_FILTERS };
  state.activeFilterIds = [];
  const defaultSort = viewSortDefault();
  state.sortBy = defaultSort.by;
  state.sortDir = defaultSort.dir;
  state.viewSorts[state.viewMode] = { ...defaultSort };

  renderFilterControls();
  renderSortControls();
  applyFilter({ selectFirst: true });
  refreshRecommendationsForCriteria();
}

function renderList() {
  const focusedId = el.list.contains(document.activeElement)
    ? document.activeElement?.dataset?.id || null
    : null;
  hideTitleTooltip();
  resetListImageObserver();
  const total = currentShelfTotal();
  el.count.textContent = currentShelfCountLabel(total);
  renderRecommendationMeta();
  el.list.replaceChildren();

  if (!state.filtered.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    if (isRecommendationView() && state.recommendationsLoading) {
      empty.textContent = "Загружаю советы...";
    } else if (isRecommendationView() && state.recommendationsError) {
      empty.textContent = "Не удалось загрузить советы";
    } else if (isUpdatesView() && state.contentUpdatesLoading) {
      empty.textContent = "Загружаю новое...";
    } else if (isUpdatesView() && state.contentUpdatesError) {
      empty.textContent = "Не удалось загрузить новое";
    } else if (isUpdatesView()) {
      empty.textContent = "За выбранный период обновлений нет";
    } else {
      empty.textContent = isRecommendationView() ? "Пока нет рекомендаций" : "Ничего не найдено";
    }
    el.list.append(empty);
    return;
  }

  const selectedIndex = state.filtered.findIndex(item => String(item.id) === String(state.selectedAnimeId));
  const visibleItems = state.filtered.slice(0, state.renderLimit);
  if (selectedIndex >= state.renderLimit) {
    visibleItems.unshift(state.filtered[selectedIndex]);
  }

  for (const item of visibleItems) {
    const button = document.createElement("button");
    button.className = "anime-item";
    button.type = "button";
    button.dataset.id = item.id;
    if (String(item.id) === String(state.selectedAnimeId)) button.classList.add("active");
    if (String(item.id) === String(state.selectedAnimeId)) button.setAttribute("aria-current", "true");
    if (item.is_favorite) button.classList.add("favorite");
    if (item.watched) button.classList.add("watched");
    if (item.not_interested) button.classList.add("not-interested");
    if (item.recommendation_score != null) button.classList.add("recommended");
    if (hasRecentUpdates(item)) button.classList.add("recently-updated");

    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    observeListImage(img, item.cover_url || "");

    const body = document.createElement("div");
    const titleRow = document.createElement("div");
    titleRow.className = "anime-title-row";
    const title = document.createElement("strong");
    const rank = isRecommendationView() && item.recommendation_rank ? `${item.recommendation_rank}. ` : "";
    title.textContent = `${rank}${item.is_favorite ? "★ " : ""}${item.not_interested ? "⊘ " : ""}${item.title}`;
    button.dataset.fullTitle = item.title || title.textContent;
    titleRow.append(title);
    const badgeText = recentUpdateBadgeText(item);
    if (badgeText) {
      const badge = document.createElement("span");
      badge.className = "recent-update-badge";
      badge.textContent = badgeText;
      titleRow.append(badge);
    }
    const meta = document.createElement("span");
    const available = item.available_episode_count || 0;
    const score = ratingText(item);
    const watch = progressText(item);
    const source = sourceLabelList(item);
    if (isRecommendationView()) {
      const recScore = formatRecommendationScore(item.recommendation_score);
      const confidence = item.recommendation_confidence ? `${item.recommendation_confidence}` : "";
      meta.textContent = [recScore, confidence, `${available} видео`, score, source].filter(Boolean).join(" · ");
    } else {
      meta.textContent = [text(item.kind, "тайтл"), `${available} видео`, score, source, watch].filter(Boolean).join(" · ");
    }

    body.append(titleRow, meta);
    if (isRecommendationView()) {
      const note = document.createElement("span");
      note.className = "recommendation-note";
      note.textContent = (item.recommendation_reasons || []).slice(0, 2).join(" · ");
      body.append(note);
    }
    button.append(img, body);
    button.addEventListener("pointerenter", () => showTitleTooltip(button, title));
    button.addEventListener("pointerleave", hideTitleTooltip);
    button.addEventListener("focus", () => showTitleTooltip(button, title));
    button.addEventListener("blur", hideTitleTooltip);
    button.addEventListener("click", () => {
      hideTitleTooltip();
      if (isUpdatesView()) {
        openUpdatedTitle(item).catch(reportActionError("open updated title"));
      } else {
        selectAnime(titleRefForItem(item), { scrollDetail: true, history: "push" })
          .catch(reportActionError("select anime"));
      }
    });
    el.list.append(button);
  }

  if (visibleItems.length < state.filtered.length) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "list-more";
    const remaining = state.filtered.length - visibleItems.length;
    more.textContent = `Показать ещё ${Math.min(RENDER_BATCH_SIZE, remaining)}`;
    more.addEventListener("click", () => {
      state.renderLimit += RENDER_BATCH_SIZE;
      renderList();
    });
    el.list.append(more);
  }

  if (focusedId) {
    el.list.querySelector(`.anime-item[data-id="${CSS.escape(focusedId)}"]`)?.focus({ preventScroll: true });
  }
}

function renderRecommendationMeta() {
  el.recommendationMeta.hidden = !isRecommendationView() && !isUpdatesView();
  el.recommendationMeta.replaceChildren();
  if (isUpdatesView()) {
    renderContentUpdateMeta();
    return;
  }
  if (!isRecommendationView()) return;

  if (state.recommendationsLoading || state.recommendationsError) {
    const item = document.createElement("span");
    item.textContent = state.recommendationsLoading ? "Загружаю советы" : "Советы недоступны";
    el.recommendationMeta.append(item);
    el.recommendationMeta.setAttribute("aria-label", item.textContent);
    return;
  }

  const profile = state.recommendationProfile || {};
  const mode = profile.mode === "personalized" ? "персонально" : "стартовый выбор";
  const seedText = profile.seed_count
    ? `по ${profile.seed_count} выбранным`
    : "без профиля вкусов";
  const watchableText = profile.watchable_candidate_count != null
    ? `${profile.watchable_candidate_count} с видео`
    : "";
  const genres = (profile.top_genres || []).slice(0, 4).map(item => item.genre).join(", ");
  const parts = [`${state.recommendations.length} советов`, mode, seedText];
  if (watchableText) parts.push(watchableText);
  if (genres) parts.push(genres);
  el.recommendationMeta.setAttribute("aria-label", parts.join(" · "));

  for (const part of parts) {
    const item = document.createElement("span");
    item.textContent = part;
    el.recommendationMeta.append(item);
  }
}

function renderContentUpdateMeta() {
  if (state.contentUpdatesLoading || state.contentUpdatesError) {
    const item = document.createElement("span");
    item.textContent = state.contentUpdatesLoading ? "Загружаю новое" : "Новое недоступно";
    el.recommendationMeta.append(item);
    el.recommendationMeta.setAttribute("aria-label", item.textContent);
    return;
  }

  const summary = state.contentUpdates?.summary || {};
  const counts = summary.event_counts || {};
  const period = state.contentUpdates?.period?.label || "";
  const parts = [
    `${summary.event_count || 0} событий`,
    `${summary.updated_title_count || 0} тайтлов`,
    `${counts.new_title || 0} новых`,
    `${counts.new_episode || 0} серий`,
    `${counts.new_translation || 0} озвучек`,
  ];
  if (period) parts.push(period);
  el.recommendationMeta.setAttribute("aria-label", parts.join(" · "));

  for (const part of parts) {
    const item = document.createElement("span");
    item.textContent = part;
    el.recommendationMeta.append(item);
  }
}

function updateDateHeading(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Без даты";
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
}

function updateClockLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function contentUpdateRunLabel(run) {
  if (!run) return "";
  const started = updateTimeLabel(run.started_at);
  const mode = run.mode ? `${run.mode}` : "";
  const status = run.status ? `${run.status}` : "";
  return [status, mode, started].filter(Boolean).join(" · ");
}

function renderContentUpdateControls(parent) {
  const typeRow = document.createElement("div");
  typeRow.className = "updates-control-row";
  for (const option of CONTENT_UPDATE_TYPES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "updates-segment";
    button.classList.toggle("active", state.contentUpdateType === option.id);
    button.setAttribute("aria-pressed", state.contentUpdateType === option.id ? "true" : "false");
    button.textContent = option.label;
    button.addEventListener("click", () => {
      if (state.contentUpdateType === option.id) return;
      state.contentUpdateType = option.id;
      resetContentUpdatesForQuery();
      loadContentUpdatesForView({ force: true });
      applyFilter({ selectFirst: false });
    });
    typeRow.append(button);
  }

  const periodRow = document.createElement("div");
  periodRow.className = "updates-control-row compact";
  for (const option of CONTENT_UPDATE_PERIODS) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "updates-segment";
    button.classList.toggle("active", state.contentUpdateDays === option.id);
    button.setAttribute("aria-pressed", state.contentUpdateDays === option.id ? "true" : "false");
    button.textContent = option.label;
    button.addEventListener("click", () => {
      if (state.contentUpdateDays === option.id) return;
      state.contentUpdateDays = option.id;
      resetContentUpdatesForQuery();
      loadContentUpdatesForView({ force: true });
      applyFilter({ selectFirst: false });
    });
    periodRow.append(button);
  }

  parent.append(typeRow, periodRow);
}

function renderContentUpdateStats(parent, summary) {
  const counts = summary?.event_counts || {};
  const stats = [
    ["Тайтлы", counts.new_title || 0],
    ["Серии", counts.new_episode || 0],
    ["Озвучки", counts.new_translation || 0],
    ["Плееры", counts.new_provider || 0],
  ];
  const grid = document.createElement("div");
  grid.className = "updates-stats";
  for (const [label, value] of stats) {
    const item = document.createElement("div");
    item.className = "updates-stat";
    const number = document.createElement("strong");
    number.textContent = String(value);
    const caption = document.createElement("span");
    caption.textContent = label;
    item.append(number, caption);
    grid.append(item);
  }
  parent.append(grid);
}

function renderContentUpdatePagination(parent) {
  const pagination = state.contentUpdates?.pagination || {};
  if (!pagination.has_more && !state.contentUpdatesLoadingMore && !state.contentUpdatesPageError) return;

  const row = document.createElement("div");
  row.className = "updates-control-row compact";
  if (state.contentUpdatesPageError) {
    const error = document.createElement("span");
    error.className = "updates-empty warn";
    error.textContent = "Не удалось догрузить тайтлы";
    row.append(error);
  }

  if (pagination.has_more) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "updates-segment";
    more.disabled = Boolean(state.contentUpdatesLoadingMore);
    more.textContent = state.contentUpdatesLoadingMore ? "Загружаю..." : "Показать ещё";
    more.addEventListener("click", () => {
      loadMoreContentUpdates().catch(reportActionError("load more content updates"));
    });
    row.append(more);
  }
  parent.append(row);
}

function compactEpisodeNumbers(values, maxParts = 18) {
  const numbers = [...new Set((values || []).map(value => String(value).trim()).filter(Boolean))];
  const parts = [];
  let start = null;
  let end = null;
  let rangeCount = 0;
  const flush = () => {
    if (start == null) return;
    parts.push({ label: start === end ? String(start) : `${start}–${end}`, count: rangeCount });
    start = null;
    end = null;
    rangeCount = 0;
  };
  for (const value of numbers) {
    const parsed = /^\d+$/.test(value) ? Number.parseInt(value, 10) : null;
    if (parsed == null) {
      flush();
      parts.push({ label: value, count: 1 });
    } else if (start == null) {
      start = parsed;
      end = parsed;
      rangeCount = 1;
    } else if (parsed === end + 1) {
      end = parsed;
      rangeCount += 1;
    } else {
      flush();
      start = parsed;
      end = parsed;
      rangeCount = 1;
    }
  }
  flush();

  const visible = parts.slice(0, maxParts);
  const shownCount = visible.reduce((total, part) => total + part.count, 0);
  const labels = visible.map(part => part.label);
  if (shownCount < numbers.length) labels.push(`ещё ${numbers.length - shownCount}`);
  return labels.join(", ");
}

function contentUpdateEntryText(entry) {
  const episodes = entry?.episode_numbers || [];
  const label = [entry?.title || "Без названия", entry?.translation_title].filter(Boolean).join(" — ");
  if (!episodes.length) return label;
  const count = entry?.episode_count || episodes.length;
  const word = russianPlural(count, "серия", "серии", "серий");
  return `${label} (${word} ${compactEpisodeNumbers(episodes)})`;
}

function contentUpdateReportLines(item) {
  const report = item?.report || {};
  const lines = [];
  const newTitle = report.new_title || {};
  if (newTitle.count) {
    const details = [];
    if (newTitle.episode_count) details.push(`${newTitle.episode_count} сер.`);
    if (newTitle.provider_count) {
      details.push(`${newTitle.provider_count} ${russianPlural(newTitle.provider_count, "плеер", "плеера", "плееров")}`);
    }
    if (newTitle.translations?.length) details.push(newTitle.translations.join(", "));
    lines.push(`Новый тайтл${details.length ? `: ${details.join(" · ")}` : ""}`);
  }
  if (report.episode_numbers?.length) {
    const providers = report.new_episode_provider_count
      ? ` · ${report.new_episode_provider_count} ${russianPlural(report.new_episode_provider_count, "плеер", "плеера", "плееров")}`
      : "";
    lines.push(`Новые серии: ${compactEpisodeNumbers(report.episode_numbers)}${providers}`);
  }
  if (report.translations?.length) {
    lines.push(`Новые озвучки: ${report.translations.map(contentUpdateEntryText).join("; ")}`);
  }
  if (report.providers?.length) {
    lines.push(`Новые плееры: ${report.providers.map(contentUpdateEntryText).join("; ")}`);
  }
  if (!lines.length && item?.events?.length) lines.push(updateEventTitle(item.events[0]));
  return lines;
}

function renderContentUpdateRows(parent, items) {
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "updates-empty";
    empty.textContent = "За выбранный период обновлений нет";
    parent.append(empty);
    return;
  }

  let currentDay = "";
  let group = null;
  let priorityGroup = null;
  let regularGroup = null;
  const orderedItems = [
    ...items.filter(item => item.is_priority),
    ...items.filter(item => !item.is_priority),
  ];
  for (const item of orderedItems) {
    if (item.is_priority) {
      if (!priorityGroup) {
        priorityGroup = document.createElement("section");
        priorityGroup.className = "updates-day updates-priority";
        const heading = document.createElement("h3");
        heading.textContent = "Избранное и смотрю";
        priorityGroup.append(heading);
        parent.append(priorityGroup);
      }
      group = priorityGroup;
    } else {
      const day = updateDateHeading(item.latest_update_at);
      if (day !== currentDay) {
        currentDay = day;
        regularGroup = document.createElement("section");
        regularGroup.className = "updates-day";
        const heading = document.createElement("h3");
        heading.textContent = day;
        regularGroup.append(heading);
        parent.append(regularGroup);
      }
      group = regularGroup;
    }

    const row = document.createElement("button");
    row.type = "button";
    row.className = "content-update-row";
    row.dataset.priority = item.is_priority ? "true" : "false";
    row.addEventListener("click", () => {
      const latestEvent = item.events?.[0];
      const request = latestEvent ? openContentUpdateEvent(latestEvent) : openUpdatedTitle(item);
      request.catch(reportActionError("open content update"));
    });

    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    if (item.cover_url) img.src = item.cover_url;

    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title || "Без названия";
    const details = document.createElement("div");
    details.className = "content-update-details";
    for (const text of contentUpdateReportLines(item)) {
      const update = document.createElement("span");
      update.className = "content-update-title";
      update.textContent = text;
      details.append(update);
    }
    const meta = document.createElement("span");
    meta.className = "content-update-meta";
    meta.textContent = [
      item.subtitle,
      item.is_favorite ? "в избранном" : "",
      ["watching", "paused"].includes(effectiveWatchStatus(item)) ? "смотрю" : "",
      `${item.report?.event_count || item.events?.length || 0} событий`,
      updateClockLabel(item.latest_update_at),
    ].filter(Boolean).join(" · ");
    body.append(title, details, meta);
    row.append(img, body);
    group.append(row);
  }
}

function renderContentUpdatesView() {
  el.titleDetailView.hidden = true;
  el.updatesView.hidden = false;
  if (el.player.getAttribute("src")) clearPlayer("");
  el.updatesView.replaceChildren();

  const header = document.createElement("div");
  header.className = "updates-header";
  const heading = document.createElement("h2");
  heading.textContent = "Новое в базе";
  const meta = document.createElement("span");
  meta.textContent = [
    state.contentUpdates?.period?.label,
    contentUpdateRunLabel(state.contentUpdates?.latest_run),
  ].filter(Boolean).join(" · ");
  header.append(heading, meta);
  el.updatesView.append(header);

  renderContentUpdateControls(el.updatesView);

  if (state.contentUpdatesLoading && !state.contentUpdatesLoaded) {
    const loading = document.createElement("div");
    loading.className = "updates-empty";
    loading.textContent = "Загружаю новое...";
    el.updatesView.append(loading);
    return;
  }

  if (state.contentUpdatesError && !state.contentUpdatesLoaded) {
    const error = document.createElement("div");
    error.className = "updates-empty warn";
    error.textContent = "Не удалось загрузить новое";
    el.updatesView.append(error);
    return;
  }

  const items = state.filtered;
  const totals = state.contentUpdates?.summary || {};
  const summary = document.createElement("div");
  summary.className = "updates-summary";
  const count = document.createElement("strong");
  count.textContent = `${totals.event_count || 0} событий`;
  const titles = document.createElement("span");
  titles.textContent = `${totals.updated_title_count || 0} тайтлов`;
  const type = document.createElement("span");
  type.textContent = contentUpdateTypeLabel(state.contentUpdateType);
  summary.append(count, titles, type);
  if (items.length < (totals.updated_title_count || 0)) {
    const loaded = document.createElement("span");
    loaded.textContent = `Загружено ${items.length}`;
    summary.append(loaded);
  }
  el.updatesView.append(summary);

  renderContentUpdateStats(el.updatesView, totals);
  renderContentUpdateRows(el.updatesView, items);
  renderContentUpdatePagination(el.updatesView);
}

function descriptionIsClampedLayout() {
  return Boolean(window.matchMedia?.("(max-width: 640px)").matches);
}

function updateDescriptionToggle() {
  el.descriptionToggle.hidden = !descriptionIsClampedLayout() || !state.descriptionCanExpand;
  el.descriptionToggle.textContent = state.descriptionExpanded ? "Свернуть" : "Показать полностью";
  el.descriptionToggle.setAttribute("aria-expanded", state.descriptionExpanded ? "true" : "false");
}

function renderDescription(detail) {
  const description = detail?.description || "";
  const measureId = state.descriptionMeasureId + 1;
  state.descriptionMeasureId = measureId;
  el.description.textContent = description;
  el.description.classList.toggle("expanded", state.descriptionExpanded);
  updateDescriptionToggle();
  if (!descriptionIsClampedLayout() || state.descriptionExpanded || !description) return;

  // Line-clamp exposes the full content through scrollHeight, so measure the
  // rendered box instead of guessing from character count.
  window.requestAnimationFrame(() => {
    if (measureId !== state.descriptionMeasureId || state.detail !== detail) return;
    state.descriptionCanExpand = el.description.scrollHeight > el.description.clientHeight + 1;
    updateDescriptionToggle();
  });
}

function renderDetail() {
  if (isUpdatesView()) {
    renderContentUpdatesView();
    return;
  }

  const detail = state.detail;
  if (!detail) return;
  el.updatesView.hidden = true;
  el.titleDetailView.hidden = false;

  el.poster.src = detail.cover_url || "";
  el.poster.alt = detail.title || "";
  const scoreText = ratingText(detail);
  el.meta.textContent = [detail.kind, detail.status, scoreText, sourceLabelList(detail)].filter(Boolean).join(" · ");
  el.title.textContent = detail.title || "";
  el.subtitle.textContent = detail.subtitle || "";
  renderDescription(detail);
  renderWatchState(detail);
  renderRecommendationContext(detail);
  renderRecentUpdates(detail);
  renderFields(detail);

  el.genres.replaceChildren(...(detail.genres || []).map(genre => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = genre;
    return chip;
  }));

  renderEpisodes(detail);

  renderSources();
}

function renderWatchState(detail) {
  el.favoriteToggle.classList.toggle("active", Boolean(detail.is_favorite));
  el.favoriteToggle.setAttribute("aria-pressed", detail.is_favorite ? "true" : "false");
  el.favoriteToggle.textContent = detail.is_favorite ? "★ Избранное" : "☆ Избранное";
  const status = effectiveWatchStatus(detail);
  el.watchedToggle.checked = status === "completed";
  const hasProgress = effectiveProgressEpisodeNumber(detail) != null;
  el.notWatchingButton.hidden = !hasProgress || !["watching", "paused"].includes(status);
  el.notWatchingButton.setAttribute("aria-label", "Убрать тайтл из «Смотрю»");
  el.notInterestedButton.classList.toggle("active", Boolean(detail.not_interested));
  el.notInterestedButton.setAttribute("aria-pressed", detail.not_interested ? "true" : "false");
  el.notInterestedButton.textContent = detail.not_interested ? "⊘ Не интересно" : "⊘ Не интересно";
  el.notInterestedButton.title = detail.not_interested
    ? "Вернуть тайтл в рекомендации"
    : "Не предлагать этот тайтл в рекомендациях";
}

function renderRecommendationContext(detail) {
  const rec = recommendationFor(detail.id);
  el.recommendationContext.replaceChildren();
  if (!rec) {
    el.recommendationContext.hidden = true;
    return;
  }

  el.recommendationContext.hidden = false;
  const header = document.createElement("div");
  header.className = "recommendation-context-header";

  const score = document.createElement("strong");
  score.textContent = `Рейтинг совета ${formatRecommendationScore(rec.recommendation_score)}`;
  header.append(score);
  if (rec.recommendation_confidence) {
    const confidence = document.createElement("span");
    confidence.textContent = `${rec.recommendation_confidence} уверенность`;
    header.append(confidence);
  }

  const reasons = document.createElement("div");
  reasons.className = "recommendation-reasons";
  for (const reason of rec.recommendation_reasons || []) {
    const item = document.createElement("span");
    item.textContent = reason;
    reasons.append(item);
  }

  el.recommendationContext.append(header, reasons);
}

function renderRecentUpdates(detail) {
  const events = detail.recent_updates || [];
  el.recentUpdates.replaceChildren();
  if (!events.length) {
    el.recentUpdates.hidden = true;
    return;
  }

  el.recentUpdates.hidden = false;
  const header = document.createElement("div");
  header.className = "recent-updates-header";
  const title = document.createElement("strong");
  title.textContent = "Новое за 3 дня";
  const summary = document.createElement("span");
  summary.textContent = recentUpdateSummary(detail)?.label || `${events.length} обновлений`;
  header.append(title, summary);

  const list = document.createElement("div");
  list.className = "recent-updates-list";
  for (const event of events.slice(0, 6)) {
    const row = document.createElement(event.episode_id ? "button" : "div");
    row.className = "recent-update-row";
    if (event.episode_id) {
      row.type = "button";
      row.addEventListener("click", () => {
        openContentUpdateEvent(event).catch(reportActionError("select recent update episode"));
      });
    }
    const rowTitle = document.createElement("strong");
    rowTitle.textContent = updateEventTitle(event);
    const meta = document.createElement("span");
    meta.textContent = updateEventMeta(event);
    row.append(rowTitle, meta);
    list.append(row);
  }

  el.recentUpdates.append(header, list);
}

function renderFields(detail) {
  const skip = new Set(["Жанры"]);
  const fields = [];
  if (detail.rating) fields.push(["Рейтинг", detail.rating]);
  if (detail.date_published) fields.push(["Дата", detail.date_published]);
  for (const item of detail.fields || []) {
    if (skip.has(item.label)) continue;
    if (fields.some(([label]) => label === item.label)) continue;
    fields.push([item.label, item.value]);
  }

  el.fields.replaceChildren(...fields.slice(0, 18).map(([label, value]) => {
    const node = document.createElement("div");
    node.className = "field-item";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = value || "-";
    node.append(key, val);
    return node;
  }));
}

function createEpisodeNavButton(label, ariaLabel, episode) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-button episode-nav";
  button.textContent = label;
  button.setAttribute("aria-label", ariaLabel);
  button.title = ariaLabel;
  button.disabled = !episode;
  if (episode) {
    button.addEventListener("click", () => selectEpisode(episode.id).catch(reportActionError("episode nav")));
  }
  return button;
}

function renderEpisodes(detail) {
  const episodes = detail.episodes || [];
  el.episodes.replaceChildren();

  if (!episodes.length) {
    const empty = document.createElement("div");
    empty.className = "episode-empty";
    empty.textContent = "Серии не найдены";
    el.episodes.append(empty);
    return;
  }

  const currentIndex = Math.max(0, selectedEpisodeIndex(episodes));
  const current = episodes[currentIndex];
  const prevEpisode = adjacentAvailableEpisode(episodes, currentIndex, -1);
  const nextEpisode = adjacentAvailableEpisode(episodes, currentIndex, 1);

  const header = document.createElement("div");
  header.className = "episode-picker-header";

  const label = document.createElement("span");
  label.textContent = "Серия";

  const counter = document.createElement("strong");
  counter.textContent = `${currentIndex + 1} из ${episodes.length}`;
  header.append(label, counter);

  const row = document.createElement("div");
  row.className = "episode-picker-row";

  const select = document.createElement("select");
  select.className = "episode-select";
  select.setAttribute("aria-label", "Выбор серии");

  episodes.forEach((episode, index) => {
    const option = document.createElement("option");
    option.value = episode.id;
    option.textContent = episodeOptionText(episode, index);
    option.disabled = (episode.source_count || 0) <= 0;
    option.selected = String(episode.id) === String(state.selectedEpisodeId);
    select.append(option);
  });

  select.addEventListener("change", event => {
    const episode = episodes.find(item => String(item.id) === event.target.value);
    if (episode) selectEpisode(episode.id).catch(reportActionError("episode select"));
  });

  row.append(
    createEpisodeNavButton("‹", "Предыдущая серия", prevEpisode),
    select,
    createEpisodeNavButton("›", "Следующая серия", nextEpisode),
  );

  const summary = document.createElement("div");
  summary.className = "episode-current";
  const title = episodeTitleText(current);
  summary.textContent = title || episodeNumberLabel(current, currentIndex);

  el.episodes.append(header, row, summary);
}

function uniqueTranslations(sources) {
  return frontendRuntime.groupSourcesByTranslation(sources).map(group => ({
    id: group.key,
    title: group.title,
  }));
}

function renderContentSourceOptions() {
  const variants = sourceVariants(state.detail);
  el.contentSource.replaceChildren();

  if (!variants.length) {
    state.selectedContentSource = null;
    el.contentSource.disabled = true;
    return;
  }

  if (!variants.some(variant => variant.source === state.selectedContentSource)) {
    state.selectedContentSource = preferredContentSource(state.detail, state.selectedEpisodeId);
  }

  for (const variant of variants) {
    const option = document.createElement("option");
    option.value = variant.source;
    const available = variant.available_episode_count || 0;
    option.textContent = `${sourceLabel(variant.source)} · ${available} видео`;
    option.selected = variant.source === state.selectedContentSource;
    el.contentSource.append(option);
  }
  el.contentSource.disabled = variants.length < 2;
}

function renderSources() {
  const episode = activeEpisode();
  if (episode && state.selectedContentSource && !contentSourceHasEpisode(state.selectedContentSource, episode.id)) {
    state.selectedContentSource = preferredContentSource(state.detail, episode.id);
    state.selectedTranslation = null;
    state.selectedSourceId = null;
  }
  renderContentSourceOptions();
  const sources = sourcesForEpisode(state.selectedEpisodeId);
  const translations = uniqueTranslations(sources);

  el.translation.replaceChildren();
  el.provider.replaceChildren();
  el.translation.disabled = !sources.length;
  el.provider.disabled = !sources.length;

  if (!episode) {
    clearPlayer("-");
    syncUrlFromDetail();
    return;
  }

  if (!sources.length) {
    clearPlayer(episode.unavailable_reason || "Видео недоступно");
    state.selectedTranslation = null;
    state.selectedSourceId = null;
    syncUrlFromDetail();
    return;
  }

  const selected = frontendRuntime.selectSourceForEpisode(sources, {
    selectedSourceId: state.selectedSourceId,
    selectedTranslationId: state.selectedTranslation,
    preference: state.sourceSelectionPreference,
  });
  state.selectedTranslation = selected?.translation_id != null
    ? String(selected.translation_id)
    : null;
  state.selectedSourceId = selected?.id != null ? String(selected.id) : null;
  const selectedTranslationKey = frontendRuntime.sourceTranslationKey(selected);
  if (!state.sourceSelectionPreference && selected) {
    state.sourceSelectionPreference = frontendRuntime.sourcePreference(selected);
  }

  for (const item of translations) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.title;
    option.selected = item.id === selectedTranslationKey;
    el.translation.append(option);
  }

  const providers = sources.filter(source => (
    frontendRuntime.sourceTranslationKey(source) === selectedTranslationKey
  ));

  for (const source of providers) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = `${source.provider_title} · ${source.embed_host}`;
    option.selected = String(source.id) === String(state.selectedSourceId);
    el.provider.append(option);
  }

  const activeSource = providers.find(source => String(source.id) === String(state.selectedSourceId)) || providers[0];
  if (activeSource) {
    setPlayer(activeSource, episode);
  } else {
    clearPlayer("Источник недоступен");
  }
  syncUrlFromDetail();
}

function setPlayer(source, episode) {
  const playerUrl = frontendRuntime.safeHttpsUrl(source?.embed_url, playerHosts);
  let playerHost = "";
  try {
    playerHost = playerUrl ? new URL(playerUrl).hostname : "";
  } catch (error) {
    playerHost = "";
  }
  if (!playerUrl || (source.embed_host && !frontendRuntime.hostnameMatches(playerHost, source.embed_host))) {
    clearPlayer("Небезопасный или неизвестный адрес плеера");
    reportClientError(new Error("Rejected player URL"), {
      action: "validate player URL",
      embedHost: source?.embed_host || "",
      playerHost,
    });
    return;
  }
  ensureWatchSession(source, episode);
  el.wrap.classList.remove("empty");
  configurePlayerIframe(el.player);
  if (el.player.getAttribute("src") !== playerUrl) {
    el.player.src = playerUrl;
  }
  el.host.textContent = source.embed_host || "-";
  el.episodeState.textContent = episode.title && episode.title !== "---" ? episode.title : `${episode.number} серия`;
  el.empty.textContent = "";
}

function clearPlayer(message) {
  clearWatchSession();
  el.player.removeAttribute("src");
  el.wrap.classList.add("empty");
  el.empty.textContent = message;
  el.host.textContent = "-";
  el.episodeState.textContent = "-";
}

function configurePlayerIframe(iframe = el.player) {
  iframe.setAttribute("allow", PLAYER_IFRAME_ALLOW);
  iframe.setAttribute("sandbox", PLAYER_IFRAME_SANDBOX);
  iframe.setAttribute("allowfullscreen", "");
  iframe.setAttribute("webkitallowfullscreen", "");
  iframe.setAttribute("mozallowfullscreen", "");
  iframe.allowFullscreen = true;
  iframe.referrerPolicy = "strict-origin-when-cross-origin";
  return iframe;
}

function newWatchSessionId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function watchContextKey(source, episode) {
  return [
    state.detail?.id,
    episode?.id,
    source?.id,
  ].map(value => value == null ? "" : String(value)).join(":");
}

function playerHasPlaybackEvidence(session = state.watchSession) {
  if (!session) return false;
  const fullscreen = document.fullscreenElement === el.wrap || document.fullscreenElement === el.player;
  return frontendRuntime.hasPlaybackEvidence({
    pageHidden: document.hidden,
    playerFocused: document.hasFocus() && document.activeElement === el.player,
    fullscreen,
    evidenceExpiresAt: session.evidenceExpiresAt,
    now: performanceNow(),
  });
}

function watchPayloadForSession(session, eventType, engagedSeconds = 0) {
  return {
    client_session_id: session.id,
    event_type: eventType,
    anime_id: session.animeId,
    episode_id: session.episodeId,
    episode_number: session.episodeNumber,
    progress_episode_number: session.progressEpisodeNumber,
    video_source_id: session.videoSourceId,
    source: session.source,
    source_anime_id: session.sourceAnimeId,
    translation_id: session.translationId,
    translation_title: session.translationTitle,
    provider_id: session.providerId,
    provider_title: session.providerTitle,
    embed_host: session.embedHost,
    engaged_seconds: Math.max(0, Math.min(WATCH_MAX_DELTA_SECONDS, Math.round(engagedSeconds || 0))),
    page_visible: !document.hidden,
    player_focused: playerHasPlaybackEvidence(session),
  };
}

function animeStateRevision(animeId) {
  return state.userStateFieldRevisions.get(String(animeId))?.animeRevision || 0;
}

function applyWatchState(nextState, animeId = state.selectedAnimeId, requestRevision = animeStateRevision(animeId)) {
  if (!nextState || !animeId) return false;
  if (requestRevision !== animeStateRevision(animeId)) return false;
  const watchState = {};
  for (const field of USER_STATE_RESPONSE_FIELDS) {
    if (Object.prototype.hasOwnProperty.call(nextState, field)) watchState[field] = nextState[field];
  }
  if (!Object.keys(watchState).length) return false;
  updateConfirmedUserState(animeId, watchState);
  const applyToItem = item => {
    if (!item) return { changed: false, semanticChanged: false };
    const changed = frontendRuntime.patchChanges(item, watchState);
    const semanticChanged = frontendRuntime.patchChanges(
      item,
      watchState,
      RECOMMENDATION_SEMANTIC_FIELDS,
    );
    Object.assign(item, watchState);
    return { changed, semanticChanged };
  };

  const catalogChanged = applyToItem(state.anime.find(entry => entry.id === animeId));
  const recommendationsChanged = applyToItem(state.recommendations.find(entry => entry.id === animeId));
  const detailChanged = state.detail?.id === animeId
    ? applyToItem(state.detail)
    : { changed: false, semanticChanged: false };
  const changed = catalogChanged.changed || recommendationsChanged.changed || detailChanged.changed;
  const semanticChanged = catalogChanged.semanticChanged
    || recommendationsChanged.semanticChanged
    || detailChanged.semanticChanged;

  if (!changed) return false;
  if (!semanticChanged) return false;
  invalidateRecommendations();
  if (isRecommendationView()) loadRecommendationsForView({ force: true, selectFirst: false });
  applyFilter();
  if (state.detail?.id === animeId) renderWatchState(state.detail);
  return true;
}

function postWatchPayload(payload, { beacon = false } = {}) {
  if (beacon && navigator.sendBeacon) {
    const body = new Blob([JSON.stringify(payload)], { type: "application/json" });
    if (navigator.sendBeacon(WATCH_ENDPOINT, body)) return Promise.resolve(null);
  }
  return fetch(WATCH_ENDPOINT, {
    method: "POST",
    credentials: "same-origin",
    keepalive: beacon,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).then(async response => {
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  });
}

function sendWatchEvent(eventType, { engagedSeconds = 0, beacon = false, session = state.watchSession } = {}) {
  if (!session) return Promise.resolve(null);
  const payload = watchPayloadForSession(session, eventType, engagedSeconds);
  const requestRevision = animeStateRevision(session.animeId);
  return postWatchPayload(payload, { beacon })
    .then(result => {
      const semanticChanged = result?.state
        ? applyWatchState(result.state, session.animeId, requestRevision)
        : false;
      if (result?.recommendation_signal_changed && !semanticChanged) {
        invalidateRecommendations();
        if (isRecommendationView()) {
          loadRecommendationsForView({ force: true, selectFirst: false });
        }
      }
      return result;
    })
    .catch(error => {
      if (!beacon) reportClientError(error, { action: "watch event", eventType });
    });
}

function consumeWatchEngagedSeconds({ stop = false } = {}) {
  const session = state.watchSession;
  if (!session?.engaged || !session.activeSince) return 0;
  const now = performanceNow();
  const seconds = frontendRuntime.boundedElapsedSeconds(
    session.activeSince,
    now,
    WATCH_MAX_DELTA_SECONDS,
  );
  session.activeSince = stop || document.hidden ? null : now;
  return seconds;
}

function startWatchHeartbeat() {
  if (state.watchHeartbeatTimer) return;
  state.watchHeartbeatTimer = window.setInterval(() => {
    const session = state.watchSession;
    if (!session?.engaged || document.hidden) return;
    if (!playerHasPlaybackEvidence(session)) {
      session.engaged = false;
      session.activeSince = null;
      stopWatchHeartbeat();
      return;
    }
    const seconds = consumeWatchEngagedSeconds();
    if (seconds > 0) {
      sendWatchEvent("heartbeat", { engagedSeconds: seconds }).catch(() => {});
    }
  }, WATCH_HEARTBEAT_MS);
}

function stopWatchHeartbeat() {
  if (!state.watchHeartbeatTimer) return;
  window.clearInterval(state.watchHeartbeatTimer);
  state.watchHeartbeatTimer = null;
}

function markWatchEngaged(eventType) {
  const session = state.watchSession;
  if (!session) return;
  const playbackEvidence = ["player_engaged", "fullscreen_enter", "pip_open"].includes(eventType);
  if (playbackEvidence) {
    const now = performanceNow();
    session.evidenceExpiresAt = now + WATCH_EVIDENCE_MAX_AGE_MS;
    if (!session.engaged) {
      session.engaged = true;
      session.activeSince = now;
    } else if (!session.activeSince) {
      session.activeSince = now;
    }
    startWatchHeartbeat();
  }
  sendWatchEvent(eventType).catch(() => {});
}

function flushWatchSession(eventType = "session_end", { beacon = false } = {}) {
  const session = state.watchSession;
  if (!session) return;
  const seconds = consumeWatchEngagedSeconds({ stop: true });
  if (session.engaged || eventType !== "session_end") {
    sendWatchEvent(eventType, { engagedSeconds: seconds, beacon, session }).catch(() => {});
  }
  session.engaged = false;
  session.evidenceExpiresAt = 0;
  stopWatchHeartbeat();
}

function ensureWatchSession(source, episode) {
  if (!source || !episode || !state.detail) return null;
  const key = watchContextKey(source, episode);
  if (state.watchSession?.key === key) return state.watchSession;

  flushWatchSession("session_end", { beacon: true });
  const session = {
    id: newWatchSessionId(),
    key,
    animeId: state.detail.id,
    episodeId: episode.id,
    episodeNumber: episode.number,
    progressEpisodeNumber: numberFrom(episode.number),
    videoSourceId: source.id,
    source: source.source,
    sourceAnimeId: source.source_anime_id,
    translationId: source.translation_id,
    translationTitle: source.translation_title,
    providerId: source.provider_id,
    providerTitle: source.provider_title,
    embedHost: source.embed_host,
    engaged: false,
    activeSince: null,
    evidenceExpiresAt: 0,
  };
  state.watchSession = session;
  return session;
}

function clearWatchSession({ beacon = true } = {}) {
  if (!state.watchSession) return;
  flushWatchSession("session_end", { beacon });
  state.watchSession = null;
  stopWatchHeartbeat();
}

function discardWatchSession() {
  state.watchSession = null;
  stopWatchHeartbeat();
}

function handlePlayerLoaded() {
  sendWatchEvent("player_loaded").catch(() => {});
}

function handlePlayerEngaged() {
  markWatchEngaged("player_engaged");
}

function handleVisibilityChange() {
  if (document.hidden) {
    flushWatchSession("page_hidden", { beacon: true });
  } else if (document.activeElement === el.player) {
    handlePlayerEngaged();
  }
}

function handleFullscreenStateChange() {
  const active = document.fullscreenElement === el.wrap || document.fullscreenElement === el.player;
  updateFullscreenControl();
  if (active && !state.watchFullscreenActive) markWatchEngaged("fullscreen_enter");
  state.watchFullscreenActive = active;
}

function setPlayerActionState(message = "", tone = "") {
  el.playerActionState.textContent = message;
  el.playerActionState.dataset.tone = tone;
}

function updateFullscreenControl() {
  const active = document.fullscreenElement === el.wrap || document.fullscreenElement === el.player;
  el.fullscreenToggle.classList.toggle("active", active);
  el.fullscreenToggle.title = active ? "Выйти из полного экрана" : "Во весь экран";
  el.fullscreenToggle.setAttribute("aria-label", el.fullscreenToggle.title);
  el.fullscreenToggle.setAttribute("aria-pressed", active ? "true" : "false");
}

async function toggleFullscreen() {
  if (!el.player.getAttribute("src")) {
    setPlayerActionState("Нет активного видео", "warn");
    return;
  }
  if (!el.wrap.requestFullscreen) {
    setPlayerActionState("Браузер не поддерживает fullscreen", "warn");
    return;
  }

  try {
    if (document.fullscreenElement === el.wrap) {
      await document.exitFullscreen();
    } else {
      await el.wrap.requestFullscreen();
    }
    setPlayerActionState("");
    updateFullscreenControl();
  } catch (error) {
    reportClientError(error, { action: "fullscreen" });
    setPlayerActionState(error.message || "Не удалось открыть fullscreen", "warn");
  }
}

function isEditableShortcutTarget(target) {
  if (!target || target === document.body) return false;
  if (target.isContentEditable) return true;
  return Boolean(target.closest?.("input, textarea, select, [contenteditable='true']"));
}

function isFullscreenHotkey(event) {
  if (event.defaultPrevented || event.repeat) return false;
  if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
  if (isEditableShortcutTarget(event.target)) return false;
  return event.code === "KeyF" || String(event.key || "").toLowerCase() === "f";
}

async function openPictureInPicture() {
  if (!el.player.getAttribute("src")) {
    setPlayerActionState("Нет активного видео", "warn");
    return;
  }

  // Cloning a cross-origin iframe into Document PiP starts a second player and
  // can double audio and watch tracking. Providers that support PiP expose it
  // inside their own controls, which is the only safe boundary available here.
  setPlayerActionState("PiP доступен в самом плеере", "warn");
}

async function selectAnime(id, options = {}) {
  const requestId = state.detailRequestId + 1;
  state.detailRequestId = requestId;
  state.detailRequestController?.abort();
  const controller = new AbortController();
  state.detailRequestController = controller;
  el.titleDetailView.setAttribute("aria-busy", "true");
  try {
    const detail = await api(`/api/anime/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (requestId !== state.detailRequestId) throw new DOMException("Stale detail request", "AbortError");
    state.detail = detail;
    state.selectedAnimeId = detail.id;
    state.descriptionExpanded = false;
    state.descriptionCanExpand = false;
    if (!userStateSaveQueue.pending(String(detail.id))) {
      updateConfirmedUserState(detail.id, {
        is_favorite: Boolean(detail.is_favorite),
        watched: Boolean(detail.watched),
        progress_episode_number: detail.progress_episode_number ?? null,
        watch_status: effectiveWatchStatus(detail) || null,
        not_interested: Boolean(detail.not_interested),
        updated_at: detail.updated_at || null,
        favorite_updated_at: detail.favorite_updated_at || null,
        watch_status_updated_at: detail.watch_status_updated_at || null,
        not_interested_updated_at: detail.not_interested_updated_at || null,
      });
    }
    applyDetailLinkState(options.linkState || {});

    const previousUrlSync = state.urlSyncSuspended;
    state.urlSyncSuspended = true;
    renderList();
    renderDetail();
    state.urlSyncSuspended = previousUrlSync;
    if (options.updateUrl !== false) syncUrlFromDetail({ replace: options.history !== "push" });
    if (options.scrollDetail) scrollDetailIntoViewForMobile();
    return true;
  } finally {
    if (requestId === state.detailRequestId) {
      state.detailRequestController = null;
      el.titleDetailView.removeAttribute("aria-busy");
    }
  }
}

async function selectEpisode(id, { history = "push", persist = true } = {}) {
  if (!state.sourceSelectionPreference) {
    state.sourceSelectionPreference = frontendRuntime.sourcePreference(selectedSourceForEpisode());
  }
  state.selectedEpisodeId = id;
  // Source row IDs are episode-local. Keep the semantic preference and resolve
  // it against the next episode's backend-ranked source list.
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  const episode = activeEpisode();
  const number = numberFrom(episode?.number);
  const previousUrlSync = state.urlSyncSuspended;
  state.urlSyncSuspended = true;
  renderDetail();
  state.urlSyncSuspended = previousUrlSync;
  syncUrlFromDetail({ replace: history !== "push" });
  if (persist && number != null) {
    const selectedSource = selectedSourceForEpisode();
    markWatchEngaged("episode_selected");
    await saveUserState({
      progress_episode_number: number,
      watched: false,
      watch_status: "watching",
      ...(selectedSource?.id != null ? { video_source_id: selectedSource.id } : {}),
    });
    return;
  }
}

function persistCurrentEpisodeSelection() {
  const episodeNumber = numberFrom(activeEpisode()?.number);
  const source = selectedSourceForEpisode();
  if (episodeNumber == null || source?.id == null) return Promise.resolve(null);
  return saveUserState({
    progress_episode_number: episodeNumber,
    watched: false,
    watch_status: "watching",
    video_source_id: source.id,
  });
}

function userStateTargets(animeId) {
  const key = String(animeId);
  return [
    state.anime.find(entry => String(entry.id) === key),
    state.recommendations.find(entry => String(entry.id) === key),
    ...(state.contentUpdates?.items || []).filter(entry => String(entry.id) === key),
    state.detail && String(state.detail.id) === key ? state.detail : null,
  ].filter(Boolean);
}

function applyLocalUserStatePatch(animeId, patch) {
  for (const target of userStateTargets(animeId)) Object.assign(target, patch);
}

function userStateSnapshot(animeId, patch) {
  const target = userStateTargets(animeId)[0] || {};
  return Object.fromEntries(Object.keys(patch).map(key => [key, target[key]]));
}

function updateConfirmedUserState(animeId, patch) {
  const key = String(animeId);
  const record = state.userStateFieldRevisions.get(key) || { animeRevision: 0, fields: {}, confirmed: {} };
  record.confirmed ||= {};
  Object.assign(record.confirmed, patch);
  state.userStateFieldRevisions.set(key, record);
}

function registerUserStateMutation(animeId, patch, before) {
  const key = String(animeId);
  const revision = state.userStateRevision + 1;
  state.userStateRevision = revision;
  const record = state.userStateFieldRevisions.get(key) || { animeRevision: 0, fields: {}, confirmed: {} };
  record.confirmed ||= {};
  record.animeRevision = revision;
  for (const field of Object.keys(patch)) {
    if (!Object.prototype.hasOwnProperty.call(record.confirmed, field)) record.confirmed[field] = before[field];
    record.fields[field] = revision;
  }
  state.userStateFieldRevisions.set(key, record);
  return revision;
}

function mutationStillCurrent(animeId, field, revision) {
  return state.userStateFieldRevisions.get(String(animeId))?.fields?.[field] === revision;
}

function userStatePatchChangesLocalState(animeId, patch, { includeObjects = true } = {}) {
  const targets = userStateTargets(animeId);
  return Object.entries(patch).some(([field, value]) => {
    if (!includeObjects && value && typeof value === "object") return false;
    return targets.some(target => !Object.is(target[field], value));
  });
}

function rerenderAfterUserState(animeId, { list = true, detail = true } = {}) {
  if (list) applyFilter();
  if (detail && String(state.detail?.id) === String(animeId)) renderDetail();
}

const userStateSaveQueue = frontendRuntime.createKeyedSerialQueue(async (animeKey, mutation) => {
  const { patch, requestPatch, before, revision } = mutation;
  try {
    const payload = await api(`/api/anime/${encodeURIComponent(animeKey)}/state`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestPatch),
    });
    const confirmedRecord = state.userStateFieldRevisions.get(String(animeKey));
    const confirmed = {};
    const mutationIsLatest = confirmedRecord?.animeRevision === revision;
    for (const field of USER_STATE_RESPONSE_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(payload.state || {}, field)) continue;
      const belongsToPatch = Object.prototype.hasOwnProperty.call(patch, field);
      if (confirmedRecord && (belongsToPatch || mutationIsLatest)) {
        confirmedRecord.confirmed[field] = payload.state[field];
      }
      if ((belongsToPatch && mutationStillCurrent(animeKey, field, revision)) || (!belongsToPatch && mutationIsLatest)) {
        confirmed[field] = payload.state[field];
      }
    }
    if (
      "progress_episode_number" in patch
      && mutationStillCurrent(animeKey, "progress_episode_number", revision)
      && Object.prototype.hasOwnProperty.call(payload.state || {}, "last_watch")
    ) {
      confirmed.last_watch = payload.state.last_watch;
    }
    const listChanged = userStatePatchChangesLocalState(animeKey, confirmed, { includeObjects: false });
    const detailChanged = userStatePatchChangesLocalState(animeKey, confirmed);
    applyLocalUserStatePatch(animeKey, confirmed);
    if (["is_favorite", "watched", "progress_episode_number", "watch_status", "not_interested"].some(field => field in patch)) {
      state.recommendationsDirtyConfirmed = true;
    }
    rerenderAfterUserState(animeKey, { list: listChanged, detail: detailChanged });
    return payload;
  } catch (error) {
    const rollback = {};
    const confirmedRecord = state.userStateFieldRevisions.get(String(animeKey));
    for (const field of Object.keys(patch)) {
      if (mutationStillCurrent(animeKey, field, revision)) {
        rollback[field] = Object.prototype.hasOwnProperty.call(confirmedRecord?.confirmed || {}, field)
          ? confirmedRecord.confirmed[field]
          : before[field];
      }
    }
    applyLocalUserStatePatch(animeKey, rollback);
    rerenderAfterUserState(animeKey);
    const failure = new Error(`Не удалось сохранить изменения: ${error?.message || "ошибка сети"}`);
    failure.cause = error;
    throw failure;
  }
});

async function saveUserState(patch, animeId = state.selectedAnimeId) {
  if (!animeId || !patch || !Object.keys(patch).length) return null;
  const requestPatch = { ...patch };
  const normalizedPatch = Object.fromEntries(
    Object.entries(requestPatch).filter(([field]) => !USER_STATE_TRANSPORT_FIELDS.has(field))
  );
  if (!Object.keys(normalizedPatch).length) return null;
  const before = userStateSnapshot(animeId, normalizedPatch);
  const revision = registerUserStateMutation(animeId, normalizedPatch, before);
  applyLocalUserStatePatch(animeId, normalizedPatch);
  const affectsRecommendations = ["is_favorite", "watched", "progress_episode_number", "watch_status", "not_interested"]
    .some(key => key in normalizedPatch);
  if (affectsRecommendations) {
    supersedeRecommendationsRequest();
  }
  applyFilter();
  if (String(state.detail?.id) === String(animeId)) renderDetail();
  const animeKey = String(animeId);
  try {
    return await userStateSaveQueue.enqueue(animeKey, {
      patch: normalizedPatch,
      requestPatch,
      before,
      revision,
    });
  } finally {
    if (userStateSaveQueue.pending() === 0 && state.recommendationsDirtyConfirmed) {
      state.recommendationsDirtyConfirmed = false;
      invalidateRecommendations();
      if (isRecommendationView()) {
        loadRecommendationsForView({ force: true, selectFirst: false });
      }
    } else if (
      userStateSaveQueue.pending() === 0
      && isRecommendationView()
      && !state.recommendationsLoaded
      && !state.recommendationsLoading
    ) {
      loadRecommendationsForView({ force: true, selectFirst: false });
    }
  }
}

function applyFilter({ selectFirst = false } = {}) {
  const query = catalogSearch.searchQuery(el.search.value.trim());
  const baseItems = baseItemsForView();
  state.renderLimit = INITIAL_RENDER_LIMIT;
  const fallbackCompare = isRecommendationView()
    ? compareRecommendations
    : isUpdatesView()
      ? compareContentUpdates
      : compareAnime;

  state.filtered = baseItems.map((item, index) => {
    const searchScore = query.tokens.length ? catalogSearch.scoreSearchItem(item, query) : 0;
    return { item, index, searchScore };
  }).filter(entry => {
    const item = entry.item;
    const queryMatch = !query.tokens.length || entry.searchScore > 0;
    const viewMatch = itemMatchesView(item);
    const filterMatch = filterDefinitions.every(definition => (
      definition.match(item, state.filters[definition.id])
    ));
    return queryMatch && viewMatch && filterMatch;
  }).sort((left, right) => {
    if (isUpdatesView()) {
      const result = compareContentUpdates(left.item, right.item);
      return result || left.index - right.index;
    }
    if (query.tokens.length && left.searchScore !== right.searchScore) {
      return right.searchScore - left.searchScore;
    }
    const result = fallbackCompare(left.item, right.item);
    return result || left.index - right.index;
  }).map(entry => entry.item);
  renderList();
  renderActiveFilters();
  el.resetFilters.hidden = !hasActiveCatalogTools();
  if (isUpdatesView()) renderContentUpdatesView();

  if (selectFirst && !isUpdatesView() && state.filtered.length && !state.filtered.some(item => item.id === state.selectedAnimeId)) {
    selectAnime(titleRefForItem(state.filtered[0])).catch(reportActionError("select filtered anime"));
  }
}

async function loadSearchFields() {
  const payload = await api("/api/anime/search-fields");
  const fieldsById = new Map(
    (payload.items || []).map(item => [String(item.id), item.search_fields || []])
  );
  state.searchFieldsById = fieldsById;
  applyLoadedSearchFields(state.anime);
  applyLoadedSearchFields(state.recommendations);

  state.searchFieldsLoaded = true;
  state.searchFieldsError = null;
  markPerformanceCheckpoint("search_fields_loaded", { items: fieldsById.size });

  if (el.search.value.trim()) {
    applyFilter({ selectFirst: false });
  }
}

function applyLoadedSearchFields(items) {
  if (!state.searchFieldsById) return;
  for (const item of items || []) {
    setItemSearchFields(item, state.searchFieldsById.get(String(item.id)) || []);
  }
}

function ensureSearchFields() {
  if (state.searchFieldsLoaded) return Promise.resolve();
  if (state.searchFieldsLoading) return state.searchFieldsLoading;

  state.searchFieldsError = null;
  state.searchFieldsLoading = loadSearchFields()
    .catch(error => {
      state.searchFieldsError = error;
      throw error;
    })
    .finally(() => {
      state.searchFieldsLoading = null;
    });
  return state.searchFieldsLoading;
}

function loadSearchFieldsInBackground() {
  ensureSearchFields().catch(reportActionError("load search fields"));
}

async function loadRecommendations(requestId, queryKey) {
  const payload = await api(`/api/recommendations?${queryKey}`);
  if (requestId !== state.recommendationsRequestId) return state.recommendations;
  state.recommendationProfile = payload.profile || null;
  state.recommendations = (payload.items || []).map(item => {
    const local = state.anime.find(entry => entry.id === item.id);
    return local ? { ...local, ...item } : item;
  });
  applyLoadedSearchFields(state.recommendations);
  state.recommendationsLoaded = true;
  state.recommendationsQueryKey = queryKey;
  state.recommendationsError = null;
  return state.recommendations;
}

function ensureRecommendations({ force = false } = {}) {
  const queryKey = currentRecommendationQueryKey();
  if (!force && state.recommendationsLoaded && state.recommendationsQueryKey === queryKey) {
    return Promise.resolve(state.recommendations);
  }
  if (!force && state.recommendationsLoading) return state.recommendationsLoading;

  state.recommendationsError = null;
  const requestId = state.recommendationsRequestId + 1;
  state.recommendationsRequestId = requestId;
  const request = loadRecommendations(requestId, queryKey)
    .catch(error => {
      if (requestId !== state.recommendationsRequestId) return state.recommendations;
      state.recommendationsError = error;
      throw error;
    })
    .finally(() => {
      if (state.recommendationsLoading === request) {
        state.recommendationsLoading = null;
      }
    });
  state.recommendationsLoading = request;
  return state.recommendationsLoading;
}

function loadRecommendationsForView({ force = false, selectFirst = true } = {}) {
  if (!isRecommendationView()) return;
  if (
    !force
    && state.recommendationsLoaded
    && state.recommendationsQueryKey === currentRecommendationQueryKey()
  ) return;
  const request = ensureRecommendations({ force });
  const requestId = state.recommendationsRequestId;
  request
    .then(() => {
      if (requestId !== state.recommendationsRequestId) return;
      if (isRecommendationView()) applyFilter({ selectFirst });
      if (state.detail) renderRecommendationContext(state.detail);
    })
    .catch(error => {
      if (requestId !== state.recommendationsRequestId) return;
      reportActionError("load recommendations")(error);
      if (isRecommendationView()) applyFilter();
    });
}

function resetContentUpdatesForQuery() {
  state.contentUpdatesRequestId += 1;
  state.contentUpdates = null;
  state.contentUpdatesLoaded = false;
  state.contentUpdatesLoading = null;
  state.contentUpdatesLoadingMore = null;
  state.contentUpdatesError = null;
  state.contentUpdatesPageError = null;
}

function contentUpdateEventKey(event) {
  if (event?.id != null) return `id:${event.id}`;
  return [
    event?.event_type,
    event?.source_anime_id ?? event?.anime_id,
    event?.episode_id,
    event?.video_source_id,
    event?.occurred_at,
  ].join(":");
}

function mergeContentUpdateList(current, incoming, keyGetter) {
  const merged = new Map();
  for (const item of [...(current || []), ...(incoming || [])]) {
    merged.set(keyGetter(item), item);
  }
  return [...merged.values()];
}

async function loadContentUpdates(requestId, { offset = 0, append = false } = {}) {
  const params = new URLSearchParams({
    days: state.contentUpdateDays,
    limit: String(CONTENT_UPDATE_LIMIT),
    event_type: state.contentUpdateType,
    offset: String(offset),
  });
  const payload = await api(`${CONTENT_UPDATE_ENDPOINT}?${params.toString()}`);
  if (requestId !== state.contentUpdatesRequestId) return state.contentUpdates;
  if (append && state.contentUpdates) {
    const events = mergeContentUpdateList(
      state.contentUpdates.events,
      payload.events,
      contentUpdateEventKey,
    ).sort((left, right) => {
      const byTime = String(right?.occurred_at || "").localeCompare(String(left?.occurred_at || ""));
      if (byTime) return byTime;
      return Number(right?.id || 0) - Number(left?.id || 0);
    });
    const items = mergeContentUpdateList(
      state.contentUpdates.items,
      payload.items,
      item => String(item?.id ?? item?.slug ?? ""),
    );
    state.contentUpdates = { ...payload, events, items };
  } else {
    state.contentUpdates = payload;
  }
  state.contentUpdatesLoaded = true;
  state.contentUpdatesError = null;
  state.contentUpdatesPageError = null;
  return state.contentUpdates;
}

function ensureContentUpdates({ force = false } = {}) {
  if (!force && state.contentUpdatesLoaded) {
    return Promise.resolve(state.contentUpdates);
  }
  if (!force && state.contentUpdatesLoading) return state.contentUpdatesLoading;

  state.contentUpdatesError = null;
  state.contentUpdatesPageError = null;
  const requestId = state.contentUpdatesRequestId + 1;
  state.contentUpdatesRequestId = requestId;
  const request = loadContentUpdates(requestId)
    .catch(error => {
      if (requestId !== state.contentUpdatesRequestId) return state.contentUpdates;
      state.contentUpdatesError = error;
      throw error;
    })
    .finally(() => {
      if (state.contentUpdatesLoading === request) {
        state.contentUpdatesLoading = null;
      }
    });
  state.contentUpdatesLoading = request;
  return state.contentUpdatesLoading;
}

async function loadMoreContentUpdates() {
  const pagination = state.contentUpdates?.pagination;
  if (!isUpdatesView() || !pagination?.has_more || state.contentUpdatesLoadingMore) return;
  const nextOffset = Number(pagination.next_offset);
  if (!Number.isInteger(nextOffset) || nextOffset < 0) return;

  state.contentUpdatesPageError = null;
  const requestId = state.contentUpdatesRequestId + 1;
  state.contentUpdatesRequestId = requestId;
  const request = loadContentUpdates(requestId, { offset: nextOffset, append: true });
  state.contentUpdatesLoadingMore = request;
  renderContentUpdatesView();
  try {
    await request;
    if (requestId !== state.contentUpdatesRequestId) return;
    applyFilter({ selectFirst: false });
  } catch (error) {
    if (requestId !== state.contentUpdatesRequestId) return;
    state.contentUpdatesPageError = error;
    throw error;
  } finally {
    if (state.contentUpdatesLoadingMore === request) {
      state.contentUpdatesLoadingMore = null;
    }
    if (requestId === state.contentUpdatesRequestId && isUpdatesView()) {
      renderDetail();
    }
  }
}

function loadContentUpdatesForView({ force = false } = {}) {
  if (!isUpdatesView()) return;
  if (!force && state.contentUpdatesLoaded) return;
  const request = ensureContentUpdates({ force });
  const requestId = state.contentUpdatesRequestId;
  request
    .then(() => {
      if (requestId !== state.contentUpdatesRequestId) return;
      if (isUpdatesView()) {
        applyFilter({ selectFirst: false });
        renderDetail();
      }
    })
    .catch(error => {
      if (requestId !== state.contentUpdatesRequestId) return;
      reportActionError("load content updates")(error);
      if (isUpdatesView()) {
        applyFilter({ selectFirst: false });
        renderDetail();
      }
    });
}

function renderViewTabs() {
  for (const item of el.viewTabs) {
    const active = item.dataset.view === state.viewMode;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

function activateViewMode(mode, { selectFirst = true } = {}) {
  rememberCurrentViewSort();
  state.viewMode = mode || "all";
  restoreViewSort();
  renderViewTabs();
  renderSortControls();
  loadRecommendationsForView();
  loadContentUpdatesForView();
  applyFilter({ selectFirst: isUpdatesView() ? false : selectFirst });
  renderDetail();
}

function detailContainsUpdateEvent(event) {
  if (!state.detail || !event) return false;
  const ids = [state.detail.id, ...(state.detail.source_member_ids || [])].map(value => String(value));
  return ids.includes(String(event.anime_id)) || ids.includes(String(event.source_anime_id));
}

async function openUpdatedTitle(item) {
  activateViewMode("all", { selectFirst: false });
  await selectAnime(titleRefForItem(item), { scrollDetail: true, history: "push" });
}

async function openContentUpdateEvent(event) {
  activateViewMode("all", { selectFirst: false });
  let openedTitle = false;
  if (!detailContainsUpdateEvent(event)) {
    await selectAnime(event.anime_ref || event.anime_slug || event.anime_id, {
      scrollDetail: true,
      history: "push",
    });
    openedTitle = true;
  }
  const episodeId = episodeIdForUpdateEvent(event);
  if (episodeId) {
    await selectEpisode(episodeId, {
      history: openedTitle ? "replace" : "push",
      persist: false,
    });
  }
}

el.search.addEventListener("input", () => {
  if (searchInputTimer) window.clearTimeout(searchInputTimer);
  const query = el.search.value.trim();
  if (query.length >= 2) loadSearchFieldsInBackground();
  searchInputTimer = window.setTimeout(() => {
    searchInputTimer = 0;
    applyFilter({ selectFirst: false });
  }, SEARCH_INPUT_DEBOUNCE_MS);
});
el.sortBy.addEventListener("change", () => {
  if (fixedSortForView()) return;
  state.sortBy = el.sortBy.value;
  state.sortDir = sortDefinition(state.sortBy).defaultDir;
  rememberCurrentViewSort();
  renderSortDirection();
  applyFilter({ selectFirst: true });
});
el.sortDirToggle.addEventListener("click", () => {
  if (fixedSortForView()) return;
  state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
  rememberCurrentViewSort();
  renderSortDirection();
  applyFilter({ selectFirst: true });
});
el.addFilter.addEventListener("change", () => {
  const id = el.addFilter.value;
  if (!id || state.activeFilterIds.includes(id)) {
    el.addFilter.value = "";
    return;
  }
  state.activeFilterIds.push(id);
  renderFilterControls();
  applyFilter({ selectFirst: true });
});
el.resetFilters.addEventListener("click", resetCatalogTools);
el.descriptionToggle.addEventListener("click", () => {
  state.descriptionExpanded = !state.descriptionExpanded;
  renderDescription(state.detail);
});
for (const button of el.viewTabs) {
  button.addEventListener("click", () => {
    activateViewMode(button.dataset.view || "all");
  });
}
el.favoriteToggle.addEventListener("click", () => {
  if (!state.detail) return;
  saveUserState({ is_favorite: !state.detail.is_favorite }).catch(reportActionError("toggle favorite"));
});
el.notWatchingButton.addEventListener("click", () => {
  if (!state.detail) return;
  discardWatchSession();
  saveUserState({ progress_episode_number: null, watched: false, watch_status: null })
    .catch(reportActionError("clear watching state"));
});
el.notInterestedButton.addEventListener("click", () => {
  if (!state.detail) return;
  saveUserState({ not_interested: !state.detail.not_interested })
    .catch(reportActionError("toggle recommendation dismissal"));
});
el.logoutButton.addEventListener("click", () => {
  logout().catch(reportActionError("logout"));
});
el.watchedToggle.addEventListener("change", () => {
  if (!state.detail) return;
  const watched = el.watchedToggle.checked;
  saveUserState({
    watched,
    watch_status: watched ? "completed" : null,
    ...(watched ? {} : { progress_episode_number: null }),
  }).catch(reportActionError("toggle watched"));
});
el.contentSource.addEventListener("change", event => {
  const selectedContentSource = event.target.value || null;
  const selectedEpisodeId = nearestEpisodeIdForContentSource(selectedContentSource);
  state.selectedContentSource = selectedContentSource;
  if (selectedEpisodeId != null) state.selectedEpisodeId = selectedEpisodeId;
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  const previousUrlSync = state.urlSyncSuspended;
  state.urlSyncSuspended = true;
  renderEpisodes(state.detail);
  renderSources();
  state.urlSyncSuspended = previousUrlSync;
  syncUrlFromDetail({ replace: false });
  markWatchEngaged("source_changed");
  persistCurrentEpisodeSelection().catch(reportActionError("save content source"));
});
el.translation.addEventListener("change", event => {
  const matchingSources = sourcesForEpisode(state.selectedEpisodeId).filter(source => (
    frontendRuntime.sourceTranslationKey(source) === event.target.value
  ));
  const selected = frontendRuntime.selectPreferredProvider(
    matchingSources,
    state.sourceSelectionPreference,
  );
  state.selectedTranslation = selected?.translation_id != null
    ? String(selected.translation_id)
    : null;
  state.selectedSourceId = selected?.id != null ? String(selected.id) : null;
  state.sourceSelectionPreference = frontendRuntime.sourcePreference(selected);
  const previousUrlSync = state.urlSyncSuspended;
  state.urlSyncSuspended = true;
  renderSources();
  state.urlSyncSuspended = previousUrlSync;
  syncUrlFromDetail({ replace: false });
  markWatchEngaged("source_changed");
  persistCurrentEpisodeSelection().catch(reportActionError("save translation"));
});
el.provider.addEventListener("change", event => {
  state.selectedSourceId = event.target.value;
  state.sourceSelectionPreference = frontendRuntime.sourcePreference(selectedSourceForEpisode());
  const previousUrlSync = state.urlSyncSuspended;
  state.urlSyncSuspended = true;
  renderSources();
  state.urlSyncSuspended = previousUrlSync;
  syncUrlFromDetail({ replace: false });
  markWatchEngaged("source_changed");
  persistCurrentEpisodeSelection().catch(reportActionError("save provider"));
});
el.fullscreenToggle.addEventListener("click", () => {
  toggleFullscreen().catch(reportActionError("fullscreen button"));
});
el.pipToggle.addEventListener("click", () => {
  openPictureInPicture().catch(reportActionError("picture in picture button"));
});
el.player.addEventListener("load", handlePlayerLoaded);
el.player.addEventListener("focus", handlePlayerEngaged);
el.player.addEventListener("pointerdown", handlePlayerEngaged);
document.addEventListener("fullscreenchange", handleFullscreenStateChange);
document.addEventListener("webkitfullscreenchange", handleFullscreenStateChange);
document.addEventListener("visibilitychange", handleVisibilityChange);
document.addEventListener("keydown", event => {
  if (!isFullscreenHotkey(event)) return;
  event.preventDefault();
  toggleFullscreen().catch(reportActionError("fullscreen hotkey"));
});
window.addEventListener("blur", () => {
  window.setTimeout(() => {
    if (document.activeElement === el.player) handlePlayerEngaged();
  }, 0);
});
window.addEventListener("focus", () => {
  if (document.activeElement === el.player) handlePlayerEngaged();
});
window.addEventListener("pagehide", event => {
  if (event.persisted) {
    flushWatchSession("page_hidden", { beacon: true });
  } else {
    clearWatchSession({ beacon: true });
  }
});
window.addEventListener("resize", () => {
  hideTitleTooltip();
  if (!state.detail) return;
  if (!descriptionIsClampedLayout()) state.descriptionExpanded = false;
  state.descriptionCanExpand = false;
  renderDescription(state.detail);
});
el.list.addEventListener("scroll", hideTitleTooltip);
el.list.addEventListener("keydown", event => {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const buttons = [...el.list.querySelectorAll(".anime-item")];
  const current = buttons.indexOf(document.activeElement);
  if (current < 0 || !buttons.length) return;
  event.preventDefault();
  const targetIndex = event.key === "Home"
    ? 0
    : event.key === "End"
      ? buttons.length - 1
      : Math.max(0, Math.min(buttons.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
  buttons[targetIndex].focus();
});
window.addEventListener("popstate", () => {
  const linkState = readLinkState();
  if (linkState.animeId) {
    selectAnime(linkState.animeId, { linkState, updateUrl: false, scrollDetail: true }).catch(reportActionError("popstate anime"));
  }
});

async function selectInitialAnime() {
  const linkState = readLinkState();
  if (linkState.animeId) {
    try {
      await selectAnime(linkState.animeId, { linkState, scrollDetail: true });
      return;
    } catch (error) {
      if (isAbortError(error)) return;
      reportClientError(error, { action: "open shared anime link", animeId: linkState.animeId });
      console.warn("Failed to open shared anime link", error);
    }
  }

  if (state.continueWatching?.anime_ref) {
    const target = state.continueWatching;
    try {
      await selectAnime(target.anime_ref, {
        linkState: {
          episodeId: target.episode_id,
          contentSource: target.source,
          translation: target.translation_id,
          provider: target.video_source_id,
        },
      });
      return;
    } catch (error) {
      if (isAbortError(error)) return;
      reportClientError(error, { action: "open continue watching", animeId: target.anime_id });
      console.warn("Failed to open continue target", error);
    }
  }

  const first = state.filtered.find(item => item.source_count > 0) || state.filtered[0] || state.anime[0];
  if (first) await selectAnime(titleRefForItem(first));
}

async function boot() {
  markPerformanceCheckpoint("boot_start");
  configurePlayerIframe(el.player);
  const continuePromise = api("/api/continue-watching").catch(error => {
    reportClientError(error, { action: "load continue watching" });
    return { item: null };
  });
  const [me, appConfig, payload, continuePayload] = await Promise.all([
    api("/api/me"),
    api("/api/app-config"),
    api("/api/anime"),
    continuePromise,
  ]);
  state.user = me.user;
  playerHosts = Array.isArray(appConfig.player_hosts) ? appConfig.player_hosts : [];
  state.continueWatching = continuePayload.item || null;
  renderAccount();
  markPerformanceCheckpoint("me_loaded", { is_admin: Boolean(state.user?.is_admin) });
  state.anime = payload.items || [];
  applyLoadedSearchFields(state.anime);
  markPerformanceCheckpoint("catalog_loaded", { items: state.anime.length });
  markPerformanceCheckpoint("recommendations_deferred");
  renderFilterControls();
  renderSortControls();
  applyFilter();
  markPerformanceCheckpoint("catalog_rendered", { filtered: state.filtered.length });
  try {
    await selectInitialAnime();
  } catch (error) {
    if (!isAbortError(error)) throw error;
  }
  markPerformanceCheckpoint("initial_detail_loaded", { selected_anime_id: state.selectedAnimeId });
  markPerformanceCheckpoint("boot_complete");
  reportHomePerformance("success");
}

boot().catch(error => {
  if (isAbortError(error)) return;
  markPerformanceCheckpoint("boot_failed");
  reportClientError(error, { action: "boot app" });
  showAppStatus(error?.message || "Не удалось загрузить приложение", "warn", 0);
  clearPlayer(error.message);
  console.error(error);
});

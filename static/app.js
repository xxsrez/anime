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
const RECOMMENDATION_LIMIT = 20;
const CONTENT_UPDATE_LIMIT = 220;
const CONTENT_UPDATE_DEFAULT_DAYS = "7";
const INITIAL_RENDER_LIMIT = 40;
const RENDER_BATCH_SIZE = 80;
const LINK_PARAM_KEYS = ["episode", "source", "translation", "provider"];
const PLAYER_IFRAME_ALLOW = "autoplay *; fullscreen *; picture-in-picture *; encrypted-media *; clipboard-write *; web-share *; screen-wake-lock *; accelerometer *; gyroscope *";
const WATCH_ENDPOINT = "/api/watch-events";
const CONTENT_UPDATE_ENDPOINT = "/api/content-updates";
const WATCH_HEARTBEAT_MS = 30000;
const WATCH_MAX_DELTA_SECONDS = 300;
const reportClientError = window.reportClientError || (() => {});
const reportActionError = window.reportActionError || (() => error => console.error(error));
const PERFORMANCE_ENDPOINT = "/api/performance";
const MAX_PERFORMANCE_API_REQUESTS = 40;
const MAX_PERFORMANCE_RESOURCES = 24;
const catalogSearch = window.AnimeSearch;
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

if (!catalogSearch) {
  throw new Error("AnimeSearch is not loaded");
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
  contentUpdates: null,
  contentUpdatesLoaded: false,
  contentUpdatesLoading: null,
  contentUpdatesError: null,
  contentUpdatesRequestId: 0,
  contentUpdateDays: CONTENT_UPDATE_DEFAULT_DAYS,
  contentUpdateType: "all",
  continueWatching: null,
  searchFieldsLoaded: false,
  searchFieldsLoading: null,
  searchFieldsError: null,
  filtered: [],
  selectedAnimeId: null,
  detail: null,
  selectedEpisodeId: null,
  selectedContentSource: null,
  selectedTranslation: null,
  selectedSourceId: null,
  viewMode: "all",
  filters: { ...DEFAULT_FILTERS },
  activeFilterIds: [],
  sortBy: DEFAULT_SORT_BY,
  sortDir: DEFAULT_SORT_DIR,
  renderLimit: INITIAL_RENDER_LIMIT,
  filterControls: {},
  savingState: false,
  pendingStateSave: null,
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
  watchedToggle: document.getElementById("watched-toggle"),
  recommendationContext: document.getElementById("recommendation-context"),
  recentUpdates: document.getElementById("recent-updates"),
  genres: document.getElementById("genres"),
  description: document.getElementById("description"),
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
};

let listImageObserver = null;
let titleTooltip = null;
let titleTooltipTarget = null;

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
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return await response.json();
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
  catalogSearch.ensureSearchIndex(item);
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

function progressText(item) {
  if (item.watched) return "просмотрено";
  return "";
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

function formatPercent(value) {
  const score = numericValue(value);
  return score == null ? "" : `${Math.round(score)}%`;
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
  const id = String(item?.id ?? "");
  if (!id || !state.contentUpdatesLoaded) return [];
  return filteredContentUpdateEvents().filter(event => String(event.anime_id) === id);
}

function contentUpdateSummaryFromEvents(events, days = state.contentUpdates?.period?.days) {
  if (!events.length) return null;
  const counts = {};
  for (const event of events) {
    counts[event.event_type] = (counts[event.event_type] || 0) + 1;
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
  if (isUpdatesView() && state.contentUpdatesLoaded) {
    return contentUpdateSummaryFromEvents(itemContentUpdateEvents(item));
  }
  return item?.recent_update_summary || null;
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
  const diffDays = Math.floor((now.setHours(0, 0, 0, 0) - date.setHours(0, 0, 0, 0)) / 86400000);
  if (diffDays <= 0) return "сегодня";
  if (diffDays === 1) return "вчера";
  return `${diffDays} дн. назад`;
}

function updateEventTitle(event) {
  if (event.event_type === "new_title") return "Новый тайтл";
  if (event.event_type === "new_episode") {
    return event.episode_number ? `Добавлена ${event.episode_number} серия` : "Добавлена серия";
  }
  if (event.event_type === "new_translation") {
    return `Новая озвучка${event.translation_title ? `: ${event.translation_title}` : ""}`;
  }
  if (event.event_type === "new_provider") {
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
  state.recommendationProfile = null;
  state.recommendationsError = null;
  state.recommendationsRequestId += 1;
}

function baseItemsForView() {
  if (!isRecommendationView()) return state.anime;
  return state.recommendationsLoaded || (state.recommendationsLoading && state.recommendations.length)
    ? state.recommendations
    : [];
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

function sortDefinition(id = state.sortBy) {
  return sortDefinitions.find(item => item.id === id) || sortDefinitions[0];
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
    });

    state.filterControls[definition.id] = select;
    label.append(caption, select);
    row.append(label, remove);
    el.filterGrid.append(row);
  }

  renderAddFilterControl();
}

function renderSortControls() {
  el.sortBy.replaceChildren(...sortDefinitions.map(definition => {
    const option = document.createElement("option");
    option.value = definition.id;
    option.textContent = definition.label;
    return option;
  }));
  el.sortBy.value = state.sortBy;
  renderSortDirection();
}

function renderSortDirection() {
  const isDesc = state.sortDir === "desc";
  const label = isDesc ? "По убыванию" : "По возрастанию";
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
  return Boolean(query)
    || filtersChanged
    || state.activeFilterIds.length > 0
    || state.sortBy !== DEFAULT_SORT_BY
    || state.sortDir !== DEFAULT_SORT_DIR;
}

function resetCatalogTools() {
  el.search.value = "";
  state.filters = { ...DEFAULT_FILTERS };
  state.activeFilterIds = [];
  state.sortBy = DEFAULT_SORT_BY;
  state.sortDir = DEFAULT_SORT_DIR;

  renderFilterControls();
  el.sortBy.value = state.sortBy;
  renderSortDirection();
  applyFilter({ selectFirst: true });
}

function renderList() {
  hideTitleTooltip();
  resetListImageObserver();
  const total = isRecommendationView()
    ? state.recommendations.length
    : isUpdatesView()
      ? (state.contentUpdates?.summary?.updated_title_count ?? state.filtered.length)
      : state.anime.length;
  el.count.textContent = isRecommendationView()
    ? `${state.filtered.length} из ${total} советов`
    : isUpdatesView()
      ? `${state.filtered.length} из ${total} обновл.`
      : `${state.filtered.length} из ${total} тайтлов`;
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

  const selectedIndex = state.filtered.findIndex(item => item.id === state.selectedAnimeId);
  const renderLimit = selectedIndex >= state.renderLimit ? selectedIndex + 1 : state.renderLimit;
  const visibleItems = state.filtered.slice(0, renderLimit);

  for (const item of visibleItems) {
    const button = document.createElement("button");
    button.className = "anime-item";
    button.type = "button";
    button.dataset.id = item.id;
    if (item.id === state.selectedAnimeId) button.classList.add("active");
    if (item.is_favorite) button.classList.add("favorite");
    if (item.watched) button.classList.add("watched");
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
    title.textContent = `${rank}${item.is_favorite ? "★ " : ""}${item.title}`;
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
      const recScore = formatPercent(item.recommendation_score);
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
        selectAnime(titleRefForItem(item), { scrollDetail: true });
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
  const mode = profile.mode === "personalized" ? "персонально" : "популярное";
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

function contentUpdateCounts(events) {
  const counts = {};
  for (const option of CONTENT_UPDATE_TYPES) {
    if (option.id !== "all") counts[option.id] = 0;
  }
  for (const event of events) {
    if (event.event_type in counts) counts[event.event_type] += 1;
  }
  return counts;
}

function uniqueContentUpdateTitleCount(events) {
  return new Set(events.map(event => event.anime_id).filter(value => value != null).map(String)).size;
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
      state.contentUpdateType = option.id;
      renderContentUpdatesView();
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
      state.contentUpdatesLoaded = false;
      state.contentUpdates = null;
      loadContentUpdatesForView({ force: true });
      applyFilter({ selectFirst: false });
      renderContentUpdatesView();
    });
    periodRow.append(button);
  }

  parent.append(typeRow, periodRow);
}

function renderContentUpdateStats(parent, events) {
  const counts = contentUpdateCounts(events);
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

function eventAnimeTitle(event) {
  return event?.anime_title || event?.title || "Без названия";
}

function eventAnimeSubtitle(event) {
  return event?.anime_subtitle || "";
}

function renderContentUpdateRows(parent, events) {
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "updates-empty";
    empty.textContent = "За выбранный период обновлений нет";
    parent.append(empty);
    return;
  }

  let currentDay = "";
  let group = null;
  for (const event of events) {
    const day = updateDateHeading(event.occurred_at);
    if (day !== currentDay) {
      currentDay = day;
      group = document.createElement("section");
      group.className = "updates-day";
      const heading = document.createElement("h3");
      heading.textContent = day;
      group.append(heading);
      parent.append(group);
    }

    const row = document.createElement("button");
    row.type = "button";
    row.className = "content-update-row";
    row.dataset.type = event.event_type || "";
    row.addEventListener("click", () => {
      openContentUpdateEvent(event).catch(reportActionError("open content update"));
    });

    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    if (event.cover_url) img.src = event.cover_url;

    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = eventAnimeTitle(event);
    const update = document.createElement("span");
    update.className = "content-update-title";
    update.textContent = updateEventTitle(event);
    const meta = document.createElement("span");
    meta.className = "content-update-meta";
    meta.textContent = [
      eventAnimeSubtitle(event),
      updateEventMeta(event),
      updateClockLabel(event.occurred_at),
    ].filter(Boolean).join(" · ");
    body.append(title, update, meta);
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

  if (state.contentUpdatesLoading) {
    const loading = document.createElement("div");
    loading.className = "updates-empty";
    loading.textContent = "Загружаю новое...";
    el.updatesView.append(loading);
    return;
  }

  if (state.contentUpdatesError) {
    const error = document.createElement("div");
    error.className = "updates-empty warn";
    error.textContent = "Не удалось загрузить новое";
    el.updatesView.append(error);
    return;
  }

  const events = filteredContentUpdateEvents();
  const summary = document.createElement("div");
  summary.className = "updates-summary";
  const count = document.createElement("strong");
  count.textContent = `${events.length} событий`;
  const titles = document.createElement("span");
  titles.textContent = `${uniqueContentUpdateTitleCount(events)} тайтлов`;
  const type = document.createElement("span");
  type.textContent = contentUpdateTypeLabel(state.contentUpdateType);
  summary.append(count, titles, type);
  el.updatesView.append(summary);

  renderContentUpdateStats(el.updatesView, events);
  renderContentUpdateRows(el.updatesView, events);
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
  el.description.textContent = detail.description || "";
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
  el.favoriteToggle.textContent = detail.is_favorite ? "★ В избранном" : "☆ Избранное";
  el.watchedToggle.checked = Boolean(detail.watched);
  const hasProgress = effectiveProgressEpisodeNumber(detail) != null;
  el.notWatchingButton.hidden = !hasProgress || Boolean(detail.watched);
  el.notWatchingButton.setAttribute("aria-label", "Убрать тайтл из «Смотрю»");
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
  score.textContent = `Совет ${formatPercent(rec.recommendation_score)}`;
  const confidence = document.createElement("span");
  confidence.textContent = `${rec.recommendation_confidence || "средняя"} уверенность`;
  header.append(score, confidence);

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
  const map = new Map();
  for (const source of sources) {
    if (!map.has(source.translation_id)) {
      map.set(source.translation_id, source.translation_title || String(source.translation_id));
    }
  }
  return [...map.entries()].map(([id, title]) => ({ id: String(id), title }));
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

  if (!translations.some(item => item.id === String(state.selectedTranslation))) {
    state.selectedTranslation = translations[0]?.id || null;
  }

  for (const item of translations) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.title;
    option.selected = item.id === String(state.selectedTranslation);
    el.translation.append(option);
  }

  const providers = sources.filter(source => String(source.translation_id) === String(state.selectedTranslation));
  if (!providers.some(source => String(source.id) === String(state.selectedSourceId))) {
    state.selectedSourceId = providers[0]?.id ? String(providers[0].id) : null;
  }

  for (const source of providers) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = `${source.provider_title} · ${source.embed_host}`;
    option.selected = String(source.id) === String(state.selectedSourceId);
    el.provider.append(option);
  }

  const selected = providers.find(source => String(source.id) === String(state.selectedSourceId)) || providers[0];
  if (selected) {
    setPlayer(selected, episode);
  } else {
    clearPlayer("Источник недоступен");
  }
  syncUrlFromDetail();
}

function setPlayer(source, episode) {
  ensureWatchSession(source, episode);
  el.wrap.classList.remove("empty");
  configurePlayerIframe(el.player);
  if (el.player.getAttribute("src") !== source.embed_url) {
    el.player.src = source.embed_url;
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
  iframe.setAttribute("allowfullscreen", "");
  iframe.setAttribute("webkitallowfullscreen", "");
  iframe.setAttribute("mozallowfullscreen", "");
  iframe.allowFullscreen = true;
  iframe.referrerPolicy = iframe.referrerPolicy || "origin";
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
    player_focused: document.activeElement === el.player,
  };
}

function applyWatchState(nextState, animeId = state.selectedAnimeId) {
  if (!nextState || !animeId) return;
  const watched = Boolean(nextState.watched);
  const progress = nextState.progress_episode_number == null ? null : nextState.progress_episode_number;
  const applyToItem = item => {
    if (!item) return false;
    const changed = item.progress_episode_number !== progress || Boolean(item.watched) !== watched;
    Object.assign(item, nextState);
    return changed;
  };

  const catalogChanged = applyToItem(state.anime.find(entry => entry.id === animeId));
  const recommendationsChanged = applyToItem(state.recommendations.find(entry => entry.id === animeId));
  const detailChanged = state.detail?.id === animeId ? applyToItem(state.detail) : false;
  const changed = catalogChanged || recommendationsChanged || detailChanged;

  if (!changed) return;
  invalidateRecommendations();
  if (isRecommendationView()) loadRecommendationsForView({ force: true, selectFirst: false });
  applyFilter();
  if (state.detail?.id === animeId) renderWatchState(state.detail);
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
  return postWatchPayload(payload, { beacon })
    .then(result => {
      if (result?.state) applyWatchState(result.state, session.animeId);
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
  const seconds = Math.max(0, Math.round((now - session.activeSince) / 1000));
  session.activeSince = stop || document.hidden ? null : now;
  return Math.min(WATCH_MAX_DELTA_SECONDS, seconds);
}

function startWatchHeartbeat() {
  if (state.watchHeartbeatTimer) return;
  state.watchHeartbeatTimer = window.setInterval(() => {
    const session = state.watchSession;
    if (!session?.engaged || document.hidden) return;
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
  if (!session.engaged) {
    session.engaged = true;
    session.activeSince = performanceNow();
  } else if (!session.activeSince) {
    session.activeSince = performanceNow();
  }
  startWatchHeartbeat();
  sendWatchEvent(eventType).catch(() => {});
}

function flushWatchSession(eventType = "session_end", { beacon = false } = {}) {
  const session = state.watchSession;
  if (!session) return;
  const seconds = consumeWatchEngagedSeconds({ stop: true });
  if (session.engaged || eventType !== "session_end") {
    sendWatchEvent(eventType, { engagedSeconds: seconds, beacon, session }).catch(() => {});
  }
  if (eventType === "session_end") stopWatchHeartbeat();
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
  } else if (state.watchSession?.engaged && !state.watchSession.activeSince) {
    state.watchSession.activeSince = performanceNow();
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

function clonePlayerForPipWindow(pipWindow) {
  const doc = pipWindow.document;
  doc.body.style.margin = "0";
  doc.body.style.background = "#050607";
  const iframe = doc.createElement("iframe");
  configurePlayerIframe(iframe);
  iframe.src = el.player.src;
  iframe.title = el.player.title || "Аниме плеер";
  Object.assign(iframe.style, {
    border: "0",
    width: "100vw",
    height: "100vh",
    background: "#050607",
  });
  doc.body.append(iframe);
}

async function openPictureInPicture() {
  if (!el.player.getAttribute("src")) {
    setPlayerActionState("Нет активного видео", "warn");
    return;
  }

  if ("documentPictureInPicture" in window) {
    try {
      const pipWindow = await window.documentPictureInPicture.requestWindow({
        width: 560,
        height: 315,
      });
      clonePlayerForPipWindow(pipWindow);
      setPlayerActionState("PiP открыт", "ok");
      markWatchEngaged("pip_open");
      return;
    } catch (error) {
      if (error.name !== "NotAllowedError") {
        reportClientError(error, { action: "picture in picture" });
        setPlayerActionState("PiP доступен в самом плеере", "warn");
        return;
      }
    }
  }

  setPlayerActionState("PiP доступен в самом плеере", "warn");
}

async function selectAnime(id, options = {}) {
  state.detail = await api(`/api/anime/${encodeURIComponent(id)}`);
  state.selectedAnimeId = state.detail.id;
  applyDetailLinkState(options.linkState || {});

  const previousUrlSync = state.urlSyncSuspended;
  state.urlSyncSuspended = previousUrlSync || options.updateUrl === false;
  renderList();
  renderDetail();
  state.urlSyncSuspended = previousUrlSync;
  if (options.updateUrl !== false) syncUrlFromDetail();
  if (options.scrollDetail) scrollDetailIntoViewForMobile();
}

async function selectEpisode(id) {
  state.selectedEpisodeId = id;
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  const episode = activeEpisode();
  const number = numberFrom(episode?.number);
  if (number != null) {
    renderDetail();
    markWatchEngaged("episode_selected");
    await saveUserState({ progress_episode_number: number, watched: false });
    return;
  }
  renderDetail();
}

async function saveUserState(patch, animeId = state.selectedAnimeId) {
  if (!animeId) return;
  if (state.savingState) {
    if (state.pendingStateSave?.animeId === animeId) {
      Object.assign(state.pendingStateSave.patch, patch);
    } else {
      state.pendingStateSave = { animeId, patch: { ...patch } };
    }
    return;
  }

  state.savingState = true;
  try {
    const payload = await api(`/api/anime/${animeId}/state`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const nextState = payload.state || {};
    const pendingForSameAnime = state.pendingStateSave?.animeId === animeId
      ? state.pendingStateSave.patch
      : null;
    const visibleState = pendingForSameAnime
      ? { ...nextState, ...pendingForSameAnime }
      : nextState;
    const item = state.anime.find(entry => entry.id === animeId);
    if (item) Object.assign(item, visibleState);
    const affectsRecommendations = ["is_favorite", "watched", "progress_episode_number"].some(key => key in patch);
    if (affectsRecommendations) {
      invalidateRecommendations();
      if (isRecommendationView()) {
        loadRecommendationsForView({ force: true, selectFirst: false });
      }
    }
    const recommended = state.recommendations.find(entry => entry.id === animeId);
    if (recommended) Object.assign(recommended, visibleState);
    if (state.detail?.id === animeId) Object.assign(state.detail, visibleState);
    applyFilter();
    if (state.detail?.id === animeId) renderDetail();
  } finally {
    state.savingState = false;
    const pending = state.pendingStateSave;
    state.pendingStateSave = null;
    if (pending) await saveUserState(pending.patch, pending.animeId);
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
    const viewMatch =
      state.viewMode === "recommendations"
        ? true
        : state.viewMode === "updates"
          ? hasRecentUpdates(item)
        : state.viewMode === "favorites"
        ? item.is_favorite
        : state.viewMode === "progress"
          ? !item.watched && item.progress_episode_number != null
          : true;
    const filterMatch = filterDefinitions.every(definition => (
      definition.match(item, state.filters[definition.id])
    ));
    return queryMatch && viewMatch && filterMatch;
  }).sort((left, right) => {
    if (query.tokens.length && left.searchScore !== right.searchScore) {
      return right.searchScore - left.searchScore;
    }
    const result = fallbackCompare(left.item, right.item);
    return result || left.index - right.index;
  }).map(entry => entry.item);
  renderList();
  renderActiveFilters();
  el.resetFilters.hidden = !hasActiveCatalogTools();

  if (selectFirst && !isUpdatesView() && state.filtered.length && !state.filtered.some(item => item.id === state.selectedAnimeId)) {
    selectAnime(titleRefForItem(state.filtered[0])).catch(reportActionError("select filtered anime"));
  }
}

async function loadSearchFields() {
  const payload = await api("/api/anime/search-fields");
  const fieldsById = new Map(
    (payload.items || []).map(item => [String(item.id), item.search_fields || []])
  );
  const applyFields = item => setItemSearchFields(item, fieldsById.get(String(item.id)) || []);

  for (const item of state.anime) applyFields(item);
  for (const item of state.recommendations) applyFields(item);

  state.searchFieldsLoaded = true;
  state.searchFieldsError = null;
  markPerformanceCheckpoint("search_fields_loaded", { items: fieldsById.size });

  if (el.search.value.trim()) {
    applyFilter({ selectFirst: true });
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

async function loadRecommendations(requestId) {
  const payload = await api(`/api/recommendations?limit=${RECOMMENDATION_LIMIT}`);
  if (requestId !== state.recommendationsRequestId) return state.recommendations;
  state.recommendationProfile = payload.profile || null;
  state.recommendations = catalogSearch.prepareSearchIndexes((payload.items || []).map(item => {
    const local = state.anime.find(entry => entry.id === item.id);
    return local ? { ...local, ...item } : item;
  }));
  state.recommendationsLoaded = true;
  state.recommendationsError = null;
  return state.recommendations;
}

function ensureRecommendations({ force = false } = {}) {
  if (!force && state.recommendationsLoaded) {
    return Promise.resolve(state.recommendations);
  }
  if (!force && state.recommendationsLoading) return state.recommendationsLoading;

  state.recommendationsError = null;
  const requestId = state.recommendationsRequestId + 1;
  state.recommendationsRequestId = requestId;
  const request = loadRecommendations(requestId)
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
  if (!force && state.recommendationsLoaded) return;
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

async function loadContentUpdates(requestId) {
  const params = new URLSearchParams({
    days: state.contentUpdateDays,
    limit: String(CONTENT_UPDATE_LIMIT),
  });
  const payload = await api(`${CONTENT_UPDATE_ENDPOINT}?${params.toString()}`);
  if (requestId !== state.contentUpdatesRequestId) return state.contentUpdates;
  state.contentUpdates = payload;
  state.contentUpdatesLoaded = true;
  state.contentUpdatesError = null;
  return state.contentUpdates;
}

function ensureContentUpdates({ force = false } = {}) {
  if (!force && state.contentUpdatesLoaded) {
    return Promise.resolve(state.contentUpdates);
  }
  if (!force && state.contentUpdatesLoading) return state.contentUpdatesLoading;

  state.contentUpdatesError = null;
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
  state.viewMode = mode || "all";
  renderViewTabs();
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
  await selectAnime(titleRefForItem(item), { scrollDetail: true });
}

async function openContentUpdateEvent(event) {
  activateViewMode("all", { selectFirst: false });
  if (!detailContainsUpdateEvent(event)) {
    await selectAnime(event.anime_ref || event.anime_slug || event.anime_id, { scrollDetail: true });
  }
  const episodeId = episodeIdForUpdateEvent(event);
  if (episodeId) await selectEpisode(episodeId);
}

el.search.addEventListener("input", () => {
  if (el.search.value.trim()) loadSearchFieldsInBackground();
  applyFilter({ selectFirst: true });
});
el.sortBy.addEventListener("change", () => {
  state.sortBy = el.sortBy.value;
  state.sortDir = sortDefinition(state.sortBy).defaultDir;
  renderSortDirection();
  applyFilter({ selectFirst: true });
});
el.sortDirToggle.addEventListener("click", () => {
  state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
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
  saveUserState({ progress_episode_number: null, watched: false }).catch(reportActionError("clear watching state"));
});
el.logoutButton.addEventListener("click", () => {
  logout().catch(reportActionError("logout"));
});
el.watchedToggle.addEventListener("change", () => {
  if (!state.detail) return;
  saveUserState({ watched: el.watchedToggle.checked }).catch(reportActionError("toggle watched"));
});
el.contentSource.addEventListener("change", event => {
  state.selectedContentSource = event.target.value || null;
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  renderSources();
  markWatchEngaged("source_changed");
});
el.translation.addEventListener("change", event => {
  state.selectedTranslation = event.target.value;
  state.selectedSourceId = null;
  renderSources();
  markWatchEngaged("source_changed");
});
el.provider.addEventListener("change", event => {
  state.selectedSourceId = event.target.value;
  renderSources();
  markWatchEngaged("source_changed");
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
window.addEventListener("pagehide", () => {
  clearWatchSession({ beacon: true });
});
window.addEventListener("resize", hideTitleTooltip);
el.list.addEventListener("scroll", hideTitleTooltip);
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
  const [me, payload, continuePayload] = await Promise.all([
    api("/api/me"),
    api("/api/anime"),
    continuePromise,
  ]);
  state.user = me.user;
  state.continueWatching = continuePayload.item || null;
  renderAccount();
  markPerformanceCheckpoint("me_loaded", { is_admin: Boolean(state.user?.is_admin) });
  state.anime = catalogSearch.prepareSearchIndexes(payload.items || []);
  markPerformanceCheckpoint("catalog_loaded", { items: state.anime.length });
  markPerformanceCheckpoint("recommendations_deferred");
  renderFilterControls();
  renderSortControls();
  applyFilter();
  markPerformanceCheckpoint("catalog_rendered", { filtered: state.filtered.length });
  await selectInitialAnime();
  markPerformanceCheckpoint("initial_detail_loaded", { selected_anime_id: state.selectedAnimeId });
  markPerformanceCheckpoint("boot_complete");
  reportHomePerformance("success");
}

boot().catch(error => {
  markPerformanceCheckpoint("boot_failed");
  reportClientError(error, { action: "boot app" });
  clearPlayer(error.message);
  console.error(error);
});

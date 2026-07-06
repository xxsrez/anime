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
const INITIAL_RENDER_LIMIT = 40;
const RENDER_BATCH_SIZE = 80;
const LINK_PARAM_KEYS = ["episode", "source", "translation", "provider"];
const PLAYER_IFRAME_ALLOW = "autoplay *; fullscreen *; picture-in-picture *; encrypted-media *; clipboard-write *; web-share *; screen-wake-lock *; accelerometer *; gyroscope *";

const state = {
  anime: [],
  recommendations: [],
  recommendationProfile: null,
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
};

const el = {
  count: document.getElementById("catalog-count"),
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
  poster: document.getElementById("poster"),
  meta: document.getElementById("meta-line"),
  title: document.getElementById("title"),
  subtitle: document.getElementById("subtitle"),
  favoriteToggle: document.getElementById("favorite-toggle"),
  progressEpisode: document.getElementById("progress-episode"),
  watchedToggle: document.getElementById("watched-toggle"),
  progressSummary: document.getElementById("progress-summary"),
  recommendationContext: document.getElementById("recommendation-context"),
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

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function text(value, fallback = "") {
  return value == null || value === "" ? fallback : String(value);
}

function searchText(value) {
  return String(value || "").toLocaleLowerCase("ru").replaceAll("ё", "е").replaceAll("э", "е");
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
  const sources = state.detail?.sources_by_episode?.[episodeId] || [];
  return sources
    .filter(source => source.embed_url)
    .filter(source => !state.selectedContentSource || source.source === state.selectedContentSource);
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
  if (item.progress_episode_number != null) return `серия ${item.progress_episode_number}`;
  return "";
}

function progressSummary(detail) {
  if (!detail) return "Не начато";
  const progress = detail.progress_episode_number;
  const total = numberFrom(detail.episodes_text);
  if (detail.watched) return "Просмотрено";
  if (progress != null && total) return `Серия ${progress} из ${total}`;
  if (progress != null) return `Серия ${progress}`;
  return "Не начато";
}

function progressInputValue() {
  if (el.progressEpisode.value === "") return null;
  const parsed = Number.parseInt(el.progressEpisode.value, 10);
  if (!Number.isFinite(parsed)) return null;
  const total = numberFrom(state.detail?.episodes_text);
  const normalized = Math.max(0, parsed);
  return total ? Math.min(total, normalized) : normalized;
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

function preferredContentSource(detail) {
  const variants = sourceVariants(detail);
  const withVideo = variants.find(variant => (variant.source_count || 0) > 0);
  return (withVideo || variants[0] || {}).source || detail?.source || null;
}

function matchingEpisodeId(episodeId) {
  if (!episodeId) return null;
  const episode = state.detail?.episodes?.find(item => String(item.id) === String(episodeId));
  return episode ? episode.id : null;
}

function matchingContentSource(source) {
  if (!source) return null;
  return sourceVariants(state.detail).some(variant => variant.source === source) ? source : null;
}

function applyDetailLinkState(linkState = {}) {
  state.selectedContentSource = matchingContentSource(linkState.contentSource)
    || preferredContentSource(state.detail);

  const firstAvailable = state.detail.episodes.find(episode => episode.source_count > 0);
  state.selectedEpisodeId = matchingEpisodeId(linkState.episodeId)
    || (firstAvailable || state.detail.episodes[0] || {}).id
    || null;

  state.selectedTranslation = linkState.translation || null;
  state.selectedSourceId = linkState.provider || null;
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

function isRecommendationView() {
  return state.viewMode === "recommendations";
}

function recommendationFor(id) {
  return state.recommendations.find(item => String(item.id) === String(id)) || null;
}

function baseItemsForView() {
  return isRecommendationView() ? state.recommendations : state.anime;
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
  const total = isRecommendationView() ? state.recommendations.length : state.anime.length;
  el.count.textContent = isRecommendationView()
    ? `${state.filtered.length} из ${total} советов`
    : `${state.filtered.length} из ${total} тайтлов`;
  renderRecommendationMeta();
  el.list.replaceChildren();

  if (!state.filtered.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = isRecommendationView() ? "Пока нет рекомендаций" : "Ничего не найдено";
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

    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    observeListImage(img, item.cover_url || "");

    const body = document.createElement("div");
    const title = document.createElement("strong");
    const rank = isRecommendationView() && item.recommendation_rank ? `${item.recommendation_rank}. ` : "";
    title.textContent = `${rank}${item.is_favorite ? "★ " : ""}${item.title}`;
    button.dataset.fullTitle = item.title || title.textContent;
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
      meta.textContent = `${text(item.kind, "тайтл")} ${text(item.episodes_text, "")} · ${available} видео${score ? ` · ${score}` : ""}${source ? ` · ${source}` : ""}${watch ? ` · ${watch}` : ""}`;
    }

    body.append(title, meta);
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
      selectAnime(titleRefForItem(item));
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
  el.recommendationMeta.hidden = !isRecommendationView();
  el.recommendationMeta.replaceChildren();
  if (!isRecommendationView()) return;

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

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;

  el.poster.src = detail.cover_url || "";
  el.poster.alt = detail.title || "";
  const scoreText = ratingText(detail);
  el.meta.textContent = [detail.kind, detail.status, detail.episodes_text, scoreText, sourceLabelList(detail)].filter(Boolean).join(" · ");
  el.title.textContent = detail.title || "";
  el.subtitle.textContent = detail.subtitle || "";
  el.description.textContent = detail.description || "";
  renderWatchState(detail);
  renderRecommendationContext(detail);
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
  el.progressEpisode.value = detail.progress_episode_number == null ? "" : detail.progress_episode_number;
  const total = numberFrom(detail.episodes_text);
  if (total) {
    el.progressEpisode.max = total;
  } else {
    el.progressEpisode.removeAttribute("max");
  }
  el.watchedToggle.checked = Boolean(detail.watched);
  el.progressSummary.textContent = progressSummary(detail);
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
    button.addEventListener("click", () => selectEpisode(episode.id).catch(console.error));
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
    if (episode) selectEpisode(episode.id).catch(console.error);
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
    state.selectedContentSource = preferredContentSource(state.detail);
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
  el.wrap.classList.remove("empty");
  configurePlayerIframe(el.player);
  el.player.src = source.embed_url;
  el.host.textContent = source.embed_host || "-";
  el.episodeState.textContent = episode.title && episode.title !== "---" ? episode.title : `${episode.number} серия`;
  el.empty.textContent = "";
}

function clearPlayer(message) {
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
      return;
    } catch (error) {
      if (error.name !== "NotAllowedError") {
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
}

async function selectEpisode(id) {
  state.selectedEpisodeId = id;
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  const episode = activeEpisode();
  const number = numberFrom(episode?.number);
  if (number != null) {
    renderDetail();
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
      await loadRecommendations();
    }
    const recommended = state.recommendations.find(entry => entry.id === animeId);
    if (recommended) Object.assign(recommended, visibleState);
    if (state.detail?.id === animeId) Object.assign(state.detail, visibleState);
    applyFilter({ selectFirst: isRecommendationView() && state.selectedAnimeId === animeId });
    if (state.detail?.id === animeId) renderDetail();
  } finally {
    state.savingState = false;
    const pending = state.pendingStateSave;
    state.pendingStateSave = null;
    if (pending) await saveUserState(pending.patch, pending.animeId);
  }
}

function applyFilter({ selectFirst = false } = {}) {
  const query = searchText(el.search.value.trim());
  const baseItems = baseItemsForView();
  state.renderLimit = INITIAL_RENDER_LIMIT;
  state.filtered = baseItems.filter(item => {
    const variantText = (item.source_variants || []).flatMap(variant => [
      variant.title,
      variant.subtitle,
      sourceLabel(variant.source),
    ]);
    const haystack = searchText([
      item.title,
      item.subtitle,
      item.kind,
      item.status,
      item.year,
      sourceLabelList(item),
      ...(item.genres || []),
      ...variantText,
    ].join(" "));
    const queryMatch = haystack.includes(query);
    const viewMatch =
      state.viewMode === "recommendations"
        ? true
        : state.viewMode === "favorites"
        ? item.is_favorite
        : state.viewMode === "progress"
          ? item.watched || item.progress_episode_number != null
          : true;
    const filterMatch = filterDefinitions.every(definition => (
      definition.match(item, state.filters[definition.id])
    ));
    return queryMatch && viewMatch && filterMatch;
  }).sort(isRecommendationView() ? compareRecommendations : compareAnime);
  renderList();
  renderActiveFilters();
  el.resetFilters.hidden = !hasActiveCatalogTools();

  if (selectFirst && state.filtered.length && !state.filtered.some(item => item.id === state.selectedAnimeId)) {
    selectAnime(titleRefForItem(state.filtered[0])).catch(console.error);
  }
}

async function loadRecommendations() {
  const payload = await api(`/api/recommendations?limit=${RECOMMENDATION_LIMIT}`);
  state.recommendationProfile = payload.profile || null;
  state.recommendations = (payload.items || []).map(item => {
    const local = state.anime.find(entry => entry.id === item.id);
    return local ? { ...local, ...item } : item;
  });
}

el.search.addEventListener("input", () => applyFilter({ selectFirst: true }));
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
    state.viewMode = button.dataset.view || "all";
    for (const item of el.viewTabs) {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", active ? "true" : "false");
    }
    applyFilter({ selectFirst: true });
  });
}
el.favoriteToggle.addEventListener("click", () => {
  if (!state.detail) return;
  saveUserState({ is_favorite: !state.detail.is_favorite }).catch(console.error);
});
el.progressEpisode.addEventListener("change", () => {
  if (!state.detail) return;
  saveUserState({
    progress_episode_number: progressInputValue(),
    watched: false,
  }).catch(console.error);
});
el.watchedToggle.addEventListener("change", () => {
  if (!state.detail) return;
  saveUserState({ watched: el.watchedToggle.checked }).catch(console.error);
});
el.contentSource.addEventListener("change", event => {
  state.selectedContentSource = event.target.value || null;
  state.selectedTranslation = null;
  state.selectedSourceId = null;
  renderSources();
});
el.translation.addEventListener("change", event => {
  state.selectedTranslation = event.target.value;
  state.selectedSourceId = null;
  renderSources();
});
el.provider.addEventListener("change", event => {
  state.selectedSourceId = event.target.value;
  renderSources();
});
el.fullscreenToggle.addEventListener("click", () => {
  toggleFullscreen().catch(console.error);
});
el.pipToggle.addEventListener("click", () => {
  openPictureInPicture().catch(console.error);
});
document.addEventListener("fullscreenchange", updateFullscreenControl);
document.addEventListener("webkitfullscreenchange", updateFullscreenControl);
document.addEventListener("keydown", event => {
  if (!isFullscreenHotkey(event)) return;
  event.preventDefault();
  toggleFullscreen().catch(console.error);
});
window.addEventListener("resize", hideTitleTooltip);
el.list.addEventListener("scroll", hideTitleTooltip);
window.addEventListener("popstate", () => {
  const linkState = readLinkState();
  if (linkState.animeId) {
    selectAnime(linkState.animeId, { linkState, updateUrl: false }).catch(console.error);
  }
});

async function selectInitialAnime() {
  const linkState = readLinkState();
  if (linkState.animeId) {
    try {
      await selectAnime(linkState.animeId, { linkState });
      return;
    } catch (error) {
      console.warn("Failed to open shared anime link", error);
    }
  }

  const first = state.filtered.find(item => item.source_count > 0) || state.filtered[0] || state.anime[0];
  if (first) await selectAnime(titleRefForItem(first));
}

async function boot() {
  configurePlayerIframe(el.player);
  const payload = await api("/api/anime");
  state.anime = payload.items || [];
  await loadRecommendations();
  renderFilterControls();
  renderSortControls();
  applyFilter();
  await selectInitialAnime();
}

boot().catch(error => {
  clearPlayer(error.message);
  console.error(error);
});

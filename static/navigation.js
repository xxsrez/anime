/* URL contract shared by the app and regression tests. Legacy title URLs remain valid. */
(function (root) {
  const views = ["all", "recommendations", "updates", "library-all", "progress", "favorites", "completed", "maintenance"];
  const libraryViews = ["library-all", "progress", "favorites", "completed"];
  const viewNames = { all: "catalog", recommendations: "recommendations", updates: "new", "library-all": "library", progress: "watching", favorites: "favorites", completed: "completed", maintenance: "maintenance" };
  const sortDefaults = {
    all: { by: "rating_best", dir: "desc" },
    favorites: { by: "favorite_added", dir: "desc" },
    progress: { by: "watch_recent", dir: "desc" },
    completed: { by: "watch_completed", dir: "desc" },
  };
  const filterKeys = ["genre", "year", "kind", "status", "source", "editorial_status", "video"];
  function read(search) {
    const params = new URLSearchParams(search);
    return {
      view: Object.keys(viewNames).find(view => viewNames[view] === params.get("view")) || (views.includes(params.get("view")) ? params.get("view") : "all"),
      query: params.get("q") || "",
      sortBy: params.get("sort") || "",
      sortDir: params.get("dir") === "asc" ? "asc" : "desc",
      filters: Object.fromEntries(filterKeys.filter(key => params.has(`filter.${key}`)).map(key => [key, params.get(`filter.${key}`)])),
      days: ["3", "7", "30", "all"].includes(params.get("days")) ? params.get("days") : "7",
      updateType: params.get("event") || "all",
    };
  }
  function url(state) {
    const params = new URLSearchParams();
    const view = views.includes(state.viewMode) ? state.viewMode : "all";
    const defaults = sortDefaults[view] || sortDefaults.all;
    if (view !== "all") params.set("view", viewNames[view]);
    if (state.query) params.set("q", state.query);
    if (!["recommendations", "updates", "maintenance"].includes(view)) {
      if (state.sortBy && state.sortBy !== defaults.by) params.set("sort", state.sortBy);
      if (state.sortDir === "asc") params.set("dir", "asc");
    }
    for (const key of filterKeys) {
      const value = state.filters?.[key];
      if (value && !(key === "video" && value === "any")) params.set(`filter.${key}`, value);
    }
    if (state.viewMode === "updates") {
      if (state.contentUpdateDays && state.contentUpdateDays !== "7") params.set("days", state.contentUpdateDays);
      if (state.contentUpdateType && state.contentUpdateType !== "all") params.set("event", state.contentUpdateType);
    }
    return params.size ? `/?${params}` : "/";
  }
  function inLibrary(item, status) {
    return Boolean(item.is_favorite) || status === "watching" || status === "completed";
  }
  const sectionViews = { catalog: ["all", "recommendations", "updates"], library: libraryViews };
  const lastViews = {};
  function rememberedView(section, storage) {
    const allowed = sectionViews[section];
    if (!allowed) return null;
    let saved;
    try { saved = (storage || root.localStorage)?.getItem(`anime-last-view-${section}`); } catch { /* Storage may be unavailable. */ }
    return allowed.includes(saved) ? saved : lastViews[section] || allowed[0];
  }
  function rememberView(view, storage) {
    const section = Object.keys(sectionViews).find(key => sectionViews[key].includes(view));
    if (!section) return;
    lastViews[section] = view;
    try { (storage || root.localStorage)?.setItem(`anime-last-view-${section}`, view); } catch { /* Keep the choice for this page session. */ }
  }
  function updateSectionLinks() {
    for (const link of root.document?.querySelectorAll("a[data-section]") || []) {
      const view = rememberedView(link.dataset.section);
      if (view) link.setAttribute("href", url({ viewMode: view }));
    }
  }
  if (root.document) {
    root.document.addEventListener("DOMContentLoaded", updateSectionLinks);
    root.addEventListener("storage", updateSectionLinks);
  }
  const api = { views, libraryViews, sortDefaults, read, url, inLibrary, rememberedView, rememberView, updateSectionLinks };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.catalogNavigation = api;
})(typeof window === "undefined" ? globalThis : window);

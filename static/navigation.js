/* URL contract shared by the app and regression tests. Legacy title URLs remain valid. */
(function (root) {
  const views = ["all", "recommendations", "updates", "library-all", "progress", "favorites", "completed", "maintenance"];
  const libraryViews = ["library-all", "progress", "favorites", "completed"];
  const filterKeys = ["genre", "year", "kind", "status", "source", "editorial_status", "video"];
  function read(search) {
    const params = new URLSearchParams(search);
    return {
      view: views.includes(params.get("view")) ? params.get("view") : "all",
      query: params.get("q") || "",
      sortBy: params.get("sort") || "",
      sortDir: params.get("dir") === "asc" ? "asc" : "desc",
      filters: Object.fromEntries(filterKeys.filter(key => params.has(`filter.${key}`)).map(key => [key, params.get(`filter.${key}`)])),
      days: ["3", "7", "30", "all"].includes(params.get("days")) ? params.get("days") : "7",
      updateType: params.get("event") || "all",
    };
  }
  function url(state) {
    const params = new URLSearchParams({ view: state.viewMode });
    if (state.query) params.set("q", state.query);
    if (state.sortBy) { params.set("sort", state.sortBy); params.set("dir", state.sortDir); }
    for (const key of filterKeys) {
      const value = state.filters[key];
      if (value && !(key === "video" && value === "any")) params.set(`filter.${key}`, value);
    }
    if (state.viewMode === "updates") {
      params.set("days", state.contentUpdateDays);
      params.set("event", state.contentUpdateType);
    }
    return `/?${params}`;
  }
  function inLibrary(item, status) {
    return Boolean(item.is_favorite) || status === "watching" || status === "completed";
  }
  const api = { views, libraryViews, read, url, inLibrary };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.catalogNavigation = api;
})(typeof window === "undefined" ? globalThis : window);

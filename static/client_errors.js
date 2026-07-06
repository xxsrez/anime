(function () {
  const ENDPOINT = "/api/client-errors";
  const MAX_REPORTS_PER_PAGE = 12;
  const DEDUP_WINDOW_MS = 30000;
  const MAX_TEXT_LENGTH = 1800;
  const recentReports = new Map();
  let reportCount = 0;

  function text(value, fallback = "") {
    if (value == null) return fallback;
    if (typeof value === "string") return value;
    if (value instanceof Error) return value.message || fallback;
    try {
      return JSON.stringify(value);
    } catch (error) {
      return String(value);
    }
  }

  function trim(value, fallback = "") {
    const normalized = text(value, fallback);
    return normalized.length > MAX_TEXT_LENGTH
      ? `${normalized.slice(0, MAX_TEXT_LENGTH)}...[truncated]`
      : normalized;
  }

  function compactContext(context = {}) {
    const result = {};
    for (const [key, value] of Object.entries(context).slice(0, 20)) {
      if (value == null) continue;
      if (typeof value === "object") {
        result[key] = trim(value);
      } else {
        result[key] = value;
      }
    }
    return result;
  }

  function dedupeKey(payload) {
    const stackHead = String(payload.stack || "").split("\n").slice(0, 2).join("\n");
    return `${payload.type}|${payload.message}|${stackHead}`;
  }

  function shouldSend(payload) {
    if (reportCount >= MAX_REPORTS_PER_PAGE) return false;
    const key = dedupeKey(payload);
    const now = Date.now();
    const previous = recentReports.get(key) || 0;
    if (now - previous < DEDUP_WINDOW_MS) return false;
    recentReports.set(key, now);
    reportCount += 1;
    return true;
  }

  function payloadFromError(error, context = {}) {
    const message = trim(error?.message || error, "Unknown client error");
    return {
      type: trim(context.type || error?.name || "error", "error"),
      message,
      stack: trim(error?.stack || ""),
      timestamp: new Date().toISOString(),
      url: `${window.location.pathname}${window.location.search}${window.location.hash}`,
      path: window.location.pathname,
      source: trim(context.source || context.action || ""),
      lineno: context.lineno,
      colno: context.colno,
      userAgent: trim(navigator.userAgent || ""),
      context: compactContext(context),
    };
  }

  function reportClientError(error, context = {}) {
    const payload = payloadFromError(error, context);
    if (!shouldSend(payload)) return;
    fetch(ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }

  function reportActionError(action, context = {}) {
    return error => {
      reportClientError(error, { ...context, action });
      console.error(error);
    };
  }

  window.reportClientError = reportClientError;
  window.reportActionError = reportActionError;

  window.addEventListener("error", event => {
    reportClientError(event.error || event.message, {
      type: "window.error",
      source: event.filename || "window",
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  window.addEventListener("unhandledrejection", event => {
    reportClientError(event.reason || "Unhandled promise rejection", {
      type: "unhandledrejection",
      source: "window",
    });
  });
})();

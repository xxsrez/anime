(function (root) {
  "use strict";

  function createCatalogPoller({ request, refresh, isVisible, setTimer, clearTimer, interval = 30000 }) {
    let version = null;
    let pendingVersion = null;
    let timer = null;
    let running = false;
    let started = false;

    function schedule() {
      if (timer !== null) clearTimer(timer);
      timer = null;
      if (started && isVisible()) timer = setTimer(check, interval);
    }

    async function check() {
      if (!started || !isVisible() || running) return;
      if (timer !== null) clearTimer(timer);
      timer = null;
      running = true;
      try {
        if (pendingVersion === null) {
          const result = await request(version);
          if (typeof result?.version === "string") pendingVersion = result.version;
        }
        if (pendingVersion !== null) {
          await refresh();
          // Keep the version observed before refresh: a concurrent write must
          // still be detected by the next request.
          version = pendingVersion;
          pendingVersion = null;
        }
      } catch (_) {
        // A failed request or refresh must not acknowledge the version.
      } finally {
        running = false;
        schedule();
      }
    }

    return {
      start(initialVersion) {
        version = initialVersion || null;
        started = true;
        return check();
      },
      visibilityChanged() {
        if (isVisible()) return check();
        if (timer !== null) clearTimer(timer);
        timer = null;
      },
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { createCatalogPoller };
  else root.catalogPoll = { createCatalogPoller };
})(typeof globalThis !== "undefined" ? globalThis : this);

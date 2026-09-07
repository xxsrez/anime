(() => {
  "use strict";

  const extensionApi = globalThis.browser ?? globalThis.chrome;
  const ALLOWED_ORIGINS = new Set([
    "http://127.0.0.1:8765",
    "https://anime-srez.up.railway.app",
  ]);
  const FORWARDED_EVENTS = new Set([
    "animego-scan-progress",
    "animego-scan-complete",
    "animego-scan-error",
    "animego-scanner-permission-granted",
  ]);

  function announceReady() {
    document.dispatchEvent(
      new CustomEvent("animego-scanner-ready", {
        detail: {
          version: extensionApi.runtime.getManifest().version,
        },
      }),
    );
  }

  function plainClone(value) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_error) {
      return null;
    }
  }

  document.addEventListener("animego-scanner-ping", announceReady);
  document.addEventListener("animego-scanner-prepare", () => {
    if (!ALLOWED_ORIGINS.has(location.origin)) {
      return;
    }
    extensionApi.runtime
      .sendMessage({ type: "animego-scanner-prepare" })
      .then((response) => {
        if (response?.ok === false) {
          throw new Error(response.error || "Не удалось проверить доступ к AnimeGo.");
        }
        document.dispatchEvent(
          new CustomEvent("animego-scanner-permission-state", {
            detail: { granted: Boolean(response?.granted) },
          }),
        );
      })
      .catch((error) => {
        document.dispatchEvent(
          new CustomEvent("animego-scanner-permission-state", {
            detail: {
              granted: false,
              error: error?.message || "Не удалось проверить доступ к AnimeGo.",
            },
          }),
        );
      });
  });
  document.addEventListener("animego-scanner-open", () => {
    if (!ALLOWED_ORIGINS.has(location.origin)) {
      return;
    }
    extensionApi.runtime
      .sendMessage({ type: "animego-scanner-open" })
      .then((response) => {
        if (response?.ok === false) {
          throw new Error(response.error || "Не удалось открыть сканер.");
        }
      })
      .catch((error) => {
        document.dispatchEvent(
          new CustomEvent("animego-scan-error", {
            detail: { error: error?.message || "Не удалось открыть сканер." },
          }),
        );
      });
  });
  document.addEventListener("animego-scan-start", (event) => {
    if (!ALLOWED_ORIGINS.has(location.origin)) {
      return;
    }
    const detail = plainClone(event.detail);
    if (!detail || detail.origin !== location.origin) {
      document.dispatchEvent(
        new CustomEvent("animego-scan-error", {
          detail: { error: "Некорректный источник задания сканирования." },
        }),
      );
      return;
    }
    extensionApi.runtime
      .sendMessage({ type: "animego-scan-start", detail })
      .then((response) => {
        if (response?.ok === false) {
          throw new Error(response.error || "Не удалось запустить расширение.");
        }
      })
      .catch((error) => {
        document.dispatchEvent(
          new CustomEvent("animego-scan-error", {
            detail: { error: error?.message || "Не удалось запустить расширение." },
          }),
        );
      });
  });

  extensionApi.runtime.onMessage.addListener((message) => {
    if (!message || !FORWARDED_EVENTS.has(message.type)) {
      return;
    }
    document.dispatchEvent(
      new CustomEvent(message.type, {
        detail: plainClone(message.detail) || {},
      }),
    );
  });

  announceReady();
  window.addEventListener("pageshow", announceReady, { once: true });
})();

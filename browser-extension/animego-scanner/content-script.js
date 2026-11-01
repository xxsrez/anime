(() => {
  "use strict";

  const extensionApi = globalThis.browser?.runtime ? globalThis.browser : globalThis.chrome;
  const RELOAD_MESSAGE = "Связь с расширением потеряна. Обновите страницу Anime Catalog и повторите сканирование.";
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
    let manifest;
    try {
      manifest = extensionApi?.runtime?.getManifest();
    } catch (_error) {
      return;
    }
    if (!manifest) return;
    document.dispatchEvent(
      new CustomEvent("animego-scanner-ready", {
        detail: {
          version: manifest.version,
        },
      }),
    );
  }

  // Reloading an unpacked extension invalidates content scripts in open tabs.
  // An async boundary also catches synchronous sendMessage failures.
  async function sendMessage(message) {
    try {
      const runtime = extensionApi?.runtime;
      if (!runtime?.sendMessage) throw new Error(RELOAD_MESSAGE);
      runtime.getManifest();
      return await runtime.sendMessage(message);
    } catch (error) {
      if (/Extension context invalidated/i.test(error?.message || "")) {
        throw new Error(RELOAD_MESSAGE);
      }
      throw error;
    }
  }

  function plainClone(value) {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_error) {
      return null;
    }
  }

  function catalogTheme() {
    const theme = document.documentElement?.dataset?.theme;
    return theme === "light" || theme === "dark" ? theme : null;
  }

  if (document.documentElement && typeof MutationObserver === "function") {
    let previousTheme = catalogTheme();
    new MutationObserver(() => {
      const theme = catalogTheme();
      if (theme === previousTheme || !ALLOWED_ORIGINS.has(location.origin)) return;
      previousTheme = theme;
      sendMessage({ type: "animego-scanner-theme", theme }).catch(() => {});
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  document.addEventListener("animego-scanner-ping", announceReady);
  document.addEventListener("animego-scanner-prepare", () => {
    if (!ALLOWED_ORIGINS.has(location.origin)) {
      return;
    }
    sendMessage({ type: "animego-scanner-prepare", theme: catalogTheme() })
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
    sendMessage({ type: "animego-scanner-open", theme: catalogTheme() })
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
    detail.theme = catalogTheme();
    sendMessage({ type: "animego-scan-start", detail })
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

  extensionApi?.runtime?.onMessage?.addListener((message, _sender, sendResponse) => {
    if (message?.type === "animego-scanner-create-job") {
      if (!ALLOWED_ORIGINS.has(location.origin) || !["partial", "full"].includes(message.mode)) {
        sendResponse({ ok: false, error: "Некорректный запрос сканирования." });
        return false;
      }
      (async () => {
        const response = await fetch("/api/animego-scans", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: message.mode }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(response.status === 409
          ? `На сервере уже идёт скан №${payload.job?.id || "?"} (${payload.job?.checked_items || 0}/${payload.job?.total_items || "?"}). Откройте его вкладку сканера. Срок задания: ${payload.job?.expires_at || "не указан"}.`
          : payload.error || payload.message || `Ошибка сервера: ${response.status}`);
        const tasks = payload.tasks || payload.items || [];
        if (!tasks.length && !payload.discovery) {
          return { ok: true, empty: true, message: payload.message || "Сейчас нечего проверять — каталог актуален." };
        }
        const detail = {
          job: payload.job || null,
          job_id: payload.job_id ?? payload.job?.id ?? payload.job,
          token: payload.token || payload.job_token,
          tasks, discovery: payload.discovery === true,
          origin: location.origin, mode: message.mode, theme: catalogTheme(),
        };
        try {
          const saved = await sendMessage({ type: "animego-scanner-persist-job", detail });
          if (!saved?.ok) throw new Error(saved?.error || "Не удалось сохранить новое задание в расширении.");
        } catch (error) {
          // Only cancel the job just created by this request, never a conflicting job.
          try {
            const stopped = await fetch(`/api/animego-scans/${encodeURIComponent(detail.job_id)}/complete`, {
              method: "POST", credentials: "omit",
              headers: { "Content-Type": "application/json", Authorization: `Bearer ${detail.token}` },
              body: JSON.stringify({ stopped: true }),
            });
            if (!stopped.ok) throw new Error("stop failed");
          } catch {
            throw new Error(`Не удалось передать или отменить задание №${detail.job_id}. Оно может занимать очередь. ${error.message}`);
          }
          throw new Error(`Задание №${detail.job_id} отменено: ${error.message}`);
        }
        return { ok: true, detail };
      })().then(sendResponse).catch(error => sendResponse({ ok: false, error: error.message }));
      return true;
    }
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

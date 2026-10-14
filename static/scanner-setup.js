(function scannerSetupModule(globalScope) {
  "use strict";

  function versionMessage(installed, available) {
    const valid = value => typeof value === "string" && /^\d+(\.\d+){2,3}$/.test(value);
    const current = valid(installed) ? installed : null;
    const latest = valid(available) ? available : null;
    const parts = [current ? `Установлена: ${current}.` : "Расширение не ответило. Перезагрузите его и обновите вкладку.",
      latest ? `Доступна: ${latest}.` : "Доступную версию проверить не удалось. Войдите в каталог и обновите страницу."];
    if (current && latest) {
      const a = current.split(".").map(Number);
      const b = latest.split(".").map(Number);
      let comparison = 0;
      for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
        comparison = (a[i] || 0) - (b[i] || 0);
        if (comparison) break;
      }
      parts.push(comparison < 0 ? "Доступно обновление." : comparison === 0 ? "Версия актуальна." : "Установлена более новая версия.");
    }
    return parts.join(" ");
  }

  function trackVersions(documentObject, environment) {
    const status = documentObject.getElementById("scanner-version-state");
    if (!status) return;
    let installed = null;
    let available = null;
    const render = () => { status.textContent = versionMessage(installed, available); };
    documentObject.addEventListener("animego-scanner-ready", event => {
      installed = event.detail?.version;
      render();
    });
    documentObject.dispatchEvent(new environment.CustomEvent("animego-scanner-ping"));
    environment.fetch("/api/app-config", { cache: "no-store" })
      .then(response => { if (!response.ok) throw new Error("Config unavailable"); return response.json(); })
      .then(config => { available = config.scanner_version; render(); })
      .catch(render);
    environment.setTimeout(render, 1500);
  }

  function detectBrowser(userAgent = "") {
    const safari = /Safari\//i.test(userAgent);
    const anotherBrowser = /(Chrome|Chromium|CriOS|Edg|EdgiOS|EdgA|OPR|OPiOS|Firefox|FxiOS|SamsungBrowser|Android)/i
      .test(userAgent);
    return safari && !anotherBrowser ? "safari" : "chrome";
  }

  function createBrowserTabs({ tabs, panels, initialBrowser = "chrome" }) {
    const tabList = Array.from(tabs || []);
    const panelList = Array.from(panels || []);
    const browserNames = tabList.map(tab => tab.dataset.browser);

    function activate(browser, { focus = false } = {}) {
      if (!browserNames.includes(browser)) return false;

      tabList.forEach(tab => {
        const active = tab.dataset.browser === browser;
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      panelList.forEach(panel => {
        panel.hidden = panel.dataset.browserPanel !== browser;
      });
      return true;
    }

    tabList.forEach((tab, index) => {
      tab.addEventListener("click", () => activate(tab.dataset.browser));
      tab.addEventListener("keydown", event => {
        let targetIndex = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          targetIndex = (index + 1) % tabList.length;
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          targetIndex = (index - 1 + tabList.length) % tabList.length;
        } else if (event.key === "Home") {
          targetIndex = 0;
        } else if (event.key === "End") {
          targetIndex = tabList.length - 1;
        }

        if (targetIndex === null) return;
        event.preventDefault();
        activate(tabList[targetIndex].dataset.browser, { focus: true });
      });
    });

    activate(initialBrowser);
    return { activate };
  }

  function initScannerSetup(documentObject, navigatorObject) {
    const tabs = documentObject.querySelectorAll('[role="tab"][data-browser]');
    const panels = documentObject.querySelectorAll('[role="tabpanel"][data-browser-panel]');
    const initialBrowser = detectBrowser(navigatorObject?.userAgent);
    const tabController = createBrowserTabs({ tabs, panels, initialBrowser });
    const copyButton = documentObject.getElementById("copy-extensions-url");
    const setupState = documentObject.getElementById("setup-state");

    copyButton?.addEventListener("click", async () => {
      try {
        await navigatorObject.clipboard.writeText("chrome://extensions");
        setupState.textContent = "Адрес скопирован.";
      } catch (error) {
        setupState.textContent = "Скопируйте chrome://extensions вручную.";
      }
    });

    return tabController;
  }

  const api = { createBrowserTabs, detectBrowser, initScannerSetup, versionMessage, trackVersions };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (globalScope.document) {
    initScannerSetup(globalScope.document, globalScope.navigator || {});
    trackVersions(globalScope.document, globalScope);
  }
})(typeof globalThis === "undefined" ? this : globalThis);

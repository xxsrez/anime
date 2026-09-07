(function scannerSetupModule(globalScope) {
  "use strict";

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

  const api = { createBrowserTabs, detectBrowser, initScannerSetup };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (globalScope.document) initScannerSetup(globalScope.document, globalScope.navigator || {});
})(typeof globalThis === "undefined" ? this : globalThis);

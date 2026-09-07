const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");

const { createBrowserTabs, detectBrowser } = require("./scanner-setup.js");

function interactiveElement(dataset) {
  const attributes = new Map();
  const listeners = new Map();
  return {
    dataset,
    hidden: false,
    tabIndex: 0,
    focused: false,
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    focus() {
      this.focused = true;
    },
    dispatch(type, event = {}) {
      listeners.get(type)?.(event);
    },
  };
}

function tabsHarness(initialBrowser) {
  const safariTab = interactiveElement({ browser: "safari" });
  const chromeTab = interactiveElement({ browser: "chrome" });
  const safariPanel = interactiveElement({ browserPanel: "safari" });
  const chromePanel = interactiveElement({ browserPanel: "chrome" });
  const controller = createBrowserTabs({
    tabs: [safariTab, chromeTab],
    panels: [safariPanel, chromePanel],
    initialBrowser,
  });
  return { chromePanel, chromeTab, controller, safariPanel, safariTab };
}

test("detectBrowser distinguishes desktop Safari from Chromium user agents", () => {
  assert.equal(detectBrowser(
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
  ), "safari");
  assert.equal(detectBrowser(
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
  ), "chrome");
  assert.equal(detectBrowser(
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
      + "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/140.0 Mobile/15E148 Safari/605.1.15",
  ), "chrome");
  assert.equal(detectBrowser(""), "chrome");
});

test("initial selection and click switch the visible browser panel", () => {
  const harness = tabsHarness("safari");
  assert.equal(harness.safariTab.getAttribute("aria-selected"), "true");
  assert.equal(harness.safariTab.tabIndex, 0);
  assert.equal(harness.safariPanel.hidden, false);
  assert.equal(harness.chromePanel.hidden, true);

  harness.chromeTab.dispatch("click");
  assert.equal(harness.safariTab.getAttribute("aria-selected"), "false");
  assert.equal(harness.chromeTab.getAttribute("aria-selected"), "true");
  assert.equal(harness.safariPanel.hidden, true);
  assert.equal(harness.chromePanel.hidden, false);
});

test("arrow keys switch tabs and move keyboard focus", () => {
  const harness = tabsHarness("safari");
  let prevented = false;
  harness.safariTab.dispatch("keydown", {
    key: "ArrowRight",
    preventDefault() {
      prevented = true;
    },
  });

  assert.equal(prevented, true);
  assert.equal(harness.chromeTab.getAttribute("aria-selected"), "true");
  assert.equal(harness.chromeTab.focused, true);
  assert.equal(harness.chromePanel.hidden, false);
});

test("setup page exposes accessible tabs and the required Safari 26 guidance", () => {
  const html = fs.readFileSync(`${__dirname}/scanner-setup.html`, "utf8");
  assert.match(html, /role="tablist"/);
  assert.match(html, /role="tab"/);
  assert.match(html, /role="tabpanel"/);
  assert.match(html, /Safari → Settings → Advanced/);
  assert.match(html, /Show features for web developers/);
  assert.match(html, /Safari → Settings → Developer/);
  assert.match(html, /Add Temporary Extension/);
  assert.match(html, /ZIP или распакованную папку/);
  assert.match(html, /через 24 часа или при выходе из Safari/);
  assert.match(html, /Anime Catalog/);
  assert.match(html, /animego\.me/);
  assert.match(html, /Разрешить AnimeGo/);
  assert.match(html, /задание создастся только после разрешения/);
  assert.match(html, /перезагрузите страницу/);
  assert.match(html, /chrome:\/\/extensions/);
});

(() => {
  const preference = window.matchMedia("(prefers-color-scheme: dark)");
  let saved;
  try { saved = localStorage.getItem("anime-theme"); } catch { /* Storage may be disabled. */ }
  function apply(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#101317" : "#eae5db");
    for (const button of document.querySelectorAll("[data-theme-toggle]")) {
      button.textContent = theme === "dark" ? "Светлая тема" : "Тёмная тема";
      button.setAttribute("aria-label", `Включить ${theme === "dark" ? "светлую" : "тёмную"} тему`);
    }
  }
  apply(saved === "light" || saved === "dark" ? saved : preference.matches ? "dark" : "light");
  preference.addEventListener("change", event => { if (!saved) apply(event.matches ? "dark" : "light"); });
  document.addEventListener("DOMContentLoaded", () => {
    apply(document.documentElement.dataset.theme);
    document.addEventListener("click", event => {
      if (!event.target.closest("[data-theme-toggle]")) return;
      saved = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("anime-theme", saved); } catch { /* Keep in-memory preference. */ }
      apply(saved);
    });
  });
})();

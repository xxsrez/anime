(() => {
  const service = document.body.dataset.service;
  const rail = document.createElement("nav");
  rail.className = "navigation-rail";
  rail.setAttribute("aria-label", "Основные разделы");
  const brand = document.createElement("a");
  brand.className = "wordmark";
  brand.href = "/";
  brand.append("ANIME ");
  const suffix = document.createElement("span");
  suffix.textContent = "CATALOG";
  brand.append(suffix);
  const main = document.createElement("div");
  main.className = "navigation-main";
  const services = document.createElement("div");
  services.className = "navigation-service";
  const caption = document.createElement("span");
  caption.className = "navigation-caption";
  caption.textContent = "Служебное";
  services.append(caption);
  function link(parent, title, href, current = false) {
    const node = document.createElement("a");
    node.textContent = title;
    node.href = href;
    if (current) node.setAttribute("aria-current", "page");
    parent.append(node);
    return node;
  }
  link(main, "Каталог", "/").dataset.section = "catalog";
  link(main, "Библиотека", "/?view=library").dataset.section = "library";
  link(services, "Обновление каталога", "/?view=maintenance", service === "scanner");
  if (service === "admin") link(services, "Администрирование", "/admin", true);
  else fetch("/api/me").then(response => response.ok ? response.json() : null).then(data => {
    if (data?.user?.is_admin) link(services, "Администрирование", "/admin");
  }).catch(() => {});
  const footer = document.createElement("div");
  footer.className = "navigation-footer";
  const theme = document.querySelector("[data-theme-toggle]");
  if (theme) { theme.classList.remove("theme-service-toggle"); footer.append(theme); }
  rail.append(brand, main, services, footer);
  document.body.prepend(rail);
  window.catalogNavigation.updateSectionLinks();
})();

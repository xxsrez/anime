/* Move the existing controls, preserving their state and event listeners. */
(() => {
  const rail = document.querySelector('.navigation-rail');
  const search = document.querySelector('.search-box');
  const tools = document.querySelector('.catalog-tools');
  if (!rail || !search || !tools) return;
  document.body.classList.add('mobile-layout-ready');
  const mobile = matchMedia('(max-width: 700px)');
  const scannerLink = document.createElement('a');
  scannerLink.href = '/scanner-setup';
  scannerLink.textContent = 'Настройка сканера';
  scannerLink.className = 'mobile-scanner-link';
  document.querySelector('.navigation-service').append(scannerLink);
  const dialog = document.createElement('dialog');
  dialog.className = 'mobile-sheet';
  dialog.setAttribute('aria-labelledby', 'mobile-sheet-title');
  dialog.innerHTML = '<div class="mobile-sheet-header"><h2 id="mobile-sheet-title"></h2><button type="button" aria-label="Закрыть">×</button></div><div class="mobile-sheet-content"></div><button type="button" class="mobile-sheet-done">Готово</button>';
  document.body.append(dialog);
  const content = dialog.querySelector('.mobile-sheet-content');
  let moved = [];
  let opener;
  let scrollY = 0;
  function restore() {
    for (const [node, marker] of moved) marker.replaceWith(node);
    moved = [];
    document.body.classList.remove('mobile-sheet-open');
    document.body.style.removeProperty('top');
    window.scrollTo(0, scrollY);
    opener?.setAttribute('aria-expanded', 'false');
    opener?.focus({ preventScroll: true });
  }
  function open(button, title, nodes) {
    if (!mobile.matches || dialog.open) return;
    opener = button;
    scrollY = window.scrollY;
    dialog.querySelector('h2').textContent = title;
    for (const node of nodes.filter(Boolean)) {
      const marker = document.createComment('mobile-sheet-slot');
      node.before(marker);
      content.append(node);
      moved.push([node, marker]);
    }
    document.body.style.top = `-${scrollY}px`;
    document.body.classList.add('mobile-sheet-open');
    button.setAttribute('aria-expanded', 'true');
    dialog.showModal();
  }
  function trigger(label, icon) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mobile-control';
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.innerHTML = icon;
    return button;
  }
  const menu = trigger('Открыть меню', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>');
  rail.append(menu);
  const filters = trigger('Фильтры и сортировка', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h4m4 0h10M3 17h10m4 0h4"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="17" r="2"/></svg>');
  search.after(filters);
  filters.classList.add('mobile-filter-toggle');
  search.querySelector('input').placeholder = 'Поиск';
  menu.addEventListener('click', () => open(menu, 'Меню', [document.querySelector('.navigation-footer'), document.querySelector('.navigation-service')]));
  filters.addEventListener('click', () => open(filters, 'Фильтры и сортировка', [tools]));
  dialog.querySelector('.mobile-sheet-header button').addEventListener('click', () => dialog.close());
  dialog.querySelector('.mobile-sheet-done').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', restore);
  dialog.addEventListener('click', event => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (event.clientY < rect.top || event.clientX < rect.left || event.clientX > rect.right) dialog.close();
    }
    if (event.target.closest('a')) dialog.close();
  });
  mobile.addEventListener('change', () => { if (dialog.open) dialog.close(); });
  const updateBadge = () => {
    const count = document.querySelectorAll('#filter-grid .filter-control').length;
    filters.classList.toggle('has-filters', count > 0);
    filters.setAttribute('aria-label', count ? `Фильтры и сортировка: ${count} фильтров` : 'Фильтры и сортировка');
  };
  new MutationObserver(updateBadge).observe(document.getElementById('filter-grid'), { childList: true });
  updateBadge();
})();

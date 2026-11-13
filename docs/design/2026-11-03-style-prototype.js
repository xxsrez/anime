const titles = [
  {name: 'Сасаки и Пи 2', subtitle: 'Sasaki to Pii-chan Season 2', genre: 'Фэнтези', score: '7.25', year: '2026', favorite: true, watching: true},
  {name: 'Пожиратель звёзд 3', subtitle: 'Swallowed Star', genre: 'Приключения', score: '8.2', year: '2023', watching: true},
  {name: 'Поднятие уровня в одиночку: как стать сильнее', subtitle: 'Solo Leveling', genre: 'Приключения', score: '8.1', year: '2024', favorite: true},
  {name: 'Магия вернувшегося должна быть особенной 2', subtitle: 'A Returner’s Magic Should Be Special', genre: 'Фэнтези', score: '7.4', year: '2025', completed: true}
];
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const asset = () => 'style-prototype-placeholder.svg';
// Повторяем порядок действующего экрана: плеер и его настройки, затем сведения.
const detailInfo = document.createElement('section');
detailInfo.className = 'title-info';
detailInfo.innerHTML = '<h2>О тайтле</h2>';
for (const selector of ['.chips', '.description', '.franchise']) detailInfo.append($('#detail ' + selector));
$('#detail').append(detailInfo);
$('#detail .detail-actions select option').textContent = 'Не смотрю';
const episodeHeading = $('#episodes').previousElementSibling;
$('.player-settings').prepend(episodeHeading, $('#episodes'));
let currentTab = 'Все';
let libraryTab = 'Все';
let activeTitle = 0;
let toastTimer;
function toast(message) { const el = $('#toast'); el.textContent = message; el.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => el.hidden = true, 3500); }
function page(id) {
  $$('.page').forEach(el => el.hidden = el.id !== id);
  $$('.rail-link').forEach(el => el.classList.toggle('active', el.dataset.page === (id === 'detail' || id === 'franchise' ? 'catalog' : id === 'setup' ? 'maintenance' : id)));
  window.scrollTo(0, 0);
}
function card(item) {
  const index = titles.indexOf(item);
  return `<button class="card" data-title="${index}"><span class="cover"><img src="${asset()}" alt=""><span class="score">★ ${item.score}</span>${item.watching ? '<span class="cover-badge">Смотрю</span>' : ''}</span><span class="card-body"><strong>${item.name}</strong><span class="card-meta">Сериал · ${item.year} · ${item.genre}</span><span class="card-genre">${item.watching ? 'Есть что смотреть · пример' : 'Данные о сериях · пример'}</span></span></button>`;
}
function render() {
  const query = $('#search').value.toLocaleLowerCase('ru');
  const genre = $('#genre').value;
  let list = titles.filter(item => item.name.toLocaleLowerCase('ru').includes(query) && (!genre || item.genre === genre));
  if ($('#sort').value === 'По названию') list.sort((a,b) => a.name.localeCompare(b.name, 'ru'));
  if (currentTab === 'Новое') list = list.slice(0, 2);
  if (currentTab === 'Рекомендации') list = list.slice().reverse();
  $('#cards').innerHTML = list.map((item, index) => card(item).replace('</span></button>', `${currentTab === 'Рекомендации' ? `<span class="recommendation">${index+1}. Похоже на то, что вы смотрите</span>` : ''}</span></button>`)).join('');
  $('#cards').hidden = currentTab === 'Новое';
  $('#updates-demo').hidden = currentTab !== 'Новое';
  $('#empty').hidden = list.length > 0 || currentTab === 'Новое';
  $('#listing-title').textContent = currentTab === 'Все' ? 'Все тайтлы' : currentTab;
  $('#listing-count').textContent = currentTab === 'Новое' ? 'Серии, тайтлы, озвучки и плееры · пример' : `${list.length} демонстрационных тайтла`;
  const library = titles.filter(x => libraryTab === 'Все' ? x.favorite || x.watching || x.completed : libraryTab === 'Смотрю' ? x.watching : libraryTab === 'Избранное' ? x.favorite : x.completed);
  $('#library-cards').innerHTML = libraryTab === 'Смотрю' ? `<h3 class="library-heading">Продолжить просмотр</h3>${library.slice(0,1).map(card).join('')}<h3 class="library-heading">Есть что смотреть</h3>${library.slice(1).map(card).join('')}` : library.map(card).join('');
  $('#library-cards').classList.toggle('library-list', libraryTab === 'Смотрю');
}
function openTitle(index) {
  activeTitle = index;
  const item = titles[index];
  $('#detail-title').textContent = item.name;
  $('#detail-subtitle').textContent = item.subtitle;
  $('#detail-meta').textContent = `Сериал · ${item.year} · пример`;
  $('#detail-genre').textContent = item.genre;
  $('#detail .franchise strong').textContent = 'Пример оформления франшизы';
  $('#detail-poster').src = asset();
  $('#detail-poster').alt = `Макет обложки: ${item.name}`;
  $('#favorite').textContent = item.favorite ? '♥  В избранном' : '♡  В избранное';
  page('detail');
}
document.addEventListener('click', event => {
  const nav = event.target.closest('[data-page]'); if (nav) page(nav.dataset.page);
  const title = event.target.closest('[data-title]'); if (title) openTitle(Number(title.dataset.title));
  const demo = event.target.closest('[data-demo]'); if (demo) toast(demo.dataset.demo);
});
$('#episodes').innerHTML = `<button id="prev-episode" aria-label="Предыдущая серия">‹</button><select id="episode-select" aria-label="Серия">${Array.from({length: 12}, (_,i) => `<option value="${i+1}">Серия ${i+1}</option>`).join('')}</select><button id="next-episode" aria-label="Следующая серия">›</button>`;
function episodeStep(step) { const select = $('#episode-select'); select.value = Math.max(1, Math.min(12, Number(select.value) + step)); $('#episode-number').textContent = select.value; }
$('#episode-select').onchange = () => episodeStep(0); $('#prev-episode').onclick = () => episodeStep(-1); $('#next-episode').onclick = () => episodeStep(1);
$('#catalog-tabs').addEventListener('click', event => { if (!event.target.dataset.tab) return; currentTab = event.target.dataset.tab; $$('#catalog-tabs button').forEach(b => b.classList.toggle('active', b === event.target)); render(); });
$('#library-tabs').addEventListener('click', event => { if (event.target.tagName !== 'BUTTON') return; libraryTab = event.target.textContent; $$('#library-tabs button').forEach(b => b.classList.toggle('active', b === event.target)); render(); });
$('#search').addEventListener('input', render); $('#genre').addEventListener('change', render); $('#sort').addEventListener('change', render);
$('#filter-toggle').onclick = () => { $('#filters').hidden = !$('#filters').hidden; };
$('#reset').onclick = () => { $('#search').value = ''; $('#genre').value = ''; render(); };
$('#favorite').onclick = () => { titles[activeTitle].favorite = !titles[activeTitle].favorite; $('#favorite').textContent = titles[activeTitle].favorite ? '♥  В избранном' : '♡  В избранное'; render(); };
$('#share').onclick = () => toast('В прототипе ссылка не создаётся.');
$('#player-menu').onclick = () => toast('Действия с серией показаны для примера.');
function toggleTheme() { document.body.classList.toggle('light'); $('#theme').textContent = document.body.classList.contains('light') ? '☾   Тёмная тема' : '☼   Светлая тема'; }
$('#theme').onclick = toggleTheme; $('#mobile-theme').onclick = toggleTheme;
render();

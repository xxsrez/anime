const reportClientError = window.reportClientError || (() => {});
const reportActionError = window.reportActionError || (() => error => console.error(error));

const el = {
  account: document.getElementById("admin-account"),
  logout: document.getElementById("admin-logout"),
  summary: document.getElementById("admin-summary"),
  users: document.getElementById("admin-users"),
  usersCount: document.getElementById("admin-users-count"),
  topTitles: document.getElementById("admin-top-titles"),
  state: document.getElementById("admin-state"),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 401) {
    window.location.replace(`/login?next=${encodeURIComponent("/admin")}`);
    throw new Error("authentication required");
  }
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function number(value) {
  return new Intl.NumberFormat("ru-RU").format(Number(value || 0));
}

function dateText(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function setState(message = "", tone = "") {
  el.state.textContent = message;
  el.state.dataset.tone = tone;
}

function metric(label, value) {
  const node = document.createElement("div");
  node.className = "admin-metric";
  const caption = document.createElement("span");
  caption.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = number(value);
  node.append(caption, strong);
  return node;
}

function renderSummary(summary) {
  el.summary.replaceChildren(
    metric("Пользователи", summary.registered_users),
    metric("Входили за 7 дней", summary.recent_logins),
    metric("С активной сессией", summary.active_sessions),
    metric("С избранным", summary.users_with_favorites),
    metric("С прогрессом", summary.users_with_progress),
    metric("Избранных тайтлов", summary.total_favorites),
    metric("Тайтлов в прогрессе", summary.total_progress_titles),
    metric("Просмотренных", summary.total_watched_titles),
  );
}

function userRole(user) {
  const node = document.createElement("span");
  node.className = user.is_admin ? "admin-pill warn" : "admin-pill";
  node.textContent = user.is_admin ? "админ" : "пользователь";
  return node;
}

function renderUsers(users) {
  el.usersCount.textContent = `${number(users.length)} всего`;
  el.users.replaceChildren();
  for (const user of users) {
    const row = document.createElement("tr");

    const account = document.createElement("td");
    account.className = "admin-user-cell";
    const name = document.createElement("strong");
    name.textContent = user.name || user.email || "Google user";
    const email = document.createElement("span");
    email.textContent = user.email || "-";
    account.append(name, email);

    const role = document.createElement("td");
    role.append(userRole(user));

    const favorite = document.createElement("td");
    favorite.textContent = number(user.favorite_titles);

    const progress = document.createElement("td");
    progress.textContent = number(user.progress_titles);

    const watched = document.createElement("td");
    watched.textContent = number(user.watched_titles);

    const login = document.createElement("td");
    login.className = "admin-cell-muted";
    login.textContent = dateText(user.last_login_at || user.created_at);

    const sessions = document.createElement("td");
    sessions.className = "admin-cell-muted";
    sessions.textContent = `${number(user.active_sessions)} / ${dateText(user.last_session_at)}`;

    row.append(account, role, favorite, progress, watched, login, sessions);
    el.users.append(row);
  }
}

function renderTopTitles(titles) {
  el.topTitles.replaceChildren();
  if (!titles.length) {
    const empty = document.createElement("div");
    empty.className = "admin-title-item";
    const text = document.createElement("span");
    text.textContent = "Пока нет пользовательской активности";
    empty.append(text);
    el.topTitles.append(empty);
    return;
  }

  for (const title of titles) {
    const item = document.createElement("div");
    item.className = "admin-title-item";
    const name = document.createElement("strong");
    name.textContent = title.title || `#${title.anime_id}`;
    const meta = document.createElement("span");
    meta.textContent = [
      `${number(title.users)} польз.`,
      `${number(title.favorites)} избранное`,
      `${number(title.in_progress)} прогресс`,
      `${number(title.watched)} просмотрено`,
      title.source,
    ].filter(Boolean).join(" · ");
    item.append(name, meta);
    el.topTitles.append(item);
  }
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  window.location.replace("/login");
}

async function boot() {
  const me = await api("/api/me");
  if (!me.user?.is_admin) {
    setState("Недоступно", "warn");
    return;
  }
  el.account.textContent = me.user.email || me.user.name || "";

  const payload = await api("/api/admin/users");
  renderSummary(payload.summary || {});
  renderUsers(payload.users || []);
  renderTopTitles(payload.top_titles || []);
  setState(`Обновлено: ${dateText(payload.generated_at)}`);
}

el.logout.addEventListener("click", () => {
  logout().catch(reportActionError("admin logout"));
});

boot().catch(error => {
  reportClientError(error, { action: "boot admin" });
  setState(error.message, "warn");
  console.error(error);
});

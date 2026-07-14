const reportClientError = window.reportClientError || (() => {});
const reportActionError = window.reportActionError || (() => error => console.error(error));

const el = {
  account: document.getElementById("admin-account"),
  logout: document.getElementById("admin-logout"),
  summary: document.getElementById("admin-summary"),
  telemetryScope: document.getElementById("admin-telemetry-scope"),
  users: document.getElementById("admin-users"),
  usersCount: document.getElementById("admin-users-count"),
  topTitles: document.getElementById("admin-top-titles"),
  recentActivity: document.getElementById("admin-recent-activity"),
  telemetryStart: document.getElementById("admin-telemetry-start"),
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

function durationText(value) {
  const seconds = Math.max(0, Number(value || 0));
  if (!seconds) return "0 мин";
  if (seconds < 60) return "<1 мин";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (!hours) return `${number(minutes)} мин`;
  return remainingMinutes ? `${number(hours)} ч ${number(remainingMinutes)} мин` : `${number(hours)} ч`;
}

function setState(message = "", tone = "") {
  el.state.textContent = message;
  el.state.dataset.tone = tone;
}

function metric(label, value, { formatter = number, description = "" } = {}) {
  const node = document.createElement("div");
  node.className = "admin-metric";
  const caption = document.createElement("span");
  caption.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = formatter(value);
  node.append(caption, strong);
  if (description) {
    const help = document.createElement("small");
    help.className = "admin-metric-description";
    help.textContent = description;
    node.append(help);
  }
  return node;
}

function renderSummary(summary) {
  el.summary.replaceChildren(
    metric("Пользователи", summary.registered_users),
    metric("Активны за 7 дней", summary.active_users_7d, {
      description: "Уникальные пользователи с авторизованной активностью за последние 7 дней",
    }),
    metric("Начинали за 7 дней", summary.viewers_7d, {
      description: "Уникальные пользователи, у которых подтверждено начало воспроизведения",
    }),
    metric("Входов за 7 дней", summary.logins_7d, {
      description: "Успешно созданные авторизации, а не регистрации",
    }),
    metric("Действующих авторизаций", summary.valid_authorizations, {
      description: "Неистекшие и неотозванные 30-дневные входы; это не пользователи онлайн",
    }),
    metric("Серий открыто", summary.total_opened_episodes, {
      description: "Любое открытие плеера; слабый сигнал, не обязательно просмотр",
    }),
    metric("Серий начато", summary.total_started_episodes, {
      description: "Уникальные серии с продвижением таймкода, fullscreen или PiP",
    }),
    metric("Серий 5+ минут", summary.total_meaningful_episodes, {
      description: "Уникальные серии с пятью или более минутами учтенной активности",
    }),
    metric("Вероятно досмотрено", summary.total_completed_episodes, {
      description: "Эвристика: длительный просмотр или переход к следующей серии",
    }),
    metric("Учтенное время", summary.total_engaged_seconds, {
      formatter: durationText,
      description: "Активное время по таймкоду; для плееров без API — ограниченное окно после 30 секунд фокуса",
    }),
    metric("Избранных тайтлов", summary.total_favorites),
    metric("Завершенных тайтлов", summary.total_completed_titles, {
      description: "Тайтлы, явно помеченные пользователем как полностью просмотренные",
    }),
  );
}

function renderTelemetryScope(telemetryStartedAt) {
  el.telemetryScope.textContent = telemetryStartedAt
    ? `Сохраненные события начинаются с ${dateText(telemetryStartedAt)}. Ранние данные могут быть неполными: серия учитывается только при поддержанном сигнале плеера.`
    : "Статистика серий начнет накапливаться после первого подтвержденного просмотра.";
}

function userRole(user) {
  const node = document.createElement("span");
  node.className = user.is_admin ? "admin-pill warn" : "admin-pill";
  node.textContent = user.is_admin ? "админ" : "пользователь";
  return node;
}

function statusPill(text, tone = "") {
  const node = document.createElement("span");
  node.className = `admin-pill${tone ? ` ${tone}` : ""}`;
  node.textContent = text;
  return node;
}

function statCell(primary, ...details) {
  const cell = document.createElement("td");
  cell.className = "admin-stat-cell";
  const strong = document.createElement("strong");
  strong.textContent = primary || "-";
  cell.append(strong);
  for (const detail of details.filter(Boolean)) {
    const line = document.createElement("span");
    line.textContent = detail;
    cell.append(line);
  }
  return cell;
}

function renderUsers(users) {
  el.usersCount.textContent = `${number(users.length)} всего · по последней активности`;
  el.users.replaceChildren();
  for (const user of users) {
    const row = document.createElement("tr");

    const account = document.createElement("td");
    account.className = "admin-user-cell";
    const name = document.createElement("strong");
    name.textContent = user.name || user.email || "Google user";
    const email = document.createElement("span");
    email.textContent = user.email || "-";
    const accountMeta = document.createElement("span");
    accountMeta.textContent = `рег. ${dateText(user.created_at)}`;
    const accountPills = document.createElement("div");
    accountPills.className = "admin-pill-row";
    accountPills.append(userRole(user));
    account.append(name, email, accountMeta, accountPills);

    const activity = statCell(dateText(user.last_activity_at));
    const activityPills = document.createElement("div");
    activityPills.className = "admin-pill-row";
    if (user.viewer_7d) {
      activityPills.append(statusPill("начинал 7д"));
    } else if (user.active_7d) {
      activityPills.append(statusPill("активен 7д", "muted"));
    } else if (user.active_30d) {
      activityPills.append(statusPill("активен 30д", "muted"));
    } else {
      activityPills.append(statusPill("нет активности 30д", "quiet"));
    }
    activity.append(activityPills);

    const episodes = statCell(
      `${number(user.started_episodes)} начато`,
      `${number(user.opened_episodes)} открыто · ${number(user.meaningful_episodes)} 5+ мин`,
      `${number(user.completed_episodes)} вероятно досмотрено`,
    );
    const watchTime = statCell(
      durationText(user.engaged_seconds),
      `${number(user.episode_titles)} тайтлов с просмотром`,
    );
    const library = statCell(
      `${number(user.favorite_titles)} избранное`,
      `${number(user.progress_titles)} в прогрессе · ${number(user.completed_titles)} завершено`,
    );
    const lastWatch = statCell(
      user.last_watch_title || "-",
      user.last_watch_episode_number ? `${user.last_watch_episode_number} серия` : "",
      user.last_watch_at ? dateText(user.last_watch_at) : "",
    );
    const login = statCell(
      user.last_login_at ? dateText(user.last_login_at) : "не входил",
      `${number(user.login_count)} входов всего`,
      `${number(user.valid_authorizations)} действующих авторизаций`,
    );

    const cells = [account, activity, episodes, watchTime, library, lastWatch, login];
    const labels = [
      "Аккаунт",
      "Активность",
      "Серии",
      "Учтенное время",
      "Библиотека",
      "Последний просмотр",
      "Последний вход",
    ];
    cells.forEach((cell, index) => {
      cell.dataset.label = labels[index];
    });
    row.append(...cells);
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
      `${number(title.viewers)} начинали`,
      `${number(title.started_episodes)} серий`,
      `${number(title.meaningful_episodes)} серий 5+ мин`,
      durationText(title.engaged_seconds),
      dateText(title.last_activity_at),
      title.source,
    ].filter(Boolean).join(" · ");
    item.append(name, meta);
    el.topTitles.append(item);
  }
}

function renderRecentActivity(items, telemetryStartedAt) {
  el.telemetryStart.textContent = telemetryStartedAt
    ? `история с ${dateText(telemetryStartedAt)}`
    : "история пока пуста";
  el.recentActivity.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "admin-title-item";
    empty.textContent = "Пока нет подтвержденных просмотров";
    el.recentActivity.append(empty);
    return;
  }

  for (const activity of items) {
    const item = document.createElement("div");
    item.className = "admin-title-item";
    const name = document.createElement("strong");
    name.textContent = `${activity.user_name || activity.user_email || "Пользователь"} · ${activity.title}`;
    const meta = document.createElement("span");
    meta.textContent = [
      activity.episode_number ? `${activity.episode_number} серия` : "",
      durationText(activity.engaged_seconds),
      dateText(activity.last_event_at),
      activity.source,
    ].filter(Boolean).join(" · ");
    item.append(name, meta);
    el.recentActivity.append(item);
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
  renderTelemetryScope(payload.telemetry_started_at);
  renderUsers(payload.users || []);
  renderTopTitles(payload.top_titles || []);
  renderRecentActivity(payload.recent_watch_sessions || [], payload.telemetry_started_at);
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

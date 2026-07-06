const el = {
  googleButton: document.getElementById("google-button"),
  oneTapAnchor: document.getElementById("one-tap-anchor"),
  state: document.getElementById("login-state"),
};
const GOOGLE_LOCALE = "ru";
const reportClientError = window.reportClientError || (() => {});
const LOGIN_RECOVERY_STARTED_KEY = "anime-login-recovery-started-at";
const LOGIN_RECOVERY_RELOADS_KEY = "anime-login-recovery-reloads";
const LOGIN_RECOVERY_MAX_AGE_MS = 60_000;
const LOGIN_RECOVERY_MAX_RELOADS = 2;
const LOGIN_RECOVERY_CHECK_ATTEMPTS = 20;
const LOGIN_RECOVERY_CHECK_DELAY_MS = 150;

function setLoginState(message, tone = "") {
  el.state.textContent = message || "";
  if (tone) {
    el.state.dataset.tone = tone;
  } else {
    delete el.state.dataset.tone;
  }
}

function renderUnavailableGoogleButton() {
  el.googleButton.replaceChildren();
  const button = document.createElement("button");
  button.className = "google-fallback-button";
  button.type = "button";
  button.disabled = true;
  button.textContent = "Войти через Google";
  el.googleButton.append(button);
}

function nextPath() {
  const raw = new URLSearchParams(window.location.search).get("next") || "/";
  try {
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin) return "/";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch (error) {
    return "/";
  }
}

function authError() {
  const raw = new URLSearchParams(window.location.search).get("auth_error");
  return raw ? raw.trim() : "";
}

function authComplete() {
  return new URLSearchParams(window.location.search).get("auth_complete") === "1";
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function markLoginRecovery() {
  try {
    window.sessionStorage.setItem(LOGIN_RECOVERY_STARTED_KEY, String(Date.now()));
    window.sessionStorage.setItem(LOGIN_RECOVERY_RELOADS_KEY, "0");
  } catch (error) {
    reportClientError(error, { action: "mark login recovery" });
  }
}

function clearLoginRecovery() {
  try {
    window.sessionStorage.removeItem(LOGIN_RECOVERY_STARTED_KEY);
    window.sessionStorage.removeItem(LOGIN_RECOVERY_RELOADS_KEY);
  } catch (error) {
    reportClientError(error, { action: "clear login recovery" });
  }
}

function hasRecentLoginRecovery() {
  if (authComplete()) return true;
  try {
    const startedAt = Number(window.sessionStorage.getItem(LOGIN_RECOVERY_STARTED_KEY) || 0);
    return startedAt > 0 && Date.now() - startedAt < LOGIN_RECOVERY_MAX_AGE_MS;
  } catch (error) {
    reportClientError(error, { action: "read login recovery" });
    return false;
  }
}

function loginRecoveryReloadCount() {
  try {
    return Number(window.sessionStorage.getItem(LOGIN_RECOVERY_RELOADS_KEY) || 0);
  } catch (error) {
    reportClientError(error, { action: "read login recovery reloads" });
    return LOGIN_RECOVERY_MAX_RELOADS;
  }
}

function bumpLoginRecoveryReloadCount(value) {
  try {
    window.sessionStorage.setItem(LOGIN_RECOVERY_RELOADS_KEY, String(value));
  } catch (error) {
    reportClientError(error, { action: "write login recovery reloads" });
  }
}

async function recoverExistingSession() {
  if (!hasRecentLoginRecovery()) return false;
  setLoginState("Завершаю вход...", "ok");
  for (let attempt = 0; attempt < LOGIN_RECOVERY_CHECK_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch("/api/me", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (response.ok) {
        clearLoginRecovery();
        window.location.replace(nextPath());
        return true;
      }
    } catch (error) {
      // A reload below is the recovery path for transient cookie timing issues.
    }
    await delay(LOGIN_RECOVERY_CHECK_DELAY_MS);
  }

  const reloadCount = loginRecoveryReloadCount();
  if (reloadCount < LOGIN_RECOVERY_MAX_RELOADS) {
    bumpLoginRecoveryReloadCount(reloadCount + 1);
    window.location.reload();
    return true;
  }
  setLoginState("Вход выполнен. Обновите страницу, если приложение не открылось.", "warn");
  return false;
}

function waitForGoogle() {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (window.google?.accounts?.id) {
        window.clearInterval(timer);
        resolve(window.google);
      } else if (Date.now() - startedAt > 8000) {
        window.clearInterval(timer);
        reject(new Error("Google Sign-In не загрузился"));
      }
    }, 80);
  });
}

async function submitCredential(response) {
  if (!response?.credential) {
    setLoginState("Google не вернул credential", "warn");
    return;
  }
  setLoginState("Проверяю вход...", "ok");
  const authResponse = await fetch("/api/auth/google", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      credential: response.credential,
      next: nextPath(),
      state: response.state || "",
    }),
  });
  const payload = await authResponse.json().catch(() => ({}));
  if (!authResponse.ok) {
    throw new Error(payload.error || `${authResponse.status} ${authResponse.statusText}`);
  }
  if (!payload.complete_url) {
    throw new Error("Сервер не вернул завершение входа");
  }
  markLoginRecovery();
  window.location.replace(payload.complete_url);
}

function handleCredential(response) {
  submitCredential(response).catch(error => {
    clearLoginRecovery();
    reportClientError(error, { action: "submit google credential" });
    setLoginState(error.message || "Не удалось войти", "warn");
    console.error(error);
  });
}

function handleGoogleButtonClick() {
  setLoginState("Открываю вход Google...", "ok");
}

function maybeShowOneTap(google, keepMessage = false) {
  google.accounts.id.prompt(notification => {
    if (notification.isDisplayed?.()) {
      if (!keepMessage) setLoginState("");
      return;
    }
    if (notification.isDismissedMoment?.()) {
      return;
    }
    if (notification.isSkippedMoment?.() || notification.isNotDisplayed?.()) {
      if (!keepMessage) setLoginState("");
    }
  });
}

async function bootLogin() {
  const redirectError = authError();
  if (redirectError) {
    setLoginState(redirectError, "warn");
  } else {
    recoverExistingSession();
  }

  const configResponse = await fetch(`/api/auth/config?next=${encodeURIComponent(nextPath())}`);
  const config = await configResponse.json();
  if (!config.configured || !config.client_id) {
    renderUnavailableGoogleButton();
    setLoginState(
      "Ошибка конфигурации деплоймента: Google OAuth Client ID не настроен. Настройте Sign in with Google для этого окружения.",
      "warn",
    );
    return;
  }
  if (!config.state) {
    renderUnavailableGoogleButton();
    setLoginState(
      "Ошибка конфигурации деплоймента: Sign in with Google не настроен для этого окружения.",
      "warn",
    );
    return;
  }

  const google = await waitForGoogle();
  google.accounts.id.initialize({
    client_id: config.client_id,
    callback: handleCredential,
    auto_select: true,
    prompt_parent_id: el.oneTapAnchor?.id,
  });
  google.accounts.id.renderButton(el.googleButton, {
    theme: "filled_black",
    size: "large",
    type: "standard",
    shape: "rectangular",
    text: "signin_with",
    locale: GOOGLE_LOCALE,
    click_listener: handleGoogleButtonClick,
    state: config.state,
    width: Math.min(360, el.googleButton.clientWidth || 360),
  });
  maybeShowOneTap(google, Boolean(redirectError));
}

bootLogin().catch(error => {
  reportClientError(error, { action: "boot login" });
  setLoginState(error.message || "Не удалось открыть вход", "warn");
  console.error(error);
});

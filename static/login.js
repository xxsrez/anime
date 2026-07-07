const el = {
  googleButton: document.getElementById("google-button"),
  oneTapAnchor: document.getElementById("one-tap-anchor"),
  state: document.getElementById("login-state"),
};
const GOOGLE_LOCALE = "ru";
const reportClientError = window.reportClientError || (() => {});
const LOGIN_RECOVERY_STARTED_KEY = "anime-login-recovery-started-at";
const LOGIN_RECOVERY_MAX_AGE_MS = 60_000;
const LOGIN_SESSION_POLL_INTERVAL_MS = 1_000;
const LOGIN_RECOVERY_POLL_INTERVAL_MS = 150;
const LOGIN_RECOVERY_FAST_WINDOW_MS = 12_000;

let credentialSubmitInFlight = false;
let sessionCheckInFlight = false;
let sessionCheckTimer = 0;
let fastSessionChecksUntil = 0;
let redirectingAfterSession = false;
let sessionCheckErrorReported = false;

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

function markLoginRecovery() {
  try {
    window.sessionStorage.setItem(LOGIN_RECOVERY_STARTED_KEY, String(Date.now()));
  } catch (error) {
    reportClientError(error, { action: "mark login recovery" });
  }
}

function clearLoginRecovery() {
  try {
    window.sessionStorage.removeItem(LOGIN_RECOVERY_STARTED_KEY);
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

function stopSessionWatcher() {
  if (sessionCheckTimer) {
    window.clearTimeout(sessionCheckTimer);
    sessionCheckTimer = 0;
  }
}

async function checkExistingSession() {
  if (sessionCheckInFlight || redirectingAfterSession) return false;
  sessionCheckInFlight = true;
  try {
    const response = await fetch("/api/me", {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (response.ok) {
      redirectingAfterSession = true;
      clearLoginRecovery();
      stopSessionWatcher();
      window.location.replace(nextPath());
      return true;
    }
  } catch (error) {
    if (!sessionCheckErrorReported) {
      sessionCheckErrorReported = true;
      reportClientError(error, { action: "check existing login session" });
    }
  } finally {
    sessionCheckInFlight = false;
  }
  return false;
}

function scheduleSessionCheck(delayMs = 0) {
  if (sessionCheckTimer || redirectingAfterSession) return;
  sessionCheckTimer = window.setTimeout(runSessionCheck, delayMs);
}

async function runSessionCheck() {
  sessionCheckTimer = 0;
  const foundSession = await checkExistingSession();
  if (foundSession || redirectingAfterSession) return;
  const delayMs = Date.now() < fastSessionChecksUntil
    ? LOGIN_RECOVERY_POLL_INTERVAL_MS
    : LOGIN_SESSION_POLL_INTERVAL_MS;
  scheduleSessionCheck(delayMs);
}

function startSessionWatcher({ recovery = false } = {}) {
  if (recovery) {
    fastSessionChecksUntil = Math.max(
      fastSessionChecksUntil,
      Date.now() + LOGIN_RECOVERY_FAST_WINDOW_MS,
    );
    setLoginState("Завершаю вход...", "ok");
  }
  scheduleSessionCheck(0);
}

function recoverExistingSession() {
  if (!hasRecentLoginRecovery()) return false;
  startSessionWatcher({ recovery: true });
  return true;
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
  if (credentialSubmitInFlight || redirectingAfterSession) return;
  if (!response?.credential) {
    setLoginState("Google не вернул credential", "warn");
    return;
  }
  credentialSubmitInFlight = true;
  setLoginState("Проверяю вход...", "ok");
  try {
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
  } catch (error) {
    credentialSubmitInFlight = false;
    throw error;
  }
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
    startSessionWatcher();
  } else if (!recoverExistingSession()) {
    startSessionWatcher();
  }

  const configResponse = await fetch(`/api/auth/config?next=${encodeURIComponent(nextPath())}`);
  const config = await configResponse.json();
  if (redirectingAfterSession) return;
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
  if (redirectingAfterSession) return;
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

window.addEventListener("focus", () => scheduleSessionCheck(0));
window.addEventListener("pageshow", () => scheduleSessionCheck(0));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) scheduleSessionCheck(0);
});

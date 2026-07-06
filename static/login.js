const el = {
  googleButton: document.getElementById("google-button"),
  oneTapAnchor: document.getElementById("one-tap-anchor"),
  state: document.getElementById("login-state"),
};
const GOOGLE_LOCALE = "ru";

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
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credential: response.credential }),
  });
  if (!authResponse.ok) {
    const payload = await authResponse.json().catch(() => ({}));
    throw new Error(payload.error || `${authResponse.status} ${authResponse.statusText}`);
  }
  window.location.replace(nextPath());
}

function handleCredential(response) {
  submitCredential(response).catch(error => {
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
    ux_mode: "redirect",
    login_uri: `${window.location.origin}/api/auth/google`,
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
  setLoginState(error.message || "Не удалось открыть вход", "warn");
  console.error(error);
});

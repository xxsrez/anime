"use strict";

(() => {
  const video = document.querySelector("video");
  const player = document.getElementById("player");
  const quality = document.getElementById("quality");
  const translation = document.getElementById("translation");
  const translationLabel = document.getElementById("translation-label");
  const resume = document.getElementById("resume");
  const skipIntro = document.getElementById("skip-intro");
  const pip = document.getElementById("pip");
  const pipLabel = document.getElementById("pip-label");
  const settings = document.getElementById("player-settings");
  const settingsToggle = document.getElementById("settings-toggle");
  const settingsClose = document.getElementById("settings-close");
  const settingsQuality = document.getElementById("settings-quality");
  const message = document.getElementById("message");
  const feedbackClose = document.getElementById("feedback-close");
  const retry = document.getElementById("retry");
  const original = document.getElementById("original");
  const id = location.pathname.match(/^\/player\/kodik\/([1-9]\d{0,18})$/)?.[1];
  const serial = new URLSearchParams(location.search || "").get("serial");
  const preferenceKey = /^[1-9]\d{0,19}$/.test(serial || "") ? `kodik-translation-${serial}` : null;
  let streams = {};
  let requestId = 0;
  let mediaGeneration = 0;
  let hlsPlayer = null;
  let savedPosition = null;
  let lastPosition = 0;
  let pendingPosition = null;
  let resumeOnMetadata = false;
  let activeTranslation = null;
  let intro = null;
  let introMediaReady = false;
  let availableTranslations = [];
  let retryOptions = { recovering: true };
  let pipMessageTimer = null;

  function updateQualityDisplay(value) {
    const text = value ? `${value}p` : "";
    settingsQuality.textContent = text;
    settingsToggle.setAttribute("aria-label", text ? `Настройки видео, ${text}` : "Настройки видео");
  }

  function savedTranslation() {
    try {
      const value = preferenceKey && localStorage.getItem(preferenceKey);
      return /^[1-9]\d{0,9}$/.test(value || "") ? value : null;
    } catch {
      return null;
    }
  }

  function saveTranslation(value) {
    try {
      if (preferenceKey && value) localStorage.setItem(preferenceKey, value);
    } catch {
      // Playback remains available when browser storage is disabled.
    }
  }

  function post(key, value) {
    window.parent.postMessage(value === undefined ? { key } : { key, value }, location.origin);
  }

  function fail(text) {
    player.classList.remove("is-loading");
    player.classList.add("has-error");
    player.classList.toggle("is-empty", !Object.keys(streams).length);
    const restoreFocus = settings.open && settings.contains(document.activeElement);
    settings.open = false;
    settings.hidden = !Object.keys(streams).length;
    skipIntro.hidden = true;
    message.textContent = text;
    retry.hidden = false;
    original.hidden = false;
    feedbackClose.hidden = !Object.keys(streams).length;
    if (restoreFocus) retry.focus();
    if (Object.keys(streams).length) {
      quality.disabled = Object.keys(streams).length < 2;
      translation.disabled = availableTranslations.length < 2;
      if (activeTranslation) translation.value = activeTranslation;
    }
  }

  function dismissError() {
    const restoreFocus = document.activeElement === feedbackClose;
    player.classList.remove("has-error");
    message.textContent = "";
    retry.hidden = true;
    original.hidden = true;
    feedbackClose.hidden = true;
    if (restoreFocus) video.focus();
  }

  feedbackClose.addEventListener("click", dismissError);

  function validStream(value) {
    try {
      const url = new URL(value);
      const host = url.hostname;
      return url.protocol === "https:" && !url.username && !url.password
        && (!url.port || url.port === "443") && !url.hash
        && (host === "solodcdn.com" || host.endsWith(".solodcdn.com"))
        && url.pathname.endsWith(".m3u8");
    } catch {
      return false;
    }
  }

  function setSource(selected, position = 0, continuePlaying = false) {
    if (!validStream(streams[selected])) return fail("Не удалось загрузить видео.");
    player.classList.remove("is-empty");
    player.classList.remove("has-error");
    feedbackClose.hidden = true;
    retry.hidden = true;
    original.hidden = true;
    message.textContent = "";
    const generation = ++mediaGeneration;
    resumeOnMetadata = continuePlaying;
    introMediaReady = false;
    skipIntro.hidden = true;
    pendingPosition = Number.isFinite(position) && position > 0 ? position : null;
    if (pendingPosition != null) lastPosition = pendingPosition;
    if (hlsPlayer) {
      hlsPlayer.destroy();
      hlsPlayer = null;
    }
    if (pendingPosition != null || continuePlaying) {
      video.addEventListener("loadedmetadata", () => {
        if (generation !== mediaGeneration) return;
        if (pendingPosition != null) {
          const limit = Number.isFinite(video.duration) ? Math.max(0, video.duration - 1) : pendingPosition;
          video.currentTime = Math.min(pendingPosition, limit);
          lastPosition = video.currentTime;
          pendingPosition = null;
        }
        const shouldResume = resumeOnMetadata;
        resumeOnMetadata = false;
        if (shouldResume) video.play().catch(() => {
          message.textContent = "Нажмите воспроизведение, чтобы продолжить.";
        });
      }, { once: true });
    }
    if (window.Hls?.isSupported?.()) {
      const player = new window.Hls({ backBufferLength: 60, startPosition: pendingPosition ?? -1 });
      hlsPlayer = player;
      player.on(window.Hls.Events.ERROR, (_event, details) => {
        if (generation === mediaGeneration && hlsPlayer === player && details.fatal) {
          player.stopLoad();
          resumeOnMetadata = false;
          fail("Не удалось воспроизвести видео.");
        }
      });
      player.loadSource(streams[selected]);
      player.attachMedia(video);
    } else {
      video.src = streams[selected];
      video.load();
    }
  }

  function formatTime(value) {
    const seconds = Math.floor(value);
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function validatedIntro(value) {
    return value && Number.isInteger(value.start) && Number.isInteger(value.end)
      && value.start >= 0 && value.start < value.end && value.end <= 86400
      && value.end - value.start >= 5 && value.end - value.start <= 300
      ? { start: value.start, end: value.end } : null;
  }

  function updateSkipIntro() {
    const duration = video.duration;
    skipIntro.hidden = !introMediaReady || !intro || !Number.isFinite(duration) || duration <= 0
      || intro.end > duration / 2 || intro.start >= duration / 3
      || !Number.isFinite(video.currentTime) || video.currentTime < intro.start
      || video.currentTime >= intro.end - 1;
  }

  function seekRelative(seconds) {
    if (![15, -15].includes(seconds) || !Object.keys(streams).length) return false;
    const base = pendingPosition ?? video.currentTime;
    if (!Number.isFinite(base) || (pendingPosition == null && video.readyState < 1)) return false;
    const limit = Number.isFinite(video.duration) && video.duration > 0
      ? Math.max(0, video.duration - 1) : Infinity;
    const position = Math.max(0, Math.min(base + seconds, limit));
    post("anime_catalog_kodik_seeking");
    if (pendingPosition != null) pendingPosition = position;
    else video.currentTime = position;
    updateSkipIntro();
    return true;
  }

  async function loadStreams({ recovering = false, translationId = null, persistChoice = false } = {}) {
    const currentRequest = ++requestId;
    const recoveryQuality = recovering ? quality.value : null;
    const hadStreams = Object.keys(streams).length > 0;
    player.classList.add("is-loading");
    player.classList.remove("has-error");
    feedbackClose.hidden = true;
    const requestedTranslation = translationId || savedTranslation();
    retryOptions = { recovering: true, translationId: requestedTranslation, persistChoice };
    message.textContent = hadStreams && translationId ? "Загружаем озвучку…" : "Загружаем видео…";
    retry.hidden = true;
    original.hidden = true;
    const selectionFocused = settings.open && [quality, translation].includes(document.activeElement);
    quality.disabled = true;
    translation.disabled = true;
    if (selectionFocused) settingsClose.focus();
    resume.hidden = true;
    if (!id || !(window.Hls?.isSupported?.() || video.canPlayType("application/vnd.apple.mpegurl"))) {
      fail("Этот браузер не поддерживает такой видеопоток.");
      return;
    }
    try {
      const request = value => fetch(`/api/kodik-stream/${id}${value ? `?translation=${encodeURIComponent(value)}` : ""}`,
        { credentials: "same-origin" });
      let response = await request(requestedTranslation);
      if (currentRequest !== requestId) return;
      let translationUnavailable = false;
      if (response.status === 409 && requestedTranslation && !hadStreams) {
        response = await request(null);
        translationUnavailable = response.ok;
      }
      if (!response.ok) throw new Error(response.status === 409 ? "translation" : "unavailable");
      const payload = await response.json();
      if (currentRequest !== requestId) return;
      const translations = Array.isArray(payload.translations) ? payload.translations.filter(item => (
        item && /^[1-9]\d{0,9}$/.test(item.id) && typeof item.title === "string" && item.title.length <= 160
      )) : [];
      const nextStreams = Object.fromEntries(Object.entries(payload.streams || {}).filter(
        ([key, value]) => /^\d{3,4}$/.test(key) && validStream(value),
      ));
      const values = Object.keys(nextStreams).sort((a, b) => Number(a) - Number(b));
      if (!values.length || (requestedTranslation && !translationUnavailable
          && payload.translation_id !== requestedTranslation)) throw new Error("unavailable");
      const recoveryPosition = recovering
        ? (pendingPosition ?? (Number.isFinite(video.currentTime) && video.currentTime > 0 ? video.currentTime : lastPosition))
        : 0;
      const continuePlaying = recovering && !video.paused && !video.ended && !translationUnavailable;
      streams = nextStreams;
      intro = validatedIntro(payload.intro);
      settings.hidden = false;
      availableTranslations = translations;
      activeTranslation = translations.some(item => item.id === payload.translation_id) ? payload.translation_id : null;
      translation.replaceChildren(...translations.map(item => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.title;
        return option;
      }));
      translation.hidden = translationLabel.hidden = translations.length < 2;
      translation.disabled = translations.length < 2;
      if (activeTranslation) {
        translation.value = activeTranslation;
        if (persistChoice && !translationUnavailable) saveTranslation(activeTranslation);
      }
      quality.replaceChildren(...values.map(value => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = `${value}p`;
        return option;
      }));
      const defaultQuality = values.includes(recoveryQuality)
        ? recoveryQuality : values.filter(value => Number(value) <= 720).at(-1) || values[0];
      quality.value = defaultQuality;
      updateQualityDisplay(defaultQuality);
      quality.disabled = values.length < 2;
      savedPosition = Number.isInteger(payload.resume_position) && payload.resume_position >= 15
        ? payload.resume_position : null;
      if (savedPosition != null && recoveryPosition < 15) {
        resume.textContent = `Продолжить с ${formatTime(savedPosition)}`;
        resume.hidden = false;
      }
      retryOptions = { recovering: true, translationId: activeTranslation };
      setSource(defaultQuality, recoveryPosition, continuePlaying);
      player.classList.remove("is-loading");
      retry.hidden = true;
      original.hidden = true;
      message.textContent = translationUnavailable ? "Выбранная озвучка недоступна. Выберите другую." : "";
    } catch (error) {
      if (currentRequest === requestId) fail(error.message === "translation"
        ? "Выбранная озвучка недоступна. Выберите другую."
        : "Не удалось загрузить видео.");
    }
  }

  quality.addEventListener("change", () => {
    updateQualityDisplay(quality.value);
    const position = pendingPosition ?? video.currentTime;
    const playing = !video.paused && !video.ended;
    setSource(quality.value, position, playing);
  });
  translation.addEventListener("change", () => {
    const restoreFocus = settings.open && settings.contains(document.activeElement);
    settings.open = false;
    if (restoreFocus) settingsToggle.focus();
    loadStreams({ recovering: true, translationId: translation.value, persistChoice: true });
  });
  resume.addEventListener("click", () => {
    if (savedPosition == null) return;
    const position = savedPosition;
    const generation = mediaGeneration;
    pendingPosition = position;
    if (video.readyState >= 1) {
      video.currentTime = position;
      lastPosition = video.currentTime;
      pendingPosition = null;
    } else {
      video.addEventListener("loadedmetadata", () => {
        if (generation !== mediaGeneration) return;
        video.currentTime = position;
        lastPosition = video.currentTime;
        pendingPosition = null;
      }, { once: true });
    }
    resume.hidden = true;
    video.play().catch(() => { message.textContent = "Нажмите воспроизведение, чтобы продолжить."; });
  });
  skipIntro.addEventListener("click", () => {
    updateSkipIntro();
    if (skipIntro.hidden) return;
    post("anime_catalog_kodik_seeking");
    video.currentTime = intro.end;
    skipIntro.hidden = true;
    video.focus();
  });
  retry.addEventListener("click", () => loadStreams(retryOptions));
  original.addEventListener("click", () => post("anime_catalog_kodik_original"));
  video.addEventListener("loadedmetadata", () => {
    introMediaReady = true;
    updateSkipIntro();
    if (Number.isFinite(video.duration) && video.duration > 0) {
      post("kodik_player_duration_update", video.duration);
      if (savedPosition != null && savedPosition >= video.duration * 0.9) resume.hidden = true;
    }
  });
  video.addEventListener("play", () => {
    resume.hidden = true;
    if (player.classList.contains("has-error")) dismissError();
    message.textContent = "";
    post("kodik_player_play");
  });
  video.addEventListener("seeking", () => post("anime_catalog_kodik_seeking"));
  video.addEventListener("seeked", () => {
    if (pendingPosition == null && Number.isFinite(video.currentTime)) lastPosition = video.currentTime;
    updateSkipIntro();
  });
  video.addEventListener("timeupdate", () => {
    updateSkipIntro();
    if (pendingPosition == null && Number.isFinite(video.currentTime)) lastPosition = video.currentTime;
    if (!video.paused && !video.seeking && Number.isFinite(video.currentTime)) {
      post("kodik_player_time_update", video.currentTime);
    }
  });
  video.addEventListener("pause", () => post("kodik_player_pause"));
  video.addEventListener("ended", () => post("kodik_player_video_ended"));
  video.addEventListener("error", () => {
    retryOptions = { recovering: true, translationId: activeTranslation };
    resumeOnMetadata = false;
    fail("Не удалось воспроизвести видео.");
  });
  video.addEventListener("enterpictureinpicture", () => {
    pipLabel.textContent = "Вернуть видео из отдельного окна";
    post("kodik_player_enter_pip");
  });
  video.addEventListener("leavepictureinpicture", () => {
    pipLabel.textContent = "Картинка в картинке";
    post("kodik_player_exit_pip");
  });
  video.addEventListener("webkitpresentationmodechanged", () => {
    pipLabel.textContent = video.webkitPresentationMode === "picture-in-picture"
      ? "Вернуть видео из отдельного окна" : "Картинка в картинке";
    post(video.webkitPresentationMode === "picture-in-picture"
      ? "kodik_player_enter_pip" : "kodik_player_exit_pip");
  });

  async function setPip(enter) {
    try {
      const active = video.webkitPresentationMode === "picture-in-picture"
        || document.pictureInPictureElement === video;
      if (active === enter) return;
      if (video.webkitSetPresentationMode) {
        video.webkitSetPresentationMode(enter ? "picture-in-picture" : "inline");
      } else if (!enter && document.pictureInPictureElement === video) {
        await document.exitPictureInPicture();
      } else if (enter && video.requestPictureInPicture) {
        await video.requestPictureInPicture();
      }
    } catch {
      const text = "Не удалось включить картинку в картинке.";
      message.textContent = text;
      window.clearTimeout(pipMessageTimer);
      pipMessageTimer = window.setTimeout(() => {
        if (message.textContent === text) message.textContent = "";
      }, 4000);
    }
  }
  pip.hidden = !(video.webkitSetPresentationMode || document.pictureInPictureEnabled);
  pip.addEventListener("click", () => {
    const restoreFocus = document.activeElement === pip;
    setPip(!(video.webkitPresentationMode === "picture-in-picture" || document.pictureInPictureElement === video));
    settings.open = false;
    if (restoreFocus) settingsToggle.focus();
  });
  settings.addEventListener("toggle", () => {
    if (settings.open && document.activeElement === settingsToggle
        && window.matchMedia?.("(max-height: 260px)").matches) settingsClose.focus();
  });
  settingsClose.addEventListener("click", () => {
    settings.open = false;
    settingsToggle.focus();
  });
  document.addEventListener("pointerdown", event => {
    if (settings.open && !settings.contains(event.target)) settings.open = false;
  });
  function editableShortcutTarget(target) {
    return Boolean(target?.isContentEditable || target?.closest?.(
      "input, textarea, select, [contenteditable]",
    ));
  }

  function interactiveShortcutTarget(target) {
    return editableShortcutTarget(target) || Boolean(target?.closest?.(
      "button, a, summary, [role='slider'], [role='menu'], [role='listbox']",
    ));
  }

  function togglePlayback() {
    if (!Object.keys(streams).length) return false;
    if (resumeOnMetadata || (!video.paused && !video.ended)) {
      resumeOnMetadata = false;
      video.pause();
    } else {
      try {
        video.play()?.catch(error => {
          if (error?.name !== "AbortError") message.textContent = "Нажмите воспроизведение, чтобы продолжить.";
        });
      } catch {
        message.textContent = "Нажмите воспроизведение, чтобы продолжить.";
      }
    }
    return true;
  }

  // The same-origin parent calls this synchronously from its keydown handler,
  // so the browser can associate play() with the user's gesture.
  window.animeCatalogKodikPlayer = Object.freeze({ togglePlayback });

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (window.parent.document.fullscreenElement) await window.parent.document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else if (document.documentElement.webkitRequestFullscreen) document.documentElement.webkitRequestFullscreen();
      else message.textContent = "Используйте кнопку полноэкранного режима на видео.";
    } catch {
      message.textContent = "Не удалось открыть полноэкранный режим.";
    }
  }

  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && settings.open) {
      settings.open = false;
      settingsToggle.focus();
      event.preventDefault();
      return;
    }
    if (event.defaultPrevented || event.isComposing || event.keyCode === 229
        || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey
        || editableShortcutTarget(event.target)) return;
    const key = String(event.key || "").toLowerCase();
    if (!event.repeat && (event.code === "KeyF" || key === "f" || key === "а")) {
      event.preventDefault();
      event.stopPropagation();
      toggleFullscreen();
    } else if (!interactiveShortcutTarget(event.target)
        && (event.code === "Space" || event.key === " " || event.key === "Spacebar")
        && Object.keys(streams).length && (event.repeat || togglePlayback())) {
      event.preventDefault();
      event.stopPropagation();
    } else if (!interactiveShortcutTarget(event.target) && (key === "arrowleft" || key === "arrowright")
        && seekRelative(key === "arrowleft" ? -15 : 15)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.origin !== location.origin) return;
    if (event.data?.key !== "kodik_player_api") return;
    if (event.data.value?.method === "enter_pip") setPip(true);
    if (event.data.value?.method === "exit_pip") setPip(false);
    if (event.data.value?.method === "seek_relative") seekRelative(event.data.value.seconds);
  });
  loadStreams();
})();

"use strict";

(() => {
  const video = document.querySelector("video");
  const quality = document.getElementById("quality");
  const translation = document.getElementById("translation");
  const translationLabel = document.getElementById("translation-label");
  const resume = document.getElementById("resume");
  const pip = document.getElementById("pip");
  const message = document.getElementById("message");
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
  let activeTranslation = null;
  let availableTranslations = [];
  let retryOptions = { recovering: true };

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
    message.textContent = text;
    retry.hidden = false;
    original.hidden = false;
    if (Object.keys(streams).length) {
      quality.disabled = Object.keys(streams).length < 2;
      translation.disabled = availableTranslations.length < 2;
      if (activeTranslation) translation.value = activeTranslation;
    }
  }

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
    const generation = ++mediaGeneration;
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
          const limit = Number.isFinite(video.duration) ? Math.max(0, video.duration - 1) : position;
          video.currentTime = Math.min(position, limit);
          lastPosition = video.currentTime;
          pendingPosition = null;
        }
        if (continuePlaying) video.play().catch(() => {
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

  async function loadStreams({ recovering = false, translationId = null, persistChoice = false } = {}) {
    const currentRequest = ++requestId;
    const recoveryQuality = recovering ? quality.value : null;
    const hadStreams = Object.keys(streams).length > 0;
    const requestedTranslation = translationId || savedTranslation();
    retryOptions = { recovering: true, translationId: requestedTranslation, persistChoice };
    message.textContent = "Загружаем видео…";
    retry.hidden = true;
    original.hidden = true;
    quality.disabled = true;
    translation.disabled = true;
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
      quality.disabled = values.length < 2;
      savedPosition = Number.isInteger(payload.resume_position) && payload.resume_position >= 15
        ? payload.resume_position : null;
      if (savedPosition != null && recoveryPosition < 15) {
        resume.textContent = `Продолжить с ${formatTime(savedPosition)}`;
        resume.hidden = false;
      }
      retryOptions = { recovering: true, translationId: activeTranslation };
      setSource(defaultQuality, recoveryPosition, continuePlaying);
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
    const position = pendingPosition ?? video.currentTime;
    const playing = !video.paused && !video.ended;
    setSource(quality.value, position, playing);
  });
  translation.addEventListener("change", () => {
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
  retry.addEventListener("click", () => loadStreams(retryOptions));
  original.addEventListener("click", () => post("anime_catalog_kodik_original"));
  video.addEventListener("loadedmetadata", () => {
    if (Number.isFinite(video.duration) && video.duration > 0) {
      post("kodik_player_duration_update", video.duration);
      if (savedPosition != null && savedPosition >= video.duration * 0.9) resume.hidden = true;
    }
  });
  video.addEventListener("play", () => {
    message.textContent = "";
    post("kodik_player_play");
  });
  video.addEventListener("seeking", () => post("anime_catalog_kodik_seeking"));
  video.addEventListener("seeked", () => {
    if (pendingPosition == null && Number.isFinite(video.currentTime)) lastPosition = video.currentTime;
  });
  video.addEventListener("timeupdate", () => {
    if (pendingPosition == null && Number.isFinite(video.currentTime)) lastPosition = video.currentTime;
    if (!video.paused && !video.seeking && Number.isFinite(video.currentTime)) {
      post("kodik_player_time_update", video.currentTime);
    }
  });
  video.addEventListener("pause", () => post("kodik_player_pause"));
  video.addEventListener("ended", () => post("kodik_player_video_ended"));
  video.addEventListener("error", () => {
    retryOptions = { recovering: true, translationId: activeTranslation };
    fail("Не удалось воспроизвести видео.");
  });
  video.addEventListener("enterpictureinpicture", () => post("kodik_player_enter_pip"));
  video.addEventListener("leavepictureinpicture", () => post("kodik_player_exit_pip"));
  video.addEventListener("webkitpresentationmodechanged", () => {
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
      message.textContent = "Не удалось включить картинку в картинке.";
    }
  }
  pip.hidden = !(video.webkitSetPresentationMode || document.pictureInPictureEnabled);
  pip.addEventListener("click", () => setPip(
    !(video.webkitPresentationMode === "picture-in-picture" || document.pictureInPictureElement === video),
  ));
  window.addEventListener("message", event => {
    if (event.source !== window.parent || event.origin !== location.origin) return;
    if (event.data?.key !== "kodik_player_api") return;
    if (event.data.value?.method === "enter_pip") setPip(true);
    if (event.data.value?.method === "exit_pip") setPip(false);
  });
  loadStreams();
})();

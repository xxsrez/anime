(function initAnimeFrontendRuntime(root) {
  function normalizeHostname(value) {
    return String(value || "").trim().toLocaleLowerCase("en-US").replace(/\.$/, "");
  }

  function normalizeSourceIdentity(value) {
    return String(value || "")
      .trim()
      .toLocaleLowerCase("ru-RU")
      .replace(/ё/g, "е")
      .replace(/э/g, "е")
      .normalize("NFKD")
      .replace(/\p{M}+/gu, "")
      .replace(/[^\p{L}\p{N}_\s]+/gu, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function normalizeTranslationKey(value) {
    let key = normalizeSourceIdentity(value);
    if (key.startsWith("озвучка ")) key = key.slice("озвучка ".length).trim();
    const compact = key.replace(/\s+/g, "");
    const aliases = {
      dreamcast: "dream cast",
      "dream cast": "dream cast",
      "light family": "lightfamily",
    };
    return aliases[key] || aliases[compact] || key;
  }

  function sourceTranslationKey(source) {
    return normalizeTranslationKey(
      source?.translation_key
      || source?.translation_title
      || source?.translation_id,
    );
  }

  function groupSourcesByTranslation(sources) {
    const groups = new Map();
    for (const source of (sources || []).filter(Boolean)) {
      const key = sourceTranslationKey(source);
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          title: source.translation_title || String(source.translation_id ?? "Без озвучки"),
          sources: [],
        });
      }
      groups.get(key).sources.push(source);
    }
    return [...groups.values()];
  }

  function sourcePreference(source) {
    if (!source) return null;
    return {
      translationKey: sourceTranslationKey(source),
      providerTitleKey: normalizeSourceIdentity(source.provider_title),
      providerHost: normalizeHostname(source.embed_host),
    };
  }

  function normalizeSourcePreference(preference) {
    if (!preference) return null;
    return {
      translationKey: normalizeTranslationKey(
        preference.translationKey
          || preference.translation_key
          || preference.translationTitle
          || preference.translation_title,
      ),
      providerTitleKey: normalizeSourceIdentity(
        preference.providerTitleKey
          || preference.provider_title_key
          || preference.providerTitle
          || preference.provider_title,
      ),
      providerHost: normalizeHostname(
        preference.providerHost
          || preference.provider_host
          || preference.embedHost
          || preference.embed_host,
      ),
    };
  }

  function selectPreferredProvider(sources, preference) {
    const ranked = (sources || []).filter(Boolean);
    if (!ranked.length) return null;
    const normalized = normalizeSourcePreference(preference);
    if (!normalized) return ranked[0];

    const providerTitleMatches = source => (
      normalized.providerTitleKey
      && normalizeSourceIdentity(source.provider_title) === normalized.providerTitleKey
    );
    const providerHostMatches = source => (
      normalized.providerHost
      && normalizeHostname(source.embed_host) === normalized.providerHost
    );
    return ranked.find(source => providerTitleMatches(source) && providerHostMatches(source))
      || ranked.find(providerTitleMatches)
      || ranked.find(providerHostMatches)
      || ranked[0];
  }

  function selectPreferredSource(sources, preference) {
    const ranked = (sources || []).filter(Boolean);
    if (!ranked.length) return null;
    const normalized = normalizeSourcePreference(preference);
    if (!normalized?.translationKey) return ranked[0];

    const matchingTranslation = ranked.filter(source => (
      sourceTranslationKey(source) === normalized.translationKey
    ));
    return matchingTranslation.length
      ? selectPreferredProvider(matchingTranslation, normalized)
      : ranked[0];
  }

  function selectSourceForEpisode(sources, {
    selectedSourceId = null,
    selectedTranslationId = null,
    preference = null,
  } = {}) {
    const ranked = (sources || []).filter(Boolean);
    if (!ranked.length) return null;

    if (selectedSourceId != null && selectedSourceId !== "") {
      const exactSource = ranked.find(source => String(source.id) === String(selectedSourceId));
      if (exactSource) return exactSource;
    }

    if (selectedTranslationId != null && selectedTranslationId !== "") {
      const matchingTranslationId = ranked.filter(source => (
        String(source.translation_id) === String(selectedTranslationId)
      ));
      if (matchingTranslationId.length) {
        return selectPreferredProvider(matchingTranslationId, preference);
      }
    }

    return selectPreferredSource(ranked, preference);
  }

  function nearestAvailableEpisodeId(episodes, availableEpisodeIds, selectedEpisodeId = null) {
    const ordered = (episodes || []).filter(episode => episode?.id != null);
    if (!ordered.length) return null;

    const available = new Set(
      (availableEpisodeIds || [])
        .filter(id => id != null)
        .map(id => String(id)),
    );
    if (!available.size) return null;

    const selectedKey = selectedEpisodeId == null ? null : String(selectedEpisodeId);
    const selectedIndex = ordered.findIndex(episode => String(episode.id) === selectedKey);
    if (selectedIndex >= 0 && available.has(selectedKey)) return ordered[selectedIndex].id;

    const candidates = ordered
      .map((episode, index) => ({ episode, index }))
      .filter(({ episode }) => available.has(String(episode.id)));
    if (!candidates.length) return null;
    if (selectedIndex < 0) return candidates[0].episode.id;

    candidates.sort((left, right) => (
      Math.abs(left.index - selectedIndex) - Math.abs(right.index - selectedIndex)
      || (left.index > selectedIndex ? 1 : 0) - (right.index > selectedIndex ? 1 : 0)
      || right.index - left.index
    ));
    return candidates[0].episode.id;
  }

  function hostnameMatches(hostname, allowedHostname) {
    const host = normalizeHostname(hostname);
    const allowed = normalizeHostname(allowedHostname);
    return Boolean(host && allowed && (host === allowed || host.endsWith(`.${allowed}`)));
  }

  function safeHttpsUrl(value, allowedHostnames) {
    try {
      const raw = String(value || "").trim();
      const url = new URL(raw.startsWith("//") ? `https:${raw}` : raw);
      if (url.protocol !== "https:" || url.username || url.password) return null;
      if (!(allowedHostnames || []).some(host => hostnameMatches(url.hostname, host))) return null;
      return url.href;
    } catch (error) {
      return null;
    }
  }

  function localCalendarDayNumber(value) {
    const date = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
  }

  function localCalendarDayDifference(later, earlier) {
    const laterDay = localCalendarDayNumber(later);
    const earlierDay = localCalendarDayNumber(earlier);
    if (laterDay == null || earlierDay == null) return null;
    return laterDay - earlierDay;
  }

  function boundedElapsedSeconds(startedAt, endedAt, maximumSeconds) {
    const start = Number(startedAt);
    const end = Number(endedAt);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return 0;
    const seconds = Math.max(0, Math.round((end - start) / 1000));
    return Number.isFinite(maximumSeconds) ? Math.min(maximumSeconds, seconds) : seconds;
  }

  function hasPlaybackEvidence({ pageHidden = false, playerFocused = false, fullscreen = false, evidenceExpiresAt = 0, now = Date.now() } = {}) {
    if (pageHidden || Number(evidenceExpiresAt) <= Number(now)) return false;
    return Boolean(playerFocused || fullscreen);
  }

  function effectiveWatchStatus(item) {
    const status = String(item?.watch_status || "").trim();
    if (status === "completed" || item?.watched) return "completed";
    if (["watching", "paused"].includes(status)) return "watching";
    if (["none", "planned", "dropped"].includes(status)) return "none";
    if (item?.progress_episode_number != null || item?.last_watch?.progress_episode_number != null) return "watching";
    return "none";
  }

  function watchStatusLabel(status) {
    return {
      watching: "смотрю",
      completed: "просмотрено",
    }[status] || "";
  }

  function patchChanges(target, patch, fields = null) {
    const keys = fields || Object.keys(patch || {});
    return keys.some(key => (
      Object.prototype.hasOwnProperty.call(patch || {}, key)
      && !Object.is(target?.[key], patch[key])
    ));
  }

  function createKeyedSerialQueue(worker) {
    if (typeof worker !== "function") throw new TypeError("worker must be a function");
    const queues = new Map();

    async function drain(key, queue) {
      if (queue.running) return;
      queue.running = true;
      try {
        while (queue.items.length) {
          const item = queue.items[0];
          try {
            item.resolve(await worker(key, item.value));
          } catch (error) {
            item.reject(error);
          } finally {
            queue.items.shift();
          }
        }
      } finally {
        queue.running = false;
        if (!queue.items.length && queues.get(key) === queue) queues.delete(key);
      }
    }

    function enqueue(key, value) {
      return new Promise((resolve, reject) => {
        let queue = queues.get(key);
        if (!queue) {
          queue = { items: [], running: false };
          queues.set(key, queue);
        }
        queue.items.push({ value, resolve, reject });
        drain(key, queue).catch(() => {});
      });
    }

    function pending(key) {
      if (arguments.length) return queues.get(key)?.items.length || 0;
      let count = 0;
      for (const queue of queues.values()) count += queue.items.length;
      return count;
    }

    return { enqueue, pending };
  }

  const api = {
    hostnameMatches,
    safeHttpsUrl,
    normalizeSourceIdentity,
    normalizeTranslationKey,
    sourceTranslationKey,
    groupSourcesByTranslation,
    sourcePreference,
    selectPreferredProvider,
    selectPreferredSource,
    selectSourceForEpisode,
    nearestAvailableEpisodeId,
    localCalendarDayNumber,
    localCalendarDayDifference,
    boundedElapsedSeconds,
    hasPlaybackEvidence,
    effectiveWatchStatus,
    watchStatusLabel,
    patchChanges,
    createKeyedSerialQueue,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.AnimeFrontendRuntime = api;
})(typeof globalThis !== "undefined" ? globalThis : window);

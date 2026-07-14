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

  function integerValue(value, { minimum = null } = {}) {
    if (typeof value === "string" && !value.trim()) return null;
    const number = Number(value);
    if (!Number.isInteger(number)) return null;
    if (minimum != null && number < minimum) return null;
    return number;
  }

  function parseKodikSerialUrl(value) {
    try {
      const safeUrl = safeHttpsUrl(value, ["kodikplayer.com"]);
      if (!safeUrl) return null;
      const url = new URL(safeUrl);
      const parts = url.pathname.split("/").filter(Boolean);
      const serialIndex = parts.indexOf("serial");
      if (serialIndex < 0 || !parts[serialIndex + 1] || !parts[serialIndex + 2]) return null;
      return {
        serialId: parts[serialIndex + 1],
        serialHash: parts[serialIndex + 2].toLocaleLowerCase("en-US"),
        seasonNumber: integerValue(url.searchParams.get("season"), { minimum: 0 }),
        episodeNumber: integerValue(url.searchParams.get("episode"), { minimum: 1 }),
      };
    } catch (error) {
      return null;
    }
  }

  function sameKodikSerial(left, right) {
    return Boolean(
      left
      && right
      && left.serialId === right.serialId
      && left.serialHash === right.serialHash
    );
  }

  function normalizePlayerMessage(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;

    const kodikKey = String(data.key || "");
    if (kodikKey.startsWith("kodik_player_")) {
      if (kodikKey === "kodik_player_current_episode") {
        const value = data.value;
        if (!value || typeof value !== "object" || Array.isArray(value)) return null;
        const episodeNumber = integerValue(value.episode, { minimum: 1 });
        if (episodeNumber == null) return null;
        return {
          provider: "kodik",
          type: "episode_changed",
          episodeNumber,
          seasonNumber: integerValue(value.season, { minimum: 0 }),
        };
      }
      if (["kodik_player_video_started", "kodik_player_play"].includes(kodikKey)) {
        return { provider: "kodik", type: "playback_started" };
      }
      if (kodikKey === "kodik_player_pause") {
        return { provider: "kodik", type: "playback_paused" };
      }
      if (kodikKey === "kodik_player_video_ended") {
        return { provider: "kodik", type: "playback_ended" };
      }
      if (kodikKey === "kodik_player_enter_pip") {
        return { provider: "kodik", type: "pip_entered" };
      }
      if (kodikKey === "kodik_player_exit_pip") {
        return { provider: "kodik", type: "pip_exited" };
      }
      if (kodikKey === "kodik_player_time_update") {
        const positionSeconds = Number(data.value);
        return Number.isFinite(positionSeconds) && positionSeconds >= 0
          ? { provider: "kodik", type: "time_update", positionSeconds }
          : null;
      }
      return null;
    }

    const source = String(data.source || "");
    const eventType = String(data.type || data.event || "");
    const payload = data.payload && typeof data.payload === "object"
      ? data.payload
      : data.detail && typeof data.detail === "object"
        ? data.detail
        : data;

    if (source === "aniboom-player") {
      if (["player.start", "player.play"].includes(eventType)) {
        return { provider: "aniboom", type: "playback_started" };
      }
      if (eventType === "player.pause") {
        return { provider: "aniboom", type: "playback_paused" };
      }
      if (eventType === "player.ended") {
        return { provider: "aniboom", type: "playback_ended" };
      }
      if (eventType === "player.timeupdate") {
        const positionSeconds = Number(payload.currentTime);
        return Number.isFinite(positionSeconds) && positionSeconds >= 0
          ? { provider: "aniboom", type: "time_update", positionSeconds }
          : null;
      }
      return null;
    }

    return null;
  }

  function playerMessageProvider(source) {
    if (hostnameMatches(source?.embed_host, "kodikplayer.com")) return "kodik";
    if (hostnameMatches(source?.embed_host, "aniboom.one")) return "aniboom";
    return null;
  }

  function providerPlaybackDelta({
    previousPosition,
    currentPosition,
    pageHidden = false,
    pictureInPicture = false,
    maximumStepSeconds = 30,
  } = {}) {
    if (pageHidden && !pictureInPicture) return 0;
    if (previousPosition == null || currentPosition == null) return 0;
    const previous = Number(previousPosition);
    const current = Number(currentPosition);
    const maximum = Number(maximumStepSeconds);
    if (!Number.isFinite(previous) || !Number.isFinite(current) || !Number.isFinite(maximum)) {
      return 0;
    }
    const delta = current - previous;
    return delta > 0 && delta <= Math.max(1, maximum) ? delta : 0;
  }

  function providerPlaybackProgressed(options = {}) {
    return providerPlaybackDelta(options) > 0;
  }

  function providerPlaybackEngagedSeconds({
    previousObservedAt,
    currentObservedAt,
    maximumWallStepSeconds = 30,
    ...playback
  } = {}) {
    const mediaSeconds = providerPlaybackDelta(playback);
    if (!(mediaSeconds > 0)) return 0;
    const previous = Number(previousObservedAt);
    const current = Number(currentObservedAt);
    const maximum = Number(maximumWallStepSeconds);
    if (!Number.isFinite(previous) || !Number.isFinite(current) || current <= previous) return 0;
    const elapsed = (current - previous) / 1000;
    if (!Number.isFinite(maximum)) return Math.min(mediaSeconds, elapsed);
    return Math.min(mediaSeconds, elapsed, Math.max(0, maximum));
  }

  function findKodikEpisodeTarget({
    episodes,
    sourcesByEpisode,
    currentSource,
    episodeNumber,
    seasonNumber = null,
  } = {}) {
    const reportedEpisode = integerValue(episodeNumber, { minimum: 1 });
    const reportedSeason = integerValue(seasonNumber, { minimum: 0 });
    const currentIdentity = parseKodikSerialUrl(currentSource?.embed_url);
    if (reportedEpisode == null || !currentIdentity) return null;

    const episodeList = (episodes || []).filter(episode => episode?.id != null);
    const episodeById = new Map(episodeList.map(episode => [String(episode.id), episode]));
    const matchingRows = [];
    for (const [episodeId, sources] of Object.entries(sourcesByEpisode || {})) {
      for (const source of sources || []) {
        const identity = parseKodikSerialUrl(source?.embed_url);
        if (!sameKodikSerial(identity, currentIdentity)) continue;
        if (identity.episodeNumber !== reportedEpisode) continue;
        if (
          reportedSeason != null
          && identity.seasonNumber != null
          && identity.seasonNumber !== reportedSeason
        ) continue;
        matchingRows.push({
          episode: episodeById.get(String(episodeId)) || null,
          source,
        });
      }
    }
    matchingRows.sort((left, right) => {
      const score = row => (
        (currentSource?.source_anime_id != null
          && String(row.source?.source_anime_id) === String(currentSource.source_anime_id) ? 16 : 0)
        + (row.source?.source === currentSource?.source ? 8 : 0)
        + (String(row.source?.translation_id) === String(currentSource?.translation_id) ? 4 : 0)
        + (sourceTranslationKey(row.source) === sourceTranslationKey(currentSource) ? 2 : 0)
        + (normalizeSourceIdentity(row.source?.provider_title) === normalizeSourceIdentity(currentSource?.provider_title) ? 1 : 0)
      );
      return score(right) - score(left);
    });

    const exactEpisode = episodeList.find(episode => (
      integerValue(episode.number, { minimum: 1 }) === reportedEpisode
    )) || null;
    const mappedRow = matchingRows[0] || null;
    if (
      !mappedRow
      && reportedSeason != null
      && currentIdentity.seasonNumber != null
      && reportedSeason !== currentIdentity.seasonNumber
    ) return null;
    // Kodik option values are not always the catalog's visible episode number.
    // Grouped releases can report value=5 for a source bucket labelled 1-5, so
    // the URL-backed source mapping is authoritative when it exists.
    const episode = mappedRow?.episode || exactEpisode || null;
    if (!episode) return null;

    return {
      episode,
      source: mappedRow?.source || null,
    };
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

  function hasPlaybackEvidence({
    pageHidden = false,
    fullscreen = false,
    providerPlaybackActive = false,
    pictureInPicture = false,
    fallbackFocused = false,
    evidenceExpiresAt = 0,
    now = Date.now(),
  } = {}) {
    if ((pageHidden && !pictureInPicture) || Number(evidenceExpiresAt) <= Number(now)) {
      return false;
    }
    return Boolean(fullscreen || providerPlaybackActive || pictureInPicture || fallbackFocused);
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

  function franchiseStatusKey(value) {
    const normalized = String(value ?? "")
      .trim()
      .toLocaleLowerCase("ru-RU")
      .replace(/\s+/g, "_");
    if (["ongoing", "releasing", "airing", "current", "выходит", "онгоинг"].includes(normalized)) return "ongoing";
    if (["upcoming", "announced", "planned", "анонс", "анонсировано", "скоро"].includes(normalized)) return "upcoming";
    if (["completed", "finished", "released", "завершено", "вышло"].includes(normalized)) return "completed";
    return normalized || "unknown";
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
    parseKodikSerialUrl,
    normalizePlayerMessage,
    playerMessageProvider,
    providerPlaybackDelta,
    providerPlaybackProgressed,
    providerPlaybackEngagedSeconds,
    findKodikEpisodeTarget,
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
    franchiseStatusKey,
    patchChanges,
    createKeyedSerialQueue,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.AnimeFrontendRuntime = api;
})(typeof globalThis !== "undefined" ? globalThis : window);

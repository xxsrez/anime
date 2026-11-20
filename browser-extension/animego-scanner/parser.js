function attribute(node, name) {
  const value = node?.getAttribute?.(name);
  return value == null ? null : String(value);
}

function positiveInteger(value) {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }
  return /^\d+$/.test(String(value || "")) ? Number(value) || null : null;
}

export function normalizeEmbedUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  const normalized = value
    .trim()
    .replace(/&amp;/gi, "&")
    .replace(/&#0*38;/gi, "&")
    .replace(/&#x0*26;/gi, "&");
  return normalized || null;
}

export function embedUrlParts(value) {
  const normalized = normalizeEmbedUrl(value);
  if (!normalized) {
    return null;
  }
  try {
    const parsed = new URL(normalized.startsWith("//") ? `https:${normalized}` : normalized);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || !parsed.hostname) {
      return null;
    }
    return { normalized, parsed, host: parsed.host };
  } catch (_error) {
    return null;
  }
}

export function redactEmbedUrl(value) {
  const parts = embedUrlParts(value);
  if (!parts) {
    return null;
  }
  const { parsed, host, normalized } = parts;
  // URL.pathname percent-encodes literal spaces/non-ASCII characters. The
  // backend's urllib.parse.urlparse preserves them when redacting the URL,
  // so use the original path after URL has validated the HTTPS authority.
  let path = normalized.match(/^(?:https:)?\/\/[^/?#]+([^?#]*)/i)?.[1] || "";
  let query = "";

  if (host.toLowerCase().includes("aniboom.one")) {
    path = path.replace(/(\/embed\/)[^/?#]+/, "$1<redacted>");
    const params = new URLSearchParams();
    for (const [key, currentValue] of parsed.searchParams.entries()) {
      params.append(key, key === "episode" || key === "translation" ? currentValue : "<redacted>");
    }
    const encoded = params.toString();
    query = encoded ? `?${encoded}` : "";
  } else if (host.toLowerCase().includes("kodikplayer.com")) {
    path = path.replace(
      /(\/(?:seria|serial|season|video)(?:\/\d+)?\/)[^/?#]+/i,
      "$1<redacted>",
    );
    const params = new URLSearchParams();
    for (const [key] of parsed.searchParams.entries()) {
      params.append(key, "<redacted>");
    }
    const encoded = params.toString();
    query = encoded ? `?${encoded}` : "";
  } else {
    path = path.replace(/\/[A-Za-z0-9_-]{8,}/g, "/<redacted>");
    query = parsed.search ? "?<redacted>" : "";
  }
  return `//${host}${path}${query}`;
}

export function parseProviderNode(node) {
  const providerId = attribute(node, "data-provider");
  const embedParts = embedUrlParts(attribute(node, "data-player"));
  const redacted = redactEmbedUrl(attribute(node, "data-player"));
  if (!providerId || !embedParts || !redacted) {
    return null;
  }
  const rawTranslationId = attribute(node, "data-ptranslation");
  const translationId = /^\d+$/.test(rawTranslationId || "") ? Number(rawTranslationId) : 0;
  return {
    provider_id: providerId,
    provider_title: attribute(node, "data-provider-title"),
    translation_id: translationId,
    translation_title: attribute(node, "data-translation-title") || "unknown",
    embed_host: embedParts.host,
    embed_url: embedParts.normalized,
    embed_url_redacted: redacted,
  };
}

export function parsePlayerDocument(documentNode) {
  const selected = documentNode.querySelector("select[name='series'] option[selected]");
  const selectedEpisodeId = positiveInteger(attribute(selected, "value"));

  const episodes = [];
  const seenEpisodeIds = new Set();
  let invalidEpisodeCount = 0;
  for (const node of documentNode.querySelectorAll(".player-video-bar__item[data-episode]")) {
    const id = positiveInteger(attribute(node, "data-episode"));
    if (!id) {
      invalidEpisodeCount += 1;
      continue;
    }
    if (seenEpisodeIds.has(id)) {
      continue;
    }
    seenEpisodeIds.add(id);
    episodes.push({
      id,
      number: attribute(node, "data-episode-number"),
      title: attribute(node, "data-episode-title"),
      release_label: attribute(node, "data-episode-released"),
      episode_type: attribute(node, "data-episode-type"),
      description: attribute(node, "data-episode-description"),
    });
  }

  const providers = [];
  const seenProviders = new Set();
  let invalidProviderCount = 0;
  for (const node of documentNode.querySelectorAll("[data-player][data-provider][data-ptranslation]")) {
    const provider = parseProviderNode(node);
    if (!provider) {
      invalidProviderCount += 1;
      continue;
    }
    const identity = `${provider.provider_id}\u0000${provider.translation_id}`;
    if (!seenProviders.has(identity)) {
      seenProviders.add(identity);
      providers.push(provider);
    }
  }
  return { selectedEpisodeId, episodes, providers, invalidEpisodeCount, invalidProviderCount };
}

export function parsePlayerContent(content, Parser = globalThis.DOMParser) {
  if (typeof Parser !== "function") {
    throw new Error("DOMParser недоступен в этом окружении.");
  }
  const documentNode = new Parser().parseFromString(String(content || ""), "text/html");
  return parsePlayerDocument(documentNode);
}

export function parseUnavailableReason(content, Parser = globalThis.DOMParser) {
  if (typeof Parser !== "function") {
    throw new Error("DOMParser недоступен в этом окружении.");
  }
  const documentNode = new Parser().parseFromString(String(content || ""), "text/html");
  return String(documentNode.body?.textContent || "").replace(/\s+/g, " ").trim() || null;
}

// AnimeGO may list an announced episode before any video exists. Only accept
// its explicit release-date message, and keep unknown or expired dates as
// incomplete checks. For unknown time zones, require the date to be in the
// future even in the world's easternmost time zone.
export function isFutureRelease(reason, now = Date.now()) {
  const months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  const match = String(reason || "").match(/^Эпизод выйдет\s+(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?(?:\s+\(([^)]+)\))?(?:\s|$)/i);
  if (!match || !Number.isFinite(now)) return false;
  const day = Number(match[1]);
  const month = months.indexOf(match[2].toLowerCase());
  const year = Number(match[3]);
  const hour = match[4] === undefined ? null : Number(match[4]);
  const minute = match[5] === undefined ? null : Number(match[5]);
  const date = new Date(Date.UTC(year, month, day));
  if (month < 0 || date.getUTCFullYear() !== year || date.getUTCMonth() !== month || date.getUTCDate() !== day
      || (hour !== null && (hour > 23 || minute > 59))) return false;

  const zones = { "москва": "Europe/Moscow", "чехия": "Europe/Prague", "красноярск": "Asia/Krasnoyarsk" };
  const zone = zones[(match[6] || "").toLowerCase()];
  if (!zone) {
    const earliest = Date.UTC(year, month, day, hour ?? 0, minute ?? 0) - 14 * 60 * 60 * 1000;
    return earliest > now;
  }
  let current;
  try {
    current = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date(now)).map(part => [part.type, part.value]));
  } catch (_error) {
    return false;
  }
  const announcedDate = year * 10000 + (month + 1) * 100 + day;
  const currentDate = Number(current.year) * 10000 + Number(current.month) * 100 + Number(current.day);
  if (announcedDate !== currentDate) return announcedDate > currentDate;
  return Boolean(hour !== null && hour * 60 + minute > Number(current.hour) * 60 + Number(current.minute));
}

export function unknownEpisodes(episodes, knownEpisodeIds) {
  const known = new Set(
    (Array.isArray(knownEpisodeIds) ? knownEpisodeIds : [])
      .map(positiveInteger)
      .filter((value) => value != null),
  );
  return (Array.isArray(episodes) ? episodes : []).filter((episode) => {
    const id = positiveInteger(episode?.id);
    return id != null && !known.has(id);
  });
}

export function syntheticEpisode(animeId, title) {
  const id = positiveInteger(animeId);
  if (!id || id >= 2_000_000) {
    throw new Error("Некорректный AnimeGo id для синтетической серии.");
  }
  return {
    id: id * 1000 + 1,
    number: "1",
    title: typeof title === "string" && title ? title : null,
    release_label: null,
    episode_type: "movie",
    description: "Single player entry without an upstream episode list.",
  };
}

export function shouldUseInitialProviders(episode, selectedEpisodeId, episodeCount, animeId) {
  return Boolean(
    episode &&
      (episode.id === Number(animeId) * 1000 + 1 ||
        episode.id === selectedEpisodeId ||
        episodeCount === 1),
  );
}

export function looksLikeChallenge(body) {
  const text = String(body || "").toLowerCase();
  return [
    "captcha",
    "ddos-guard",
    "cf-chl-",
    "challenge-platform",
    "проверка браузера",
    "подтвердите, что вы не робот",
    "verify you are human",
  ].some((marker) => text.includes(marker));
}

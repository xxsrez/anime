import { redactEmbedUrl } from "./parser.js";

export const KINOGOO_ORIGINS = ["https://kinogoo.fm/*", "https://api.nextembed.ws/*"];

export async function fetchKinogooHtml(url, signal, fetcher = globalThis.fetch) {
  const requested = new URL(url);
  const titleId = requested.origin === "https://kinogoo.fm"
    && /^\/(\d+)-[a-z0-9-]+\.html$/.exec(requested.pathname)?.[1];
  const isPlayer = requested.origin === "https://api.nextembed.ws"
    && /^\/embed\/kp\/\d+$/.test(requested.pathname);
  if ((!titleId && !isPlayer) || requested.username || requested.password) {
    throw new Error("Некорректный адрес Kinogoo");
  }
  // Kinogoo changes title slugs and redirects old links. Player redirects
  // remain forbidden; page responses must still identify the same title.
  const label = `Kinogoo (${titleId ? "страница сериала" : "плейлист"}, ${requested.origin}${requested.pathname})`;
  let response;
  try {
    response = await fetcher(url, {
      signal, credentials: "omit", redirect: titleId ? "follow" : "error",
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new Error(`${label}: запрос не выполнен: ${error?.message || String(error)}. Подробная причина — в Console Chrome.`, { cause: error });
  }
  if (!response.ok) throw new Error(`Kinogoo: HTTP ${response.status}`);
  const resolved = new URL(response.url);
  if (resolved.origin !== requested.origin || resolved.username || resolved.password
      || (titleId && /^\/(\d+)-[a-z0-9-]+\.html$/.exec(resolved.pathname)?.[1] !== titleId)) {
    throw new Error("Kinogoo перенаправил запрос на неподдерживаемый адрес");
  }
  try {
    return await response.text();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new Error(`${label}: не удалось прочитать ответ: ${error?.message || String(error)}`, { cause: error });
  }
}

function decodeAttribute(value) {
  const named = { amp: "&", quot: '"', apos: "'", lt: "<", gt: ">" };
  return value.replace(/&(?:#(x[0-9a-f]+|[0-9]+)|(amp|quot|apos|lt|gt));/gi, (_match, numeric, name) => {
    if (name) return named[name.toLowerCase()];
    const number = numeric[0].toLowerCase() === "x"
      ? Number.parseInt(numeric.slice(1), 16) : Number.parseInt(numeric, 10);
    return number > 0 && number <= 0x10ffff && !(number >= 0xd800 && number <= 0xdfff)
      ? String.fromCodePoint(number) : "\ufffd";
  });
}

function attributeValue(tag, start, wanted) {
  let index = start;
  while (index < tag.length) {
    while (index < tag.length && /[\s/]/.test(tag[index])) index += 1;
    if (index >= tag.length || tag[index] === ">") break;
    const begin = index;
    while (index < tag.length && !/[\s=/>]/.test(tag[index])) index += 1;
    const name = tag.slice(begin, index).toLowerCase();
    if (!name) { index += 1; continue; }
    while (index < tag.length && /\s/.test(tag[index])) index += 1;
    let value = "";
    if (tag[index] === "=") {
      index += 1;
      while (index < tag.length && /\s/.test(tag[index])) index += 1;
      const quote = tag[index] === '"' || tag[index] === "'" ? tag[index++] : null;
      const valueStart = index;
      if (quote) {
        while (index < tag.length && tag[index] !== quote) index += 1;
        value = tag.slice(valueStart, index);
        if (tag[index] === quote) index += 1;
      } else {
        while (index < tag.length && !/[\s>]/.test(tag[index])) index += 1;
        value = tag.slice(valueStart, index);
      }
    }
    // HTML uses the first occurrence of a repeated attribute.
    if (name === wanted) return decodeAttribute(value);
  }
  return null;
}

function tagEnd(html, start) {
  let quote = null;
  for (let index = start; index < html.length; index += 1) {
    const char = html[index];
    if (quote) {
      if (char === quote) quote = null;
    } else if (char === '"' || char === "'") quote = char;
    else if (char === ">") return index + 1;
  }
  return -1;
}

export function kinogooPlayer(html, page) {
  const url = new URL(page);
  if (url.origin !== "https://kinogoo.fm" || url.username || url.password
      || !/^\/\d+-[a-z0-9-]+\.html$/.test(url.pathname)) {
    throw new Error("Некорректный адрес Kinogoo");
  }
  // Inspect markup as text. DOMParser on a third-party page may load images or
  // frames and cause its inline event handlers to trigger extension CSP errors.
  if (typeof html !== "string") throw new Error("Некорректная страница Kinogoo");
  let index = 0, rawText = null, templateDepth = 0;
  while (index < html.length) {
    if (rawText) {
      const closing = new RegExp(`</${rawText}(?=[\\s/>])[^>]*>`, "ig");
      closing.lastIndex = index;
      const found = closing.exec(html);
      if (!found) break;
      index = closing.lastIndex;
      rawText = null;
      continue;
    }
    const opening = html.indexOf("<", index);
    if (opening < 0) break;
    if (html.startsWith("<!--", opening)) {
      const end = html.indexOf("-->", opening + 4);
      if (end < 0) break;
      index = end + 3;
      continue;
    }
    const match = /^<(\/?)(([a-z][a-z0-9:-]*))(?=[\s/>])/i.exec(html.slice(opening));
    if (!match) { index = opening + 1; continue; }
    const end = tagEnd(html, opening + match[0].length);
    if (end < 0) break;
    const name = match[2].toLowerCase();
    const closing = Boolean(match[1]);
    const tag = html.slice(opening, end);
    index = end;
    if (name === "template") {
      templateDepth = closing ? Math.max(0, templateDepth - 1) : templateDepth + 1;
    } else if (!closing && name === "iframe" && templateDepth === 0) {
      const source = attributeValue(tag, match[0].length, "src");
      if (source) {
        try {
          const target = new URL(source, page);
          if (target.origin === "https://api.nextembed.ws" && !target.username && !target.password
              && /^\/embed\/kp\/\d+$/.test(target.pathname)) return target.href;
        } catch (_error) {
          // Ignore a malformed unrelated iframe and continue to the player.
        }
      }
    }
    if (!closing && ["script", "style", "textarea", "title", "xmp", "iframe"].includes(name)) rawText = name;
    if (!closing && name === "plaintext") break;
  }
  throw new Error("Поддерживаемый плеер Kinogoo не найден");
}

export function kinogooEpisodes(html, embed, season) {
  const match = /\bseasons\s*:\s*(\[)/.exec(html);
  if (!match) throw new Error("Список серий Kinogoo не найден");
  const start = match.index + match[0].length - 1;
  let depth = 0, quoted = false, escaped = false, end = -1;
  for (let i = start; i < html.length; i++) {
    const c = html[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') quoted = false;
    } else if (c === '"') quoted = true;
    else if (c === "[") depth++;
    else if (c === "]" && --depth === 0) { end = i + 1; break; }
  }
  if (end < 0) throw new Error("Повреждённый список серий Kinogoo");
  const result = [], seen = new Set();
  let coverageComplete = true;
  for (const entry of JSON.parse(html.slice(start, end))) {
    if (entry.season !== season) continue;
    if (entry.blocked) { coverageComplete = false; continue; }
    for (const ep of entry.episodes || []) {
      if (ep.blocked || !(ep.hls || ep.dash)) { coverageComplete = false; continue; }
      const number = String(ep.episode);
      // Unnumbered extras are not part of the season's main episode list.
      if (number === "0") { coverageComplete = false; continue; }
      if (!Number.isInteger(ep.id) || ep.id <= 0 || ep.id >= 500000000 || !/^[1-9]\d*$/.test(number)) throw new Error("Некорректная серия Kinogoo");
      const id = 1500000000 + ep.id;
      if (seen.has(id)) throw new Error("Повтор серии Kinogoo");
      seen.add(id);
      const url = new URL(embed);
      if (url.origin !== "https://api.nextembed.ws") throw new Error("Некорректный плеер Kinogoo");
      url.searchParams.set("season", season);
      url.searchParams.set("episode", number);
      result.push({ episode: { id, number, title: `Серия ${number}` }, providers: [{
        provider_id: "kinogoo-nextembed", provider_title: "Kinogoo", translation_id: 0,
        translation_title: "Оригинал", embed_host: url.host, embed_url: url.href,
        embed_url_redacted: redactEmbedUrl(url.href),
      }] });
    }
  }
  if (!result.length) throw new Error("Нет доступных серий выбранного сезона Kinogoo");
  Object.defineProperty(result, "coverageComplete", { value: coverageComplete });
  return result;
}

import { redactEmbedUrl } from "./parser.js";

export const KINOGOO_ORIGINS = ["https://kinogoo.fm/*", "https://api.nextembed.ws/*"];

export function kinogooPlayer(html, page, Parser = globalThis.DOMParser) {
  const url = new URL(page);
  if (url.origin !== "https://kinogoo.fm" || !/^\/\d+-[a-z0-9-]+\.html$/.test(url.pathname)) {
    throw new Error("Некорректный адрес Kinogoo");
  }
  const doc = new Parser().parseFromString(html, "text/html");
  for (const frame of doc.querySelectorAll("iframe[src]")) {
    const target = new URL(frame.getAttribute("src"), page);
    if (target.origin === "https://api.nextembed.ws" && /^\/embed\/kp\/\d+$/.test(target.pathname)) return target.href;
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
  for (const entry of JSON.parse(html.slice(start, end))) {
    if (entry.season !== season || entry.blocked) continue;
    for (const ep of entry.episodes || []) {
      if (ep.blocked || !(ep.hls || ep.dash)) continue;
      const number = String(ep.episode);
      // Unnumbered extras are not part of the season's main episode list.
      if (number === "0") continue;
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
  return result;
}

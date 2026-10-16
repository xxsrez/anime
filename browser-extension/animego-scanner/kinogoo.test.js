import assert from "node:assert/strict";
import test from "node:test";
import { fetchKinogooHtml, kinogooEpisodes } from "./kinogoo.js";

test("Kinogoo follows a renamed title page and discovers the new playable episode", async () => {
  const page = "https://kinogoo.fm/4037-polden-1-sezon.html";
  const signal = new AbortController().signal;
  const body = await fetchKinogooHtml(page, signal, async (url, options) => {
    assert.equal(url, page);
    assert.equal(options.redirect, "follow");
    assert.equal(options.credentials, "omit");
    assert.equal(options.signal, signal);
    return { ok: true, url: "https://kinogoo.fm/4037-polden-1-sezon1.html", text: async () => "page" };
  });
  assert.equal(body, "page");
  const playlist = await fetchKinogooHtml(embed, signal, async (_url, options) => {
    assert.equal(options.redirect, "error");
    return { ok: true, url: embed, text: async () => html([{ season: 1,
      episodes: [1, 2, 3, 4].map(n => ({ id: n, episode: n, hls: "stream" })) }]) };
  });
  const known = new Set([1500000001, 1500000002, 1500000003]);
  assert.deepEqual(kinogooEpisodes(playlist, embed, 1)
    .filter(x => !known.has(x.episode.id)).map(x => x.episode.number), ["4"]);
});

test("Kinogoo rejects redirects to another host, protocol, title or unsupported path", async () => {
  for (const url of ["https://example.test/4037-polden.html", "http://kinogoo.fm/4037-polden.html",
    "https://kinogoo.fm/4038-other.html", "https://kinogoo.fm/login"]) {
    await assert.rejects(fetchKinogooHtml("https://kinogoo.fm/4037-polden.html", undefined,
      async () => ({ ok: true, url, text: async () => { throw new Error("must not read"); } })),
    /неподдерживаемый адрес/);
  }
});

test("Kinogoo network failures identify the stage without leaking query parameters", async () => {
  for (const [url, stage] of [["https://kinogoo.fm/4037-polden.html?private=secret", "страница сериала"],
    [embed + "&private=secret", "плейлист"]]) {
    await assert.rejects(fetchKinogooHtml(url, undefined, async () => { throw new TypeError("Failed to fetch"); }), error => {
      assert.match(error.message, new RegExp(stage));
      assert.match(error.message, /Failed to fetch/);
      assert.ok(!error.message.includes("secret"));
      return true;
    });
  }
});

test("Kinogoo preserves aborts so Pause and Stop keep their semantics", async () => {
  const aborted = new DOMException("Aborted", "AbortError");
  await assert.rejects(fetchKinogooHtml(embed, undefined, async () => { throw aborted; }), error => error === aborted);
});

const embed = "https://api.nextembed.ws/embed/kp/123?host=kinogoo.fm&geoBlock=RU%2CEE";
const html = entries => `makePlayer({playlist:{seasons:${JSON.stringify(entries)}}});`;
test("Kinogoo skips zero-numbered extras without losing the main episodes", () => {
  for (const zero of [0, "0"]) {
    const episodes = [{ id: 7, episode: zero, hls: "x" }, { id: 8, episode: "1", hls: "x" }];
    const result = kinogooEpisodes(html([{ season: 2, episodes }]), embed, 2);
    assert.deepEqual(result.map(x => x.episode.number), ["1"]);
    assert.throws(() => kinogooEpisodes(html([{ season: 2, episodes: episodes.slice(0, 1) }]), embed, 2));
  }
});
test("Kinogoo uses playable playlist entries of one season, retaining geo restrictions", () => {
  const result = kinogooEpisodes(html([
    { season: 1, episodes: [{ id: 7, episode: "1", hls: "https://example.test/video" }, { id: 8, episode: "2" }] },
    { season: 2, episodes: [{ id: 9, episode: "1", hls: "x" }] },
  ]), embed, 1);
  assert.equal(result.length, 1);
  assert.equal(result[0].episode.id, 1500000007);
  const url = new URL(result[0].providers[0].embed_url);
  assert.equal(url.searchParams.get("episode"), "1");
  assert.equal(url.searchParams.get("season"), "1");
  assert.equal(url.searchParams.get("geoBlock"), "RU,EE");
});
test("Kinogoo rejects malformed, blocked, empty and duplicate playlists", () => {
  for (const value of ["", "seasons: [", html([{ season: 1, blocked: true, episodes: [] }]),
    html([{ season: 1, episodes: [{ id: 1, episode: "1", hls: "x" }, { id: 1, episode: "2", hls: "x" }] }])]) {
    assert.throws(() => kinogooEpisodes(value, embed, 1));
  }
});

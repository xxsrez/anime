import assert from "node:assert/strict";
import test from "node:test";
import { kinogooEpisodes } from "./kinogoo.js";

const embed = "https://api.nextembed.ws/embed/kp/123?host=kinogoo.fm&geoBlock=RU%2CEE";
const html = entries => `makePlayer({playlist:{seasons:${JSON.stringify(entries)}}});`;
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

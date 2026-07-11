import assert from "node:assert/strict";
import test from "node:test";

import {
  looksLikeChallenge,
  parsePlayerContent,
  redactEmbedUrl,
  shouldUseInitialProviders,
  syntheticEpisode,
  unknownEpisodes,
} from "./parser.js";

class FakeNode {
  constructor(attributes = {}, textContent = "") {
    this.attributes = attributes;
    this.textContent = textContent;
  }

  getAttribute(name) {
    return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;
  }
}

function fakeDocument({ selected = null, episodes = [], providers = [], text = "" } = {}) {
  return {
    body: { textContent: text },
    querySelector(selector) {
      assert.equal(selector, "select[name='series'] option[selected]");
      return selected;
    },
    querySelectorAll(selector) {
      if (selector === ".player-video-bar__item[data-episode]") {
        return episodes;
      }
      if (selector === "[data-player][data-provider][data-ptranslation]") {
        return providers;
      }
      throw new Error(`Unexpected selector: ${selector}`);
    },
  };
}

test("redacts supported provider URLs with the same stable shape as the backend", () => {
  assert.equal(
    redactEmbedUrl("//aniboom.one/embed/super-secret?episode=12&token=abc&translation=3"),
    "//aniboom.one/embed/<redacted>?episode=12&token=%3Credacted%3E&translation=3",
  );
  assert.equal(
    redactEmbedUrl("https://kodikplayer.com/seria/123/super-secret?only_episode=1"),
    "//kodikplayer.com/seria/123/<redacted>?only_episode=%3Credacted%3E",
  );
  assert.equal(
    redactEmbedUrl("https://video.sibnet.ru/shell.php?videoid=123"),
    "//video.sibnet.ru/shell.php?<redacted>",
  );
  assert.equal(redactEmbedUrl("http://aniboom.one/embed/nope"), null);
  assert.equal(redactEmbedUrl("https://name:secret@aniboom.one/embed/nope"), null);
});

test("parses episodes and deduplicates providers by backend identity", () => {
  const documentNode = fakeDocument({
    selected: new FakeNode({ value: "501" }),
    episodes: [
      new FakeNode({
        "data-episode": "501",
        "data-episode-number": "1",
        "data-episode-title": "Начало",
        "data-episode-released": "Сегодня",
        "data-episode-type": "episode",
        "data-episode-description": "Описание",
      }),
      new FakeNode({ "data-episode": "bad" }),
      new FakeNode({ "data-episode": "501", "data-episode-number": "duplicate" }),
      new FakeNode({ "data-episode": "502", "data-episode-number": "2" }),
    ],
    providers: [
      new FakeNode({
        "data-player": "//aniboom.one/embed/secret-one?episode=501",
        "data-provider": "aniboom",
        "data-provider-title": "AniBoom",
        "data-ptranslation": "44",
        "data-translation-title": "Дубляж",
      }),
      new FakeNode({
        "data-player": "//aniboom.one/embed/secret-two?episode=501",
        "data-provider": "aniboom",
        "data-provider-title": "AniBoom duplicate",
        "data-ptranslation": "44",
        "data-translation-title": "Дубляж",
      }),
      new FakeNode({
        "data-player": "http://unsafe.example/player",
        "data-provider": "unsafe",
        "data-ptranslation": "2",
      }),
    ],
  });
  let parserMime = null;
  class FakeParser {
    parseFromString(_content, mime) {
      parserMime = mime;
      return documentNode;
    }
  }

  const parsed = parsePlayerContent("<ignored>", FakeParser);
  assert.equal(parserMime, "text/html");
  assert.equal(parsed.selectedEpisodeId, 501);
  assert.deepEqual(parsed.episodes, [
    {
      id: 501,
      number: "1",
      title: "Начало",
      release_label: "Сегодня",
      episode_type: "episode",
      description: "Описание",
    },
    {
      id: 502,
      number: "2",
      title: null,
      release_label: null,
      episode_type: null,
      description: null,
    },
  ]);
  assert.equal(parsed.providers.length, 1);
  assert.deepEqual(parsed.providers[0], {
    provider_id: "aniboom",
    provider_title: "AniBoom",
    translation_id: 44,
    translation_title: "Дубляж",
    embed_host: "aniboom.one",
    embed_url: "//aniboom.one/embed/secret-one?episode=501",
    embed_url_redacted: "//aniboom.one/embed/<redacted>?episode=501",
  });
});

test("diffs known episode ids without type-sensitive duplicates", () => {
  const episodes = [{ id: 10 }, { id: 11 }, { id: 12 }];
  assert.deepEqual(unknownEpisodes(episodes, ["10", 12]), [{ id: 11 }]);
});

test("matches initial-provider and synthetic movie rules", () => {
  const synthetic = syntheticEpisode(321, "Фильм");
  assert.equal(synthetic.id, 321001);
  assert.equal(synthetic.episode_type, "movie");
  assert.equal(shouldUseInitialProviders(synthetic, null, 1, 321), true);
  assert.equal(shouldUseInitialProviders({ id: 80 }, 80, 5, 321), true);
  assert.equal(shouldUseInitialProviders({ id: 81 }, 80, 5, 321), false);
});

test("recognizes common anti-bot challenge responses", () => {
  assert.equal(looksLikeChallenge("<title>DDoS-Guard</title>"), true);
  assert.equal(looksLikeChallenge("Please verify you are human"), true);
  assert.equal(looksLikeChallenge('{"data":{"content":"normal player"}}'), false);
});

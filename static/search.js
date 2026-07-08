(function initAnimeSearch(root) {
  const SEARCH_FOLDS = [
    [/тсу/g, "цу"],
    [/дж([аеёиоуыэюя])/g, "дз$1"],
    [/ши/g, "си"],
    [/чи/g, "ти"],
    [/tsu/g, "tu"],
    [/shi/g, "si"],
    [/chi/g, "ti"],
    [/ji/g, "zi"],
    [/ou/g, "o"],
    [/oo/g, "o"],
  ];
  const MIN_FUZZY_LENGTH = 4;
  const PRIMARY_TITLE_WEIGHT = 14;
  const SUBTITLE_WEIGHT = 11;
  const VARIANT_TITLE_WEIGHT = 10;
  const VARIANT_SUBTITLE_WEIGHT = 9;
  const SOURCE_WEIGHT = 3;
  const GENRE_WEIGHT = 5;

  function foldSearchText(value) {
    return SEARCH_FOLDS.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), value);
  }

  function searchText(value) {
    return foldSearchText(
      String(value || "")
        .normalize("NFKD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLocaleLowerCase("ru")
        .replaceAll("ё", "е")
        .replaceAll("э", "е")
        .replace(/[^\p{L}\p{N}]+/gu, " ")
        .replace(/\s+/g, " ")
        .trim()
    );
  }

  function searchTokens(value) {
    return searchText(value).split(/\s+/).filter(Boolean);
  }

  function uniqueTokens(tokens) {
    return [...new Set(tokens)];
  }

  function addSearchField(fields, value, weight) {
    const normalized = searchText(value);
    if (!normalized) return;
    fields.push({
      text: normalized,
      tokens: uniqueTokens(normalized.split(/\s+/).filter(Boolean)),
      weight,
    });
  }

  function addStructuredSearchFields(fields, searchFields) {
    for (const field of searchFields || []) {
      if (!field || typeof field !== "object") {
        addSearchField(fields, field, 1);
        continue;
      }
      const weight = Number.isFinite(Number(field.weight)) ? Number(field.weight) : 1;
      addSearchField(fields, field.value, weight);
    }
  }

  function buildSearchIndex(item) {
    const fields = [];
    addSearchField(fields, item?.title, PRIMARY_TITLE_WEIGHT);
    addSearchField(fields, item?.subtitle, SUBTITLE_WEIGHT);

    for (const variant of item?.source_variants || []) {
      addSearchField(fields, variant.title, VARIANT_TITLE_WEIGHT);
      addSearchField(fields, variant.subtitle, VARIANT_SUBTITLE_WEIGHT);
      addSearchField(fields, variant.source, SOURCE_WEIGHT);
    }

    for (const genre of item?.genres || []) addSearchField(fields, genre, GENRE_WEIGHT);
    addSearchField(fields, item?.kind, 3);
    addSearchField(fields, item?.status, 3);
    addSearchField(fields, item?.year, 2);
    addSearchField(fields, item?.source, SOURCE_WEIGHT);
    for (const source of item?.sources || []) addSearchField(fields, source, SOURCE_WEIGHT);
    addStructuredSearchFields(fields, item?.search_fields);

    const tokenWeights = new Map();
    for (const field of fields) {
      for (const token of field.tokens) {
        tokenWeights.set(token, Math.max(tokenWeights.get(token) || 0, field.weight));
      }
    }

    return {
      fields,
      tokens: [...tokenWeights.entries()].map(([token, weight]) => ({ token, weight })),
    };
  }

  function ensureSearchIndex(item) {
    if (!item) return buildSearchIndex(null);
    if (!item._searchIndex) {
      Object.defineProperty(item, "_searchIndex", {
        value: buildSearchIndex(item),
        configurable: true,
      });
    }
    return item._searchIndex;
  }

  function prepareSearchIndexes(items) {
    for (const item of items || []) ensureSearchIndex(item);
    return items || [];
  }

  function searchQuery(value) {
    const text = searchText(value);
    return {
      text,
      tokens: uniqueTokens(text.split(/\s+/).filter(Boolean)),
    };
  }

  function maxEditDistance(token) {
    if (token.length < MIN_FUZZY_LENGTH) return 0;
    return token.length >= 10 ? 2 : 1;
  }

  function boundedDamerauLevenshtein(left, right, maxDistance) {
    if (left === right) return 0;
    if (Math.abs(left.length - right.length) > maxDistance) return maxDistance + 1;

    let previous = Array.from({ length: right.length + 1 }, (_, index) => index);
    let beforePrevious = null;

    for (let i = 1; i <= left.length; i += 1) {
      const current = [i];
      let rowMin = current[0];
      for (let j = 1; j <= right.length; j += 1) {
        const cost = left[i - 1] === right[j - 1] ? 0 : 1;
        let value = Math.min(
          previous[j] + 1,
          current[j - 1] + 1,
          previous[j - 1] + cost
        );
        if (
          beforePrevious
          && i > 1
          && j > 1
          && left[i - 1] === right[j - 2]
          && left[i - 2] === right[j - 1]
        ) {
          value = Math.min(value, beforePrevious[j - 2] + 1);
        }
        current[j] = value;
        rowMin = Math.min(rowMin, value);
      }
      if (rowMin > maxDistance) return maxDistance + 1;
      beforePrevious = previous;
      previous = current;
    }

    return previous[right.length];
  }

  function tokenMatchScore(queryToken, candidate) {
    const token = candidate.token;
    const weight = candidate.weight;
    if (token === queryToken) return 120 + weight * 12 + token.length;

    if (token.length >= 3 && queryToken.length >= 3) {
      if (token.startsWith(queryToken)) return 92 + weight * 10 + queryToken.length;
      if (
        queryToken.startsWith(token)
        && token.length >= MIN_FUZZY_LENGTH
        && token.length / queryToken.length >= 0.6
      ) {
        return 78 + weight * 8 + token.length;
      }
      if (token.includes(queryToken)) return 66 + weight * 7;
      if (
        queryToken.includes(token)
        && token.length >= MIN_FUZZY_LENGTH
        && token.length / queryToken.length >= 0.6
      ) {
        return 66 + weight * 7;
      }
    }

    const maxDistance = Math.min(maxEditDistance(queryToken), maxEditDistance(token));
    if (!maxDistance) return 0;
    const distance = boundedDamerauLevenshtein(queryToken, token, maxDistance);
    if (distance <= maxDistance) {
      return 48 + weight * 6 + Math.min(queryToken.length, token.length) - distance * 10;
    }
    return 0;
  }

  function bestTokenScore(queryToken, candidates) {
    let best = 0;
    for (const candidate of candidates) {
      best = Math.max(best, tokenMatchScore(queryToken, candidate));
    }
    return best;
  }

  function phraseScore(query, field) {
    if (!query.text || !field.text) return 0;
    if (field.text === query.text) return 500 + field.weight * 30;
    if (field.text.includes(query.text)) return 360 + field.weight * 24 + query.text.length;
    if (query.text.includes(field.text) && field.text.length >= 4) return 240 + field.weight * 12;
    return 0;
  }

  function requiredTokenMatches(count) {
    if (count <= 2) return count;
    return Math.ceil(count * 0.67);
  }

  function scoreSearchItem(item, query) {
    if (!query?.tokens?.length) return 0;

    const index = ensureSearchIndex(item);
    let phrase = 0;
    for (const field of index.fields) phrase = Math.max(phrase, phraseScore(query, field));

    let matched = 0;
    let tokenScore = 0;
    for (const token of query.tokens) {
      const score = bestTokenScore(token, index.tokens);
      if (score > 0) {
        matched += 1;
        tokenScore += score;
      }
    }

    if (matched < requiredTokenMatches(query.tokens.length)) return phrase;
    const coverage = matched / query.tokens.length;
    return phrase + tokenScore * coverage + matched * 20;
  }

  function compareSearchResults(left, right, query, fallbackCompare) {
    if (query?.tokens?.length) {
      const scoreDiff = scoreSearchItem(right, query) - scoreSearchItem(left, query);
      if (scoreDiff !== 0) return scoreDiff;
    }
    return fallbackCompare(left, right);
  }

  const api = {
    searchText,
    searchTokens,
    searchQuery,
    buildSearchIndex,
    ensureSearchIndex,
    prepareSearchIndexes,
    scoreSearchItem,
    compareSearchResults,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.AnimeSearch = api;
})(typeof globalThis !== "undefined" ? globalThis : window);

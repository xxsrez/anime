import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

import {
  ANIMEGO_HOST_PERMISSION,
  hasAnimeGoHostPermission,
  requestAnimeGoHostPermission,
  resolveExtensionApi,
} from "./permissions.js";

test("resolves Safari browser namespace before the Chromium fallback", () => {
  const browser = { runtime: {} };
  const chrome = { runtime: {} };
  assert.equal(resolveExtensionApi({ browser, chrome }), browser);
  assert.equal(resolveExtensionApi({ chrome }), chrome);
  assert.equal(resolveExtensionApi({}), null);
});

test("checks and requests only the AnimeGo origin", async () => {
  const calls = [];
  const api = {
    permissions: {
      async contains(request) {
        calls.push(["contains", request]);
        return true;
      },
      async request(request) {
        calls.push(["request", request]);
        return true;
      },
    },
  };

  assert.equal(await hasAnimeGoHostPermission(api), true);
  assert.equal(await requestAnimeGoHostPermission(api), true);
  assert.deepEqual(calls, [
    ["contains", { origins: [ANIMEGO_HOST_PERMISSION] }],
    ["request", { origins: [ANIMEGO_HOST_PERMISSION] }],
  ]);
});

test("manifest keeps app hosts required and AnimeGo access optional", () => {
  const manifest = JSON.parse(fs.readFileSync(new URL("manifest.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.optional_host_permissions, [ANIMEGO_HOST_PERMISSION]);
  assert.equal(manifest.host_permissions.includes(ANIMEGO_HOST_PERMISSION), false);
  assert.equal(manifest.browser_specific_settings.safari.strict_min_version, "16.4");
});

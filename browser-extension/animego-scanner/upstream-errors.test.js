import assert from "node:assert/strict";
import test from "node:test";
import { upstreamHttpError } from "./upstream-errors.js";

test("404 diagnostics identify the failed stage and exact public URL", () => {
  for (const [path, stage] of [["/player/2727", "Плеер тайтла"], ["/player/videos/123", "Плеер серии"], ["/anime/old-2727", "Карточка тайтла"]]) {
    const message = upstreamHttpError(path, 404);
    assert.ok(message.startsWith(stage));
    assert.ok(message.includes(`https://animego.me${path}`));
    assert.ok(message.includes("Проверка не завершена"));
  }
  assert.ok(upstreamHttpError("/player/1", 503).includes("HTTP 503"));
});

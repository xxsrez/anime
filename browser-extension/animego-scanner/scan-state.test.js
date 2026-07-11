import assert from "node:assert/strict";
import test from "node:test";

import { shouldRestartAfterReload } from "./scan-state.js";

test("a running replacement session restarts after the superseded loop exits", () => {
  assert.equal(shouldRestartAfterReload(2, 3, "running"), true);
  assert.equal(shouldRestartAfterReload(3, 3, "running"), false);
  assert.equal(shouldRestartAfterReload(2, 3, "paused"), false);
});

const assert = require("node:assert/strict");
const runtime = require("./frontend_runtime.js");

assert.equal(
  runtime.safeHttpsUrl("https://video.sibnet.ru/shell.php?videoid=1", ["sibnet.ru"]),
  "https://video.sibnet.ru/shell.php?videoid=1",
);
assert.equal(runtime.safeHttpsUrl("http://kodikplayer.com/video", ["kodikplayer.com"]), null);
assert.equal(runtime.safeHttpsUrl("https://kodikplayer.com.evil.test/video", ["kodikplayer.com"]), null);
assert.equal(runtime.safeHttpsUrl("https://user@example.com/video", ["example.com"]), null);
assert.equal(
  runtime.safeHttpsUrl("//kodikplayer.com/seria/1/token/720p", ["kodikplayer.com"]),
  "https://kodikplayer.com/seria/1/token/720p",
);

const springBefore = new Date(2026, 2, 28, 23, 30);
const springAfter = new Date(2026, 2, 29, 23, 30);
assert.equal(runtime.localCalendarDayDifference(springAfter, springBefore), 1);

assert.equal(runtime.boundedElapsedSeconds(0, 30_000, 300), 30);
assert.equal(runtime.boundedElapsedSeconds(0, 400_000, 300), 300);
assert.equal(runtime.boundedElapsedSeconds(10_000, 5_000, 300), 0);
assert.equal(runtime.hasPlaybackEvidence({ playerFocused: true, evidenceExpiresAt: 20, now: 10 }), true);
assert.equal(runtime.hasPlaybackEvidence({ playerFocused: true, evidenceExpiresAt: 5, now: 10 }), false);
assert.equal(runtime.hasPlaybackEvidence({ fullscreen: true, evidenceExpiresAt: 20, now: 10 }), true);
assert.equal(runtime.hasPlaybackEvidence({ pageHidden: true, playerFocused: true, evidenceExpiresAt: 20, now: 10 }), false);

let meaningfulPlaybackSeconds = 0;
for (let heartbeat = 0; heartbeat < 11; heartbeat += 1) {
  meaningfulPlaybackSeconds += runtime.boundedElapsedSeconds(heartbeat * 30_000, (heartbeat + 1) * 30_000, 300);
}
assert.equal(meaningfulPlaybackSeconds, 330);

async function testKeyedQueue() {
  const events = [];
  const releases = new Map();
  const queue = runtime.createKeyedSerialQueue(async (key, value) => {
    events.push(`start:${key}:${value}`);
    await new Promise(resolve => releases.set(`${key}:${value}`, resolve));
    events.push(`end:${key}:${value}`);
    return value;
  });

  const a = queue.enqueue("anime-1", "A");
  const b = queue.enqueue("anime-1", "B");
  const c = queue.enqueue("anime-2", "C");
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(events, ["start:anime-1:A", "start:anime-2:C"]);
  assert.equal(queue.pending(), 3);

  releases.get("anime-1:A")();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(events.slice(-2), ["end:anime-1:A", "start:anime-1:B"]);

  releases.get("anime-1:B")();
  releases.get("anime-2:C")();
  assert.deepEqual(await Promise.all([a, b, c]), ["A", "B", "C"]);
  assert.equal(queue.pending(), 0);
}

testKeyedQueue().then(() => {
  console.log("frontend_runtime tests passed");
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});

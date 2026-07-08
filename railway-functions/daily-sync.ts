declare const process: {
  env: Record<string, string | undefined>;
};

const DEFAULT_PUBLIC_URL = "https://anime-srez.up.railway.app";

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function timestamp(): string {
  return new Date().toISOString();
}

function logEvent(event: Record<string, unknown>, error = false): void {
  const line = JSON.stringify({ timestamp: timestamp(), ...event });
  if (error) {
    console.error(line);
  } else {
    console.log(line);
  }
}

async function responsePayload(response: Response): Promise<unknown> {
  const body = await response.text();
  if (!body) {
    return null;
  }
  try {
    return JSON.parse(body);
  } catch {
    return { body: body.slice(0, 1000) };
  }
}

const mode = process.env.ANIME_SYNC_MODE || "daily";
const publicUrl = trimTrailingSlash(process.env.ANIME_PUBLIC_URL || DEFAULT_PUBLIC_URL);
const syncUrl = process.env.ANIME_SYNC_URL || `${publicUrl}/api/internal/daily-sync`;
const token = process.env.ANIME_SYNC_TOKEN;
const timeoutSeconds = Number.parseInt(process.env.ANIME_SYNC_TIMEOUT_SECONDS || "1800", 10);

if (!token) {
  logEvent({ event: "daily_sync_error", mode, url: syncUrl, error: "ANIME_SYNC_TOKEN is required" }, true);
  throw new Error("ANIME_SYNC_TOKEN is required");
}

const started = performance.now();
logEvent({ event: "daily_sync_start", mode, url: syncUrl, timeout_seconds: timeoutSeconds });

const controller = new AbortController();
const timeoutId = setTimeout(() => controller.abort(), Math.max(1, timeoutSeconds) * 1000);
try {
  const response = await fetch(`${syncUrl}?mode=${encodeURIComponent(mode)}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
    },
    signal: controller.signal,
  });
  const payload = await responsePayload(response);
  const durationMs = Math.max(0, Math.round(performance.now() - started));

  if (!response.ok) {
    logEvent(
      {
        event: "daily_sync_http_error",
        mode,
        url: syncUrl,
        status: response.status,
        duration_ms: durationMs,
        response: payload,
      },
      true,
    );
    throw new Error(`Daily sync failed with HTTP ${response.status}`);
  }

  logEvent({
    event: "daily_sync_finish",
    mode,
    url: syncUrl,
    status: response.status,
    duration_ms: durationMs,
    response: payload,
  });
} catch (error) {
  const durationMs = Math.max(0, Math.round(performance.now() - started));
  const message = error instanceof Error ? error.message : String(error);
  logEvent({ event: "daily_sync_error", mode, url: syncUrl, error: message, duration_ms: durationMs }, true);
  throw error;
} finally {
  clearTimeout(timeoutId);
}

export {};

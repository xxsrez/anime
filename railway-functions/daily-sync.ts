declare const process: {
  env: Record<string, string | undefined>;
};

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function configuredSyncUrl(): string {
  const explicit = process.env.ANIME_SYNC_URL?.trim();
  const publicUrl = process.env.ANIME_PUBLIC_URL?.trim();
  const raw = explicit || (publicUrl ? `${trimTrailingSlash(publicUrl)}/api/internal/daily-sync` : "");
  if (!raw) {
    throw new Error("ANIME_SYNC_URL or ANIME_PUBLIC_URL is required");
  }
  const parsed = new URL(raw);
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && local)) {
    throw new Error("Daily sync URL must use HTTPS (HTTP is allowed only for localhost)");
  }
  if (parsed.username || parsed.password) {
    throw new Error("Daily sync URL must not contain credentials");
  }
  return parsed.toString();
}

function containsFailure(value: unknown, depth = 0): boolean {
  if (!value || typeof value !== "object" || depth > 12) {
    return depth > 12;
  }
  const record = value as Record<string, unknown>;
  if (record.status === "partial" || record.status === "failed" || record.error) {
    return true;
  }
  if (Object.hasOwn(record, "failed")) {
    const failed = Number(record.failed);
    if (!Number.isFinite(failed) || failed > 0) {
      return true;
    }
  }
  return Object.values(record).some((child) => containsFailure(child, depth + 1));
}

function payloadSucceeded(payload: unknown): boolean {
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const record = payload as Record<string, unknown>;
  return record.ok === true && !containsFailure(record);
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
if (!["hourly", "daily", "full"].includes(mode)) {
  throw new Error(`Unsupported ANIME_SYNC_MODE: ${mode}`);
}
const syncUrl = configuredSyncUrl();
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
  const targetUrl = new URL(syncUrl);
  targetUrl.searchParams.set("mode", mode);
  const response = await fetch(targetUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
    },
    signal: controller.signal,
  });
  const payload = await responsePayload(response);
  const durationMs = Math.max(0, Math.round(performance.now() - started));

  if (!response.ok || !payloadSucceeded(payload)) {
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
    throw new Error(
      response.ok
        ? "Daily sync response did not confirm a complete success"
        : `Daily sync failed with HTTP ${response.status}`,
    );
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

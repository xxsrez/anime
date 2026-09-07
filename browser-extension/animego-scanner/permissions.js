export const ANIMEGO_HOST_PERMISSION = "https://animego.me/*";

export function resolveExtensionApi(scope = globalThis) {
  return scope?.browser ?? scope?.chrome ?? null;
}

export async function hasAnimeGoHostPermission(api = resolveExtensionApi()) {
  if (!api?.permissions?.contains) {
    return false;
  }
  return Boolean(
    await api.permissions.contains({
      origins: [ANIMEGO_HOST_PERMISSION],
    }),
  );
}

export async function requestAnimeGoHostPermission(api = resolveExtensionApi()) {
  if (!api?.permissions?.request) {
    return false;
  }
  return Boolean(
    await api.permissions.request({
      origins: [ANIMEGO_HOST_PERMISSION],
    }),
  );
}

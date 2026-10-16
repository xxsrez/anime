export function upstreamHttpError(path, status) {
  const url = new URL(path, "https://animego.me");
  const stage = url.pathname.startsWith("/player/videos/")
    ? "Плеер серии"
    : url.pathname.startsWith("/player/") ? "Плеер тайтла" : "Карточка тайтла";
  return status === 404
    ? `${stage} отсутствует на AnimeGO (HTTP 404): ${url.href}. Проверка не завершена; это не означает отсутствие новых серий.`
    : `${stage}: AnimeGO ответил HTTP ${status}: ${url.href}.`;
}

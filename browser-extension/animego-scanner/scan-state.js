export function shouldRestartAfterReload(runGeneration, currentGeneration, status) {
  return runGeneration !== currentGeneration && status === "running";
}

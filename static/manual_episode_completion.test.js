const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync(`${__dirname}/app.js`, "utf8");
const source = app.slice(app.indexOf("let manualCompletionTarget ="), app.indexOf("function uniqueTranslations("));
let release;
const sent = [];
const messages = [];
const detail = {id: 1, episodes: []};
const context = vm.createContext({
  state: {detail, selectedEpisodeId: 11},
  document: {getElementById: () => null},
  registerUserStateMutation() {}, discardWatchSession() {}, clearPlayer() {},
  applyLoadedSearchFields() {}, applyFilter() {}, renderWatchState() {}, renderEpisodes() {},
  showAppStatus: message => messages.push(message),
  api: async (path, options) => {
    if (options) {
      sent.push(JSON.parse(options.body));
      await new Promise(resolve => { release = resolve; });
      return {operation_id: "test-operation", undone: false};
    }
    if (path === "/api/anime") return {items: [detail]};
    if (path === "/api/continue-watching") return {item: {episode_number: "2"}};
    return detail;
  },
});
vm.runInContext(source, context);
(async () => {
  vm.runInContext("manualCompletionTarget = {anime_id: 1, episode_id: 10}; closeStaleManualCompletion()", context);
  assert.equal(vm.runInContext("manualCompletionTarget", context), null, "episode changes invalidate confirmation");
  const request = {anime_id: 1, episode_id: 11, operation_id: "test-operation"};
  const first = context.submitManualCompletion(request);
  await context.submitManualCompletion(request);
  assert.equal(sent.length, 1, "double click sends one mutation");
  release();
  await first;
  assert.equal(context.state.anime[0], detail, "catalog response uses items");
  assert.equal(context.state.continueWatching.episode_number, "2");
  assert.equal(context.state.selectedEpisodeId, 11, "completion never selects next episode");
  assert.equal(messages.at(-1), "Серия отмечена просмотренной");
  assert.equal(vm.runInContext("manualCompletionBusy", context), false);
  console.log("Manual episode completion UI checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

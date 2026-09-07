# AnimeGo Scanner extension

Cross-browser Manifest V3 extension used by Anime Catalog to fetch AnimeGo
player metadata through the user's Chrome or Safari connection. Safari 16.4+
is the declared minimum because the background service worker is an ES module.

## Local installation

Use the authenticated `/scanner-setup` page for browser-specific instructions.

- Chrome: open `chrome://extensions`, enable Developer mode, choose **Load
  unpacked**, and select this directory.
- Safari 26: enable web-developer features, then use Safari **Settings →
  Developer → Add Temporary Extension…** and select this directory or the ZIP.
  Safari removes a temporary extension after 24 hours or when Safari quits.

Reload Anime Catalog after installation. Safari also requires website access
for the catalog origin. On the first Scan in either browser, the scanner asks
for optional access only to `https://animego.me/*`; no server job is created
until the user grants it.

The extension sends only episodes that are not yet playable in the catalog,
together with playable HTTPS providers. The server preserves existing metadata
and may fill a previously null player URL.

## Recovery behavior

- Progress is checkpointed in WebExtension local storage after every title.
- Collected results are also saved before delivery. If delivery fails, Resume resends the saved result, including after a scanner reload, without scraping the title again.
- A failed Stop can be retried; reloading during Stop restores a controllable job. The server rejects normal completion while titles remain pending.
- Pause keeps the server job active and resume continues at the current title.
- Closing the scanner tab does not discard the checkpoint. Click the extension action, or ask Anime Catalog to reopen the scanner, to resume.
- Stop reports a terminal `stopped` result to the API and releases the scan lease.
- HTTP 403/429 and CAPTCHA-like pages halt the scan without completing the server job. After the upstream check clears, use **Продолжить**; use **Остановить** to release the job instead.

## Tests

```sh
npm test
```

The unit suite covers parser/state behavior, manifest permission boundaries,
and the Safari `browser.*` runtime bridge. A real Safari smoke is still required
for website permissions, cross-origin AnimeGo fetches, and cookies.

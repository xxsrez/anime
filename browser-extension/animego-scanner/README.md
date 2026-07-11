# AnimeGo Scanner extension

Chrome Manifest V3 extension used by Anime Catalog to fetch AnimeGo player metadata through the user's browser.

## Local installation

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open Anime Catalog at `http://127.0.0.1:8765/` and use the Scan menu.

The extension only receives host permissions for AnimeGo, the local development
origin, and the production Anime Catalog origin. It sends only episodes that
are not yet playable in the catalog, together with playable HTTPS providers;
the server preserves existing metadata and may fill a previously null player
URL.

## Recovery behavior

- Progress is checkpointed in `chrome.storage.local` after every title.
- Pause keeps the server job active and resume continues at the current title.
- Closing the scanner tab does not discard the checkpoint. Click the extension action, or ask Anime Catalog to reopen the scanner, to resume.
- Stop reports a terminal `stopped` result to the API and releases the scan lease.
- HTTP 403/429 and CAPTCHA-like pages halt the scan without completing the server job. After the upstream check clears, use **Продолжить**; use **Остановить** to release the job instead.

## Tests

```sh
npm test
```

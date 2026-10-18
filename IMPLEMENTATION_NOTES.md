# AuraDisc Pro: reliability and retro UI pass

Updated 2026-10-06. Existing uncommitted Coverflow/artwork work was preserved.
The historical standalone mockup and the real music library are not app fixtures.

## Implemented

- Cream/olive receiver styling, Comfortaa text, tactile transport, static paper
  texture, and a responsive two-line mobile vault. Retains Coverflow and
  fullscreen-only lyrics. Removes the continuous WebGL mesh background.
- Escape metadata, playlist labels, artwork attributes, and lyric text before
  HTML rendering; encode apostrophes in filenames used by inline handlers.
  Search/download payloads keep original text, not HTML entities.
- Labels, keyboard-operable sliders and carousel cards, dialog focus trapping,
  Escape dismissal, focus recovery after queue updates, and reduced motion.
- Same-host/same-origin browser checks, cross-site request rejection, JSON type
  validation, a 1 MB request limit, audio-only streaming, and safe playlist paths.
  CLI/n8n calls without Origin and synchronous download response shapes remain
  compatible. This is still a trusted local-machine app, not an authenticated
  public service; do not expose its port to a network.
- Matching fails when all sources fail the title gate. There is no unvalidated
  first-search-result fallback. Fatal private/unavailable errors fail fast.
- One shared download concurrency budget (configured 1–8 workers), bounded
  playlist size, cancellation through nested playlists and subprocess search /
  legacy spotdl, atomic playlist queue capacity checks, and idempotent archive
  writes. Queued cancellation prevents the worker from starting.
- Playlist failures/partial results are failures, not false success. Queue
  errors have details/retry. Cancel/delete/open-playlist requests check HTTP
  results instead of silently pretending success.
- Monotonic SSE event IDs survive ring-buffer trimming. SSE reconnects with
  polling fallback; the page restores jobs from the running server. Progress
  still patches only affected bar/percent nodes, coalesced at 400 ms.
- Playback identity follows filenames across library reorder/refresh. Missing
  current files stop playback; rejected playback surfaces an error.
- Honest MP3/FLAC/resampled-FLAC descriptions and MP3 defaults. Unknown bitrate
  remains unknown. Encoding/resampling cannot recover source quality.
- ALAC is included in library scans; requirements.txt is no longer ignored;
  text line-ending policy is explicit in .gitattributes.

## Verification

```powershell
python -m pytest tests/ -q
python -m py_compile app.py download_song.py tests/preview_server.py
git diff --check
python tests/preview_server.py
```

58 tests pass, including inline JavaScript syntax (when Node is available),
asset locality, escaping helpers, and mocked API/engine regressions. Node was
available for this run. No new dependencies were installed.

The preview serves silent synthetic WAV tracks on http://127.0.0.1:5057 and
replaces provider, artwork, play-count, and library access. Enter `quit` to shut
it down and remove its temporary files. `--clean` omits the HTML-marker title
for screenshots. Only the explicit `?figma=1` preview response injects Figma's
capture script; the production template has local assets only.

Browser checks covered desktop and an actual 390 x 844 emulated viewport:
artwork fallback, apostrophe filenames, plain-text lyrics/metadata, playback,
keyboard volume, fullscreen lyrics, dialog Escape/focus, completion/failure/
retry/cancellation, reload recovery, playlist selection, and playing-track
identity after a new download reorders the vault. Mobile vault, playlist rows,
and queue cards fit without horizontal overflow. Provider calls and downloads
were synthetic; no real songs were downloaded or deleted in this pass.

Two abandoned fixture folders from forced server restarts remain under
`C:\Users\RJ\AppData\Local\Temp`: `auradisc-preview-2boqlc8f` and
`auradisc-preview-2fsddpcx`. Their contents were verified as synthetic silent
WAV files only; cleanup was rejected by execution policy. Later previews use
graceful shutdown and clean up their own temporary folder.

## Figma reference and remaining limits

[Retro reference capture](https://www.figma.com/design/D5IdcpVJXsR1AUwuW5BPTM?node-id=2-2)
contains editable captured text and frames (204 frames, 101 text nodes inspected;
only discrete record artwork is raster). Figma's Starter-plan tool limit stopped
the component/token reconstruction and visual QA. This is a reference capture,
not a finished component library, and predates the final minor app refinements.

- Jobs are in memory: page reload recovery works while the server is alive.
  A server restart loses jobs; active clients mark expired/missing jobs failed.
- Cancellation is cooperative during in-process extraction, metadata requests,
  tagging, and ffmpeg processing. An already-running operation may finish before
  the next cancellation check; there is no rollback of downloaded files.
- Real YouTube/Spotify downloads, credentials/cookies fallback, live provider
  lyrics, ffmpeg conversion, n8n workflows, and Windows launcher/folder actions
  have not been exercised by this isolated verification.
- Previously documented Spotify scrape fragility, transliteration quality,
  provider matching limits, and single-element fade remain unchanged.

Do not treat this scoped audit as proof that every possible defect is removed.

## Startup navigation fix (2026-10-08)

The cross-site request guard also rejected the player document when Chrome
opened it from another site/tab. Allow GET/HEAD navigation to the index only
when Fetch Metadata identifies a top-level document navigation. Host and Origin
checks remain enforced; cross-site API calls, non-navigation fetches, iframe
loads, and POST requests remain blocked. Regression tests reproduce the former
403 and cover the narrow exception. Restart the Flask server to load this fix
(`debug=False` means code changes are not automatically reloaded).

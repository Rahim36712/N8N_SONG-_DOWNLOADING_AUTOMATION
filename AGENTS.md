# AGENTS.md

Windows-only local music downloader: Flask API (`app.py`) + yt-dlp engine (`download_song.py`) + one monolithic frontend (`templates/index.html`, no build step). Owner machine: user `RJ`.

## Commands

```powershell
python app.py                                  # start server -> http://127.0.0.1:5000
python download_song.py "<query or url>"       # run engine standalone; prints JSON result (use to test engine without Flask)
pip install -r requirements.txt                # flask, yt-dlp, spotdl, syncedlyrics, requests, beautifulsoup4, mutagen, spotipy
powershell -ExecutionPolicy Bypass -File setup.ps1   # full env setup (admin): deps + ffmpeg via winget + n8n
```

- Tests: `python -m pytest tests/ -q` covers the lyric matcher (title parsing, candidate scoring gates, provenance index) — pure logic, no network. No linters or typecheckers otherwise. Verify changes by: `python -m py_compile app.py download_song.py`, then starting the server and hitting endpoints (`Invoke-RestMethod -Method Post http://127.0.0.1:5000/api/download -Body '{"query":"test song"}' -ContentType application/json`).
- Shell is PowerShell 5.1 (no `&&`; use `if ($?)`; no heredocs — write temp `.py` files instead). Project path contains spaces — always quote.
- Commit style (existing history): `feat:` / `fix:` + concise description.

## Architecture facts

- Flow: browser/n8n → Flask routes in `app.py` → functions in `download_song.py` → `yt_dlp` **Python API** (in-process). No more subprocess stdout-scraping for downloads.
- Downloads are async by default from the UI: `POST /api/download` with `"mode":"async"` returns `{job_id}` (202); jobs run in `JOB_POOL`, progress flows through yt-dlp progress hooks → `JOBS` dict → `GET /api/jobs/<id>` polling or `/api/jobs/<id>/events` SSE. Without `mode:"async"` the request blocks and returns the result directly — this sync shape is what n8n workflows depend on; do not change it.
- Smart source selection (`search_best_candidate`): fetches `ytsearch8 --dump-json` flat, scores candidates (penalize live/remix/sped-up/etc., boost `- Topic`/VEVO/Official, duration ±10% when expected duration known, view-count tiebreak), downloads best URL.
- Robustness ladder in `download_single`: player-client cycle `android,web → tv → mweb → ios` with exponential backoff on transient errors; fatal errors (private/unavailable) fail fast without cycling; optional final `cookiesfrombrowser` attempt when `config.json` sets `cookies_browser`.
- Spotify pipeline (`handle_spotify`): metadata via spotipy (env creds or legacy `~/.spotdl/config.json`) with hard 25s timeout + session blacklist on rate-limit, falling back to no-auth scrape of `open.spotify.com/embed/<kind>/<id>` `__NEXT_DATA__` JSON. Files renamed to `<artist> - <title>`, tagged with mutagen (iTunes Search gap-fill), and BOTH `spotify <id>` and `youtube <video_id>` keys written to the shared archive. `_handle_spotify_legacy` (spotdl snapshot-diff) remains only as last-resort fallback.
- Dedupe archive `<music-adjacent>/.downloaded.txt` holds both `youtube <id>` and `spotify <id>` line keys; empty filepath + success means "already archived" → status `"skipped"`.
- Library listing goes through SQLite (`get_music_library_indexed` → `sync_library_db`): DB at `~/Music/library.db` stores play counts, durations, search history; filesystem stays source of truth; response shape = old shape + additive fields.
- Lyrics: sidecar `.lrc`/`.txt` per track stem in `<music>/.lyrics/`, language variants `<stem>.<lang>.lrc` (urdu/roman/english). Engine v2 in `fetch_lyrics_multi_provider` is correctness-first: synced LRC only; every candidate hard-gated on duration (±12s vs the real audio file via mutagen, or provider-reported duration) and title/artist token overlap; LRCLIB search picks the BEST-scored hit, never the first; channel/uploader name is only used as artist fallback when the filename has none; Genius provider dropped. No confident match → no sidecar (engine returns error) — intentional anti-wrong-lyrics behavior, don't loosen it. Rebuild-all = broom button in the Lyrics header or `POST /api/lyrics/refresh` with `{"purge_all": true}` (runs wipe+refetch in a background thread). Per-track retry = refresh icon (`/api/lyrics?refresh=true`, now also deletes `.en.lrc`/`.english.lrc`). Provenance/negative cache at `<music>/.lyrics/.index.json`: per-stem source + fetched_at; `no_match` results skip the network for 7 days; per-track refresh clears the entry, `purge_all` wipes the file. C4 generators: `.roman.lrc` offline word+char transliteration; `.english.lrc` via Google gtx (on-demand only).
- Player extras: keyboard Space=play/pause, ←/→ seek ±10s, ↑/↓ volume, N/P track switch, F fullscreen/Esc close; Media Session API wired (Windows media keys + lock-screen metadata, artwork from the DiscEngine label canvas via `getLabelDataURL()`); shuffle/repeat modes live in `App.shuffle` / `App.repeatMode`.
- Playlists: JSON sidecars in `<music>/.playlists/<name>.json`; M3U export at `/api/playlists/<name>/export.m3u` uses absolute paths + EXTINF durations from the DB.
- Config: `config.json` (gitignored) overrides defaults in `load_config()` — paths, `max_workers`, `auto_tag`/`auto_lyrics`, `loudnorm`, `cookies_browser`, `spotify_watch_url`. Ship changes to `config.example.json`.
- Frontend is vanilla HTML/CSS/JS in a single large `templates/index.html`; `app.py` sets `TEMPLATES_AUTO_RELOAD` so edits show on refresh. Download queue UI consumes SSE; cancel via `/api/jobs/<id>/cancel`.
- Frontend stack: Tailwind **Play CDN** (`tailwind.config` inline in `<head>` defines all theme tokens) + FontAwesome CDN + Google Fonts; custom CSS in one `<style>` block. Theme colors/fonts are centralized in those two spots — change tokens there, not scattered inline styles. Karaoke lyric styling lives in the `.lyric-row` / `.lyric-u` / `.lyric-e` CSS classes (shared by panel and fullscreen via `buildLyricRows()`); progress fill in `.scrub-fill`.
- Page layout is a single responsive column: hero command bar → turntable stage (disc → transport → scrubber → track meta) → full-width Lyrics section (`#karaoke-panel`, Spotify-style big bold left-aligned lines) → Vault & Queue. There is no right sidebar anymore — don't reintroduce one for lyrics.

## Frontend design system (user-mandated, applied 2026-08)

The frontend ships this design — keep it intact in any UI work:

- Font: **Comfortaa for the entire app** (tailwind `fontFamily.sans`); **Comfortaa Bold** for subtitles/karaoke lyric lines (`.lyric-active-e`, fs active lines use `font-bold`). Comfortaa has no Arabic-script glyphs — Urdu lines keep the `font-urdu` class (Noto Nastaliq Urdu) or they render tofu.
- Palette: **white base + olive green accents** (`app.bg #f7f8f2`, `app.olive #6b8e23`, `app.oliveDeep`, `app.oliveSoft`, `app.ink`). Do not reintroduce dark/crimson tokens.
- 3D stack (all client-side, no build step): Three.js r128 UMD from cdnjs renders the turntable (`DiscEngine` script block at end of body — **dark olive-vinyl** shader disc with micro-grooves and a slow sage sheen (user rejected rainbow/iridescent — don't bring it back), procedural CanvasTexture hub label, spin easing, mouse-parallax tilt, track-change glide); Web Audio `AnalyserNode` drives the orbital particle ring (context created lazily on user gesture via `DiscEngine.unlockAudio()`). CSS fallback disc shows if WebGL/THREE is unavailable.
- Library items carry no artwork URL — the disc label is drawn procedurally from title/artist; don't expect a thumbnail field.
- `01-AuraDisc Pro - Player.html` at repo root is an untracked static mockup kept as historical design reference only — it is not app code; don't wire it up or commit it as part of the app.
- n8n: `n8n_workflow.json` (form → batch API) and `n8n_watch_workflow.json` (cron → `/api/watch/sync`). Keep `/api/*` request/response shapes backward compatible.

## Data locations (outside repo, do not commit)

- Music library: `~/Music/AutoDownloads`
- Dedupe archive: `~/Music/.downloaded.txt`
- CSV log: `~/Music/download_log.csv` (writes serialized by `_LOG_LOCK`)
- SQLite index: `~/Music/library.db`
- `.gitignore` blocks `*.csv`, `*.txt`, `config.json` globally — don't add tracked fixtures without an exception.

## Known limitations (not bugs)

- Spotify no-auth embed scrape depends on Spotify's `__NEXT_DATA__` page structure; if it breaks, set real credentials in env (`SPOTIPY_CLIENT_ID/SECRET`) or `~/.spotdl/config.json`. Shared spotdl demo creds are often rate-limited (429) — handled by fallback.
- Romanization is dictionary+char-map based (no vowel restoration); quality is decent for common words only.
- `yt-dlp` breaks frequently against YouTube changes; diagnostics surface an outdated hint (`ytdlp_hint`) when PyPI version is newer — advise `yt-dlp -U` before debugging extraction errors.
- Crossfade is single-element auto-fade (fade-out last N seconds), not true dual-element overlap.

## Implementation status (2026-08)

All roadmap items are implemented: A1 bug fixes (path traversal, concurrent batch, CSV lock, timeouts, generic lyric validation), A2 retries/client-cycling/update-hint, A3 smart search, B1 async job queue + SSE, B2 yt_dlp Python API, B3 unified Spotify pipeline, C1 mutagen tagging, C2 SQLite index, C3 playlists/M3U/fade, C4 lyric sidecars, C5 watch/sync + cron workflow, C6 config.json.

Frontend was rebuilt around the AuraDisc Pro player design (streaming, karaoke dual-language lyrics, playlists UI), then reskinned to the Comfortaa/white-olive/3D design system above (Three.js turntable + Web Audio particles live in the `DiscEngine` block at the end of `templates/index.html`).

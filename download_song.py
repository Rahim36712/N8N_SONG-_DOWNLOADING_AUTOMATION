#!/usr/bin/env python3
"""
Song Downloader v3.5 - yt-dlp + spotdl powered
Supports: song names, YouTube URLs, YouTube playlists, Spotify URLs
Best audio quality | Duplicate detection | CSV logging | Library & Diagnostics
"""

import sys
import subprocess
import os
import json
import csv
import tempfile
import traceback
import uuid
import re
import concurrent.futures
import urllib.request
import urllib.parse
import requests
import syncedlyrics
from bs4 import BeautifulSoup
from datetime import datetime

# Centralized yt-dlp arguments to eliminate HTTP 403 Forbidden and EJS extraction warnings
YTDLP_BASE_ARGS = [
    "--js-runtimes", "node",
    "--extractor-args", "youtube:player_client=android,web",
]

SPOTDL_YTDLP_ARGS = "--js-runtimes node --extractor-args youtube:player_client=android,web"


def get_dirs():
    home = os.path.expanduser("~")
    lyrics_dir = os.path.join(home, "Music", "AutoDownloads", ".lyrics")
    os.makedirs(lyrics_dir, exist_ok=True)
    return {
        "music": os.path.join(home, "Music", "AutoDownloads"),
        "archive": os.path.join(home, "Music", ".downloaded.txt"),
        "log": os.path.join(home, "Music", "download_log.csv"),
        "lyrics": lyrics_dir,
    }


def is_lrc_synced(text):
    if not text:
        return False
    return bool(re.search(r"\[\d{1,2}:\d{2}", str(text)))


def _safe_stem(filename):
    """Sanitize stem by replacing Windows invalid filesystem characters (< > : " / \ | ? *) with underscores."""
    if not filename:
        return ""
    safe_filename = os.path.basename(filename)
    stem = os.path.splitext(safe_filename)[0]
    clean_stem = re.sub(r'[<>:"/\\|?*]', '_', stem).strip()
    return clean_stem


def save_sidecar_lyrics(filename, lyrics_text):
    """Save sidecar lyrics file with UTF-8 encoding into .lyrics/ directory."""
    if not filename or lyrics_text is None:
        return None
    stem = _safe_stem(filename)
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]
    os.makedirs(lyrics_dir, exist_ok=True)
    filepath = os.path.join(lyrics_dir, f"{stem}.lrc")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(lyrics_text)
    return filepath


def get_sidecar_lyrics(filename, lang=None):
    """Retrieve sidecar lyrics from .lyrics/<stem>.<lang>.lrc, .lyrics/<stem>.lrc or .lyrics/<stem>.txt with UTF-8 encoding."""
    if not filename:
        return None
    stem = _safe_stem(filename)
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]

    if lang:
        lang_path = os.path.join(lyrics_dir, f"{stem}.{lang}.lrc")
        if os.path.exists(lang_path):
            with open(lang_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

    lrc_path = os.path.join(lyrics_dir, f"{stem}.lrc")
    txt_path = os.path.join(lyrics_dir, f"{stem}.txt")

    if os.path.exists(lrc_path):
        with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    elif os.path.exists(txt_path):
        with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return None


def extract_artist_and_title(raw_name):
    """Cleanly extract authentic artist and song title from raw audio filenames or queries.
    Strips producer credits, featured artists, duplicate artist prefixes, and video tags.
    """
    if not raw_name:
        return "", ""
    s = os.path.splitext(os.path.basename(raw_name))[0]
    s = s.replace('_', ' ').replace('\uff5c', ' ').replace('|', ' ').replace('~', ' ')
    # Remove bracketed content (e.g. [Official Video], (Prod. by X))
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    s = re.sub(r'\([^\)]*\)', ' ', s)
    
    # Remove common video / promotional noise
    noise = [
        r'\bOfficial\s*Music\s*Video\b', r'\bOfficial\s*Lyric\s*Video\b',
        r'\bOfficial\s*Audio\b', r'\bOfficial\s*Video\b', r'\bOfficial\b',
        r'\bMusic\s*Video\b', r'\bLyric\s*Video\b', r'\bVideo\b', r'\bAudio\b',
        r'\bVisuali[sz]er\b', r'\bLyrics?\b', r'\bFull\s*Song\b',
        r'\bProd(?:\.|uced)?\s*(?:by)?\s*[\w\s]+\b', r'\bft\.?\s*[\w\s]+\b', r'\bfeat\.?\s*[\w\s]+\b',
        r'\bHeartfelt\s*Breakup\s*Song\b', r'\bChillBazaar\b'
    ]
    for n in noise:
        s = re.sub(n, ' ', s, flags=re.IGNORECASE)

    s = re.sub(r'\s*-\s*', ' - ', s)
    raw_parts = [re.sub(r'\s{2,}', ' ', p).strip() for p in s.split(' - ') if p.strip()]

    # Deduplicate repeating artist/title segments
    unique_parts = []
    for p in raw_parts:
        clean_p = p.strip()
        if clean_p and (not unique_parts or clean_p.lower() != unique_parts[-1].lower()):
            unique_parts.append(clean_p)

    if len(unique_parts) >= 2:
        artist = unique_parts[0]
        title = " ".join(unique_parts[1:])
    elif len(unique_parts) == 1:
        artist = ""
        title = unique_parts[0]
    else:
        artist = ""
        title = s

    # Strip leftover trailing punctuation
    artist = re.sub(r'^[^\w]+|[^\w]+$', '', artist).strip()
    title = re.sub(r'^[^\w]+|[^\w]+$', '', title).strip()

    # Deduplicate repeated words inside title
    title = re.sub(r'\b([A-Za-z0-9]+(?:\s+[A-Za-z0-9]+)?)\s+\1\b', r'\1', title, flags=re.IGNORECASE)
    return artist, title


def _clean_search_query(raw):
    """Wrapper that returns combined clean search string."""
    artist, title = extract_artist_and_title(raw)
    if artist and title:
        return f"{artist} {title}".strip()
    return title or artist or raw


def fetch_lyrics_multi_provider(query, title=None, artist=None):
    """High-accuracy authentic lyrics cascade:
    1. Direct LRCLIB exact match (/api/get)
    2. Direct LRCLIB fuzzy search (/api/search)
    3. Musixmatch & NetEase multi-provider (syncedlyrics)
    4. Primary song title fallback
    5. Clean plain lyrics fallback
    """
    clean_artist, clean_title = extract_artist_and_title(query)
    if artist:
        clean_artist = artist
    if title:
        clean_title = title

    queries = []
    if clean_artist and clean_title:
        queries.append((clean_artist, clean_title))
        queries.append(('', f"{clean_artist} {clean_title}"))
    if clean_title:
        # Also try first 2 words of title if multi-word
        t_words = clean_title.split()
        if len(t_words) > 2:
            short_t = " ".join(t_words[:2])
            if clean_artist:
                queries.append((clean_artist, short_t))
                queries.append(('', f"{clean_artist} {short_t}"))
            queries.append(('', short_t))
        queries.append(('', clean_title))

    headers = {"User-Agent": "AutoSongDownloader/2.0"}

    # 1. Try LRCLIB API
    for a_q, t_q in queries:
        if a_q and t_q:
            try:
                r = requests.get("https://lrclib.net/api/get", params={"artist_name": a_q, "track_name": t_q}, timeout=4, headers=headers)
                if r.status_code == 200:
                    d = r.json()
                    synced = d.get("syncedLyrics")
                    if synced and "[" in synced:
                        return {"status": "success", "synced": True, "lyrics": synced, "source": "lrclib_exact"}
            except Exception:
                pass

        search_str = f"{a_q} {t_q}".strip()
        try:
            r = requests.get("https://lrclib.net/api/search", params={"q": search_str}, timeout=4, headers=headers)
            if r.status_code == 200:
                items = r.json()
                if isinstance(items, list) and items:
                    for item in items:
                        synced = item.get("syncedLyrics")
                        if synced and "[" in synced:
                            return {"status": "success", "synced": True, "lyrics": synced, "source": "lrclib_search"}
        except Exception:
            pass

    # 2. Try syncedlyrics multi-provider (Musixmatch, NetEase, Genius, LRCLIB)
    for a_q, t_q in queries:
        search_str = f"{a_q} {t_q}".strip()
        try:
            res = syncedlyrics.search(search_str, providers=['lrclib', 'musixmatch', 'netease', 'genius'])
            if res and "[" in res:
                return {"status": "success", "synced": True, "lyrics": res, "source": "syncedlyrics"}
        except Exception:
            pass

    return {"status": "error", "synced": False, "lyrics": None}


def get_all_sidecar_lyrics(filename):
    """Return dictionary of available language lyrics for a given track stem."""
    if not filename:
        return {}
    stem = _safe_stem(filename)
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]

    base_lrc = get_sidecar_lyrics(filename)
    if not base_lrc:
        res = fetch_lyrics_multi_provider(filename)
        base_lrc = res.get("lyrics") if isinstance(res, dict) else res
        if base_lrc:
            save_sidecar_lyrics(filename, base_lrc)

    if not base_lrc:
        return {}

    result = {}
    # Detect if lyrics are in authentic Urdu script
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", base_lrc):
        result["urdu"] = base_lrc
    else:
        result["roman"] = base_lrc

    # Check if explicit language sidecars exist
    for lang in ["urdu", "roman", "english"]:
        path = os.path.join(lyrics_dir, f"{stem}.{lang}.lrc")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                result[lang] = f.read()

    return result


def auto_fetch_and_save_lyrics(filename, title=None, uploader=None):
    """Auto-fetch and save authentic lyrics sidecar for a downloaded track."""
    try:
        res = fetch_lyrics_multi_provider(filename, title=title, artist=uploader)
        lyrics = res.get("lyrics") if isinstance(res, dict) else res
        if lyrics:
            save_sidecar_lyrics(filename, lyrics)
            return True
    except Exception as e:
        print(f"Error auto-fetching lyrics for {filename}: {e}", file=sys.stderr)
    return False


def purge_and_reindex_lyrics(dirs=None, purge_all=False):
    """Purge orphaned or corrupted lyrics from .lyrics/ and re-generate authentic synced lyrics.
    If purge_all is True: deletes all files in .lyrics/ directory.
    If purge_all is False: deletes orphaned files (e.g. test.lrc, stems not matching audio files,
    corrupted translation sidecars) and reindexes/regenerates authentic sidecars for all existing audio files.
    Returns: {"status": "success", "purged_count": int, "reindexed_count": int}
    """
    if dirs is None:
        dirs = get_dirs()

    lyrics_dir = dirs.get("lyrics")
    music_dir = dirs.get("music")
    if not lyrics_dir or not os.path.exists(lyrics_dir):
        if lyrics_dir:
            os.makedirs(lyrics_dir, exist_ok=True)
        return {"status": "success", "purged_count": 0, "reindexed_count": 0}

    purged_count = 0
    reindexed_count = 0

    # Get list of existing media files in music_dir
    valid_exts = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".aac", ".webm", ".ogg"}
    audio_files = []
    valid_stems = set()
    if music_dir and os.path.exists(music_dir):
        for f in os.listdir(music_dir):
            stem, ext = os.path.splitext(f)
            if ext.lower() in valid_exts:
                audio_files.append(f)
                valid_stems.add(stem)

    # 1. Purge
    for fname in list(os.listdir(lyrics_dir)):
        fpath = os.path.join(lyrics_dir, fname)
        if not os.path.isfile(fpath):
            continue

        should_purge = False
        if purge_all:
            should_purge = True
        else:
            # Check test dummy files
            if fname.lower().startswith("test.") or fname.lower() == "test.lrc":
                should_purge = True
            else:
                # Extract base stem without .ur.lrc, .urdu.lrc, .roman.lrc, .en.lrc, .english.lrc, .lrc, .txt
                clean_name = fname
                for suffix in [".ur.lrc", ".urdu.lrc", ".roman.lrc", ".en.lrc", ".english.lrc", ".lrc", ".txt"]:
                    if clean_name.lower().endswith(suffix):
                        clean_name = clean_name[:-len(suffix)]
                        break
                else:
                    clean_name = os.path.splitext(clean_name)[0]

                if clean_name not in valid_stems:
                    should_purge = True
                elif fname.endswith(".ur.lrc") or fname.endswith(".urdu.lrc"):
                    # Check if urdu sidecar contains corrupted content (e.g. contains Bikhra text for non-Bikhra track)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                        if "دل پہ لکھا جو تُو پڑھ نہ سکے" in content and "bikhra" not in fname.lower():
                            should_purge = True
                    except Exception:
                        should_purge = True

        if should_purge:
            try:
                os.remove(fpath)
                purged_count += 1
            except Exception as e:
                print(f"Error removing cached file {fpath}: {e}", file=sys.stderr)

    # 2. Re-generate / Reindex sidecars for existing audio files
    for af in audio_files:
        try:
            sidecars = get_all_sidecar_lyrics(af)
            if sidecars:
                reindexed_count += 1
        except Exception as e:
            print(f"Error reindexing lyrics for {af}: {e}", file=sys.stderr)

    return {
        "status": "success",
        "purged_count": purged_count,
        "reindexed_count": reindexed_count
    }


def is_yt_playlist(q):
    return "youtube.com/playlist" in q or ("youtube.com" in q and "list=" in q) or ("youtu.be/" in q and "list=" in q)


def is_spotify(q):
    return "open.spotify.com/" in q


def is_url(q):
    return q.startswith("http://") or q.startswith("https://")


def log_download(log_file, data):
    try:
        exists = os.path.exists(log_file)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        with open(log_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp", "status", "query", "title",
                             "artist", "url", "filepath", "size_mb"])
            w.writerow([
                data.get("timestamp", datetime.now().isoformat()),
                data.get("status", ""),
                data.get("query", ""),
                data.get("title", ""),
                data.get("uploader", ""),
                data.get("url", ""),
                data.get("filepath", ""),
                data.get("file_size_mb", "")
            ])
    except Exception as e:
        print(f"Error logging download: {e}", file=sys.stderr)


def download_single(query, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Download a single song by name or URL with anti-403 configuration and multi-tier search fallback."""
    now = datetime.now().isoformat()
    tpl = os.path.join(output_dir, "%(uploader)s - %(title)s.%(ext)s")

    def _run_ytdlp(search_target):
        cmd = [
            "yt-dlp",
            *YTDLP_BASE_ARGS,
            "--format", "bestaudio/best",
            "-x",
            "--audio-format", audio_format,
            "--audio-quality", audio_quality,
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
            "--embed-metadata",
            "--add-metadata",
            "--output", tpl,
            "--no-playlist",
            "--download-archive", dirs["archive"],
            "--print", "after_move:filepath",
            "--print", "before_dl:%(title)s|||%(uploader)s|||%(webpage_url)s",
            search_target
        ]
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    is_raw_url = is_url(query)
    targets = [query] if is_raw_url else [f"ytsearch1:{query} official audio", f"ytsearch1:{query}"]

    last_r = None
    for target in targets:
        r = _run_ytdlp(target)
        last_r = r

        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
            meta, fpath = "", ""
            for l in lines:
                if "|||" in l:
                    meta = l
                elif l and not l.startswith("|||"):
                    fpath = l

            title, uploader, url = "", "", ""
            if meta:
                p = meta.split("|||")
                title    = p[0] if len(p) > 0 else ""
                uploader = p[1] if len(p) > 1 else ""
                url      = p[2] if len(p) > 2 else ""

            # FIX BUG: If fpath is empty or file doesn't exist, yt-dlp skipped because it's in archive
            if not fpath or not os.path.exists(fpath):
                return {
                    "status": "skipped",
                    "query": query,
                    "title": title or query,
                    "uploader": uploader,
                    "url": url,
                    "message": "Already downloaded (recorded in archive)",
                    "timestamp": now,
                    "engine": "yt-dlp"
                }

            fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
            fname = os.path.basename(fpath)
            try:
                auto_fetch_and_save_lyrics(fname, title=title, uploader=uploader)
            except Exception:
                pass
            return {
                "status": "success", "query": query, "title": title or fname,
                "uploader": uploader or "Unknown", "url": url, "filepath": fpath,
                "filename": fname,
                "file_size_mb": round(fsize / 1024 / 1024, 2), "timestamp": now,
                "engine": "yt-dlp"
            }
        else:
            if "has already been recorded" in r.stderr or "has already been downloaded" in r.stderr:
                return {"status": "skipped", "query": query,
                        "message": "Already downloaded", "timestamp": now, "engine": "yt-dlp"}

    error_msg = last_r.stderr.strip()[:500] if last_r and last_r.stderr else "Download failed"
    return {"status": "error", "query": query,
            "error": error_msg, "timestamp": now, "engine": "yt-dlp"}


def handle_yt_playlist(playlist_url, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Extract video URLs from a YouTube playlist, then download each."""
    cmd = ["yt-dlp", *YTDLP_BASE_ARGS, "--flat-playlist", "--print", "url", playlist_url]
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if r.returncode != 0:
        return {"status": "error", "query": playlist_url,
                "error": f"Failed to extract playlist: {r.stderr.strip()[:300]}", "engine": "yt-dlp"}

    urls = [l.strip() for l in r.stdout.strip().split("\n")
            if l.strip() and l.strip().startswith("http")]

    if not urls:
        return {"status": "error", "query": playlist_url,
                "error": "No videos found in playlist", "engine": "yt-dlp"}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_url = {executor.submit(download_single, u, output_dir, dirs, audio_format, audio_quality): u for u in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            res = future.result()
            log_download(dirs["log"], res)
            results.append(res)

    ok = sum(1 for x in results if x.get("status") == "success")
    return {"status": "playlist_done", "source": "youtube", "engine": "yt-dlp",
            "total": len(urls), "downloaded": ok, "results": results}


def handle_spotify(spotify_url, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Download tracks from Spotify URL via spotdl v4 direct download with yt-dlp anti-403 args.
    
    We snapshot the output directory before/after to detect new files.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Snapshot existing files before download
    valid_exts = {".mp3", ".flac", ".m4a", ".wav", ".opus", ".aac", ".ogg"}
    before = set()
    for f in os.listdir(output_dir):
        if os.path.splitext(f)[1].lower() in valid_exts:
            before.add(f)

    # Map format names
    fmt_map = {"mp3": "mp3", "flac": "flac", "m4a": "m4a", "wav": "wav", "ogg": "ogg", "opus": "opus"}
    out_fmt = fmt_map.get(audio_format, "mp3")

    cmd = [
        sys.executable, "-m", "spotdl", "download",
        spotify_url,
        "--output", os.path.join(output_dir, "{artists} - {title}.{output-ext}"),
        "--format", out_fmt,
        "--yt-dlp-args", SPOTDL_YTDLP_ARGS,
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "query": spotify_url,
                "error": "spotdl download timed out after 5 minutes", "engine": "spotdl"}

    # Snapshot after download to find new files
    after = set()
    for f in os.listdir(output_dir):
        if os.path.splitext(f)[1].lower() in valid_exts:
            after.add(f)

    new_files = after - before

    if not new_files:
        # Check if spotdl itself reported an error
        stderr_clean = r.stderr.strip() if r.stderr else ""
        # Filter out the pkg_resources deprecation warning noise
        stderr_lines = [l for l in stderr_clean.split("\n")
                        if "pkg_resources" not in l and "UserWarning" not in l
                        and "import pkg_resources" not in l and "RuntimeWarning" not in l
                        and l.strip()]
        stderr_msg = "\n".join(stderr_lines)[:300] if stderr_lines else "No new files downloaded"
        
        if r.returncode != 0:
            return {"status": "error", "query": spotify_url,
                    "error": f"spotdl failed (code {r.returncode}): {stderr_msg}", "engine": "spotdl"}
        else:
            return {"status": "error", "query": spotify_url,
                    "error": f"spotdl completed but no new files found. {stderr_msg}", "engine": "spotdl"}

    # Build results from newly downloaded files
    results = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fname in new_files:
        fpath = os.path.join(output_dir, fname)
        fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
        raw_name = os.path.splitext(fname)[0]

        # Parse artist/title from filename
        title = raw_name
        artist = "Unknown"
        if " - " in raw_name:
            parts = raw_name.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        # Auto-fetch lyrics for each new track
        try:
            auto_fetch_and_save_lyrics(fname, title=title, uploader=artist)
        except Exception:
            pass

        result = {
            "status": "success", "query": spotify_url, "title": title,
            "uploader": artist, "url": spotify_url, "filepath": fpath,
            "filename": fname, "file_size_mb": round(fsize / 1024 / 1024, 2),
            "timestamp": now, "engine": "spotdl"
        }
        log_download(dirs["log"], result)
        results.append(result)

    ok = len(results)
    return {"status": "playlist_done", "source": "spotify", "engine": "spotdl",
            "total": ok, "downloaded": ok, "results": results}


def get_track_preview(query):
    """Fetch quick metadata preview using yt-dlp with anti-403 flags and multi-tier search fallback."""
    is_raw_url = is_url(query)
    targets = [query] if is_raw_url else [f"ytsearch1:{query} official audio", f"ytsearch1:{query}"]

    for search_q in targets:
        cmd = [
            "yt-dlp",
            *YTDLP_BASE_ARGS,
            "--dump-json",
            "--no-playlist",
            search_q
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            if r.returncode == 0 and r.stdout:
                first_line = r.stdout.strip().split("\n")[0]
                data = json.loads(first_line)
                duration_sec = data.get("duration", 0)
                mins, secs = divmod(int(duration_sec or 0), 60)
                return {
                    "status": "success",
                    "title": data.get("title", query),
                    "uploader": data.get("uploader") or data.get("artist") or "Unknown",
                    "thumbnail": data.get("thumbnail", ""),
                    "duration": f"{mins}:{secs:02d}",
                    "url": data.get("webpage_url", query),
                    "views": data.get("view_count", 0),
                    "engine": "yt-dlp"
                }
        except Exception:
            continue

    return {"status": "error", "message": "Could not fetch preview metadata"}


def get_music_library(dirs):
    """List all audio files present in the music directory."""
    music_dir = dirs["music"]
    if not os.path.exists(music_dir):
        return []

    valid_exts = {".mp3", ".flac", ".m4a", ".wav", ".opus", ".aac", ".ogg"}
    library = []

    for filename in os.listdir(music_dir):
        ext = os.path.splitext(filename)[1].lower()
        if ext in valid_exts:
            full_path = os.path.join(music_dir, filename)
            try:
                stat = os.stat(full_path)
                size_mb = round(stat.st_size / (1024 * 1024), 2)
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                # Simple title/artist parser from filename ("Artist - Title.mp3")
                raw_name = os.path.splitext(filename)[0]
                if " - " in raw_name:
                    parts = raw_name.split(" - ", 1)
                    artist = parts[0].strip()
                    title = parts[1].strip()
                else:
                    artist = "Unknown"
                    title = raw_name

                lyrics_dir = dirs.get("lyrics") or os.path.join(dirs["music"], ".lyrics")
                lrc_path = os.path.join(lyrics_dir, f"{raw_name}.lrc")
                txt_path = os.path.join(lyrics_dir, f"{raw_name}.txt")
                has_lyrics = os.path.exists(lrc_path) or os.path.exists(txt_path)

                library.append({
                    "filename": filename,
                    "filepath": full_path,
                    "title": title,
                    "artist": artist,
                    "format": ext.replace(".", "").upper(),
                    "size_mb": size_mb,
                    "date_added": mtime,
                    "has_lyrics": has_lyrics
                })
            except Exception:
                continue

    # Sort library newest first
    library.sort(key=lambda x: x["date_added"], reverse=True)
    return library


def get_system_diagnostics(dirs):
    """Check tool installation statuses and disk stats."""
    def run_ver(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if r.returncode == 0:
                line = r.stdout.strip().split("\n")[0]
                return line[:60]
        except Exception:
            pass
        return "Not Available"

    py_ver = f"Python {sys.version.split()[0]}"
    ytdlp_ver = run_ver(["yt-dlp", "--version"])
    ffmpeg_ver = run_ver(["ffmpeg", "-version"])
    spotdl_ver = run_ver([sys.executable, "-m", "spotdl", "--version"])

    library = get_music_library(dirs)
    total_size_mb = sum(item["size_mb"] for item in library)

    return {
        "python": py_ver,
        "ytdlp": ytdlp_ver,
        "ffmpeg": ffmpeg_ver if "ffmpeg" in ffmpeg_ver.lower() else ("Installed" if ffmpeg_ver != "Not Available" else "Not Installed"),
        "spotdl": spotdl_ver,
        "music_dir": dirs["music"],
        "total_files": len(library),
        "total_size_mb": round(total_size_mb, 2)
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"status": "error",
              "error": "Usage: download_song.py <query_or_url> [output_dir]"}))
        return

    query = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else None
    dirs = get_dirs()

    if out_dir is None:
        out_dir = dirs["music"]
    os.makedirs(out_dir, exist_ok=True)

    if is_yt_playlist(query):
        out = handle_yt_playlist(query, out_dir, dirs)
    elif is_spotify(query):
        out = handle_spotify(query, out_dir, dirs)
    else:
        out = download_single(query, out_dir, dirs)
        log_download(dirs["log"], out)

    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({
            "status": "error",
            "error": traceback.format_exc()
        }))


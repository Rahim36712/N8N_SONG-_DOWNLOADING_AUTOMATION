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
from datetime import datetime


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


def save_sidecar_lyrics(filename, lyrics_text):
    """Save sidecar lyrics file with UTF-8 encoding into .lyrics/ directory."""
    if not filename or lyrics_text is None:
        return None
    safe_filename = os.path.basename(filename)
    stem = os.path.splitext(safe_filename)[0]
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]
    os.makedirs(lyrics_dir, exist_ok=True)
    filepath = os.path.join(lyrics_dir, f"{stem}.lrc")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(lyrics_text)
    return filepath


def get_sidecar_lyrics(filename):
    """Retrieve sidecar lyrics from .lyrics/<stem>.lrc or .lyrics/<stem>.txt with UTF-8 encoding."""
    if not filename:
        return None
    safe_filename = os.path.basename(filename)
    stem = os.path.splitext(safe_filename)[0]
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]

    lrc_path = os.path.join(lyrics_dir, f"{stem}.lrc")
    txt_path = os.path.join(lyrics_dir, f"{stem}.txt")

    if os.path.exists(lrc_path):
        with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    elif os.path.exists(txt_path):
        with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return None


def _clean_search_query(raw):
    """Strip YouTube/Spotify metadata junk from filenames for lyrics search.
    e.g. 'Abdul_Hannan_-_Haaray_Prod_by_Shahmeer_Raza_Khan_Official_Music_Video'
    becomes 'Abdul Hannan Haaray'
    """
    if not raw:
        return ""
    s = raw.replace("_", " ")
    # Remove bracketed/parenthesized content: [Official Video], (Prod. by X), etc.
    s = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", s)
    # Remove common YouTube/Spotify metadata keywords (case-insensitive)
    noise_words = [
        r"\bOfficial\b", r"\bMusic\s*Video\b", r"\bVideo\b", r"\bAudio\b",
        r"\bLyric\s*Video\b", r"\bLyrics?\b", r"\bVisuali[sz]er\b",
        r"\bProd\.?\s*(by)?\b", r"\bft\.?\b", r"\bfeat\.?\b",
        r"\bOfficial\s*Audio\b", r"\bOfficial\s*Video\b",
    ]
    for nw in noise_words:
        s = re.sub(nw, " ", s, flags=re.IGNORECASE)
    # Collapse multiple dashes/spaces
    s = re.sub(r"\s*-\s*-+\s*", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    # Remove trailing dash artifacts
    s = re.sub(r"\s*-\s*$", "", s).strip()
    s = re.sub(r"^\s*-\s*", "", s).strip()
    return s


def fetch_lyrics_multi_provider(query, title=None, artist=None):
    """4-stage cascade lyrics fetcher:
    Stage 1: syncedlyrics.search
    Stage 2: LRCLIB /api/get
    Stage 3: LRCLIB /api/search
    Stage 4: Plain text fallback -> null/empty
    """
    clean_query = os.path.splitext(os.path.basename(query))[0] if query else ""

    if not title and not artist and " - " in clean_query:
        parts = clean_query.split(" - ", 1)
        artist = _clean_search_query(parts[0])
        title = _clean_search_query(parts[1])
    elif not title and not artist:
        # Try splitting on " - " inside underscored names too
        normalized = clean_query.replace("_-_", " - ").replace("_", " ")
        if " - " in normalized:
            parts = normalized.split(" - ", 1)
            artist = _clean_search_query(parts[0])
            title = _clean_search_query(parts[1])

    if title:
        title = _clean_search_query(title)
    if artist:
        artist = _clean_search_query(artist)

    if not title:
        title = _clean_search_query(clean_query)

    search_term = f"{artist} {title}".strip() if artist and title else _clean_search_query(clean_query)

    plain_candidate = None

    # Stage 1: syncedlyrics search
    try:
        import syncedlyrics
        sl_res = syncedlyrics.search(search_term)
        if sl_res and sl_res.strip():
            if is_lrc_synced(sl_res):
                return {"lyrics": sl_res, "synced": True}
            else:
                plain_candidate = sl_res
    except Exception:
        pass

    # Stage 2: LRCLIB /api/get
    if title:
        try:
            import requests
            params = {"track_name": title}
            if artist:
                params["artist_name"] = artist
            r = requests.get("https://lrclib.net/api/get", params=params, timeout=5, headers={"User-Agent": "AutoSongDownloader/1.0"})
            if r.status_code == 200:
                data = r.json()
                synced = data.get("syncedLyrics")
                plain = data.get("plainLyrics")
                if synced and is_lrc_synced(synced):
                    return {"lyrics": synced, "synced": True}
                if plain and not plain_candidate:
                    plain_candidate = plain
        except Exception:
            pass

    # Stage 3: LRCLIB /api/search
    try:
        import requests
        params = {"q": search_term}
        r = requests.get("https://lrclib.net/api/search", params=params, timeout=5, headers={"User-Agent": "AutoSongDownloader/1.0"})
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for item in data:
                    synced = item.get("syncedLyrics")
                    if synced and is_lrc_synced(synced):
                        return {"lyrics": synced, "synced": True}
                    if not plain_candidate and item.get("plainLyrics"):
                        plain_candidate = item.get("plainLyrics")
    except Exception:
        pass

    # Stage 4: Plain text fallback -> null/empty
    if plain_candidate and plain_candidate.strip():
        return {"lyrics": plain_candidate, "synced": False}

    return {"lyrics": None, "synced": False}


def auto_fetch_and_save_lyrics(filename, title=None, uploader=None):
    """Auto-fetch and save lyrics sidecar for a downloaded track."""
    try:
        res = fetch_lyrics_multi_provider(filename, title=title, artist=uploader)
        lyrics = res.get("lyrics") if isinstance(res, dict) else res
        if lyrics:
            save_sidecar_lyrics(filename, lyrics)
            return True
    except Exception as e:
        print(f"Error auto-fetching lyrics for {filename}: {e}", file=sys.stderr)
    return False


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
    """Download a single song by name or URL."""
    search_q = query
    if not is_url(query):
        search_q = f"ytsearch1:{query} official audio"

    tpl = os.path.join(output_dir, "%(uploader)s - %(title)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "--format", "bestaudio/best",
        "-x",
        "--audio-format", audio_format,
        "--audio-quality", audio_quality,
        "--embed-thumbnail",
        "--embed-metadata",
        "--add-metadata",
        "--output", tpl,
        "--no-playlist",
        "--download-archive", dirs["archive"],
        "--print", "after_move:filepath",
        "--print", "before_dl:%(title)s|||%(uploader)s|||%(webpage_url)s",
        search_q
    ]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    now = datetime.now().isoformat()

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
                "timestamp": now
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
            "file_size_mb": round(fsize / 1024 / 1024, 2), "timestamp": now
        }
    else:
        if "has already been recorded" in r.stderr or "has already been downloaded" in r.stderr:
            return {"status": "skipped", "query": query,
                    "message": "Already downloaded", "timestamp": now}
        return {"status": "error", "query": query,
                "error": r.stderr.strip()[:500], "timestamp": now}


def handle_yt_playlist(playlist_url, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Extract video URLs from a YouTube playlist, then download each."""
    r = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--print", "url", playlist_url],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if r.returncode != 0:
        return {"status": "error", "query": playlist_url,
                "error": f"Failed to extract playlist: {r.stderr.strip()[:300]}"}

    urls = [l.strip() for l in r.stdout.strip().split("\n")
            if l.strip() and l.strip().startswith("http")]

    if not urls:
        return {"status": "error", "query": playlist_url,
                "error": "No videos found in playlist"}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_url = {executor.submit(download_single, u, output_dir, dirs, audio_format, audio_quality): u for u in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            res = future.result()
            log_download(dirs["log"], res)
            results.append(res)

    ok = sum(1 for x in results if x["status"] == "success")
    return {"status": "playlist_done", "source": "youtube",
            "total": len(urls), "downloaded": ok, "results": results}


def handle_spotify(spotify_url, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Extract tracks from Spotify URL via spotdl, then download each."""
    tmp = os.path.join(output_dir, f"spotify_{uuid.uuid4().hex[:8]}.spotdl")
    if os.path.exists(tmp):
        try: os.remove(tmp)
        except Exception: pass

    r = subprocess.run(
        [sys.executable, "-m", "spotdl", "save", spotify_url, "--save-file", tmp],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    if not os.path.exists(tmp):
        return {"status": "error", "query": spotify_url,
                "error": f"spotdl save failed. Code: {r.returncode} | stderr: {r.stderr.strip()[:300]}"}

    try:
        with open(tmp, "r", encoding="utf-8") as f:
            tracks = json.load(f)
        os.remove(tmp)
    except Exception as e:
        return {"status": "error", "query": spotify_url,
                "error": f"Failed to parse spotdl output: {str(e)}"}

    if not tracks:
        return {"status": "error", "query": spotify_url,
                "error": "No tracks found in Spotify URL"}

    queries = []
    for t in tracks:
        if isinstance(t, dict):
            artists = ", ".join(t.get("artists", ["Unknown"]))
            name = t.get("name", "Unknown")
            queries.append(f"{artists} {name}")
        elif isinstance(t, str):
            queries.append(t)

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_query = {executor.submit(download_single, q, output_dir, dirs, audio_format, audio_quality): q for q in queries}
        for future in concurrent.futures.as_completed(future_to_query):
            res = future.result()
            log_download(dirs["log"], res)
            results.append(res)

    ok = sum(1 for x in results if x["status"] == "success")
    return {"status": "playlist_done", "source": "spotify",
            "total": len(queries), "downloaded": ok, "results": results}


def get_track_preview(query):
    """Fetch quick metadata preview using yt-dlp."""
    search_q = query if is_url(query) else f"ytsearch1:{query} official audio"
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-playlist",
        search_q
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        if r.returncode == 0 and r.stdout:
            data = json.loads(r.stdout.strip().split("\n")[0])
            duration_sec = data.get("duration", 0)
            mins, secs = divmod(int(duration_sec or 0), 60)
            return {
                "status": "success",
                "title": data.get("title", query),
                "uploader": data.get("uploader") or data.get("artist") or "Unknown",
                "thumbnail": data.get("thumbnail", ""),
                "duration": f"{mins}:{secs:02d}",
                "url": data.get("webpage_url", query),
                "views": data.get("view_count", 0)
            }
    except Exception as e:
        pass
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


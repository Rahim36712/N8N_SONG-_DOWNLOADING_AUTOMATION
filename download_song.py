#!/usr/bin/env python3
"""
Song Downloader v3 - yt-dlp + spotdl powered
Supports: song names, YouTube URLs, YouTube playlists, Spotify URLs
Best audio quality | Duplicate detection | CSV logging
"""

import sys
import subprocess
import os
import json
import csv
import tempfile
import traceback
import concurrent.futures
from datetime import datetime


def get_dirs():
    home = os.path.expanduser("~")
    return {
        "music": os.path.join(home, "Music", "AutoDownloads"),
        "archive": os.path.join(home, "Music", ".downloaded.txt"),
        "log": os.path.join(home, "Music", "download_log.csv"),
    }


def is_yt_playlist(q):
    return "youtube.com/playlist" in q or ("youtube.com" in q and "list=" in q)


def is_spotify(q):
    return "open.spotify.com/" in q


def is_url(q):
    return q.startswith("http://") or q.startswith("https://")


def log_download(log_file, data):
    exists = os.path.exists(log_file)
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "status", "query", "title",
                         "artist", "url", "filepath", "size_mb"])
        w.writerow([
            data.get("timestamp", ""), data.get("status", ""),
            data.get("query", ""), data.get("title", ""),
            data.get("uploader", ""), data.get("url", ""),
            data.get("filepath", ""), data.get("file_size_mb", "")
        ])


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
        "--restrict-filenames",
        "--download-archive", dirs["archive"],
        "--print", "after_move:filepath",
        "--print", "before_dl:%(title)s|||%(uploader)s|||%(webpage_url)s",
        search_q
    ]

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    now = datetime.now().isoformat()

    if r.returncode == 0:
        lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
        meta, fpath = "", ""
        for l in lines:
            if "|||" in l:
                meta = l
            elif l.strip():
                fpath = l

        title, uploader, url = "", "", ""
        if meta:
            p = meta.split("|||")
            title    = p[0] if len(p) > 0 else ""
            uploader = p[1] if len(p) > 1 else ""
            url      = p[2] if len(p) > 2 else ""

        if not fpath and not meta:
            return {"status": "skipped", "query": query,
                    "message": "Already downloaded", "timestamp": now}

        fsize = os.path.getsize(fpath) if fpath and os.path.exists(fpath) else 0
        return {
            "status": "success", "query": query, "title": title,
            "uploader": uploader, "url": url, "filepath": fpath,
            "file_size_mb": round(fsize / 1024 / 1024, 2), "timestamp": now
        }
    else:
        if "has already been recorded" in r.stderr:
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
    # Use concurrent downloading for playlists
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
    # Use spotdl to save track metadata
    tmp = os.path.join(output_dir, "spotify_tracks.spotdl")
    if os.path.exists(tmp):
        os.remove(tmp)

    r = subprocess.run(
        [sys.executable, "-m", "spotdl", "save", spotify_url, "--save-file", tmp],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )

    if not os.path.exists(tmp):
        return {"status": "error", "query": spotify_url,
                "error": f"spotdl failed to create file. "
                         f"Code: {r.returncode} | "
                         f"stdout: {r.stdout.strip()[:300]} | "
                         f"stderr: {r.stderr.strip()[:300]}"}

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
    # Use concurrent downloading for spotify tracks
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_query = {executor.submit(download_single, q, output_dir, dirs, audio_format, audio_quality): q for q in queries}
        for future in concurrent.futures.as_completed(future_to_query):
            res = future.result()
            log_download(dirs["log"], res)
            results.append(res)

    ok = sum(1 for x in results if x["status"] == "success")
    return {"status": "playlist_done", "source": "spotify",
            "total": len(queries), "downloaded": ok, "results": results}


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
        # Always output valid JSON so n8n can read it
        print(json.dumps({
            "status": "error",
            "error": traceback.format_exc()
        }))

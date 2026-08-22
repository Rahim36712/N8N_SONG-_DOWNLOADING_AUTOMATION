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
import time
import threading
import tempfile
import traceback
import uuid
import re
import sqlite3
import concurrent.futures
import urllib.request
import urllib.parse
import requests
import syncedlyrics
import yt_dlp
from bs4 import BeautifulSoup
from datetime import datetime

# Robustness knobs (A1/A2)
SUBPROCESS_TIMEOUT = 300        # hard cap (seconds) for remaining subprocess calls (spotdl, search)
_LOG_LOCK = threading.Lock()    # serializes CSV log writes across worker threads

_CONFIG_CACHE = None


def load_config():
    """C6: project-root config.json merged over defaults. Cached after first call."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = {
        "music_dir": None,          # override: absolute path to music library
        "archive_file": None,       # override: dedupe archive path
        "log_file": None,           # override: CSV log path
        "lyrics_dir": None,         # override: lyrics sidecar dir
        "db_file": None,            # override: SQLite library index path
        "max_workers": 4,
        "default_format": "mp3",
        "default_quality": "0",
        "search_candidates": 8,
        "auto_tag": True,
        "auto_lyrics": True,
        "loudnorm": False,
        "loudnorm_i": -16,
        "cookies_browser": None,    # e.g. "chrome" — A2 final fallback attempt
        "spotify_watch_url": None,  # C5: playlist polled by the n8n watcher
    }
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if v is not None})
    except Exception as e:
        print(f"Config load failed ({e}); using defaults", file=sys.stderr)
    _CONFIG_CACHE = cfg
    return cfg


# A2: player-client cycling for anti-403 recovery; optional cookies-from-browser fallback
PLAYER_CLIENT_CYCLE = ["android,web", "tv", "mweb", "ios"]
MAX_SAME_CLIENT_RETRIES = 2     # exponential-backoff retries per client before cycling

# Legacy spotdl flag string (still used by the legacy spotdl fallback handler)
SPOTDL_YTDLP_ARGS = "--js-runtimes node --extractor-args youtube:player_client=android,web"

# Error classification for retry decisions (A2)
_TRANSIENT_PATTERNS = [
    "timed out", "timeout", "temporary failure", "connection reset", "connection refused",
    "connection aborted", "getaddrinfo", "name or service not known", "http error 5",
    "internal server error", "bad gateway", "service unavailable", "incomplete read",
    "unable to download", "network is unreachable", "precondition check failed",
]
_EXTRACTION_PATTERNS = [
    "403", "forbidden", "sign in to confirm", "not a bot", "player response",
    "unable to extract", "no video formats", "requested format is not available",
    "nsig extraction failed", "solving", "age-restricted", "confirm your age",
]
_FATAL_PATTERNS = [
    "private video", "video is private", "video unavailable", "video is unavailable",
    "video has been removed", "does not exist", "account associated with this video has been terminated",
    "copyright",
]

# A3: smart source selection scoring
BAD_TITLE_KEYWORDS = [
    "live", "remix", "sped up", "sped-up", "slowed", "reverb", "8d", "nightcore",
    "cover", "karaoke", "instrumental", "reaction", "mashup", "full album", "loop",
]


def get_dirs():
    cfg = load_config()
    home = os.path.expanduser("~")
    music = cfg.get("music_dir") or os.path.join(home, "Music", "AutoDownloads")
    lyrics_dir = cfg.get("lyrics_dir") or os.path.join(music, ".lyrics")
    os.makedirs(lyrics_dir, exist_ok=True)
    return {
        "music": music,
        "archive": cfg.get("archive_file") or os.path.join(home, "Music", ".downloaded.txt"),
        "log": cfg.get("log_file") or os.path.join(home, "Music", "download_log.csv"),
        "lyrics": lyrics_dir,
        "db": cfg.get("db_file") or os.path.join(home, "Music", "library.db"),
    }


def is_lrc_synced(text):
    if not text:
        return False
    return bool(re.search(r"\[\d{1,2}:\d{2}", str(text)))


def _safe_stem(filename):
    r"""Sanitize stem by replacing Windows invalid filesystem characters (< > : " / \ | ? *) with underscores."""
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


# ---------------------------------------------------------------------------
# Lyrics provenance / negative cache (<lyrics>/.index.json)
# Records where each sidecar came from and caches "no match" results so the
# provider cascade is not re-hit on every play for tracks without lyrics.
# ---------------------------------------------------------------------------

_PROV_LOCK = threading.Lock()
_PROV_NO_MATCH_TTL_DAYS = 7


def _prov_index_path(dirs=None):
    if dirs is None:
        dirs = get_dirs()
    return os.path.join(dirs["lyrics"], ".index.json")


def _load_prov_index():
    try:
        with open(_prov_index_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_prov_index(index):
    try:
        path = _prov_index_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"Lyrics provenance write failed: {e}", file=sys.stderr)


def record_provenance(filename, status, source=None):
    """Record fetch outcome ('ok' | 'no_match') for a track stem."""
    stem = _safe_stem(filename)
    if not stem:
        return
    with _PROV_LOCK:
        index = _load_prov_index()
        index[stem] = {
            "status": status,
            "source": source,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
        # Bound growth: keep only the most recent 5000 entries
        if len(index) > 5000:
            index = dict(list(index.items())[-5000:])
        _save_prov_index(index)


def clear_provenance(filename):
    """Drop the cached fetch outcome so the next lookup retries providers."""
    stem = _safe_stem(filename)
    if not stem:
        return
    with _PROV_LOCK:
        index = _load_prov_index()
        if index.pop(stem, None) is not None:
            _save_prov_index(index)


def _prov_recent_no_match(stem):
    entry = _load_prov_index().get(stem)
    if not entry or entry.get("status") != "no_match":
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except Exception:
        return False
    age = datetime.now() - fetched
    return 0 <= age.total_seconds() < _PROV_NO_MATCH_TTL_DAYS * 86400


_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".opus", ".wav", ".aac", ".ogg", ".webm", ".wma", ".alac"}


def extract_artist_and_title(raw_name):
    """Cleanly extract authentic artist and song title from raw audio filenames or queries.
    Strips producer credits, featured artists, duplicate artist prefixes, and video tags.
    Only strips a trailing extension when it is a known audio format, so titles
    containing periods (e.g. "Mr. Brightside") survive intact.
    """
    if not raw_name:
        return "", ""
    base = os.path.basename(raw_name)
    stem, ext = os.path.splitext(base)
    if ext.lower() not in _AUDIO_EXTS:
        stem = base  # trailing dot wasn't a file extension — keep the full name
    s = stem.replace('_', ' ').replace('\uff5c', ' ').replace('|', ' ').replace('~', ' ')
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


def _audio_file_duration(filename):
    """Real playback duration (seconds) of a library audio file via mutagen; None if unknown."""
    try:
        import mutagen
        dirs = get_dirs()
        path = os.path.join(dirs["music"], os.path.basename(filename))
        if not os.path.exists(path):
            return None
        m = mutagen.File(path)
        length = getattr(getattr(m, "info", None), "length", None)
        return float(length) if length else None
    except Exception:
        return None


_LRC_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def _lrc_end_seconds(text):
    """Approximate position of the final lyric line (seconds) implied by an LRC file."""
    end = None
    for m in _LRC_TS_RE.finditer(text or ""):
        try:
            t = int(m.group(1)) * 60 + int(m.group(2))
            if m.group(3):
                t += int(m.group(3).ljust(3, "0")[:3]) / 1000.0
            end = t if end is None else max(end, t)
        except ValueError:
            continue
    return end


def _name_tokens(s):
    return set(re.findall(r"[a-z0-9\u0600-\u06FF]+", re.sub(r"&", " ", str(s or "").lower())))


def _token_overlap(a, b):
    """Fraction of the smaller token set shared by both strings (0..1)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# Duration mismatch beyond this means "different recording" (radio edit/live/etc.)
_LYRICS_MAX_DRIFT_SEC = 12


def _score_lrclib_item(item, want_artist, want_title, ref_dur):
    """Score one LRCLIB /api/search candidate. Returns float >= 0, or None to reject.
    Hard gates: synced content present, duration within +-12s of the real audio,
    and enough title/artist token overlap that this can't be a different song."""
    synced = item.get("syncedLyrics") or ""
    if "[" not in synced:
        return None
    try:
        cand_dur = float(item.get("duration")) if item.get("duration") else None
    except (TypeError, ValueError):
        cand_dur = None

    title_sim = _token_overlap(item.get("trackName"), want_title) if want_title else 0.0
    artist_sim = _token_overlap(item.get("artistName"), want_artist) if want_artist else 0.0

    dur_known_both = bool(ref_dur and cand_dur)
    if dur_known_both and abs(ref_dur - cand_dur) > _LYRICS_MAX_DRIFT_SEC:
        return None
    if want_title and title_sim < 0.5 and artist_sim < 0.6:
        return None
    if not want_title and not dur_known_both and title_sim == 0.0:
        return None

    score = 3.0 * title_sim + 1.5 * artist_sim
    if dur_known_both:
        score += max(0.0, 2.0 - abs(ref_dur - cand_dur) / 6.0)
    return score


def fetch_lyrics_multi_provider(query, title=None, artist=None, duration=None):
    """Lyrics engine v2 — correctness-first cascade. Never returns unverified lyrics.

    Validation rules for every candidate:
      * timestamped (synced) LRC content only — karaoke requires timing,
      * duration cross-checked against the real audio file (mutagen) or the
        provider-reported duration, rejecting mismatches > +-12s,
      * fuzzy searches pick the BEST-scored candidate, never the first hit.

    Cascade:
      1. LRCLIB /api/get exact (artist_name + track_name)
      2. LRCLIB /api/search, scored best-of ('artist title')
      3. LRCLIB /api/search, scored best-of ('title' only)
      4. syncedlyrics multi-provider (lrclib/musixmatch/netease; Genius dropped)
    """
    clean_artist, clean_title = extract_artist_and_title(query)
    if artist:
        clean_artist = artist
    if title:
        clean_title = title
    if duration is None:
        duration = _audio_file_duration(query)

    headers = {"User-Agent": "AutoSongDownloader/2.0"}

    def _accept(text, source):
        if not text or "[" not in text:
            return None
        lrc_end = _lrc_end_seconds(text)
        if duration and lrc_end and abs(duration - lrc_end) > 45:
            # last lyric line lands way off the actual track length -> different song
            return None
        return {"status": "success", "synced": True, "lyrics": text, "source": source}

    # 1. LRCLIB exact lookup
    if clean_artist and clean_title:
        try:
            r = requests.get(
                "https://lrclib.net/api/get",
                params={"artist_name": clean_artist, "track_name": clean_title},
                timeout=6, headers=headers,
            )
            if r.status_code == 200:
                d = r.json()
                try:
                    d_dur = float(d.get("duration")) if d.get("duration") else None
                except (TypeError, ValueError):
                    d_dur = None
                if not (duration and d_dur and abs(duration - d_dur) > _LYRICS_MAX_DRIFT_SEC):
                    got = _accept(d.get("syncedLyrics") or "", "lrclib_exact")
                    if got:
                        return got
        except Exception:
            pass

    # 2+3. Scored fuzzy search (best-of, never first-hit)
    def _search_best(q_str, source):
        try:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"q": q_str}, timeout=6, headers=headers,
            )
            if r.status_code != 200:
                return None
            items = r.json() or []
            best, best_score = None, 0.0
            for item in items:
                s = _score_lrclib_item(item, clean_artist, clean_title, duration)
                if s is not None and s > best_score:
                    best, best_score = item, s
            if not best:
                return None
            return _accept(best.get("syncedLyrics") or "", source)
        except Exception:
            return None

    if clean_artist and clean_title:
        got = _search_best(f"{clean_artist} {clean_title}", "lrclib_search")
        if got:
            return got
    if clean_title:
        got = _search_best(clean_title, "lrclib_search_loose")
        if got:
            return got

    # 4. syncedlyrics last resort (Genius provider dropped: plain-text junk)
    for providers in (["lrclib", "musixmatch", "netease"], ["musixmatch", "netease"]):
        try:
            res_text = syncedlyrics.search(_clean_search_query(query), providers=providers)
            got = _accept(res_text, f"syncedlyrics:{providers[-1]}")
            if got:
                return got
        except Exception:
            pass

    return {"status": "error", "synced": False, "lyrics": None, "source": None}


def get_all_sidecar_lyrics(filename):
    """Return dictionary of available language lyrics for a given track stem."""
    if not filename:
        return {}
    stem = _safe_stem(filename)
    dirs = get_dirs()
    lyrics_dir = dirs["lyrics"]

    base_lrc = get_sidecar_lyrics(filename)
    if not base_lrc and not _prov_recent_no_match(stem):
        res = fetch_lyrics_multi_provider(filename)
        src = res.get("source") if isinstance(res, dict) else None
        base_lrc = res.get("lyrics") if isinstance(res, dict) else res
        if base_lrc:
            save_sidecar_lyrics(filename, base_lrc)
            record_provenance(filename, "ok", src)
        else:
            record_provenance(filename, "no_match")

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
    """Auto-fetch and save validated lyrics sidecar for a downloaded track."""
    try:
        parsed_artist, _ = extract_artist_and_title(filename)
        # Only use the channel/uploader as artist when the filename itself has none —
        # otherwise channel names poison the search (e.g. "Gravero & faizal").
        artist = uploader if (uploader and not parsed_artist) else None
        res = fetch_lyrics_multi_provider(filename, title=title, artist=artist)
        lyrics = res.get("lyrics") if isinstance(res, dict) else res
        if lyrics:
            save_sidecar_lyrics(filename, lyrics)
            record_provenance(filename, "ok", res.get("source") if isinstance(res, dict) else None)
            return True
        record_provenance(filename, "no_match")
    except Exception as e:
        print(f"Error auto-fetching lyrics for {filename}: {e}", file=sys.stderr)
    return False


def _is_valid_sidecar_content(fname, content):
    """Generic sidecar validation: non-empty, synced .lrc files must contain timestamps,
    and urdu-variant sidecars must actually contain Arabic-script text."""
    if not content or not str(content).strip():
        return False
    lower = fname.lower()
    if lower.endswith(".lrc"):
        if not is_lrc_synced(content):
            return False
    if ".ur.lrc" in lower or ".urdu.lrc" in lower:
        if not re.search(r"[\u0600-\u06FF\u0750-\u077F]", content):
            return False
    return True


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
            else:
                # Generic content validation (replaces hardcoded string matching)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                    if not _is_valid_sidecar_content(fname, content):
                        should_purge = True
                except Exception:
                    should_purge = True

        if should_purge:
            try:
                os.remove(fpath)
                purged_count += 1
            except Exception as e:
                print(f"Error removing cached file {fpath}: {e}", file=sys.stderr)

    # Full wipe also resets the provenance/negative-cache index
    if purge_all:
        try:
            prov_path = _prov_index_path(dirs)
            if os.path.exists(prov_path):
                os.remove(prov_path)
        except Exception as e:
            print(f"Error removing lyrics provenance index: {e}", file=sys.stderr)

    # 2. Re-generate / Reindex sidecars for existing audio files (parallel, 4 workers)
    def _reindex(af):
        try:
            return 1 if get_all_sidecar_lyrics(af) else 0
        except Exception as e:
            print(f"Error reindexing lyrics for {af}: {e}", file=sys.stderr)
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        reindexed_count = sum(pool.map(_reindex, audio_files))

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
        with _LOG_LOCK:
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


def _classify_error(msg):
    """Classify a yt-dlp error message as 'transient' | 'extraction' | 'fatal' | 'unknown'."""
    m = (msg or "").lower()
    if any(p in m for p in _FATAL_PATTERNS):
        return "fatal"
    if any(p in m for p in _TRANSIENT_PATTERNS):
        return "transient"
    if any(p in m for p in _EXTRACTION_PATTERNS):
        return "extraction"
    return "unknown"


def _sleep_backoff(attempt):
    """Exponential backoff: 2s, 4s, 8s... capped at 10s."""
    time.sleep(min(2 ** attempt, 10))


def _extract_video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def _archive_contains_key(dirs, key):
    try:
        with open(dirs["archive"], "r", encoding="utf-8", errors="replace") as f:
            return any(line.strip() == key for line in f)
    except OSError:
        return False


def _archive_contains(dirs, video_id):
    return _archive_contains_key(dirs, f"youtube {video_id}")


def _archive_add(dirs, key):
    try:
        with open(dirs["archive"], "a", encoding="utf-8") as f:
            f.write(key + "\n")
    except OSError:
        pass


def score_candidate(info, expected_duration=None):
    """A3 heuristic score for a YouTube search candidate. Higher is better."""
    score = 0.0
    title = (info.get("title") or "").lower()
    uploader = (info.get("uploader") or info.get("channel") or "").lower()

    for kw in BAD_TITLE_KEYWORDS:
        if kw in title:
            score -= 15

    if uploader.endswith("- topic"):
        score += 25
    elif "vevo" in uploader:
        score += 20
    elif "official" in uploader:
        score += 8

    duration = info.get("duration") or 0
    if expected_duration:
        if duration and abs(duration - expected_duration) <= expected_duration * 0.10:
            score += 30
        elif duration:
            score -= min(25, abs(duration - expected_duration) / max(expected_duration, 1) * 100)
    else:
        if duration and duration > 900:   # >15min likely mix/compilation
            score -= 20
        if duration and duration < 30:    # likely preview/short
            score -= 15

    views = info.get("view_count") or 0
    score += min(15, views / 100000)
    return score


def search_best_candidate(query, expected_duration=None, max_candidates=None):
    """A3 smart source selection: fetch ytsearchN candidates (flat, cheap) and pick the best.

    Returns {"url","title","uploader","duration","view_count","score"} or None on failure.
    """
    if max_candidates is None:
        max_candidates = int(load_config().get("search_candidates") or 8)
    cmd = [
        "yt-dlp", "--dump-json", "--flat-playlist", "--no-playlist",
        "--socket-timeout", "15",
        f"ytsearch{max_candidates}:{query}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None

    candidates = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidates.append(json.loads(line))
        except Exception:
            continue
    if not candidates:
        return None

    best, best_score = None, float("-inf")
    for c in candidates:
        s = score_candidate(c, expected_duration)
        if s > best_score:
            best, best_score = c, s

    url = best.get("url") or best.get("webpage_url")
    if not url:
        return None
    if not str(url).startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"
    return {
        "url": url,
        "title": best.get("title") or "",
        "uploader": best.get("uploader") or best.get("channel") or "",
        "duration": best.get("duration"),
        "view_count": best.get("view_count"),
        "score": round(best_score, 1),
    }


def _build_ydl_opts(output_dir, dirs, audio_format="mp3", audio_quality="0",
                    client_set=None, cookies_browser=None, progress_cb=None,
                    cancel_check=None):
    """B2: shared YoutubeDL options builder (replaces CLI flag assembly)."""
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(uploader)s - %(title)s.%(ext)s"),
        "download_archive": dirs["archive"],
        "noplaylist": True,
        "writethumbnail": True,
        "convertthumbnails": "jpg",
        "socket_timeout": 30,
        "retries": 3,
        "js_runtimes": {"node": {}},
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": audio_quality},
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
    }
    if client_set:
        opts["extractor_args"] = {"youtube": {"player_client": client_set.split(",")}}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    cfg = load_config()
    if cfg.get("loudnorm"):
        # C6: EBU R128 loudness normalization applied during audio extraction
        i_lufs = cfg.get("loudnorm_i", -16)
        opts["postprocessor_args"] = {
            "ExtractAudio": ["-af", f"loudnorm=I={i_lufs}:TP=-1.5:LRA=11"]
        }
    if progress_cb or cancel_check:
        def _hook(d):
            if cancel_check is not None and cancel_check():
                raise yt_dlp.utils.DownloadCancelled()
            if progress_cb is None:
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = int(done * 100 / total) if total else None
            info = d.get("info_dict") or {}
            try:
                progress_cb({
                    "status": d.get("status"),
                    "percent": pct,
                    "filename": os.path.basename(d.get("filename") or info.get("title") or ""),
                })
            except Exception:
                pass
        opts["progress_hooks"] = [_hook]
    return opts


def download_single(query, output_dir, dirs, audio_format="mp3", audio_quality="0",
                    progress_cb=None, cancel_check=None, expected_duration=None):
    """Download a single song by name or URL using the yt-dlp Python API (B2).

    Robustness layers: A3 smart source selection, A2 player-client cycling with
    exponential backoff on transient errors, optional cookies-from-browser fallback.
    Return dict shape is unchanged from the subprocess era.
    """
    now = datetime.now().isoformat()

    # Resolve target: direct URL vs smart search
    target = query
    picked = None
    if not is_url(query):
        picked = search_best_candidate(query, expected_duration=expected_duration)
        if picked:
            target = picked["url"]

    # Cheap archive pre-check for direct video URLs
    vid = _extract_video_id(target)
    if vid and _archive_contains(dirs, vid):
        return {
            "status": "skipped", "query": query,
            "title": (picked or {}).get("title") or query,
            "uploader": (picked or {}).get("uploader", ""),
            "url": target,
            "message": "Already downloaded (recorded in archive)",
            "timestamp": now, "engine": "yt-dlp"
        }

    # Attempt matrix: cycle player clients; optional cookies fallback at the end
    attempts = [(cs, None) for cs in PLAYER_CLIENT_CYCLE]
    cookies_browser = load_config().get("cookies_browser")
    if cookies_browser:
        attempts.append((PLAYER_CLIENT_CYCLE[0], cookies_browser))

    last_err = ""
    for client_set, cookies_browser in attempts:
        retries = 0
        while True:
            opts = _build_ydl_opts(output_dir, dirs, audio_format, audio_quality,
                                   client_set=client_set, cookies_browser=cookies_browser,
                                   progress_cb=progress_cb, cancel_check=cancel_check)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=True)

                req = (info or {}).get("requested_downloads") or []
                fpath = req[0].get("filepath") if req else None

                if not fpath or not os.path.exists(fpath):
                    return {
                        "status": "skipped", "query": query,
                        "title": (info or {}).get("title") or (picked or {}).get("title") or query,
                        "uploader": (info or {}).get("uploader") or "",
                        "url": (info or {}).get("webpage_url") or target,
                        "message": "Already downloaded (recorded in archive)",
                        "timestamp": now, "engine": "yt-dlp"
                    }

                fname = os.path.basename(fpath)
                fsize = os.path.getsize(fpath)
                if load_config().get("auto_lyrics", True):
                    try:
                        auto_fetch_and_save_lyrics(fname, title=info.get("title"),
                                                   uploader=info.get("uploader"))
                    except Exception:
                        pass
                if load_config().get("auto_tag", True):
                    try:
                        auto_tag_file(fpath, title=info.get("title"), artist=info.get("uploader"))
                    except Exception:
                        pass
                return {
                    "status": "success", "query": query,
                    "title": info.get("title") or fname,
                    "uploader": info.get("uploader") or "Unknown",
                    "url": info.get("webpage_url") or target,
                    "filepath": fpath, "filename": fname,
                    "file_size_mb": round(fsize / 1024 / 1024, 2),
                    "timestamp": now, "engine": "yt-dlp"
                }
            except yt_dlp.utils.DownloadCancelled:
                raise
            except Exception as e:
                last_err = str(e)
                kind = _classify_error(last_err)
                if kind == "transient" and retries < MAX_SAME_CLIENT_RETRIES:
                    retries += 1
                    _sleep_backoff(retries)
                    continue
                break   # advance to next player client / give up

    return {"status": "error", "query": query,
            "error": (last_err or "Download failed")[:500],
            "timestamp": now, "engine": "yt-dlp"}


def handle_yt_playlist(playlist_url, output_dir, dirs, audio_format="mp3", audio_quality="0",
                       progress_cb=None):
    """Extract video URLs from a YouTube playlist via the Python API, then download each."""
    flat_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "socket_timeout": 30,
        "js_runtimes": {"node": {}},
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENT_CYCLE[0].split(",")}},
    }
    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except Exception as e:
        return {"status": "error", "query": playlist_url,
                "error": f"Failed to extract playlist: {str(e)[:300]}", "engine": "yt-dlp"}

    entries = (info or {}).get("entries") or []
    urls = []
    for e in entries:
        u = (e or {}).get("url") or (e or {}).get("webpage_url") or ""
        if u and not str(u).startswith("http"):
            u = f"https://www.youtube.com/watch?v={u}"
        if u:
            urls.append(u)

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


# ---------------------------------------------------------------------------
# B3: Spotify metadata resolution (spotipy when credentials exist, else no-auth
# embed scraping). C1: mutagen tagging + iTunes Search gap-fill.
# ---------------------------------------------------------------------------

_SPOTIFY_CLIENT_CACHE = {"client": None, "failed": False}


def _get_spotify_client():
    if _SPOTIFY_CLIENT_CACHE["failed"]:
        return None
    if _SPOTIFY_CLIENT_CACHE["client"] is not None:
        return _SPOTIFY_CLIENT_CACHE["client"]
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        cid = os.environ.get("SPOTIPY_CLIENT_ID")
        sec = os.environ.get("SPOTIPY_CLIENT_SECRET")
        if not (cid and sec):
            cfg_path = os.path.join(os.path.expanduser("~"), ".spotdl", "config.json")
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cid = cid or cfg.get("client_id")
                sec = sec or cfg.get("client_secret")
            except Exception:
                pass
        if not (cid and sec):
            _SPOTIFY_CLIENT_CACHE["failed"] = True
            return None
        client = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=cid, client_secret=sec),
                                 requests_timeout=8, retries=0, status_retries=1)
        _SPOTIFY_CLIENT_CACHE["client"] = client
        return client
    except Exception:
        _SPOTIFY_CLIENT_CACHE["failed"] = True
        return None


def _spotify_kind_id(url):
    m = re.search(r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist)/([A-Za-z0-9]+)", url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _embed_spotify_data(kind, eid):
    r = requests.get(f"https://open.spotify.com/embed/{kind}/{eid}", timeout=10,
                     headers={"User-Agent": "Mozilla/5.0"})
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.S)
    if not m:
        return None
    data = json.loads(m.group(1))
    return (((data.get("props") or {}).get("pageProps") or {}).get("state") or {}).get("data") or {}


def _spotify_tracks_via_api(sp, kind, eid):
    tracks = []
    if kind == "track":
        items = [sp.track(eid)]
    elif kind == "album":
        results = sp.album_tracks(eid)
        items = list(results.get("items") or [])
        while results.get("next"):
            results = sp.next(results)
            items.extend(results.get("items") or [])
    else:
        results = sp.playlist_items(eid)
        items = [it.get("track") for it in (results.get("items") or []) if it.get("track")]
        while results.get("next"):
            results = sp.next(results)
            items.extend(it.get("track") for it in (results.get("items") or []) if it.get("track"))

    for t in items:
        if not t:
            continue
        album = t.get("album") or {}
        imgs = album.get("images") or []
        tracks.append({
            "id": t.get("id"),
            "title": t.get("name"),
            "artist": ", ".join(a["name"] for a in (t.get("artists") or [])),
            "album": album.get("name"),
            "duration_ms": t.get("duration_ms") or 0,
            "cover_url": imgs[0].get("url") if imgs else None,
            "release_date": album.get("release_date"),
            "track_number": t.get("track_number"),
        })
    return [t for t in tracks if t.get("id") and t.get("title")]


def _spotify_tracks_via_embed(kind, eid):
    data = _embed_spotify_data(kind, eid)
    if not data:
        raise RuntimeError("Could not load Spotify data (no-auth scrape failed)")
    ent = data.get("entity") or {}
    tracks = []

    def _norm(tid, title, artist, duration_ms):
        return {
            "id": tid, "title": title, "artist": artist or "", "album": None,
            "duration_ms": duration_ms or 0, "cover_url": None,
            "release_date": ((ent.get("releaseDate") or {}).get("isoString") or "")[:10] or None,
            "track_number": None,
        }

    if kind == "track":
        artists = ", ".join(a.get("name") for a in (ent.get("artists") or []) if a.get("name"))
        tracks.append(_norm(ent.get("id") or eid, ent.get("name") or ent.get("title"),
                            artists, ent.get("duration")))
    else:
        for t in (ent.get("trackList") or []):
            tid = (t.get("uri") or "").split(":")[-1]
            tracks.append(_norm(tid, t.get("title"), t.get("subtitle"), t.get("duration")))

    tracks = [t for t in tracks if t.get("id") and t.get("title")]
    if not tracks:
        raise RuntimeError("No playable tracks found in Spotify URL")
    return tracks


def get_spotify_tracks(spotify_url):
    """Resolve a Spotify track/album/playlist URL into normalized track dicts.

    Returns (tracks, source) where source is 'api' or 'embed'. Raises on total failure.
    """
    kind, eid = _spotify_kind_id(spotify_url)
    if not eid:
        raise ValueError(f"Not a valid Spotify URL: {spotify_url}")

    sp = _get_spotify_client()
    if sp is not None:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(_spotify_tracks_via_api, sp, kind, eid)
            return fut.result(timeout=25), "api"
        except Exception as e:
            # Rate-limited/hanging creds: blacklist for this session, use embed scrape
            _SPOTIFY_CLIENT_CACHE["failed"] = True
            print(f"Spotify API failed/timeout ({e}); falling back to no-auth embed scrape", file=sys.stderr)
        finally:
            ex.shutdown(wait=False)

    return _spotify_tracks_via_embed(kind, eid), "embed"


def lookup_itunes(artist, title):
    """C1: metadata gap-fill via iTunes Search API (no auth)."""
    try:
        r = requests.get("https://itunes.apple.com/search",
                         params={"term": f"{artist} {title}".strip(), "entity": "song", "limit": 1},
                         timeout=6)
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
        if not results:
            return None
        it = results[0]
        art = it.get("artworkUrl100") or ""
        return {
            "album": it.get("collectionName"),
            "date": (it.get("releaseDate") or "")[:4],
            "genre": it.get("primaryGenreName"),
            "track_number": it.get("trackNumber"),
            "cover_url": art.replace("100x100", "600x600") if art else None,
        }
    except Exception:
        return None


def _download_cover(cover_url):
    try:
        r = requests.get(cover_url, timeout=10)
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def tag_audio_file(filepath, title=None, artist=None, album=None, date=None,
                   genre=None, track_number=None, cover_bytes=None):
    """C1: write ID3/Vorbis/MP4 tags + embedded artwork. Best-effort, never raises."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, TPE2, APIC
            try:
                tags = ID3(filepath)
            except Exception:
                tags = ID3()

            def _set(fid, cls, val):
                if val:
                    tags.setall(fid, [cls(encoding=3, text=[str(val)])])

            _set("TIT2", TIT2, title)
            _set("TPE1", TPE1, artist)
            _set("TALB", TALB, album)
            _set("TDRC", TDRC, date)
            _set("TCON", TCON, genre)
            _set("TRCK", TRCK, track_number)
            _set("TPE2", TPE2, artist)
            if cover_bytes:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
            tags.save(filepath)
            return True
        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(filepath)

            def _s(k, v):
                if v:
                    f[k] = [str(v)]

            _s("title", title)
            _s("artist", artist)
            _s("album", album)
            _s("date", date)
            _s("genre", genre)
            _s("tracknumber", track_number)
            if cover_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_bytes
                f.clear_pictures()
                f.add_picture(pic)
            f.save()
            return True
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(filepath)
            if title:
                f["\xa9nam"] = [str(title)]
            if artist:
                f["\xa9ART"] = [str(artist)]
            if album:
                f["\xa9alb"] = [str(album)]
            if date:
                f["\xa9day"] = [str(date)]
            if genre:
                f["\xa9gen"] = [str(genre)]
            if track_number:
                f["trkn"] = [(int(track_number), 0)]
            if cover_bytes:
                f["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            f.save()
            return True
    except Exception as e:
        print(f"Tagging failed for {filepath}: {e}", file=sys.stderr)
    return False


def auto_tag_file(filepath, title, artist, extra=None):
    """Best-effort tagging: use provided metadata, fill gaps via iTunes Search."""
    info = dict(extra or {})
    if not info.get("album"):
        it = lookup_itunes(artist or "", title or "")
        if it:
            for k, v in it.items():
                info.setdefault(k, v)
    cover = _download_cover(info["cover_url"]) if info.get("cover_url") else None
    return tag_audio_file(filepath, title=title, artist=artist, album=info.get("album"),
                          date=info.get("date"), genre=info.get("genre"),
                          track_number=info.get("track_number"), cover_bytes=cover)


def _unique_path(path):
    base, ext = os.path.splitext(path)
    cand, n = path, 1
    while os.path.exists(cand):
        cand = f"{base} ({n}){ext}"
        n += 1
    return cand


def handle_spotify(spotify_url, output_dir, dirs, audio_format="mp3", audio_quality="0",
                   progress_cb=None):
    """B3: resolve Spotify metadata ourselves, download via our yt-dlp pipeline,
    tag with mutagen (C1), dedupe through the shared archive, consistent naming."""
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now().isoformat()

    try:
        tracks, source = get_spotify_tracks(spotify_url)
    except Exception as e:
        print(f"Own Spotify resolution failed ({e}); trying legacy spotdl", file=sys.stderr)
        return _handle_spotify_legacy(spotify_url, output_dir, dirs, audio_format, audio_quality)

    results = []

    def _process(track):
        archive_key = f"spotify {track['id']}"
        if _archive_contains_key(dirs, archive_key):
            return {"status": "skipped", "query": spotify_url,
                    "title": track["title"], "uploader": track["artist"],
                    "message": "Already downloaded (recorded in archive)",
                    "timestamp": now, "engine": "yt-dlp"}

        expected = (track.get("duration_ms") or 0) / 1000.0
        query = f"{track['artist']} {track['title']}".strip()
        res = download_single(query, output_dir, dirs, audio_format, audio_quality,
                              expected_duration=expected)

        if res.get("status") == "success":
            # Consistent naming from Spotify metadata
            src_path = res.get("filepath")
            ext = os.path.splitext(src_path)[1].lower() or f".{audio_format}"
            name = f"{track['artist']} - {track['title']}" if track.get("artist") else track["title"]
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
            new_path = _unique_path(os.path.join(output_dir, f"{safe_name}{ext}"))
            try:
                os.rename(src_path, new_path)
                res["filepath"], res["filename"] = new_path, os.path.basename(new_path)
                res["file_size_mb"] = round(os.path.getsize(new_path) / 1024 / 1024, 2)
                src_path = new_path
            except OSError as e:
                print(f"Rename failed for {src_path}: {e}", file=sys.stderr)

            # C1 tagging with Spotify metadata + iTunes gap-fill
            try:
                auto_tag_file(src_path, title=track["title"], artist=track["artist"],
                              extra={"album": track.get("album"),
                                     "date": (track.get("release_date") or "")[:4] or None,
                                     "track_number": track.get("track_number"),
                                     "cover_url": track.get("cover_url")})
            except Exception as e:
                print(f"Tagging error for {src_path}: {e}", file=sys.stderr)

            # Cross-engine dedupe: mark both the spotify id and the resolved youtube video
            _archive_add(dirs, archive_key)
            vid = _extract_video_id(res.get("url") or "")
            if vid:
                _archive_add(dirs, f"youtube {vid}")

            try:
                auto_fetch_and_save_lyrics(res["filename"], title=track["title"],
                                           uploader=track["artist"])
            except Exception:
                pass

        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_process, t) for t in tracks]
        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                res = {"status": "error", "query": spotify_url, "error": str(e)}
            log_download(dirs["log"], res)
            results.append(res)

    ok = sum(1 for x in results if x.get("status") == "success")
    skipped = sum(1 for x in results if x.get("status") == "skipped")
    return {"status": "playlist_done", "source": "spotify", "engine": "yt-dlp",
            "metadata_source": source,
            "total": len(tracks), "downloaded": ok, "skipped": skipped, "results": results}


def _handle_spotify_legacy(spotify_url, output_dir, dirs, audio_format="mp3", audio_quality="0"):
    """Legacy spotdl v4 path kept as fallback when own metadata resolution fails.
    Detects new files by before/after directory snapshot diff."""
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
    """Fetch quick metadata preview via the yt-dlp Python API with smart-search fallback."""
    if is_url(query):
        targets = [query]
    else:
        best = search_best_candidate(query)
        targets = [best["url"]] if best else [f"ytsearch1:{query}"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "js_runtimes": {"node": {}},
        "extractor_args": {"youtube": {"player_client": PLAYER_CLIENT_CYCLE[0].split(",")}},
    }
    for t in targets:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                data = ydl.extract_info(t, download=False)
            if not data:
                continue
            duration_sec = data.get("duration", 0) or 0
            mins, secs = divmod(int(duration_sec), 60)
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


def _latest_ytdlp_version(_cache={"v": None, "ts": 0.0}):
    """Fetch latest yt-dlp version from PyPI (cached 1h). Returns None on failure."""
    now = time.time()
    if _cache["v"] and now - _cache["ts"] < 3600:
        return _cache["v"]
    try:
        r = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=4)
        v = (r.json().get("info") or {}).get("version")
        if v:
            _cache.update(v=v, ts=now)
        return v
    except Exception:
        return None


def _version_tuple(v):
    try:
        return tuple(int(p) for p in re.findall(r"\d+", str(v))[:4])
    except Exception:
        return ()


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

    diag = {
        "python": py_ver,
        "ytdlp": ytdlp_ver,
        "ffmpeg": ffmpeg_ver if "ffmpeg" in ffmpeg_ver.lower() else ("Installed" if ffmpeg_ver != "Not Available" else "Not Installed"),
        "spotdl": spotdl_ver,
        "music_dir": dirs["music"],
        "total_files": len(library),
        "total_size_mb": round(total_size_mb, 2)
    }

    # A2: surface an actionable hint when yt-dlp is outdated (it breaks often)
    latest = _latest_ytdlp_version()
    if latest:
        local_t, latest_t = _version_tuple(ytdlp_ver), _version_tuple(latest)
        if local_t and latest_t:
            diag["ytdlp_latest"] = latest
            diag["ytdlp_outdated"] = local_t < latest_t
            if diag["ytdlp_outdated"]:
                diag["ytdlp_hint"] = f"yt-dlp is outdated ({ytdlp_ver} < {latest}) — run: yt-dlp -U"

    return diag


# ---------------------------------------------------------------------------
# C2: SQLite library index (play counts, durations, search history)
# ---------------------------------------------------------------------------

def _db_path(dirs):
    return dirs.get("db") or os.path.join(os.path.dirname(dirs["music"]), "library.db")


def _db_connect(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS tracks(
        path TEXT PRIMARY KEY, filename TEXT, title TEXT, artist TEXT, format TEXT,
        size_mb REAL, date_added TEXT, mtime REAL, duration_sec REAL,
        play_count INTEGER DEFAULT 0, last_played TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS search_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, timestamp TEXT)""")
    return conn


def _audio_duration(path):
    try:
        from mutagen import File as MFile
        m = MFile(path)
        return round(m.info.length, 2) if m and m.info else None
    except Exception:
        return None


def sync_library_db(dirs):
    """Reconcile the SQLite index with the filesystem. Returns per-filename stats."""
    conn = _db_connect(_db_path(dirs))
    cur = conn.cursor()
    valid_exts = {".mp3", ".flac", ".m4a", ".wav", ".opus", ".aac", ".ogg"}
    music_dir = dirs["music"]
    fs = {}
    if os.path.exists(music_dir):
        for fn in os.listdir(music_dir):
            ext = os.path.splitext(fn)[1].lower()
            if ext in valid_exts:
                p = os.path.join(music_dir, fn)
                try:
                    fs[p] = os.stat(p)
                except OSError:
                    pass

    for p, st in fs.items():
        row = cur.execute("SELECT mtime FROM tracks WHERE path=?", (p,)).fetchone()
        if row is not None and abs((row[0] or 0) - st.st_mtime) <= 1:
            continue
        filename = os.path.basename(p)
        raw = os.path.splitext(filename)[0]
        if " - " in raw:
            artist, title = raw.split(" - ", 1)
        else:
            artist, title = "Unknown", raw
        dur = _audio_duration(p) if row is None else None
        cur.execute("""INSERT INTO tracks(path, filename, title, artist, format, size_mb,
                        date_added, mtime, duration_sec)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET filename=excluded.filename,
              title=excluded.title, artist=excluded.artist, format=excluded.format,
              size_mb=excluded.size_mb, mtime=excluded.mtime,
              duration_sec=COALESCE(excluded.duration_sec, tracks.duration_sec)""",
            (p, filename, title.strip(), artist.strip(), ext.replace(".", "").upper(),
             round(st.st_size / 1048576, 2),
             datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
             st.st_mtime, dur))

    for (p,) in cur.execute("SELECT path FROM tracks").fetchall():
        if p not in fs:
            cur.execute("DELETE FROM tracks WHERE path=?", (p,))
    conn.commit()

    stats = {r[0]: {"play_count": r[1], "last_played": r[2], "duration_sec": r[3]}
             for r in cur.execute("SELECT filename, play_count, last_played, duration_sec FROM tracks")}
    conn.close()
    return stats


def get_music_library_indexed(dirs):
    """C2: library listing backed by the SQLite index. Same item shape as
    get_music_library plus play_count/last_played/duration_sec extras."""
    base = get_music_library(dirs)
    try:
        stats = sync_library_db(dirs)
    except Exception as e:
        print(f"Library DB sync failed: {e}", file=sys.stderr)
        stats = {}
    for item in base:
        s = stats.get(item["filename"]) or {}
        item["play_count"] = s.get("play_count") or 0
        item["last_played"] = s.get("last_played")
        item["duration_sec"] = s.get("duration_sec")
    return base


def record_play(dirs, filename):
    try:
        conn = _db_connect(_db_path(dirs))
        conn.execute("UPDATE tracks SET play_count=play_count+1, last_played=? WHERE filename=?",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), filename))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"record_play failed: {e}", file=sys.stderr)
        return False


def record_search(dirs, query):
    try:
        conn = _db_connect(_db_path(dirs))
        conn.execute("INSERT INTO search_history(query, timestamp) VALUES(?,?)",
                     (query, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"record_search failed: {e}", file=sys.stderr)


def get_search_history(dirs, limit=50):
    try:
        conn = _db_connect(_db_path(dirs))
        rows = conn.execute("SELECT query, timestamp FROM search_history ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
        conn.close()
        return [{"query": q, "timestamp": t} for q, t in rows]
    except Exception as e:
        print(f"get_search_history failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# C3: Playlist builder + M3U export (JSON sidecars in <music>/.playlists/)
# ---------------------------------------------------------------------------

def _safe_playlist_name(name):
    safe = re.sub(r'[<>:"/\\|?*]', "_", str(name or "")).strip()
    return safe[:80]


def _playlists_dir(dirs):
    d = os.path.join(dirs["music"], ".playlists")
    os.makedirs(d, exist_ok=True)
    return d


def list_playlists(dirs):
    pdir = _playlists_dir(dirs)
    out = []
    for fn in os.listdir(pdir):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(pdir, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append({"name": data.get("name") or fn[:-5],
                            "count": len(data.get("tracks") or [])})
            except Exception:
                continue
    return sorted(out, key=lambda x: x["name"].lower())


def save_playlist(dirs, name, tracks):
    safe = _safe_playlist_name(name)
    if not safe:
        raise ValueError("Invalid playlist name")
    path = os.path.join(_playlists_dir(dirs), f"{safe}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": safe, "tracks": [str(t) for t in tracks]}, f,
                  ensure_ascii=False, indent=1)
    return {"name": safe, "count": len(tracks)}


def get_playlist(dirs, name):
    safe = _safe_playlist_name(name)
    path = os.path.join(_playlists_dir(dirs), f"{safe}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_playlist(dirs, name):
    safe = _safe_playlist_name(name)
    path = os.path.join(_playlists_dir(dirs), f"{safe}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def export_m3u(dirs, name):
    """Build an M3U8 string (absolute paths + EXTINF durations) for a stored playlist."""
    pl = get_playlist(dirs, name)
    if pl is None:
        return None
    durations = {}
    try:
        conn = _db_connect(_db_path(dirs))
        durations = {r[0]: r[1] for r in
                     conn.execute("SELECT filename, duration_sec FROM tracks").fetchall()}
        conn.close()
    except Exception:
        pass

    lines = ["#EXTM3U"]
    for fn in (pl.get("tracks") or []):
        full = os.path.join(dirs["music"], fn)
        if not os.path.exists(full):
            continue
        raw = os.path.splitext(fn)[0]
        dur = int(durations.get(fn) or 0)
        lines.append(f"#EXTINF:{dur},{raw}")
        lines.append(full)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# C4: Generic lyrics romanization / translation sidecars
# ---------------------------------------------------------------------------

_URDU_ROMAN_MAP = {
    'ا': 'a', 'آ': 'aa', 'ب': 'b', 'پ': 'p', 'ت': 't', 'ٹ': 't', 'ث': 's', 'ج': 'j',
    'چ': 'ch', 'ح': 'h', 'خ': 'kh', 'د': 'd', 'ڈ': 'd', 'ذ': 'z', 'ر': 'r', 'ڑ': 'r',
    'ز': 'z', 'ژ': 'zh', 'س': 's', 'ش': 'sh', 'ص': 's', 'ض': 'z', 'ط': 't', 'ظ': 'z',
    'ع': "'", 'غ': 'gh', 'ف': 'f', 'ق': 'q', 'ک': 'k', 'گ': 'g', 'ل': 'l', 'م': 'm',
    'ن': 'n', 'ں': 'n', 'و': 'o', 'ہ': 'h', 'ھ': 'h', 'ۃ': 'h', 'ء': "'", 'ی': 'i',
    'ے': 'e', 'ي': 'i', 'ئ': 'y', 'ؤ': 'o', 'أ': 'a', 'إ': 'i', 'ك': 'k', 'ة': 'h',
    'َ': 'a', 'ِ': 'i', 'ُ': 'u', 'ٰ': 'aa', 'ّ': '', 'ْ': '', 'ٓ': '', 'ٔ': '',
    'ً': 'an', 'ٌ': 'un', 'ٍ': 'in', '٫': '.', '،': ',',
    '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5',
    '۶': '6', '۷': '7', '۸': '8', '۹': '9',
}

_URDU_WORD_OVERRIDES = {
    'خدا': 'khuda', 'حافظ': 'hafiz', 'دل': 'dil', 'اور': 'aur', 'تیری': 'teri',
    'میری': 'meri', 'میرا': 'mera', 'یادوں': 'yaadon', 'یاد': 'yaad', 'میں': 'mein',
    'ہے': 'hai', 'ہیں': 'hain', 'نہیں': 'nahin', 'تم': 'tum', 'وہ': 'woh',
    'پیار': 'pyar', 'محبت': 'mohabbat', 'جان': 'jaan', 'عشق': 'ishq',
    'آنکھ': 'aankh', 'آنکھیں': 'aankhein', 'رات': 'raat', 'دن': 'din',
    'کیا': 'kya', 'کیوں': 'kyun', 'کہاں': 'kahan', 'بہت': 'bohat', 'اکثر': 'aksar',
}

_LRC_LINE_RE = re.compile(r'^\[(\d{1,2}:\d{2}(?:[.:]\d{1,3})?)\]\s*(.*)$')


def romanize_urdu(text):
    """Offline Urdu/Arabic-script -> Roman transliteration (word overrides + char map)."""
    if not text:
        return text

    def _roman_word(w):
        if w in _URDU_WORD_OVERRIDES:
            return _URDU_WORD_OVERRIDES[w]
        out = []
        for ch in w:
            if ch in _URDU_ROMAN_MAP:
                out.append(_URDU_ROMAN_MAP[ch])
            elif re.search(r"[\u0600-\u06FF\u0750-\u077F]", ch):
                continue  # unmapped script char -> drop
            else:
                out.append(ch)
        return "".join(out)

    s = " ".join(_roman_word(w) for w in str(text).split())
    s = re.sub(r"('{2,})", "'", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def translate_text(text, target_lang="en"):
    """Translate plain text via the free Google gtx endpoint. Returns None on failure."""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            data = r.json()
            return "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    except Exception:
        pass
    return None


def _transform_lrc(content, transform):
    """Apply a text transform to LRC content while preserving timestamps."""
    out = []
    for line in (content or "").splitlines():
        m = _LRC_LINE_RE.match(line.strip())
        if m and m.group(2).strip():
            new = transform(m.group(2))
            out.append(f"[{m.group(1)}] {new}" if new else line)
        else:
            out.append(line)
    return "\n".join(out)


def generate_lyrics_sidecars(filename, translate=False):
    """Generate <stem>.roman.lrc (offline) and optionally <stem>.english.lrc (gtx).

    Returns {"status","generated":[...]} — never overwrites existing sidecars.
    """
    base = get_sidecar_lyrics(filename)
    if not base:
        return {"status": "error", "error": "No base lyrics found for this track"}

    stem = _safe_stem(filename)
    ldir = get_dirs()["lyrics"]
    generated = []

    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", base):
        roman_path = os.path.join(ldir, f"{stem}.roman.lrc")
        if not os.path.exists(roman_path):
            with open(roman_path, "w", encoding="utf-8") as f:
                f.write(_transform_lrc(base, romanize_urdu))
            generated.append("roman")

    if translate:
        eng_path = os.path.join(ldir, f"{stem}.english.lrc")
        if not os.path.exists(eng_path):
            translated = _transform_lrc(base, lambda t: translate_text(t, "en"))

            def _drop_untranslated(line):
                m = _LRC_LINE_RE.match(line.strip())
                if m and (not m.group(2).strip() or re.search(r"[\u0600-\u06FF]", m.group(2))):
                    return ""  # failed line -> omit rather than show raw script
                return line

            translated = "\n".join(_drop_untranslated(l) for l in translated.splitlines())
            with open(eng_path, "w", encoding="utf-8") as f:
                f.write(translated)
            generated.append("english")

    return {"status": "success", "generated": generated}


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


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
from bs4 import BeautifulSoup
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


def get_sidecar_lyrics(filename, lang=None):
    """Retrieve sidecar lyrics from .lyrics/<stem>.<lang>.lrc, .lyrics/<stem>.lrc or .lyrics/<stem>.txt with UTF-8 encoding."""
    if not filename:
        return None
    safe_filename = os.path.basename(filename)
    stem = os.path.splitext(safe_filename)[0]
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


ENGLISH_STOP_WORDS = {
    "the", "you", "your", "that", "this", "with", "have", "was", "were",
    "searching", "night", "trail", "follow", "repaid", "debt", "eyes", "baby",
    "love", "like", "when", "from", "what", "about", "there", "their", "could",
    "would", "should", "know", "never", "where", "take", "back", "met", "lord",
    "huron", "lana", "del", "rey", "olivia", "dean", "new", "west", "citizen",
    "vertigo", "easy", "fall"
}

HIGH_CONFIDENCE_ROMAN_URDU_WORDS = {
    "guzre", "tumhein", "unhein", "bikhray", "iraaday", "kaha", "kahan", "zindagi",
    "aankhon", "aankhein", "honsla", "shumaar", "khasara", "siyah", "sajde", "haaray",
    "pyaar", "tumhara", "tumhari", "chaho", "chaha", "kaise", "kaisa", "batao",
    "suno", "khushi", "chup", "bulaye", "jaaye", "samjho", "mehfil", "mujhse",
    "tujhse", "zamaaney", "duniya", "sanwar", "yaad"
}

SOUTH_ASIAN_ARTISTS = [
    "abdul hannan", "asim azhar", "hasan raheem", "annural khalid",
    "faheem abdullah", "rovalio", "maanu", "kaifi khalil", "atif aslam",
    "rahat fateh ali khan", "ali zafar", "young stunners", "talha anjum",
    "talhah yunus", "bayan", "faaris", "falak shabir", "chahar", "kashmir"
]

# Expanded mapping of Roman Urdu to Urdu Nastaliq
ROMAN_URDU_WORDS = {
    "hum": "ہم", "pe": "پہ", "hi": "ہی", "guzre": "گزرے", "kyun": "کیوں", "kyu": "کیوں",
    "dil": "دل", "pyar": "پیار", "pyaar": "پیار", "zindagi": "زندگی", "mera": "میرا",
    "meri": "میری", "mere": "میرے", "kaha": "کہا", "kahan": "کہاں", "raha": "رہا",
    "tere": "تیرے", "teri": "تیری", "tera": "تیرا", "tum": "تم", "tumhari": "تمہاری",
    "tumhara": "تمہارا", "shab": "شب", "raat": "رات", "ho": "ہو", "haan": "ہاں",
    "bhi": "بھی", "toh": "تو", "tou": "تو", "se": "سے", "hai": "ہے", "hain": "ہیں",
    "mein": "میں", "mai": "میں", "ko": "کو", "kya": "کیا", "baat": "بات", "sun": "سن",
    "ja": "جا", "aaj": "آج", "phir": "پھر", "baad": "بعد", "naam": "نام", "samajh": "سمجھ",
    "soch": "سوچ", "chaho": "چاہو", "chaha": "چاہا", "tumhein": "تمہیں", "unhein": "انہیں",
    "sab": "سب", "kabhi": "کبھی", "kuch": "کچھ", "woh": "وہ", "ye": "یہ", "yeh": "یہ",
    "ik": "اک", "ek": "ایک", "paas": "پاس", "door": "دور", "batao": "بتاؤ", "suno": "سنو",
    "chup": "چپ", "dard": "درد", "khushi": "خوشی", "yaad": "یاد", "bulaye": "بلائے",
    "aaye": "آئے", "jaaye": "جائے", "laga": "لگا", "lagi": "لگی", "lage": "لگے",
    "chaand": "چاند", "chaandni": "چاندنی", "aankhon": "آنکھوں", "aankhein": "آنکھیں",
    "honsla": "حوصلہ", "saath": "ساتھ", "rooth": "روٹھ", "maan": "مان", "samjho": "سمجھو",
    "haaray": "ہارے", "bikhray": "بکھرے", "iraaday": "ارادے", "hoon": "ہوں",
    "hun": "ہوں", "kaise": "کیسے", "kaisa": "کیسا", "tujhse": "تجھ سے", "mujhse": "مجھ سے",
    "jo": "جو", "gham": "غم", "bante": "بانٹتے", "tumko": "تم کو", "mile": "ملیں",
    "jiya": "جیا", "dhadke": "دھڑکے", "yun": "یوں", "humein": "ہمیں", "ghazab": "غضب",
    "bhara": "بھرا", "anjaam": "انجام", "de": "دے", "tarasna": "ترسنا", "muskaan": "مسکان",
    "tu": "تو", "chala": "چلا", "akela": "اکیلا", "safar": "سفر", "humsafar": "ہمسفر",
    "saaye": "سائے", "khoya": "کھویا", "rahi": "رہی", "nigahein": "نگاہیں", "manzil": "منزل",
    "dhoondte": "ڈھونڈتے", "le": "لے", "lo": "لو", "mat": "مت", "karo": "کرو", "kehte": "کہتے"
}

PHONETIC_CHAR_MAP = [
    ("ch", "چ"), ("kh", "خ"), ("gh", "غ"), ("sh", "ش"), ("th", "ت"),
    ("ph", "ف"), ("bh", "بھ"), ("dh", "دھ"), ("jh", "جھ"), ("rh", "ڑ"),
    ("aa", "آ"), ("ee", "ای"), ("oo", "او"), ("ou", "او"),
    ("a", "ا"), ("b", "ب"), ("p", "پ"), ("t", "ت"), ("j", "ج"),
    ("d", "د"), ("r", "ر"), ("z", "ز"), ("s", "س"), ("f", "ف"),
    ("k", "ک"), ("l", "ل"), ("m", "م"), ("n", "ن"), ("w", "و"),
    ("h", "ہ"), ("y", "ی"), ("e", "ے"), ("o", "و"), ("u", "و"), ("i", "ی")
]

def roman_word_fallback_to_urdu(word):
    """Fallback converter that maps any remaining Latin word phonetically into Urdu script."""
    w = word.lower().strip()
    if not w:
        return word
    for orig, ur in PHONETIC_CHAR_MAP:
        w = w.replace(orig, ur)
    return w

def is_english_song(text, filename=""):
    """Return True if song is strictly English (e.g. Lord Huron, Lana Del Rey, Olivia Dean)."""
    if not text:
        return True
    # If native Urdu script is present, not English
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", text):
        return False

    clean_text = text.lower()
    fn_lower = filename.lower()

    # Check known South Asian artists
    if any(artist in fn_lower for artist in SOUTH_ASIAN_ARTISTS):
        return False

    # Check for high confidence Roman Urdu keywords
    words = set(re.findall(r"\b[a-zA-Z]+\b", clean_text))
    if any(w in words for w in HIGH_CONFIDENCE_ROMAN_URDU_WORDS):
        return False

    # Check for English stop words
    english_word_matches = sum(1 for w in words if w in ENGLISH_STOP_WORDS)
    if english_word_matches >= 2 or (len(words) > 0 and (english_word_matches / len(words)) > 0.04):
        return True

    return False

def transliterate_roman_to_urdu(text):
    """Phonetically transliterate Roman Urdu lyrics lines into 100% Urdu Nastaliq script."""
    if not text:
        return ""
    lines = text.splitlines()
    res = []
    for line in lines:
        match = re.match(r"^(\[\d{1,2}:\d{2}(?:\.\d{2,3})?\])(.*)", line)
        if match:
            ts, body = match.group(1), match.group(2)
            tokens = re.split(r"(\s+|[^\w\s])", body)
            out_tokens = []
            for t in tokens:
                clean_t = t.lower().strip()
                if not clean_t or not re.match(r"^[a-zA-Z]+$", clean_t):
                    out_tokens.append(t)
                elif clean_t in ROMAN_URDU_WORDS:
                    out_tokens.append(ROMAN_URDU_WORDS[clean_t])
                else:
                    out_tokens.append(roman_word_fallback_to_urdu(clean_t))
            res.append(f"{ts} {''.join(out_tokens)}")
        else:
            tokens = re.split(r"(\s+|[^\w\s])", line)
            out_tokens = []
            for t in tokens:
                clean_t = t.lower().strip()
                if not clean_t or not re.match(r"^[a-zA-Z]+$", clean_t):
                    out_tokens.append(t)
                elif clean_t in ROMAN_URDU_WORDS:
                    out_tokens.append(ROMAN_URDU_WORDS[clean_t])
                else:
                    out_tokens.append(roman_word_fallback_to_urdu(clean_t))
            res.append("".join(out_tokens))
    return "\n".join(res)

def fetch_genius_urdu_lyrics(title, artist=""):
    """Scrape authentic Urdu translation lyrics directly from Genius.com."""
    try:
        clean_title = _clean_search_query(title)
        clean_artist = _clean_search_query(artist)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }
        url_candidates = []

        # Construct candidate Genius slugs
        artist_slug = re.sub(r"[^\w\s-]", "", clean_artist).strip().replace(" ", "-").lower()
        title_slug = re.sub(r"[^\w\s-]", "", clean_title).strip().replace(" ", "-").lower()
        
        if artist_slug and title_slug:
            url_candidates.append(f"https://genius.com/Genius-urdu-translations-{artist_slug}-and-{title_slug}-lyrics")
            url_candidates.append(f"https://genius.com/Genius-urdu-translations-{artist_slug}-{title_slug}-lyrics")
        if title_slug:
            url_candidates.append(f"https://genius.com/Genius-urdu-translations-{title_slug}-lyrics")

        # Build search query terms
        words = re.findall(r"\b\w+\b", f"{clean_artist} {clean_title}")
        q_terms = [
            f"{clean_artist} {clean_title} Urdu",
            f"{clean_artist} {clean_title}",
            f"{clean_title} Urdu",
            " ".join(words[:3]) + " Urdu" if len(words) >= 3 else "",
            " ".join(words[:2]) + " Urdu" if len(words) >= 2 else ""
        ]
        q_terms = [q for q in q_terms if q.strip()]

        # Query Genius Search API
        headers_api = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        for q_term in q_terms:
            try:
                search_url = f"https://genius.com/api/search/multi?q={urllib.parse.quote(q_term)}"
                req = urllib.request.Request(search_url, headers=headers_api)
                res = urllib.request.urlopen(req, timeout=5)
                data = json.loads(res.read().decode('utf-8'))
                sections = data.get('response', {}).get('sections', [])
                for section in sections:
                    for hit in section.get('hits', []):
                        item = hit.get('result') or {}
                        path = item.get('path') or ''
                        full_t = str(item.get('full_title') or item.get('title') or '').lower()
                        if path and ('urdu' in path.lower() or 'urdu' in full_t or 'ترجمہ' in full_t):
                            candidate = f"https://genius.com{path}"
                            if candidate not in url_candidates:
                                url_candidates.insert(0, candidate)
            except Exception:
                pass

        for target_url in url_candidates:
            try:
                req = urllib.request.Request(target_url, headers=headers)
                html = urllib.request.urlopen(req, timeout=5).read().decode('utf-8')
                soup = BeautifulSoup(html, 'html.parser')
                containers = soup.find_all('div', {'data-lyrics-container': 'true'})
                if containers:
                    raw_text = '\n'.join([c.get_text('\n') for c in containers])
                    urdu_lines = []
                    for raw_line in raw_text.splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith('[') or 'Lyrics' in line or 'Contributor' in line:
                            continue
                        if re.search(r'[\u0600-\u06FF]', line):
                            urdu_lines.append(line)
                    if len(urdu_lines) >= 3:
                        return '\n'.join(urdu_lines)
            except Exception:
                continue

    except Exception as e:
        print(f"Genius Urdu fetch error: {e}")
        traceback.print_exc()
    return None

def align_urdu_with_lrc_timestamps(base_lrc, urdu_text):
    """Align raw Urdu text lines with timestamps from base LRC file."""
    if not base_lrc or not urdu_text:
        return urdu_text

    lrc_lines = base_lrc.splitlines()
    urdu_lines = [l.strip() for l in urdu_text.splitlines() if l.strip() and not l.strip().startswith('[')]
    
    timestamps = []
    for line in lrc_lines:
        match = re.match(r"^(\[\d{1,2}:\d{2}(?:\.\d{2,3})?\])", line.strip())
        if match:
            timestamps.append(match.group(1))

    if not timestamps or not urdu_lines:
        return urdu_text

    result = []
    for i in range(min(len(timestamps), len(urdu_lines))):
        result.append(f"{timestamps[i]} {urdu_lines[i]}")

    return "\n".join(result)

def get_all_sidecar_lyrics(filename):
    """Return dictionary of all available language lyrics for a given track stem."""
    if not filename:
        return {}
    safe_filename = os.path.basename(filename)
    stem = os.path.splitext(safe_filename)[0]
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

    # If it's an English song, return strictly English
    if is_english_song(base_lrc, filename):
        return {"english": base_lrc}

    # Otherwise it's an Urdu / Pakistani / South Asian track!
    result = {}
    if re.search(r"[\u0600-\u06FF\u0750-\u077F]", base_lrc):
        result["urdu"] = base_lrc
    else:
        result["roman"] = base_lrc
        
        # Check if authentic Urdu sidecar exists on disk
        urdu_sidecar_path = os.path.join(lyrics_dir, f"{stem}.ur.lrc")
        if not os.path.exists(urdu_sidecar_path):
            urdu_sidecar_path = os.path.join(lyrics_dir, f"{stem}.urdu.lrc")

        authentic_urdu = None
        if os.path.exists(urdu_sidecar_path):
            with open(urdu_sidecar_path, "r", encoding="utf-8", errors="replace") as f:
                authentic_urdu = f.read()

        # If authentic Urdu sidecar not on disk, fetch from Genius!
        if not authentic_urdu:
            parts = stem.split(" - ")
            artist_name = parts[0] if len(parts) > 1 else ""
            title_name = parts[1] if len(parts) > 1 else stem
            
            raw_genius_urdu = fetch_genius_urdu_lyrics(title_name, artist_name)
            if raw_genius_urdu:
                authentic_urdu = align_urdu_with_lrc_timestamps(base_lrc, raw_genius_urdu)
                try:
                    save_path = os.path.join(lyrics_dir, f"{stem}.ur.lrc")
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(authentic_urdu)
                except Exception:
                    pass

        if authentic_urdu:
            result["urdu"] = authentic_urdu
        else:
            result["urdu"] = transliterate_roman_to_urdu(base_lrc)

    # Check sidecar files for additional explicit translations if saved
    for lang in ["roman", "urdu"]:
        path = os.path.join(lyrics_dir, f"{stem}.{lang}.lrc")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                result[lang] = f.read()

    return result


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
    """Download tracks from Spotify URL via spotdl v3 direct download.
    
    spotdl v3.x doesn't have 'save' subcommand — it downloads directly.
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
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "query": spotify_url,
                "error": "spotdl download timed out after 5 minutes"}

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
                    "error": f"spotdl failed (code {r.returncode}): {stderr_msg}"}
        else:
            return {"status": "error", "query": spotify_url,
                    "error": f"spotdl completed but no new files found. {stderr_msg}"}

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
            "timestamp": now
        }
        log_download(dirs["log"], result)
        results.append(result)

    ok = len(results)
    return {"status": "playlist_done", "source": "spotify",
            "total": ok, "downloaded": ok, "results": results}


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


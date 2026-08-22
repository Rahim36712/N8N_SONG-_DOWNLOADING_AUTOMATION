"""Unit tests for the lyrics matcher's pure logic: filename parsing, LRC
validation helpers and LRCLIB candidate scoring/gates. No network access."""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import download_song as ds


# ---------------------------------------------------------------------------
# Filename -> (artist, title) extraction
# ---------------------------------------------------------------------------

def test_extract_basic_dash_format():
    assert ds.extract_artist_and_title("Arijit Singh - Tum Hi Ho.mp3") == ("Arijit Singh", "Tum Hi Ho")


def test_extract_strips_video_junk():
    assert ds.extract_artist_and_title("New West - Those Eyes (Official Audio).mp3") == ("New West", "Those Eyes")


def test_extract_strips_feat_suffix():
    assert ds.extract_artist_and_title("Artist - Title ft. Someone Else.mp3") == ("Artist", "Title")


def test_extract_no_dash_is_title_only():
    assert ds.extract_artist_and_title("Shape of You.mp3") == ("", "Shape of You")


def test_extract_multi_dash_keeps_first_segment_as_artist():
    # Pre-existing first-dash heuristic; validated search covers the rest.
    artist, title = ds.extract_artist_and_title(
        "VeroPop India - Gravero & faizal - Nazrein (Official Lyric Video).mp3")
    assert artist == "VeroPop India"
    assert "Nazrein" in title


def test_extract_preserves_periods_in_non_filename_queries():
    # Regression: splitext used to eat ". Brightside" as a fake extension.
    assert ds.extract_artist_and_title("Mr. Brightside") == ("", "Mr. Brightside")
    assert ds.extract_artist_and_title("Dua Lipa - Levitating feat. DaBaby.mp3")[0] == "Dua Lipa"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_safe_stem_replaces_invalid_chars():
    assert ds._safe_stem("a<b>:c?.mp3") == "a_b__c_"
    assert ds._safe_stem("plain name.mp3") == "plain name"


def test_clean_search_query_combines_parts():
    assert ds._clean_search_query("Arijit Singh - Tum Hi Ho.mp3") == "Arijit Singh Tum Hi Ho"


def test_token_overlap_basics():
    assert ds._token_overlap("Nazrein", "Nazrein") == 1.0
    assert ds._token_overlap("Nazrein", "Some Song") == 0.0
    assert ds._token_overlap("Nazrein", "Nazrein Remix") == 1.0  # min-set ratio
    assert ds._token_overlap("", "anything") == 0.0


def test_lrc_end_seconds_parses_last_timestamp():
    assert ds._lrc_end_seconds("[00:10.04] a\n[01:02.50] b\nno stamp here") == pytest.approx(62.5)
    assert ds._lrc_end_seconds("[01:02] x") == pytest.approx(62.0)
    assert ds._lrc_end_seconds("no timestamps at all") is None
    assert ds._lrc_end_seconds("") is None


def test_is_lrc_synced():
    assert ds.is_lrc_synced("[00:01.0] hello") is True
    assert ds.is_lrc_synced("plain lyric text") is False


# ---------------------------------------------------------------------------
# LRCLIB candidate scoring gates
# ---------------------------------------------------------------------------

GOOD_ITEM = {
    "syncedLyrics": "[00:01.00] first line\n[00:05.00] second",
    "trackName": "Nazrein",
    "artistName": "Gravero Faizal",
    "duration": 142.0,
}


def test_score_accepts_matching_candidate():
    score = ds._score_lrclib_item(GOOD_ITEM, "Gravero Faizal", "Nazrein", 142.5)
    assert score is not None and score > 0


def test_score_rejects_duration_mismatch():
    item = {**GOOD_ITEM, "duration": 200}
    assert ds._score_lrclib_item(item, "Gravero Faizal", "Nazrein", 142.5) is None


def test_score_rejects_unsynced_content():
    item = {**GOOD_ITEM, "syncedLyrics": "just plain words"}
    assert ds._score_lrclib_item(item, "Gravero Faizal", "Nazrein", 142.5) is None


def test_score_rejects_different_song():
    item = {**GOOD_ITEM, "trackName": "Whatever Else", "artistName": "Nobody Known"}
    assert ds._score_lrclib_item(item, "Gravero Faizal", "Nazrein", 142.5) is None


def test_score_artist_match_rescues_title_variance():
    item = {**GOOD_ITEM, "trackName": "Completely Different Title Words Here"}
    assert ds._score_lrclib_item(item, "Gravero Faizal", "Nazrein", 142.5) is not None


def test_score_allows_missing_duration_when_title_matches():
    item = {k: v for k, v in GOOD_ITEM.items() if k != "duration"}
    assert ds._score_lrclib_item(item, "Gravero Faizal", "Nazrein", None) is not None


def test_score_bare_query_needs_duration_or_overlap():
    # Duration known on both sides -> acceptable even without names.
    assert ds._score_lrclib_item(dict(GOOD_ITEM), "", "", 142.0) is not None
    # Nothing known at all and zero overlap -> reject.
    item = {k: v for k, v in GOOD_ITEM.items() if k != "duration"}
    assert ds._score_lrclib_item(item, "", "", None) is None


# ---------------------------------------------------------------------------
# Provenance / negative-cache index (hermetic via tmp dirs)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_dirs(tmp_path, monkeypatch):
    music = tmp_path / "music"
    lyrics = tmp_path / ".lyrics"
    lyrics.mkdir(parents=True)
    dirs = {"music": str(music), "lyrics": str(lyrics)}
    monkeypatch.setattr(ds, "get_dirs", lambda: dirs)
    return dirs


def test_provenance_record_and_clear(fake_dirs):
    ds.record_provenance("Song.mp3", "ok", "lrclib_exact")
    idx = ds._load_prov_index()
    assert idx["Song"]["status"] == "ok"
    assert idx["Song"]["source"] == "lrclib_exact"

    assert ds._prov_recent_no_match("Song") is False  # ok status is never "recent no-match"

    ds.record_provenance("Song.mp3", "no_match")
    assert ds._prov_recent_no_match("Song") is True

    ds.clear_provenance("Song.mp3")
    assert "Song" not in ds._load_prov_index()
    assert ds._prov_recent_no_match("Song") is False


def test_provenance_ttl_expiry(fake_dirs):
    ds.record_provenance("Old.mp3", "no_match")
    path = ds._prov_index_path()
    idx = ds._load_prov_index()
    stale = (datetime.now() - timedelta(days=ds._PROV_NO_MATCH_TTL_DAYS + 1)).isoformat(timespec="seconds")
    idx["Old"]["fetched_at"] = stale
    with open(path, "w", encoding="utf-8") as f:
        import json
        json.dump(idx, f)
    assert ds._prov_recent_no_match("Old") is False

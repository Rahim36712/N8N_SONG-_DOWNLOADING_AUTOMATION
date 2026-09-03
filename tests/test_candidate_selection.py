import os, sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import download_song as ds

def test_normalize_song_name():
    assert ds._normalize_song_name('Pighal Jaa (Official Music Video)') == 'pighal ja'
    assert ds._normalize_song_name('Artist - Song [Official Audio] feat. Someone') == 'artist - song'
    assert ds._normalize_song_name('Pighal Ja') == 'pighal ja'

def test_token_overlap_smart():
    # Urdu/Hindi transliteration variants
    assert ds._token_overlap_smart('pighal jaa', 'Pighal Ja') == 1.0
    assert ds._token_overlap_smart('pighal jaa', 'Rovalio & Abdul Hannan - Bikhra') == 0.0
    # Fuzzy phonetic match (muntasir ~ muntazir)
    assert ds._token_overlap_smart('Muntasir', 'Muntazir') == 1.0

def test_score_candidate_disqualifies_unrelated():
    c_bikhra = {
        'title': 'Rovalio & Abdul Hannan - Bikhra (Official Music Video)',
        'uploader': 'Rovalio',
        'duration': 168,
        'view_count': 92000000
    }
    c_qasmein = {
        'title': 'Qasmein (Official Audio) | Abdul Hannan | Maanu | Shahmeer Raza',
        'uploader': 'Shahmeer Raza Khan',
        'duration': 140,
        'view_count': 200000
    }
    c_pighal_ja = {
        'title': 'Abdul Hannan - Pighal Ja (Official Music Video) | Prod. by Shahmeer Raza',
        'uploader': 'Abdul Hannan',
        'duration': 172,
        'view_count': 470000
    }

    # Target: 'Pighal Ja'
    score_bikhra = ds.score_candidate(c_bikhra, target_title='Pighal Ja', target_artist='Abdul Hannan', expected_duration=134)
    score_qasmein = ds.score_candidate(c_qasmein, target_title='Pighal Ja', target_artist='Abdul Hannan', expected_duration=134)
    score_pighal = ds.score_candidate(c_pighal_ja, target_title='Pighal Ja', target_artist='Abdul Hannan', expected_duration=134)

    assert score_bikhra == -1000.0
    assert score_qasmein == -1000.0
    assert score_pighal > 75.0

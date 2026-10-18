"""API/engine regressions. All writes use temporary dirs; no provider traffic."""
import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as api
import download_song as ds


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    dirs = {k: str(tmp_path / v) for k, v in {
        'music': 'music', 'lyrics': 'music/.lyrics', 'archive': 'archive.txt',
        'log': 'log.csv', 'db': 'library.db'}.items()}
    os.makedirs(dirs['lyrics'])
    monkeypatch.setattr(ds, 'get_dirs', lambda: dirs)
    monkeypatch.setattr(ds, 'record_search', lambda *a: None)
    monkeypatch.setattr(ds, 'log_download', lambda *a: None)
    monkeypatch.setattr(ds.requests, 'get', lambda *a, **k: pytest.fail('Unexpected provider request'))
    monkeypatch.setattr(ds.syncedlyrics, 'search', lambda *a, **k: pytest.fail('Unexpected lyrics request'))
    with api.JOBS_LOCK:
        api.JOBS.clear()
    yield api.app.test_client(), dirs
    with api.JOBS_LOCK:
        api.JOBS.clear()


@pytest.mark.parametrize('payload', [[], [1], 42, {'query': 42}, {'query': ' '},
    {'query': 'https://127.0.0.1/secret'}, {'query': 'https://youtube.com:bad/watch'},
    {'query': 'x', 'format': 'exe'}, {'query': 'x', 'quality': '-1'}])
def test_download_input_returns_400(isolated, payload):
    client, _ = isolated
    assert client.post('/api/download', json=payload).status_code == 400


@pytest.mark.parametrize('tracks', [[7], [{'query': 7}], ['https://attacker.example/a'],
                                   [{'query': 'song', 'format': 'exe'}]])
def test_playlist_validation_is_atomic(isolated, tracks):
    client, _ = isolated
    assert client.post('/api/playlist/download', json={'tracks': tracks}).status_code == 400
    assert not api.JOBS


def test_local_host_and_origin_guards(isolated, monkeypatch):
    client, _ = isolated
    saved = MagicMock()
    monkeypatch.setattr(ds, 'save_sidecar_lyrics', saved)
    payload = {'filename': 'a.mp3', 'lyrics': '[00:00.00] example'}
    assert client.get('/', headers={'Host': 'attacker.example'}).status_code == 400
    assert client.post('/api/lyrics/save', json=payload, headers={'Origin': 'https://attacker.example'}).status_code == 403
    assert client.post('/api/lyrics/save', json=payload, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    saved.assert_not_called()
    assert client.post('/api/lyrics/save', json=payload, headers={'Origin': 'http://localhost'}).status_code == 200


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_cross_site_player_navigation_is_allowed(isolated, method):
    client, _ = isolated
    response = client.open('/', method=method, base_url='http://127.0.0.1:5000', headers={
        'Sec-Fetch-Site': 'cross-site',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Dest': 'document',
    })
    assert response.status_code == 200
    assert response.mimetype == 'text/html'


@pytest.mark.parametrize('path,method,extra_headers,expected', [
    ('/api/library', 'GET', {}, 403),
    ('/api/lyrics/save', 'POST', {}, 403),
    ('/', 'POST', {}, 403),
    ('/', 'GET', {'Sec-Fetch-Dest': 'iframe'}, 403),
    ('/', 'GET', {'Sec-Fetch-Mode': 'cors'}, 403),
    ('/', 'GET', {'Origin': 'https://attacker.example'}, 403),
    ('/', 'GET', {'Host': 'attacker.example'}, 400),
])
def test_player_navigation_exception_does_not_bypass_guards(isolated, path, method, extra_headers, expected):
    client, _ = isolated
    headers = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
               'Sec-Fetch-Dest': 'document'}
    headers.update(extra_headers)
    assert client.open(path, method=method, headers=headers).status_code == expected


def test_cross_site_player_fetch_without_navigation_headers_is_blocked(isolated):
    client, _ = isolated
    assert client.get('/', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_cancelled_queued_job_never_runs(isolated, monkeypatch):
    client, _ = isolated
    download = MagicMock(return_value={'status': 'success'})
    monkeypatch.setattr(ds, 'download_single', download)
    jid = api._new_job('song', 'mp3', '0')
    assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
    api._run_download_job(jid, 'song', 'mp3', '0')
    download.assert_not_called()
    assert api.JOBS[jid]['status'] == 'cancelled'


def test_full_queue_rejects_entire_playlist(isolated, monkeypatch):
    client, _ = isolated
    monkeypatch.setattr(api, 'JOB_LIMIT', 2)
    api._new_job('existing', 'mp3', '0')
    submit = MagicMock()
    monkeypatch.setattr(api, '_submit_job', submit)
    assert client.post('/api/playlist/download', json={'tracks': ['one', 'two']}).status_code == 429
    assert len(api.JOBS) == 1
    submit.assert_not_called()


@pytest.mark.parametrize('downloaded,partial', [(0, False), (1, True)])
def test_failed_playlist_job_is_not_done(isolated, monkeypatch, downloaded, partial):
    _, _dirs = isolated
    result = {'status': 'playlist_done', 'total': 2, 'downloaded': downloaded,
              'results': [{'status': 'error', 'error': 'private'}] + ([{'status': 'success'}] if downloaded else [{'status': 'error'}])}
    monkeypatch.setattr(ds, 'handle_yt_playlist', lambda *a, **k: result)
    jid = api._new_job('https://www.youtube.com/playlist?list=abc', 'mp3', '0')
    api._run_download_job(jid, 'https://www.youtube.com/playlist?list=abc', 'mp3', '0')
    assert api.JOBS[jid]['status'] == 'error'
    assert api.JOBS[jid]['result']['partial'] is partial


def test_cancel_propagates_to_playlist_engine(isolated, monkeypatch):
    client, _ = isolated
    jid = api._new_job('https://www.youtube.com/playlist?list=abc', 'mp3', '0')
    def download(*a, **kwargs):
        assert callable(kwargs['cancel_check'])
        client.post(f'/api/jobs/{jid}/cancel')
        ds._check_cancel(kwargs['cancel_check'])
    monkeypatch.setattr(ds, 'handle_yt_playlist', download)
    api._run_download_job(jid, 'https://www.youtube.com/playlist?list=abc', 'mp3', '0')
    assert api.JOBS[jid]['status'] == 'cancelled'


def test_sse_survives_trim_and_reconnect(isolated):
    client, _ = isolated
    jid = api._new_job('song', 'mp3', '0')
    for n in range(api.JOB_EVENT_CAP + 8):
        api._append_event(jid, {'status': 'downloading', 'percent': n % 100})
    api._update_job(jid, status='done', result={'status': 'success'}, progress=100)
    response = client.get(f'/api/jobs/{jid}/events', headers={'Last-Event-ID': '505'})
    body = response.get_data(as_text=True)
    assert 'id: 506\n' in body and 'id: 508\n' in body
    assert 'id: 505\n' not in body
    assert '"job_status": "done"' in body
    assert client.get('/api/jobs/missing/events').status_code == 404


def test_no_match_does_not_fall_back_to_first_result(monkeypatch):
    candidate = {'title': 'Completely unrelated', 'url': 'abcdefghijk', 'uploader': 'Other'}
    monkeypatch.setattr(ds.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(candidate)))
    assert ds.search_best_candidate('Baari', target_title='Baari', target_artist='Bilal') is None


def test_download_no_match_stops_before_extraction(isolated, monkeypatch):
    _, dirs = isolated
    monkeypatch.setattr(ds, 'lookup_itunes_enriched', lambda *a: None)
    monkeypatch.setattr(ds, 'search_best_candidate', lambda *a, **k: None)
    extract = MagicMock()
    monkeypatch.setattr(ds.yt_dlp, 'YoutubeDL', extract)
    result = ds.download_single('missing song', dirs['music'], dirs)
    assert result['status'] == 'error'
    extract.assert_not_called()


def test_fatal_video_error_is_attempted_once(isolated, monkeypatch):
    _, dirs = isolated
    mock = MagicMock()
    mock.__enter__.return_value.extract_info.side_effect = Exception('Video is private')
    monkeypatch.setattr(ds.yt_dlp, 'YoutubeDL', lambda *a: mock)
    result = ds.download_single('https://www.youtube.com/watch?v=abcdefghijk', dirs['music'], dirs)
    assert result['status'] == 'error'
    assert mock.__enter__.return_value.extract_info.call_count == 1


def test_shared_budget_limits_parallel_downloads(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(ds, '_DOWNLOAD_SLOTS', threading.BoundedSemaphore(2))
    active = peak = 0
    lock = threading.Lock()
    @ds._limited_download
    def worker():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.03)
        with lock:
            active -= 1
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: worker(), range(12)))
    assert peak == 2


def test_cancel_while_waiting_for_download_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(ds, '_DOWNLOAD_SLOTS', slots)
    calls = []
    @ds._limited_download
    def worker(**kwargs):
        calls.append(1)
    with pytest.raises(ds.yt_dlp.utils.DownloadCancelled):
        worker(cancel_check=lambda: True)
    slots.release()
    assert not calls


def test_only_audio_library_files_can_be_streamed(isolated):
    client, dirs = isolated
    path = os.path.join(dirs['music'], 'secret.json')
    with open(path, 'w') as stream:
        stream.write('{}')
    assert client.get('/api/stream/secret.json').status_code == 400
    assert client.get('/api/stream/.lyrics/index.json').status_code == 400


def test_playlist_rejects_outside_library_paths(isolated):
    _, dirs = isolated
    with pytest.raises(ValueError):
        ds.save_playlist(dirs, 'example', ['../outside.mp3'])


def test_format_labels_describe_conversion(monkeypatch):
    mock = MagicMock()
    mock.__enter__.return_value.extract_info.return_value = {
        'title': 'Example', 'duration': 120, 'formats': [{'acodec': 'opus', 'abr': 160, 'asr': 48000}]}
    monkeypatch.setattr(ds.yt_dlp, 'YoutubeDL', lambda *a: mock)
    result = ds._inspect_track_formats('https://www.youtube.com/watch?v=abcdefghijk')
    labels = json.dumps(result['formats'])
    assert 'Studio Master' not in labels and 'Bit-perfect' not in labels
    assert result['default_format'] == 'mp3'


def test_unknown_bitrate_is_not_invented(monkeypatch):
    mock = MagicMock()
    mock.__enter__.return_value.extract_info.return_value = {'title': 'Example', 'formats': []}
    monkeypatch.setattr(ds.yt_dlp, 'YoutubeDL', lambda *a: mock)
    result = ds._inspect_track_formats('https://www.youtube.com/watch?v=abcdefghijk')
    assert result['max_abr'] is None
    assert result['max_asr'] is None


def test_cancelled_search_does_not_launch_subprocess(monkeypatch):
    launch = MagicMock()
    monkeypatch.setattr(ds.subprocess, 'Popen', launch)
    with pytest.raises(ds.yt_dlp.utils.DownloadCancelled):
        ds.search_best_candidate('song', cancel_check=lambda: True)
    launch.assert_not_called()


def test_spotify_fallback_preserves_async_cancellation(isolated, monkeypatch):
    _, dirs = isolated
    monkeypatch.setattr(ds, 'get_spotify_tracks', MagicMock(side_effect=ValueError('metadata unavailable')))
    fallback = MagicMock(return_value={'status': 'error', 'error': 'fixture'})
    monkeypatch.setattr(ds, '_handle_spotify_legacy', fallback)
    check = lambda: False
    ds.handle_spotify('https://open.spotify.com/track/example', dirs['music'], dirs, cancel_check=check)
    assert fallback.call_args.kwargs['cancel_check'] is check


def test_cancellable_command_stops_owned_process(monkeypatch):
    process = MagicMock()
    process.communicate.side_effect = [ds.subprocess.TimeoutExpired('fixture', .25), ('', '')]
    process.poll.return_value = None
    monkeypatch.setattr(ds.subprocess, 'Popen', lambda *a, **k: process)
    kill_tree = MagicMock()
    monkeypatch.setattr(ds.subprocess, 'run', kill_tree)
    checks = iter([False, False, True])
    with pytest.raises(ds.yt_dlp.utils.DownloadCancelled):
        ds._run_cancellable_command(['fixture'], 10, lambda: next(checks))
    process.kill.assert_called_once()

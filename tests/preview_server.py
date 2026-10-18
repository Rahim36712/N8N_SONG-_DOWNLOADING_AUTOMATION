"""Isolated browser QA fixture: python tests/preview_server.py (port 5057).

No real music, archive, credentials, or providers are accessed. Ctrl+C cleans up.
"""
import os
import sys
import tempfile
import time
import wave
import threading
from flask import render_template, request
from werkzeug.serving import make_server
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import download_song as ds


def main():
    with tempfile.TemporaryDirectory(prefix='auradisc-preview-') as folder:
        root = Path(folder)
        music = root / 'music'
        music.mkdir()
        (music / '.lyrics').mkdir()
        dirs = {'music': str(music), 'lyrics': str(music / '.lyrics'),
                'archive': str(root / 'archive.txt'), 'log': str(root / 'log.csv'), 'db': str(root / 'library.db')}
        library = []
        def add_track(filename, title, artist):
            with wave.open(str(music / filename), 'wb') as audio:
                audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                audio.writeframes(bytes(8000 * 2 * 12))
            library.insert(0, {'filename': filename, 'title': title, 'artist': artist,
                'format': 'WAV', 'size_mb': .18, 'duration_sec': 12, 'has_lyrics': True})
        add_track('After Hours.wav', 'After Hours', 'The Olive Quartet')
        add_track("Artist's Record.wav", "Artist's Record", 'Paper Sleeve')
        add_track('Plain Text.wav', 'Plain Text' if '--clean' in sys.argv else '<b data-audit="title">Plain Text</b>', 'Ampersand & Friends')
        ds.get_dirs = lambda: dirs
        ds.get_music_library_indexed = lambda _: list(library)
        ds.log_download = lambda *a: None
        ds.record_search = lambda *a: None
        ds.record_play = lambda *a: True
        ds.get_all_sidecar_lyrics = lambda _: {'english': '[00:00.00] <b data-audit="lyric">Plain lyric</b>\n[00:03.00] The record keeps turning\n[00:06.00] Another evening at home'}
        app.app.view_functions['api_artwork'] = lambda filename: ('', 404)
        def preview_index():
            html = render_template('index.html')
            if request.args.get('figma') == '1':
                html = html.replace('</head>', '<script src="https://mcp.figma.com/mcp/html-to-design/capture.js" async></script></head>')
            return html
        app.app.view_functions['index'] = preview_index
        ds.lookup_itunes = lambda *a: None
        def preview(query):
            return {'status': 'success', 'title': 'Evening Signal', 'artist': 'The Olive Quartet',
                'url': 'https://www.youtube.com/watch?v=' + ('canceltest1' if 'cancel' in query else 'failfail123' if 'fail' in query else 'abcdefghijk'), 'source': 'youtube', 'duration_fmt': '00:12',
                'max_abr': 160, 'default_format': 'mp3', 'formats': [
                    {'id': 'mp3', 'name': 'MP3', 'sub': 'High-quality VBR', 'badge': 'Compatible', 'available': True, 'est_size': '~1 MB'},
                    {'id': 'flac', 'name': 'FLAC', 'sub': 'Source may be lossy', 'badge': 'Converted FLAC', 'available': True, 'est_size': '~3 MB'}]}
        ds.get_track_preview = preview
        ds.get_playlist_preview = lambda _: {'status': 'success', 'source': 'youtube', 'type': 'playlist',
            'title': 'Paper Sleeve Collection', 'total': 2, 'tracks': [
                {'title': "Artist's Record", 'artist': 'Paper & Sleeve', 'duration_fmt': '00:12'},
                {'title': 'After Hours', 'artist': 'The Olive Quartet', 'duration_fmt': '00:12'}]}
        def download(query, *args, progress_cb=None, cancel_check=None, **kwargs):
            for n in range(25):
                ds._check_cancel(cancel_check)
                if progress_cb:
                    progress_cb({'status': 'downloading', 'percent': n * 4})
                time.sleep(.8 if 'cancel' in query else .15)
            if 'fail' in query:
                return {'status': 'error', 'query': query, 'error': 'Fixture extraction failed; retry is available.'}
            if not any(t['filename'] == 'Evening Signal.wav' for t in library):
                add_track('Evening Signal.wav', 'Evening Signal', 'The Olive Quartet')
            return {'status': 'success', 'filename': 'Evening Signal.wav', 'title': 'Evening Signal', 'uploader': 'The Olive Quartet'}
        ds.download_single = download
        print('Synthetic fixture library only. All provider calls are replaced.', flush=True)
        server = make_server('127.0.0.1', 5057, app.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print('Preview: http://127.0.0.1:5057. Enter quit to stop and clean fixtures.', flush=True)
        try:
            while input().strip().lower() != 'quit':
                pass
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            server.shutdown()
            app.JOB_POOL.shutdown(wait=True, cancel_futures=True)
            server.server_close()


if __name__ == '__main__':
    main()

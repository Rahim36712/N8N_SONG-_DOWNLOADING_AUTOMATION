from flask import Flask, request, jsonify, render_template, send_from_directory, Response, stream_with_context
import os
import json
import time
import uuid
import threading
import subprocess
import traceback
import concurrent.futures
from datetime import datetime
import download_song
from yt_dlp.utils import DownloadCancelled

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Shared worker pools (C6: sizes from config.json)
_WORKERS = int(download_song.load_config().get("max_workers") or 4)
BATCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS)

# B1: async job queue
JOB_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS)
JOBS = {}                        # job_id -> job dict
JOBS_LOCK = threading.RLock()
JOB_EVENT_CAP = 500              # max buffered SSE events per job
JOB_TTL = 2 * 60 * 60            # finished jobs pruned after 2h


def _new_job(query, audio_format, audio_quality):
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        now = time.time()
        for jid in [jid for jid, j in JOBS.items()
                    if j["status"] in ("done", "error", "cancelled") and now - j["finished_at"] > JOB_TTL]:
            del JOBS[jid]
        JOBS[job_id] = {
            "id": job_id, "query": query, "format": audio_format,
            "status": "queued", "stage": "", "progress": 0,
            "result": None, "events": [],
            "created_at": now, "finished_at": 0.0, "cancel": False,
        }
    return job_id


def _get_job(job_id):
    with JOBS_LOCK:
        return JOBS.get(job_id)


def _update_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        if "status" in fields and fields["status"] in ("done", "error", "cancelled"):
            fields["finished_at"] = time.time()
        job.update(fields)
        return job


def _append_event(job_id, event):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        events = job["events"]
        events.append(event)
        if len(events) > JOB_EVENT_CAP:
            del events[:len(events) - JOB_EVENT_CAP]


def _run_download_job(job_id, query, audio_format, audio_quality):
    _update_job(job_id, status="running", stage="starting")
    dirs = download_song.get_dirs()
    os.makedirs(dirs["music"], exist_ok=True)

    def progress_cb(ev):
        ev = dict(ev)
        if ev.get("status") == "downloading":
            _update_job(job_id, stage="downloading", progress=ev.get("percent") or 0)
        elif ev.get("status") == "finished":
            _update_job(job_id, stage="processing", progress=95)
        _append_event(job_id, {"t": time.time(), **ev})

    def cancel_check():
        job = _get_job(job_id)
        return job is None or job.get("cancel")

    try:
        if download_song.is_yt_playlist(query):
            out = download_song.handle_yt_playlist(query, dirs["music"], dirs, audio_format, audio_quality)
        elif download_song.is_spotify(query):
            out = download_song.handle_spotify(query, dirs["music"], dirs, audio_format, audio_quality)
        else:
            out = download_song.download_single(query, dirs["music"], dirs, audio_format, audio_quality,
                                                progress_cb=progress_cb, cancel_check=cancel_check)
            download_song.log_download(dirs["log"], out)

        if out.get("status") == "error":
            _update_job(job_id, status="error", stage="failed", result=out, progress=100)
        else:
            _update_job(job_id, status="done", stage="complete", result=out, progress=100)
        _append_event(job_id, {"t": time.time(), "status": "job_end", "result_status": out.get("status")})
    except DownloadCancelled:
        _update_job(job_id, status="cancelled", stage="cancelled")
        _append_event(job_id, {"t": time.time(), "status": "cancelled"})
    except Exception as e:
        _update_job(job_id, status="error", stage="failed",
                    result={"status": "error", "query": query, "error": str(e),
                            "trace": traceback.format_exc()})
        _append_event(job_id, {"t": time.time(), "status": "job_end", "result_status": "error"})

@app.after_request
def add_header(response):
    if request.endpoint == 'api_stream' or (request.path and request.path.startswith('/api/stream/')):
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers.pop('Pragma', None)
        response.headers.pop('Expires', None)
    else:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json() or {}
    query = data.get("query")
    audio_format = data.get("format", "mp3")
    audio_quality = data.get("quality", "0")

    if not query:
        return jsonify({"status": "error", "error": "No query provided"}), 400

    # C2: search history (fire-and-forget)
    try:
        download_song.record_search(download_song.get_dirs(), str(query))
    except Exception:
        pass

    # B1: async mode returns a job_id immediately; default stays synchronous (n8n compatible)
    if data.get("mode") == "async":
        job_id = _new_job(query, audio_format, audio_quality)
        JOB_POOL.submit(_run_download_job, job_id, query, audio_format, audio_quality)
        return jsonify({"status": "queued", "job_id": job_id}), 202

    dirs = download_song.get_dirs()
    out_dir = dirs["music"]
    os.makedirs(out_dir, exist_ok=True)

    try:
        if download_song.is_yt_playlist(query):
            out = download_song.handle_yt_playlist(query, out_dir, dirs, audio_format, audio_quality)
        elif download_song.is_spotify(query):
            out = download_song.handle_spotify(query, out_dir, dirs, audio_format, audio_quality)
        else:
            out = download_song.download_single(query, out_dir, dirs, audio_format, audio_quality)
            download_song.log_download(dirs["log"], out)

        return jsonify(out)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/api/jobs", methods=["GET"])
def api_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)
        return jsonify({"status": "success", "jobs": [
            {k: j[k] for k in ("id", "query", "status", "stage", "progress",
                               "format", "created_at")} for j in jobs
        ]})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job_status(job_id):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"status": "error", "error": "Job not found"}), 404
    with JOBS_LOCK:
        return jsonify({"status": "success", "job": {
            k: job[k] for k in ("id", "query", "status", "stage", "progress",
                                "result", "format", "created_at")
        }})


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"status": "error", "error": "Job not found"}), 404
    if job["status"] == "queued":
        _update_job(job_id, status="cancelled")
        return jsonify({"status": "success", "message": "Queued job cancelled"})
    if job["status"] == "running":
        _update_job(job_id, cancel=True)
        return jsonify({"status": "success", "message": "Cancellation requested"})
    return jsonify({"status": "error", "error": f"Job already {job['status']}"}), 400


@app.route("/api/jobs/<job_id>/events", methods=["GET"])
def api_job_events(job_id):
    """SSE progress stream: replays buffered events, then streams until terminal state."""
    def generate():
        cursor = 0
        while True:
            job = _get_job(job_id)
            if job is None:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                return
            while cursor < len(job["events"]):
                yield f"data: {json.dumps(job['events'][cursor])}\n\n"
                cursor += 1
            if job["status"] in ("done", "error", "cancelled"):
                yield f"data: {json.dumps({'status': 'job_end', 'job_status': job['status'], 'progress': job['progress']})}\n\n"
                return
            time.sleep(0.5)

    resp = Response(stream_with_context(generate()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/batch_download", methods=["POST"])
def api_batch_download():
    data = request.get_json() or {}
    queries = data.get("queries", [])
    audio_format = data.get("format", "mp3")
    audio_quality = data.get("quality", "0")

    if not queries or not isinstance(queries, list):
        return jsonify({"status": "error", "error": "No queries list provided"}), 400

    dirs = download_song.get_dirs()
    out_dir = dirs["music"]
    os.makedirs(out_dir, exist_ok=True)

    queries = [str(q).strip() for q in queries if str(q).strip()]
    results = [None] * len(queries)

    def _process(idx, q_str):
        try:
            if download_song.is_yt_playlist(q_str):
                return idx, download_song.handle_yt_playlist(q_str, out_dir, dirs, audio_format, audio_quality)
            elif download_song.is_spotify(q_str):
                return idx, download_song.handle_spotify(q_str, out_dir, dirs, audio_format, audio_quality)
            else:
                res = download_song.download_single(q_str, out_dir, dirs, audio_format, audio_quality)
                download_song.log_download(dirs["log"], res)
                return idx, res
        except Exception as e:
            return idx, {"status": "error", "query": q_str, "error": str(e)}

    futures = [BATCH_EXECUTOR.submit(_process, i, q) for i, q in enumerate(queries)]
    for fut in concurrent.futures.as_completed(futures):
        idx, res = fut.result()
        results[idx] = res

    return jsonify({"status": "batch_done", "results": results})

@app.route("/api/library", methods=["GET"])
def api_library():
    dirs = download_song.get_dirs()
    lib = download_song.get_music_library_indexed(dirs)
    return jsonify({"status": "success", "library": lib})


@app.route("/api/plays", methods=["POST"])
def api_record_play():
    data = request.get_json() or {}
    filename = data.get("filename")
    if not filename:
        return jsonify({"status": "error", "error": "Filename missing"}), 400
    dirs = download_song.get_dirs()
    ok = download_song.record_play(dirs, os.path.basename(filename))
    return jsonify({"status": "success" if ok else "error"})


@app.route("/api/history", methods=["GET", "POST"])
def api_history():
    dirs = download_song.get_dirs()
    if request.method == "POST":
        data = request.get_json() or {}
        query = (data.get("query") or "").strip()
        if query:
            download_song.record_search(dirs, query)
        return jsonify({"status": "success"})
    return jsonify({"status": "success", "history": download_song.get_search_history(dirs)})

@app.route("/api/stream/<path:filename>", methods=["GET"])
def api_stream(filename):
    dirs = download_song.get_dirs()
    music_dir = dirs["music"]
    return send_from_directory(music_dir, filename)

@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json() or {}
    filename = data.get("filename")
    if not filename:
        return jsonify({"status": "error", "error": "Filename missing"}), 400

    dirs = download_song.get_dirs()
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename in (".", "..") or safe_filename != filename:
        return jsonify({"status": "error", "error": "Invalid filename"}), 400

    music_dir = os.path.realpath(dirs["music"])
    target_path = os.path.realpath(os.path.join(music_dir, safe_filename))
    if not target_path.startswith(music_dir + os.sep):
        return jsonify({"status": "error", "error": "Invalid filename"}), 400

    if os.path.exists(target_path):
        try:
            os.remove(target_path)
            return jsonify({"status": "success", "message": f"Deleted {filename}"})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500
    else:
        return jsonify({"status": "error", "error": "File not found"}), 404

@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    dirs = download_song.get_dirs()
    folder = dirs["music"]
    os.makedirs(folder, exist_ok=True)
    try:
        if os.name == 'nt':
            os.startfile(folder)
        else:
            subprocess.Popen(['xdg-open', folder])
        return jsonify({"status": "success", "message": f"Opened {folder}"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/lyrics/generate", methods=["POST"])
def api_lyrics_generate():
    """C4: generate roman (offline) and optionally english (translation) sidecars."""
    data = request.get_json() or {}
    filename = data.get("filename")
    translate = bool(data.get("translate", False))
    if not filename:
        return jsonify({"status": "error", "error": "Filename required"}), 400
    res = download_song.generate_lyrics_sidecars(os.path.basename(filename), translate=translate)
    return jsonify(res)


@app.route("/api/playlists", methods=["GET", "POST"])
def api_playlists():
    dirs = download_song.get_dirs()
    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("name")
        tracks = data.get("tracks") or []
        if not name or not isinstance(tracks, list):
            return jsonify({"status": "error", "error": "Name and tracks list required"}), 400
        try:
            out = download_song.save_playlist(dirs, name, tracks)
            return jsonify({"status": "success", "playlist": out})
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 400
    return jsonify({"status": "success", "playlists": download_song.list_playlists(dirs)})


@app.route("/api/playlists/<name>", methods=["GET", "DELETE"])
def api_playlist(name):
    dirs = download_song.get_dirs()
    if request.method == "DELETE":
        ok = download_song.delete_playlist(dirs, name)
        return jsonify({"status": "success" if ok else "error",
                        "message": f"Deleted {name}" if ok else "Playlist not found"})
    pl = download_song.get_playlist(dirs, name)
    if pl is None:
        return jsonify({"status": "error", "error": "Playlist not found"}), 404
    return jsonify({"status": "success", "playlist": pl})


@app.route("/api/playlists/<name>/export.m3u", methods=["GET"])
def api_playlist_export(name):
    dirs = download_song.get_dirs()
    content = download_song.export_m3u(dirs, name)
    if content is None:
        return jsonify({"status": "error", "error": "Playlist not found"}), 404
    safe = download_song._safe_playlist_name(name)
    return Response(content, mimetype="audio/x-mpegurl",
                    headers={"Content-Disposition": f"attachment; filename={safe}.m3u"})


@app.route("/api/watch/sync", methods=["POST"])
def api_watch_sync():
    """C5: Spotify playlist watcher — downloads only tracks not already in the archive.
    Designed for n8n cron polling; returns a compact summary."""
    data = request.get_json() or {}
    url = data.get("playlist_url") or data.get("url")
    if not url:
        return jsonify({"status": "error", "error": "playlist_url required"}), 400
    if not download_song.is_spotify(url):
        return jsonify({"status": "error", "error": "Not a Spotify URL"}), 400

    dirs = download_song.get_dirs()
    os.makedirs(dirs["music"], exist_ok=True)
    out = download_song.handle_spotify(url, dirs["music"], dirs)

    if out.get("status") != "playlist_done":
        return jsonify({"status": "error", "error": out.get("error", "Sync failed")})

    new_tracks = [{"title": r.get("title"), "uploader": r.get("uploader")}
                  for r in out.get("results", []) if r.get("status") == "success"]
    return jsonify({
        "status": "success",
        "watch": {
            "total": out.get("total", 0),
            "downloaded": out.get("downloaded", 0),
            "already_present": out.get("skipped", 0),
            "new_tracks": new_tracks,
        },
    })


@app.route("/api/system_info", methods=["GET"])
def api_system_info():
    dirs = download_song.get_dirs()
    diag = download_song.get_system_diagnostics(dirs)
    return jsonify({"status": "success", "diagnostics": diag})

@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json() or {}
    query = data.get("query")
    if not query:
        return jsonify({"status": "error", "error": "No query provided"}), 400
    preview = download_song.get_track_preview(query)
    return jsonify(preview)

@app.route("/api/lyrics", methods=["GET"])
def api_get_lyrics():
    filename = request.args.get("filename") or request.args.get("query")
    refresh_param = request.args.get("refresh", "").lower() in ["true", "1", "yes"]

    if not filename:
        return jsonify({"status": "error", "error": "Filename or query parameter required"}), 400

    safe_filename = os.path.basename(filename)
    stem = download_song._safe_stem(safe_filename)
    dirs = download_song.get_dirs()
    lyrics_dir = dirs["lyrics"]

    if refresh_param:
        for ext in [".lrc", ".ur.lrc", ".urdu.lrc", ".roman.lrc", ".en.lrc", ".english.lrc", ".txt"]:
            target_f = os.path.join(lyrics_dir, f"{stem}{ext}")
            if os.path.exists(target_f):
                try:
                    os.remove(target_f)
                except Exception:
                    pass
        download_song.clear_provenance(safe_filename)

    # get_all_sidecar_lyrics fetches + persists missing sidecars (respecting the
    # no-match negative cache); an empty result means "no confident match".
    sidecars = download_song.get_all_sidecar_lyrics(safe_filename)

    if not sidecars:
        return jsonify({
            "status": "success",
            "synced": False,
            "lyrics": None,
            "has_dual": False,
            "languages": [],
            "lyrics_by_lang": {}
        })

    # Primary content
    primary_lang = list(sidecars.keys())[0]
    primary_content = sidecars[primary_lang]
    synced = download_song.is_lrc_synced(primary_content)
    has_dual = "dual" in sidecars or ("urdu" in sidecars and ("roman" in sidecars or "english" in sidecars))

    return jsonify({
        "status": "success",
        "synced": synced,
        "lyrics": primary_content,
        "has_dual": has_dual,
        "languages": list(sidecars.keys()),
        "lyrics_by_lang": sidecars
    })


@app.route("/api/lyrics/refresh", methods=["POST"])
def api_lyrics_refresh():
    data = request.get_json(silent=True) or {}
    purge_all = bool(data.get("purge_all", False))
    dirs = download_song.get_dirs()

    if purge_all:
        # Full wipe + rebuild can take minutes (network fetch per track) — run in background
        def _rebuild_job():
            try:
                res = download_song.purge_and_reindex_lyrics(dirs, purge_all=True)
                print(f"[lyrics-rebuild] finished: {res}", flush=True)
            except Exception as e:
                print(f"[lyrics-rebuild] failed: {e}", flush=True)
        threading.Thread(target=_rebuild_job, daemon=True).start()
        return jsonify({"status": "started",
                        "message": "Full lyrics rebuild started in background"})

    res = download_song.purge_and_reindex_lyrics(dirs, purge_all=False)
    return jsonify(res)


@app.route("/api/lyrics/save", methods=["POST"])
def api_save_lyrics():
    data = request.get_json() or {}
    filename = data.get("filename")
    lyrics = data.get("lyrics")

    if not filename or lyrics is None:
        return jsonify({"status": "error", "error": "Filename and lyrics required"}), 400

    safe_filename = os.path.basename(filename)
    download_song.save_sidecar_lyrics(safe_filename, lyrics)
    synced = download_song.is_lrc_synced(lyrics)

    return jsonify({
        "status": "success",
        "synced": synced,
        "message": "Lyrics saved successfully"
    })


if __name__ == "__main__":
    print("Starting Auto Downloader Server on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)



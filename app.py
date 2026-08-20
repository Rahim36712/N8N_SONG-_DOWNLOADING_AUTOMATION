from flask import Flask, request, jsonify, render_template, send_from_directory
import os
import subprocess
import traceback
import download_song

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

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

    results = []
    for q in queries:
        q_str = str(q).strip()
        if not q_str:
            continue
        try:
            if download_song.is_yt_playlist(q_str):
                res = download_song.handle_yt_playlist(q_str, out_dir, dirs, audio_format, audio_quality)
            elif download_song.is_spotify(q_str):
                res = download_song.handle_spotify(q_str, out_dir, dirs, audio_format, audio_quality)
            else:
                res = download_song.download_single(q_str, out_dir, dirs, audio_format, audio_quality)
                download_song.log_download(dirs["log"], res)
            results.append(res)
        except Exception as e:
            results.append({"status": "error", "query": q_str, "error": str(e)})

    return jsonify({"status": "batch_done", "results": results})

@app.route("/api/library", methods=["GET"])
def api_library():
    dirs = download_song.get_dirs()
    lib = download_song.get_music_library(dirs)
    return jsonify({"status": "success", "library": lib})

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
    target_path = os.path.join(dirs["music"], filename)
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
        for ext in [".lrc", ".ur.lrc", ".urdu.lrc", ".roman.lrc", ".txt"]:
            target_f = os.path.join(lyrics_dir, f"{stem}{ext}")
            if os.path.exists(target_f):
                try:
                    os.remove(target_f)
                except Exception:
                    pass

    sidecars = download_song.get_all_sidecar_lyrics(safe_filename)

    if not sidecars:
        # Fetch via cascade if missing
        result = download_song.fetch_lyrics_multi_provider(safe_filename)
        lyrics = result.get("lyrics") if isinstance(result, dict) else result
        if lyrics:
            download_song.save_sidecar_lyrics(safe_filename, lyrics)
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
    res = download_song.purge_and_reindex_lyrics(dirs, purge_all=purge_all)
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



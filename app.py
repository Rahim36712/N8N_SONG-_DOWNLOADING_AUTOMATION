from flask import Flask, request, jsonify, render_template
import os
import download_song

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json()
    query = data.get("query")
    audio_format = data.get("format", "mp3")
    audio_quality = data.get("quality", "0")
    
    if not query:
        return jsonify({"status": "error", "error": "No query provided"}), 400
        
    dirs = download_song.get_dirs()
    out_dir = dirs["music"]
    os.makedirs(out_dir, exist_ok=True)
    
    try:
        # Determine the type of query and route to existing logic
        if download_song.is_yt_playlist(query):
            out = download_song.handle_yt_playlist(query, out_dir, dirs, audio_format, audio_quality)
        elif download_song.is_spotify(query):
            out = download_song.handle_spotify(query, out_dir, dirs, audio_format, audio_quality)
        else:
            out = download_song.download_single(query, out_dir, dirs, audio_format, audio_quality)
            download_song.log_download(dirs["log"], out)
            
        return jsonify(out)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == "__main__":
    # Run the API on port 5000
    print("Starting Downloader API / GUI on http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)

# 🎵 Auto Song Downloader Pro & Music Hub

<div align="center">

![Cool Yellow UI Banner](https://img.shields.io/badge/Design_Theme-Cool_Yellow_%26_Off--White-FACC15?style=for-the-badge&logoColor=0f172a)
[![Python 3](https://img.shields.io/badge/Language-Python%203.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org/)
[![Flask API](https://img.shields.io/badge/Framework-Flask-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![yt-dlp](https://img.shields.io/badge/Engine-yt--dlp-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://github.com/yt-dlp/yt-dlp)
[![spotdl](https://img.shields.io/badge/Spotify-spotdl-1DB954?style=for-the-badge&logo=spotify&logoColor=white)](https://github.com/spotdl/spotify-downloader)
[![n8n Orchestrator](https://img.shields.io/badge/Orchestrator-n8n-FF6F61?style=for-the-badge&logo=n8n&logoColor=white)](https://n8n.io/)
[![Windows 11](https://img.shields.io/badge/OS-Windows%2011-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://microsoft.com/)

**An all-in-one, high-performance automated music downloader, local audio streaming player, library manager, and n8n automation pipeline.**

[Features](#-key-features) • [1-Click Launch](#-1-click-desktop-launch) • [Architecture](#-system-architecture) • [API Reference](#-api-reference) • [Setup](#-installation--setup)

</div>

---

## ✨ Key Features

### 1. 🖥️ 1-Click Desktop Launcher
- **Instant Browser Access**: Double-click the **Auto Song Downloader** icon generated directly on your Desktop (`Auto Song Downloader.lnk`).
- **Silent Background Execution**: Runs silently via [`Start_Silent.vbs`](file:///d:/AI%20STUFF/PROJECTS/automated%20song%20downloader/Start_Silent.vbs) without leaving open terminal windows, automatically launching your default web browser to `http://127.0.0.1:5000`.

### 2. 🎨 Cool Yellow & Off-White Design System
- **State-of-the-Art Aesthetic**: Built with a **Sonic Yellow (`#FACC15`)** and **Off-White (`#FAF9F5`)** color palette.
- **Glassmorphic Cards**: Pure white (`#FFFFFF`) containers with crisp border strokes, soft multi-layered shadows, and smooth micro-interactions.

### 3. 🎧 In-Browser Audio Streaming & Player Widget
- **Play Anywhere**: Listen to downloaded songs directly inside your browser without opening third-party media players.
- **Floating Vinyl Bar**: Interactive audio bar complete with spinning vinyl disc animation, seek bar, time display, volume controls, and a 1-click **Close `(X)` Button** to stop playback and hide the player.

### 4. 🎵 Music Library & Explorer Manager
- **Live Search & Filter**: Instantly search your library by song title, artist, or file format.
- **File Metadata & Tags**: Displays format tags (`MP3`, `FLAC`, `M4A`, `WAV`), file size in MB, and download dates.
- **Open Downloads Folder Button**: Opens your local music destination (`C:\Users\RJ\Music\AutoDownloads`) directly in Windows File Explorer with one click.
- **1-Click Track Deletion**: Cleanly delete tracks from disk directly from the Web UI.

### 5. ⚡ Multi-Source & Multi-Line Batch Queue
- **Universal Input Support**: Handles YouTube video URLs, YouTube playlists, Spotify tracks/playlists, YouTube Music links, and plain text song search queries.
- **Batch Queue Downloader**: Paste multiple URLs or titles line-by-line in the text area to process batch downloads concurrently.
- **Concurrent Processing**: Multithreaded execution using Python `ThreadPoolExecutor` for fast multi-track downloading.

### 6. 📊 System Diagnostics Dashboard
- **Health Diagnostics Tab**: Real-time status reporting for Python, yt-dlp, FFmpeg audio encoder, spotdl, total download count, and disk space usage.

### 7. 🛡️ Duplicate Prevention & CSV Logging
- **Smart Archive**: Uses a persistent archive file (`.downloaded.txt`) to avoid downloading identical songs twice.
- **Transparent CSV Logs**: Appends every completed task to `download_log.csv` with timestamps, file size, artist name, and file path.

---

## 🛠️ System Architecture

```mermaid
flowchart TD
    subgraph Client ["Client Interfaces"]
        A1["🖥️ Desktop Shortcut\n(Auto Song Downloader.lnk)"] --> B1["🤫 Start_Silent.vbs\n(Silent Launcher)"]
        A2["🌐 Web Browser\n(http://127.0.0.1:5000)"]
        A3["🤖 n8n Workflow Form\n(Webhook Orchestrator)"]
    end

    subgraph Backend ["Python & Flask Backend"]
        B1 --> C1["⚡ app.py Server\n(Flask Web App & REST APIs)"]
        A2 <--> C1
        A3 <--> C2["⚙️ download_song.py\n(yt-dlp + spotdl Engine)"]
        C1 <--> C2
    end

    subgraph Storage ["Local Storage & Engines"]
        C2 --> D1["🎵 Music Library\n(C:\\Users\\RJ\\Music\\AutoDownloads)"]
        C2 --> D2["🛡️ Download Archive\n(.downloaded.txt)"]
        C2 --> D3["📊 Download Log\n(download_log.csv)"]
    end

    style A1 fill:#FACC15,stroke:#0F172A,stroke-width:2px,color:#0F172A
    style B1 fill:#F59E0B,stroke:#0F172A,stroke-width:2px,color:#0F172A
    style C1 fill:#6366F1,stroke:#fff,stroke-width:2px,color:#fff
    style C2 fill:#3B82F6,stroke:#fff,stroke-width:2px,color:#fff
    style D1 fill:#10B981,stroke:#fff,stroke-width:2px,color:#fff
```

---

## 🚀 1-Click Desktop Launch

Simply double-click the **Auto Song Downloader** icon on your Desktop!

- **Target Script**: [`Start_Silent.vbs`](file:///d:/AI%20STUFF/PROJECTS/automated%20song%20downloader/Start_Silent.vbs)
- **Batch Launcher**: [`Start Downloader.bat`](file:///d:/AI%20STUFF/PROJECTS/automated%20song%20downloader/Start%20Downloader.bat)
- **Re-create Desktop Shortcut**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File "Create_Desktop_Shortcut.ps1"
  ```

---

## 📡 API Reference

The Flask backend provides RESTful JSON endpoints for external integration:

| Method | Endpoint | Description | Payload / Response |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/download` | Single track or playlist download | `{ "query": "Song or URL", "format": "mp3", "quality": "0" }` |
| `POST` | `/api/batch_download` | Process multi-line batch download queue | `{ "queries": ["Song 1", "Song 2"], "format": "mp3" }` |
| `GET` | `/api/library` | List all downloaded music files with metadata | `{ "status": "success", "library": [...] }` |
| `GET` | `/api/stream/<filename>` | Stream audio file directly to browser | HTML5 Audio Stream (`audio/mpeg`, etc.) |
| `POST` | `/api/open_folder` | Opens `AutoDownloads` in Windows File Explorer | `{ "status": "success", "message": "Opened folder" }` |
| `GET` | `/api/system_info` | Get system status for Python, yt-dlp, FFmpeg | `{ "diagnostics": { "ffmpeg": "...", "total_files": 16 } }` |
| `POST` | `/api/delete` | Delete a music file from disk | `{ "filename": "Song.mp3" }` |

---

## ⚙️ Installation & Setup

### 1. Automated Setup Script
Run PowerShell as **Administrator** inside this project directory:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```
This script automatically:
- Checks Python and installs required packages (`yt-dlp`, `spotdl`, `flask`).
- Checks Node.js and installs `n8n` globally.
- Installs `FFmpeg` via `winget`.
- Configures environment variables and output directories.

### 2. Manual Server Startup
```bash
python app.py
```
Open `http://127.0.0.1:5000` in your web browser.

---

## 📁 Repository Structure

```
AUTOMATION-SONG/
├── app.py                      # Flask Server, REST APIs, & Streaming Engine
├── download_song.py            # Core Media Extraction Engine (yt-dlp + spotdl)
├── Create_Desktop_Shortcut.ps1 # PowerShell Desktop Shortcut Generator
├── Start_Silent.vbs            # Silent Background Window Launcher
├── Start Downloader.bat        # Command Launcher Script
├── setup.ps1                   # Automated Setup & Dependency Installer
├── n8n_workflow.json           # n8n Orchestrator Workflow import file
├── README.md                   # Application Documentation
└── templates/
    └── index.html              # Cool Yellow & Off-White Frontend Interface
```

---

## 📝 Technologies Used
* **Frontend**: HTML5, CSS3 Vanilla Glassmorphism, JavaScript ES6+
* **Backend API**: Python 3.14, Flask
* **Media Extraction**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) & [spotdl](https://github.com/spotdl/spotify-downloader)
* **Audio Encoder**: FFmpeg
* **Automation Orchestrator**: [n8n](https://n8n.io/)
* **Operating System**: Windows 11

---

<div align="center">
  <sub>Built with ❤️ for Seamless Music Downloading & Local Streaming.</sub>
</div>

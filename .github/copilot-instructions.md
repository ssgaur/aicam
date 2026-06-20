# Copilot Instructions for AiCam

## Project Overview

AiCam is a 24/7 AI surveillance system. An Android phone records 10-second MP4 chunks
via CameraX and uploads them to a FastAPI backend (local or Azure VM). The backend runs
YOLOv8n object detection, stores results in SQLite/Postgres, uploads video to Azure Blob
Storage (24h rolling), and serves a web viewer with AI insights.

## Architecture

```
Phone (AiCameraX app) → FastAPI backend → YOLOv8n → Azure Blob → Viewer UI
```

- **Phone app**: Kotlin, Jetpack Compose, CameraX, OkHttp. Key: `UploadQueue.kt` handles
  parallel uploads with retry, backoff, and self-healing.
- **Backend**: Python, FastAPI, uvicorn. Single process with background YOLO worker thread.
- **Pipeline**: `native_camera_pipeline.py` — frame sampling, YOLO inference, object tracking, blob upload.
- **Viewer**: `viewer.html` — single-page HTML/JS served by backend at `/viewer`.
- **Storage**: SQLite (primary), Postgres (durable analytics), Azure Blob (video/frames).

## Code Conventions

- **Python**: Use type hints. FastAPI endpoints with Form/File params for uploads.
- **Kotlin**: Jetpack Compose for UI, coroutines for async work, AtomicInteger for thread-safe counters.
- **HTML/JS**: Vanilla JS, no frameworks. Template literals for rendering. CSS variables for theming.
- **No secrets in code**: Use `.env` files and environment variables. Never commit storage keys.

## Key Technical Decisions

- **YOLO at imgsz=640**: Best accuracy/speed tradeoff on D4s_v5 CPU (~0.08s/frame).
- **sample_fps capped at 0.5**: Server caps phone's requested fps to avoid CPU overload. Results in 5 frames per 10s clip.
- **Blob containers are PRIVATE**: Must use SAS token URLs for browser access. Media endpoints return 307 redirects to signed URLs.
- **Files on disk = upload queue**: Phone stores chunks in `cache/aicam_chunks/`. On restart, scans disk and re-queues.
- **24h blob cleanup**: Background thread deletes blobs older than 24h. DB records (analytics) are kept permanently.
- **SQLite is primary DB**: Postgres mirrors for durability. All pipeline logic reads/writes SQLite.

## Development Workflow

1. **All code changes in this repo** — never edit directly on VM.
2. **Deploy via scp**: `scp native_camera_pipeline.py backend/main.py viewer.html azureuser@20.197.31.88:~/aicam/`
3. **Restart**: `ssh azureuser@20.197.31.88 "sudo systemctl restart aicam"`
4. **Validate Python**: `python3 -c "import ast; ast.parse(open('backend/main.py').read())"`
5. **Validate JS**: `node -e "new Function(scriptContent)"` to catch syntax errors in viewer.
6. **Test locally**: `./start.sh` runs the full stack on localhost:8100.
7. **Build APK**: `cd AiCameraX && ./gradlew assembleDebug`

## Common Gotchas

- **Heredoc SSH quotes**: Python code sent via SSH heredocs breaks on nested quotes. Use `cat << 'EOF' | ssh ... python3` pattern.
- **Systemd restart**: YOLO worker threads hang on SIGTERM (90s timeout). Use `sudo kill -9 <PID>` then `systemctl start aicam`.
- **Template literal ternaries**: Always include both branches: `${x?'a':''}` not `${x?'a'}`.
- **Burstable VMs throttle**: B-series VMs lose CPU credits under sustained YOLO load. Use D-series for dedicated cores.
- **Self-signed HTTPS**: Phone app trusts all certs (dev only). The VM uses self-signed certs at port 8100.

## File Structure

```
aicam/
├── AiCameraX/              # Android app (Kotlin, Gradle)
│   └── app/src/main/java/ai/camera/shail/
│       ├── MainActivity.kt     # Camera + UI
│       └── UploadQueue.kt      # Retry queue
├── backend/
│   └── main.py                 # FastAPI server
├── native_camera_pipeline.py   # YOLO processing pipeline
├── viewer.html                 # Web viewer UI
├── postgres_store.py           # Postgres sync
├── start.sh                    # One-command local launcher
├── requirements.txt            # Python dependencies
├── .env                        # Local env vars (gitignored)
└── data/                       # Runtime data (gitignored)
```

## Azure Resources

- VM: `aicam-server` (D4s_v5, 20.197.31.88, rg: BLF-ARCHIVER-RG)
- Storage: `aicamstorage2026` (containers: clips, frames)
- Postgres: `assamese-learn-db` (shared, DB: telegram_app)
- SSH: `ssh azureuser@20.197.31.88` (key-based, IP-locked)

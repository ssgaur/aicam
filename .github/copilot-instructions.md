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
  parallel uploads with retry, backoff, and self-healing. Records **720p @ 2 Mbps** (~2.5 MB / 10 s).
- **Backend**: Python, FastAPI, uvicorn. Single process with background YOLO worker thread.
- **Pipeline**: `native_camera_pipeline.py` — frame sampling, YOLO inference, object tracking, blob upload.
- **Viewer (web)**: `viewer.html` — single-page HTML/JS served by backend at `/viewer`.
- **Viewer (mobile)**: `AiCamViewer/` — Flutter app (iOS/Android). A **native SwiftUI** port also
  ships as the "Camera" tab in the **Neighbourly** app (separate repo:
  https://github.com/ssgaur/blf-telegram-automation, under `ios/`). Both consume the same read API.
- **Storage**: SQLite (primary), Postgres (durable analytics), Azure Blob (video/frames).

## Code Conventions

- **Python**: Use type hints. FastAPI endpoints with Form/File params for uploads.
- **Kotlin**: Jetpack Compose for UI, coroutines for async work, AtomicInteger for thread-safe counters.
- **HTML/JS**: Vanilla JS, no frameworks. Template literals for rendering. CSS variables for theming.
- **No secrets in code**: Use `.env` files and environment variables. Never commit storage keys.

## Key Technical Decisions

- **YOLO at imgsz=640**: Best accuracy/speed tradeoff on D4s_v5 CPU (~0.08s/frame).
- **720p @ 2 Mbps recording**: CameraX `Recorder.Builder().setTargetVideoEncodingBitRate(2_000_000)`
  with `Quality.HD`. Default CameraX 720p uses ~14 Mbps (~17 MB/clip); capping to 2 Mbps gives
  ~2.5 MB/clip (~21 GB/day) with quality fine for surveillance. YOLO runs at 640px regardless.
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
  Native players (iOS AVPlayer, Flutter video_player) can't use a cert-trusting HTTP client for
  streaming — **download the clip first, then play the local file** (clips are ~2.5 MB).
- **CameraX mp4 duration is wrong**: MediaMuxer files report a far-longer container duration to
  AVPlayer (clock races past the real ~10 s, last frame freezes). Mobile players must use the
  backend's `duration_sec` and loop at the real end, not rely on the player's reported duration.
- **Viewer time filter uses start/end, not minutes**: `GET /api/native/clips?start=<ts>&end=<ts>`
  (epoch seconds). There is no `minutes` param — clients compute the window themselves.

## File Structure

```
aicam/
├── AiCameraX/              # Android app (Kotlin, Gradle) — records 720p @ 2 Mbps
│   └── app/src/main/java/ai/camera/shail/
│       ├── MainActivity.kt     # Camera + UI (bitrate cap here)
│       └── UploadQueue.kt      # Retry queue
├── AiCamViewer/            # Flutter viewer app (iOS/Android): grid, player, zoom, download
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

> A native SwiftUI port of the viewer also lives as the **Camera tab** in the Neighbourly app
> (repo `ssgaur/blf-telegram-automation`, `ios/Sources/Camera*.swift`). Keep that repo's camera
> code in sync with this viewer's UX, but **don't put AiCam server changes there** — it only
> consumes the read API.

## Azure Resources

- VM: `aicam-server` (D4s_v5, 20.197.31.88, rg: BLF-ARCHIVER-RG)
- Storage: `aicamstorage2026` (containers: clips, frames)
- Postgres: `assamese-learn-db` (shared, DB: telegram_app)
- SSH: `ssh azureuser@20.197.31.88` (key-based, IP-locked)

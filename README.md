# AiCam — 24/7 AI Surveillance Camera

Turn any Android phone into a 24/7 AI-powered surveillance camera with cloud-based
object detection, rolling 24h video archival, and a web viewer with AI insights.

```
Phone (CameraX)          Azure VM (D4s_v5)              Azure Blob
┌──────────┐   HTTPS    ┌──────────────────┐  upload   ┌───────────┐
│ 10s MP4  │ ────────→  │ FastAPI + YOLOv8 │ ───────→  │ clips/    │
│ chunks   │  parallel  │ detect objects   │           │ frames/   │
│ retry Q  │  3 workers │ track + store    │           │ 24h keep  │
└──────────┘            └──────────────────┘           └───────────┘
                               │                            │
                         SQLite + Postgres            SAS URL serve
                               │                            │
                        ┌──────┴──────┐              ┌──────┴──────┐
                        │ /viewer     │  ←───────→   │ video/thumb │
                        │ AI insights │              │ playback    │
                        └─────────────┘              └─────────────┘
```

## Quick Start

### Local (laptop + phone over USB/WiFi)

```bash
git clone https://github.com/ssgaur/aicam.git && cd aicam
./start.sh
# Open http://localhost:8100/viewer
# On phone: set backend URL to http://<LAN_IP>:8100, tap Start
```

### Cloud (Azure VM)

```bash
# Deploy to Azure VM (already provisioned: 20.197.31.88)
scp native_camera_pipeline.py backend/main.py viewer.html azureuser@20.197.31.88:~/aicam/
ssh azureuser@20.197.31.88 "sudo systemctl restart aicam"
# On phone: set backend URL to https://20.197.31.88:8100, tap Start
# Viewer: https://20.197.31.88:8100/viewer
```

## Architecture

| Component | Description |
|-----------|-------------|
| **AiCameraX/** | Android app — CameraX 10s MP4 recording, enterprise retry queue (3 parallel, exponential backoff, self-healing health checks) |
| **backend/main.py** | FastAPI server — upload endpoint, YOLO worker, media serving via SAS URLs, blob cleaner, insights API |
| **native_camera_pipeline.py** | YOLO processing pipeline — frame sampling, object detection (imgsz=640), tracking, blob upload |
| **viewer.html** | Single-page timeline viewer — coverage banner, detection badges, object tags, delete clips, AI insights panel |
| **postgres_store.py** | Postgres sync for durable analytics |
| **start.sh** | One-command local setup: venv, deps, DB, network detect, launch |

## Key Features

- **24/7 recording** — 10s MP4 chunks at **720p, 2 Mbps (~2.5 MB/clip, ~21 GB/day)**, no gaps (parallel upload doesn't block recording)
- **Enterprise retry queue** — phone stores failed uploads locally, auto-retries with exponential backoff, manual retry/delete from UI
- **Self-healing** — health check every 15s, auto-resumes on server recovery, requeues on app restart
- **YOLOv8n detection** — person, car, truck, motorcycle, etc. at 640px (~0.08s/frame on D4s_v5)
- **Object tracking** — cross-frame unique object counting via IoU tracker
- **24h rolling archival** — all clips + frames uploaded to Azure Blob, auto-cleaned after 24h
- **Private blob access** — SAS token URLs for secure browser playback
- **Web viewer** — timeline slider, 5m–24h windows, active/empty badges, delete button, AI insights
- **Mobile viewers** — Flutter app (`AiCamViewer/`) and a native SwiftUI "Camera" tab in the
  [Neighbourly](https://github.com/ssgaur/blf-telegram-automation) app: clip grid, pinch-zoom
  playback, prev/next, download & share
- **Analytics preserved** — DB records (detections, tracks, counts) kept permanently even after video expires

## Azure Infrastructure

| Resource | Spec | Cost/month |
|----------|------|------------|
| VM | D4s_v5 (4 dedicated CPU, 16GB RAM) | ~₹4,000 |
| OS Disk | 64GB Premium SSD | ~₹400 |
| Blob Storage | ~432GB hot tier (24h rolling) | ~₹648 |
| Blob Ops | ~8,640 writes/day × 11 blobs | ~₹100 |
| Postgres | Shared (assamese-learn-db) | ₹0 |
| Network | Ingress free, minimal egress | ~₹100 |
| **Total** | | **~₹5,248/month (~$62)** |

## Phone App (AiCameraX)

The Android app lives in `AiCameraX/`. Key files:

- `MainActivity.kt` — camera preview, recording loop, UI controls
- `UploadQueue.kt` — enterprise retry queue (parallel uploads, backoff, health check, disk-based persistence)

### Build & Install

```bash
cd AiCameraX
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### UI Status Chip

The green chip on the top-right shows: `↑uploaded ⟳in-flight ⏳pending`

On app restart, if unsent clips exist, a banner appears: **"N unsent clips — Send / Delete?"**

## Mobile Viewers

Two native viewers consume the read API (`/api/native/clips`, `/media/clip/...`):

| Viewer | Where | Notes |
|--------|-------|-------|
| **AiCamViewer** (Flutter, iOS/Android) | `AiCamViewer/` in this repo | Clip grid, time-window filter (default 5m), video player with pinch-zoom + prev/next, download & share |
| **Neighbourly "Camera" tab** (native SwiftUI, iOS) | [`ssgaur/blf-telegram-automation`](https://github.com/ssgaur/blf-telegram-automation) → `ios/Sources/Camera*.swift` | Same UX, native; lives as a tab in the BLF community app between Community and Settings |

Both must handle two AiCam quirks:
- **Self-signed cert** — use a cert-trusting HTTP client; for playback, **download the clip first** then play the local file (AVPlayer / video_player can't stream through a custom-trust client).
- **Wrong container duration** — CameraX mp4s report a far-longer duration; use the backend's `duration_sec` and loop at the real end.

Build/run AiCamViewer:
```bash
cd AiCamViewer && flutter pub get && flutter run        # device/simulator
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Health check |
| POST | `/api/native/upload` | Upload MP4 chunk (multipart) |
| GET | `/api/native/clips?start=<ts>&end=<ts>` | List clips in time window (epoch seconds; no `minutes` param — clients compute the window) |
| GET | `/api/native/clips/range` | Min/max timestamps |
| GET | `/api/native/status` | Worker status, queue depth |
| GET | `/api/native/insights` | AI narrative + stats |
| GET | `/api/native/clips/{id}/frames` | Frame list for a clip |
| DELETE | `/api/native/clips/{id}` | Delete clip (DB + blob + disk) |
| GET | `/media/clip/{id}` | Serve video (SAS redirect) |
| GET | `/media/clip/{id}/thumb` | Serve thumbnail |
| GET | `/media/frame/{id}` | Serve frame image |
| GET | `/viewer` | Web viewer UI |

## Data Storage

```
data/native_camera/
├── native_camera.db     # SQLite: clips, frames, detections, tracks
├── clips/               # Temporary MP4 storage (deleted after blob upload)
└── frames/              # Temporary JPEG frames (deleted after blob upload)
```

**Blob containers** (`aicamstorage2026`):
- `clips/` — MP4 video files
- `frames/` — JPEG sampled frames

## Configuration

Environment variables (`.env` on VM):

```
AICAM_CLOUD=1                    # Enable blob upload + local cleanup
AZURE_STORAGE_ACCOUNT=aicamstorage2026
AZURE_STORAGE_KEY=<key>
AICAM_PG_DSN=postgresql://...    # Postgres connection string
```

## Development

All changes should be made in this repo and deployed via `scp`:

```bash
# Edit locally → deploy → restart
scp native_camera_pipeline.py backend/main.py viewer.html azureuser@20.197.31.88:~/aicam/
ssh azureuser@20.197.31.88 "sudo systemctl restart aicam"
```

Validate before deploying:

```bash
python3 -c "import ast; ast.parse(open('backend/main.py').read()); print('OK')"
node -e "const fs=require('fs');const h=fs.readFileSync('viewer.html','utf8');const m=h.match(/<script>([\s\S]*?)<\/script>/);new Function(m[1]);console.log('OK')"
```

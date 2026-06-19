# AiCam — Setup From Scratch

This is the **single source of truth** for setting up AiCam on a fresh Mac (or Linux).

> **Strict rule:** Do not modify anything inside `AiCameraX/`. The Android app
> is locked working — record + upload only. All AI / storage logic lives on
> the Mac backend.

---

## What you will end up with

```
┌──────────────┐  10s mp4 chunks   ┌─────────────────────┐
│  Android     │ ────────────────▶ │  Mac backend (8100) │
│  AiCameraX   │  HTTP upload      │  FastAPI + YOLO     │
└──────────────┘                   └─────────┬───────────┘
                                             │
                            time-bucketed mp4 + frames
                              detections + tracks
                                             │
                                             ▼
                                ┌────────────────────┐
                                │  SQLite + Postgres │
                                └────────────────────┘
```

---

## 0 · Prereqs (Mac)

```bash
# Homebrew tools
brew install python@3.12 ffmpeg postgresql@16 git

# Start Postgres locally and add to PATH
brew services start postgresql@16
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
exec zsh
```

## 1 · Clone the repo

```bash
mkdir -p ~/PersonalDev && cd ~/PersonalDev
git clone https://github.com/ssgaur/aicam.git
cd aicam
git checkout feature/camerax-real-ai-cam
```

## 2 · Python env + dependencies

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 3 · PostgreSQL database

```bash
createdb aicam                          # one-time
python postgres_store.py schema         # creates native_* tables
```

If your Postgres uses a non-default DSN:

```bash
export AICAM_PG_DSN="dbname=aicam host=localhost port=5432"
```

## 4 · Start the backend

```bash
cd backend
source ../venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8100
```

Leave this running. To make it survive terminal close:

```bash
nohup uvicorn main:app --host 0.0.0.0 --port 8100 \
  > ../data/server.log 2>&1 &
disown
```

Verify:

```bash
curl http://localhost:8100/api/native/status
```

## 5 · Find your Mac's LAN IP

```bash
ipconfig getifaddr en0   # Wi-Fi
ipconfig getifaddr en8   # iPhone hotspot, etc.
```

The phone needs to reach `http://<LAN_IP>:8100`. Phone and Mac must be on
the **same network**.

## 6 · Build & install the Android app (do not edit sources)

1. Open `AiCameraX/` in **Android Studio**.
2. Plug in your Android phone (USB debugging on).
3. Click **Run ▶**.
4. On the phone:
   - Grant camera + storage permissions.
   - In the app's **Backend** field, type `http://<LAN_IP>:8100`.
   - Tap **Test** → expect ✓.
   - Tap **Start** → recording + upload begins.

## 7 · (Optional) Companion monitor app

The Flutter app under `app/` shows live counts, charts, and a clips list.

```bash
cd app
flutter pub get
flutter run -d <device-id>     # iPad, macOS, Chrome…
```

In the app's **Settings** tab, set the same `http://<LAN_IP>:8100`.

## 8 · Storage layout

Clips and frames are bucketed by clip start-time:

```
data/native_camera/
├── clips/
│   └── YYYY/MM/Wnn/DD/HH/MM/clip_NNNNNN_TIMESTAMP.mp4
├── frames/
│   └── YYYY/MM/Wnn/DD/HH/MM/clip_NNNNNN_TIMESTAMP/frame_*.jpg
├── to-be-deleted/         # mirror — clips with zero detections
│   ├── clips/YYYY/...
│   └── frames/YYYY/...
└── native_camera.db       # SQLite (per-session, regenerable)
```

A leaf "minute" folder holds about 6 ten-second clips. Frame folders hold
~20 sampled JPGs each (10s × 2 fps). All durable analytics live in
**Postgres**; mp4/jpg are reproducible bulk and safe to prune.

### Migration

If you upgrade an older install with flat `clips/` and `frames/` folders,
move everything into the new tree:

```bash
python scripts/migrate_storage_layout.py            # dry run
python scripts/migrate_storage_layout.py --apply    # actually move
```

## 9 · Useful CLI

```bash
# Postgres-backed summary for a window
python postgres_store.py summary --since 1h

# Re-sync any clips missed since the last backend run
python postgres_store.py sync
```

## 10 · API at a glance

| Endpoint | Purpose |
|---|---|
| `POST /api/native/upload`             | CameraX uploads one mp4 chunk |
| `GET  /api/native/status`             | Live worker / queue / last-clip |
| `GET  /api/native/summary?since=10m`  | Windowed durable counts + clips list |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Backend not reachable` on phone | Same Wi-Fi? Mac firewall? `curl http://LAN_IP:8100/api/native/status` from another device. |
| `moov atom not found` in logs | mp4 was pulled before write finished — pipeline retries; safe to ignore. |
| `psycopg.OperationalError: connection refused` | `brew services start postgresql@16`; check `AICAM_PG_DSN`. |
| New clips not appearing | `lsof -nP -iTCP:8100 -sTCP:LISTEN` — make sure exactly one uvicorn listens. |
| iOS companion app refuses HTTP | Already handled in `Info.plist` (NSAllowsLocalNetworking). |

---

**Important reminders**
- Do **not** edit anything in `AiCameraX/`. Recording + upload is locked.
- Postgres is the source of truth. Deleting `data/native_camera/` is safe at any time.
- Keep this file on `feature/camerax-real-ai-cam` (or `main` once merged).

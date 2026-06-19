# AiCam Beginner Guide — run without Copilot

AiCam turns an Android phone into a local AI camera.

```text
Android CameraX app
→ records short MP4 chunks
→ uploads chunks to your laptop over local Wi-Fi/LAN
→ deletes the phone temp clip after successful upload
→ laptop samples frames
→ local YOLO counts people/cars/bikes/scooters/dogs
→ SQLite stores clips, frames, detections, and unique object tracks
```

Copilot is useful for development, but **Copilot is not needed to run AiCam**.

## What works offline?

After setup, the core CameraX pipeline does **not need internet**.

Required after setup:

- Android phone and laptop on the same local network, hotspot, or Wi-Fi LAN
- Backend running on the laptop
- Android app installed on the phone
- YOLO/SAM checkpoints already downloaded
- Python packages already installed

Not required after setup:

- Copilot
- GitHub
- Azure/OpenAI
- Public internet

Important: the phone still needs a **local network path** to the laptop backend,
for example `http://192.168.1.72:8100`. This is local LAN traffic, not internet.

## What gets saved where?

On the laptop:

```text
data/native_camera/
├── native_camera.db
├── clips/                      # clips where at least one object was detected
├── frames/                     # sampled JPG frames for detected clips
└── to-be-deleted/
    ├── clips/                  # no-detection clips, kept for review
    └── frames/                 # sampled frames for no-detection clips
```

On the phone:

- CameraX app records temporary MP4 files in app cache.
- After upload succeeds, the app deletes its temp MP4.
- It does **not** save CameraX chunks to the phone camera roll.

Durable metadata:

- SQLite keeps the fast local processing DB at `data/native_camera/native_camera.db`.
- PostgreSQL can mirror the same metadata so counts/report text survive even if
  you delete MP4/JPG files.
- The most useful Postgres table is `native_clip_reports`: one row per clip with
  time range, object counts, moving counts, clip path, frame folder, and readable
  report text.

## What is counted?

The backend currently tracks:

- `person`
- `car`
- `motorcycle` as bike/scooter
- `bicycle`
- `bus`
- `truck`
- `dog`
- `cat`

Counts are unique tracked objects, not raw per-frame boxes. The tracker is
practical, not perfect: tiny/far/overlapping objects can still be over-counted or
missed.

## Quick daily use

1. Start backend on laptop:

   ```bash
   cd /Users/shailendrasingh/PersonalDev/aicam/backend
   source ../venv/bin/activate
   uvicorn main:app --host 0.0.0.0 --port 8100
   ```

2. Open **AI CameraX** app on the phone.
3. Confirm backend URL is your laptop IP, for example:

   ```text
   http://192.168.1.72:8100
   ```

4. Tap **Test**. It should show connected/green.
5. Set:

   ```text
   Sec = 10
   FPS = 2
   ```

6. Tap **Start**.
7. Tap **Stop** when done.
8. Check laptop status:

   ```bash
   cd /Users/shailendrasingh/PersonalDev/aicam
   source venv/bin/activate
   python native_camera_pipeline.py status
   ```

Or check the backend API:

```bash
curl http://127.0.0.1:8100/api/native/status | python -m json.tool
```

Ask a summary from Postgres:

```bash
python postgres_store.py summary --since 10m
curl 'http://127.0.0.1:8100/api/native/summary?since=10m' | python -m json.tool
```

## Fresh start vs append

To start clean:

```bash
cd /Users/shailendrasingh/PersonalDev/aicam
rm -rf data/native_camera
```

Then start backend/app again. The system recreates all folders and SQLite tables.

If you do **not** delete `data/native_camera`, new clips append to the same DB.

Do not delete `data/native_camera` while the app is actively uploading or the
backend is processing. Safe sequence:

```text
Stop app
wait until queue_depth = 0
delete data/native_camera
start again
```

## Setup on a new laptop

### All platforms

Install:

- Git
- Python 3.11 or 3.12
- Android Studio
- Android SDK Platform Tools (`adb`)
- Java/JDK 17 or newer

Clone the repo:

```bash
git clone https://github.com/ssgaur/aicam.git
cd aicam
git checkout feature/camerax-real-ai-cam
```

Create Python environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Download model checkpoints:

```bash
mkdir -p checkpoints
curl -L -o checkpoints/sam2.1_hiera_tiny.pt https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt
cd checkpoints
python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
cd ..
```

If `curl` is not available on Windows, download the SAM2 checkpoint in a browser
and place it at:

```text
checkpoints/sam2.1_hiera_tiny.pt
```

### macOS notes

```bash
brew install git python android-platform-tools postgresql@16
brew services start postgresql@16
createdb aicam
```

Android Studio can install the Android SDK and JDK pieces if missing.

### Linux notes

Install distro packages similar to:

```bash
sudo apt update
sudo apt install git python3 python3-venv curl
```

Install Android Studio or Android command-line tools, then ensure `adb` is on
`PATH`.

### Windows notes

Install:

- Git for Windows
- Python from python.org or Microsoft Store
- Android Studio
- Android SDK Platform Tools

Use PowerShell. If Windows blocks venv activation:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Build/install the Android CameraX app

Open this folder in Android Studio:

```text
AiCameraX/
```

Or build from terminal:

```bash
cd AiCameraX
./gradlew :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Windows PowerShell:

```powershell
cd AiCameraX
.\gradlew.bat :app:assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

## Start backend

macOS/Linux:

```bash
cd /path/to/aicam/backend
source ../venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8100
```

Windows PowerShell:

```powershell
cd C:\path\to\aicam\backend
..\venv\Scripts\Activate.ps1
uvicorn main:app --host 0.0.0.0 --port 8100
```

Find laptop IP:

macOS:

```bash
ipconfig getifaddr en0
```

Linux:

```bash
hostname -I
```

Windows:

```powershell
ipconfig
```

Use that IP in the Android app:

```text
http://<laptop-ip>:8100
```

## How to know it is working

In the app:

```text
uploaded:N errors:0
```

Backend:

```bash
curl http://127.0.0.1:8100/api/native/status | python -m json.tool
```

Expected fields:

```text
status = processed
sampled_frames > 0
queue_depth = 0 or small while uploading
```

Postgres sync:

```bash
python postgres_store.py sync
python postgres_store.py summary --since 10m
```

Expected Postgres tables:

```text
native_clips
native_sampled_frames
native_detections
native_object_tracks
native_clip_reports
```

## Troubleshooting

Backend not reachable:

- Make sure backend is running.
- Phone and laptop must be on same Wi-Fi/hotspot/LAN.
- Use laptop IP, not `127.0.0.1`, in the Android app.
- Allow Python/uvicorn through firewall on Windows/macOS.

No clips:

- Tap **Test** first.
- Then tap **Start**.
- Watch `uploaded:N`.

Too much storage:

- Lower `FPS` from `2` to `1`.
- Review and delete `data/native_camera/to-be-deleted`.
- Keep `native_camera.db` if you only want counts.

Want a clean slate:

```bash
rm -rf data/native_camera
```

## Development notes

Project folders:

```text
AiCameraX/                # native Android CameraX app
backend/                  # FastAPI backend
native_camera_pipeline.py # local YOLO processing and SQLite schema
app/                      # older Flutter prototype
docs/                     # runbooks
```

The recommended product path is `AiCameraX/` + `backend/`.

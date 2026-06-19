# CameraX Real AI Cam

This is the no-ADB-recording path.

```text
Flutter app camera plugin on Android
→ records MP4 chunks inside the app
→ uploads each chunk to the Mac backend
→ deletes the app-local temp clip after upload
→ backend stores MP4 under data/native_camera/clips
→ backend samples JPG frames
→ backend runs local YOLO + object tracker
→ SQLite keeps clips, frames, detections, unique object tracks
```

The Flutter `camera` package uses Android camera APIs under the hood, including
CameraX on supported Android versions. This path is less fragile than driving
the OnePlus Camera UI with ADB because the app controls recording directly.

## Run backend

```bash
cd /Users/shailendrasingh/PersonalDev/aicam/backend
source ../venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8100
```

The app uploads chunks to:

```text
POST /api/native/upload
```

Check upload processing:

```bash
curl http://127.0.0.1:8100/api/native/status | python -m json.tool
```

## Run app

```bash
cd /Users/shailendrasingh/PersonalDev/aicam/app
flutter run
```

In the app:

1. Confirm backend is found or enter `http://<mac-ip>:8100`.
2. Set `Chunk sec`, e.g. `10`.
3. Set `Sample FPS`, e.g. `2`.
4. Tap **Start Real AI Cam**.

The app keeps recording chunks until you tap **Stop Real AI Cam**.

## Where data goes

Backend data:

```text
data/native_camera/
├── native_camera.db
├── clips/
└── frames/
```

Phone/app temp clips are deleted after successful upload. The phone does not
keep a camera-roll copy in this path.

## Compare with ADB native-camera path

| Path | Pros | Cons |
| --- | --- | --- |
| OnePlus Camera + ADB | Best OEM image quality | Fragile UI tapping, save/finalize gaps |
| CameraX app upload | Real app-controlled AI cam, no ADB tapping | May not match OnePlus OEM camera quality |

Use this CameraX path for the real product direction. Keep the ADB path as a lab
prototype and quality baseline.

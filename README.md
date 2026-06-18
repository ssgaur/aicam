# AiCam — Live AI camera with SAM 2

Phone (Flutter) streams JPEG frames over WebSocket → Mac backend runs **SAM 2 Hiera-Tiny** (auto mask generator) on MPS → returns colored mask overlay → phone composites on live preview.

## Architecture
- **app/** — Flutter app (Android primary). Camera preview + WebSocket client.
- **backend/** — FastAPI + sam2 + PyTorch (MPS on Mac, CUDA in cloud).
- **checkpoints/** — `sam2.1_hiera_tiny.pt` (~149 MB, gitignored). Download:
  ```
  curl -L -o checkpoints/sam2.1_hiera_tiny.pt https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt
  ```

## Run backend (Mac)
```
cd backend && source ../venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8100
```

## Performance
- Mac M1 Pro MPS @ 320px, 12×12 points: ~1 FPS, ~150 KB PNG/frame.
- Cloud GPU (T4 / A10): 5–10 FPS at 512px.

## Endpoints
- `GET /healthz` → `{ok, device, loaded}`
- `WS /ws/segment` → recv JPEG bytes, send PNG RGBA overlay bytes.

## Native OnePlus Camera chunk pipeline

For higher-quality street/object counting, use the phone's native OnePlus
Camera app instead of the Flutter preview stream:

```bash
cd /Users/shailendrasingh/PersonalDev/aicam
source venv/bin/activate

# Record 10-second MP4 chunks, pull them from the phone, sample frames at 3 FPS,
# run local YOLO tracking, and store clips/frames/counts in SQLite.
python native_camera_pipeline.py run --chunks 30 --duration 10 --sample-fps 3

# Ask what happened most recently.
python native_camera_pipeline.py status
```

Data is written under `data/native_camera/`:

- `clips/` — original high-quality MP4 chunks from the OnePlus Camera app
- `frames/` — sampled JPG frames used for YOLO processing
- `native_camera.db` — SQLite tables for clips, sampled frames, detections, and unique object tracks

This native pipeline is local-only after setup: ADB controls the phone over USB,
the OnePlus Camera records video, `adb pull` copies MP4s to the Mac, and YOLO
runs locally. It does not call Azure/OpenAI/Copilot.

See [`docs/NATIVE_CAMERA_PIPELINE.md`](docs/NATIVE_CAMERA_PIPELINE.md) for the
full tomorrow runbook, storage estimates, and troubleshooting notes.

## TODO
- Tap-to-track mode (SAM 2 video predictor with memory bank).
- Send RLE masks instead of PNG (smaller, faster).
- Auto-shutdown cloud GPU when phone disconnects.

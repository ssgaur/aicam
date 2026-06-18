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

## TODO
- Tap-to-track mode (SAM 2 video predictor with memory bank).
- Send RLE masks instead of PNG (smaller, faster).
- Auto-shutdown cloud GPU when phone disconnects.

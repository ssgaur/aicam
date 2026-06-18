# Native Camera Pipeline Runbook

This runbook is for the high-quality OnePlus native camera workflow:

```text
ADB opens OnePlus Camera
→ tap record
→ record 10-second MP4 chunk
→ tap stop
→ start next chunk after a short gap
→ pull completed MP4 to Mac
→ sample frames at lower FPS
→ run local YOLO
→ track unique people/cars/bikes/scooters/dogs
→ save everything to SQLite
```

## What needs internet?

For the native-camera pipeline, internet is **not required after setup**.

Required locally:

- Android phone connected over USB
- USB debugging authorized
- `adb` available on the Mac
- Python virtualenv already installed
- `checkpoints/yolov8n.pt` already present
- OnePlus Camera app available on the phone

Not required for this pipeline:

- Copilot
- Azure/OpenAI
- Wi-Fi
- Android Studio
- Flutter app

Internet is only needed for things like pushing to GitHub, installing packages,
or using the separate Azure captioning/web backend.

## Start a run

Interactive mode:

```bash
cd /Users/shailendrasingh/PersonalDev/aicam
./run_native_camera.sh
```

The wizard asks:

```text
How many clips? [30]
Seconds per clip? [10.0]
Sample FPS for processing? [3.0]
```

Direct mode:

```bash
cd /Users/shailendrasingh/PersonalDev/aicam
source venv/bin/activate
python native_camera_pipeline.py run --chunks 30 --duration 10 --sample-fps 3
```

This records about 30 chunks × 10 seconds. The script uses deterministic ADB
commands, not an AI agent, to operate the phone:

```text
adb shell am start -a android.media.action.VIDEO_CAPTURE
adb shell input tap 540 2037   # record
sleep 10
adb shell input tap 540 2037   # stop
adb pull /sdcard/DCIM/Camera/VID_....mp4
```

The script first checks the OnePlus Camera UI state and will stop an already
running recording before starting a new run. It also waits for each MP4 to become
stable/playable before pulling and processing it. This avoids corrupt MP4 files
with errors like `moov atom not found`, but it creates a few seconds of gap
between chunks.

## Check status

```bash
cd /Users/shailendrasingh/PersonalDev/aicam
source venv/bin/activate
python native_camera_pipeline.py status
```

Example output:

```text
Current time: 2026-06-19T00:34:43+05:30
Last chunk: #35 · 2026-06-19T00:31:16+05:30 → 2026-06-19T00:31:27+05:30 · status=processed
Local clip: .../data/native_camera/clips/clip_000035_20260619-003116.mp4
Processed images folder: .../data/native_camera/frames/clip_000035_20260619-003116
Sampled images: 33 at 3.0 fps
Detection rows: 29
Unique object counts: {"person": 2}
Moving object counts: {"person": 2}
```

## Audit and final summary

Every run writes:

```text
data/native_camera/audits/run_YYYYMMDD_HHMMSS.jsonl
data/native_camera/audits/run_YYYYMMDD_HHMMSS_summary.json
data/native_camera/audits/run_YYYYMMDD_HHMMSS_summary.md
```

The final console summary includes:

- current time
- run start/end
- where MP4 clips are stored
- where sampled JPG frames are stored
- SQLite DB path
- per-clip object table
- overall unique counts
- overall moving counts
- Azure Vision calls/cost, always `0` for this native pipeline

Example direct run for 15 minutes:

```bash
python native_camera_pipeline.py run --chunks 30 --duration 30 --sample-fps 3
```

That means:

```text
30 clips × 30 sec = 900 sec = 15 min
```

## Data layout

```text
data/native_camera/
├── native_camera.db
├── clips/
│   └── clip_000035_20260619-003116.mp4
└── frames/
    └── clip_000035_20260619-003116/
        ├── frame_0000_00.00s.jpg
        ├── frame_0001_00.33s.jpg
        └── ...
```

SQLite tables:

- `clips` — one row per MP4 chunk
- `sampled_frames` — JPG frames extracted from each clip
- `detections` — YOLO detections per sampled frame
- `object_tracks` — de-duplicated unique object tracks across frames/chunks

You can delete `data/native_camera/` entirely when you want a clean slate:

```bash
rm -rf /Users/shailendrasingh/PersonalDev/aicam/data/native_camera
```

The next `./run_native_camera.sh`, `python native_camera_pipeline.py run ...`,
or `python native_camera_pipeline.py status` will recreate:

```text
data/native_camera/
data/native_camera/clips/
data/native_camera/frames/
data/native_camera/audits/
data/native_camera/native_camera.db
```

If `data/native_camera/` already exists, the script appends new clips and DB
rows instead of resetting anything.

Quick setup check:

```bash
python native_camera_pipeline.py doctor
```

Advanced: to test with a separate scratch data folder:

```bash
AICAM_NATIVE_DATA=/tmp/aicam-test python native_camera_pipeline.py doctor
```

## Object classes counted

The tracker currently counts:

- `person`
- `car`
- `motorcycle` as bike/scooter
- `bicycle`
- `bus`
- `truck`
- `dog`
- `cat`

Counts are de-duplicated with lightweight tracking based on YOLO class, bounding
box IoU, center distance, and time. This is practical, not CCTV-grade perfect:
occlusion, poor lighting, tiny objects, or leaving/re-entering can still over or
under count.

## Storage estimate

From the first real run:

- Average 10-second MP4: about 26 MB
- Average sampled JPGs per clip at 3 FPS: about 32–34 frames
- Average sampled JPG storage per clip: about 13.5 MB
- Total at current settings: about 14 GB/hour

Approximate growth:

| Duration | MP4 only | JPG frames | Total |
| --- | ---: | ---: | ---: |
| 1 hour | ~9.2 GB | ~4.7 GB | ~14 GB |
| 6 hours | ~55 GB | ~28 GB | ~84 GB |
| 24 hours | ~221 GB | ~114 GB | ~335 GB |

To reduce storage:

```bash
python native_camera_pipeline.py run --chunks 30 --duration 10 --sample-fps 1
```

`--sample-fps 1` keeps fewer JPGs while preserving original MP4 clips.

## Useful commands

Check connected phone:

```bash
adb devices
```

Open OnePlus video camera:

```bash
adb shell am start -a android.media.action.VIDEO_CAPTURE --ei android.intent.extra.durationLimit 10
```

Check latest videos on phone:

```bash
adb shell 'find /sdcard/DCIM/Camera -maxdepth 1 -type f \( -iname "*.mp4" -o -iname "*.3gp" \) -printf "%T@ %p\n" 2>/dev/null | sort -nr | head'
```

Reset native pipeline DB, keeping a backup:

```bash
python native_camera_pipeline.py reset-db
```

## Troubleshooting

If recording stopped, first check whether the planned chunk count ended:

```bash
python native_camera_pipeline.py status
```

If the phone is not in Camera:

```bash
adb shell am start -a android.media.action.VIDEO_CAPTURE --ei android.intent.extra.durationLimit 10
```

If ADB cannot see the phone:

```bash
adb devices
```

Then reconnect USB and accept the debugging prompt on the phone.

If the record button coordinate changes, inspect the UI:

```bash
adb shell uiautomator dump /sdcard/window.xml
adb exec-out cat /sdcard/window.xml
```

Current known OnePlus record/stop coordinate:

```text
x=540, y=2037
```

## Separate live AiCam backend

The FastAPI backend in `backend/main.py` is separate from the native-camera
chunk pipeline. It can stream Flutter frames, run SAM2/YOLO, and optionally call
Azure OpenAI for captions if environment variables are configured.

Native camera chunking does **not** use that cloud caption path.

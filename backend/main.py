"""AiCam backend — SAM 2 + YOLOv8 + per-frame logging + periodic captioning + TTS messages."""
import asyncio
import base64
import io
import json
import math
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont

load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snaps"
DB_PATH = DATA / "events.db"
SNAPS.mkdir(parents=True, exist_ok=True)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import native_camera_pipeline as native_cam  # noqa: E402

CAPTION_EVERY_SEC = 3.0  # ~$2-3/day at 1 FPS, well under $10
SAM_MAX_SIDE = 320
YOLO_CONF = 0.35
ROTATE_DEG = int(os.getenv("ROTATE_DEG", "-90"))   # -90 = clockwise 90°, 90 = CCW, 180 = flip
PROCESS_EVERY_SEC = float(os.getenv("PROCESS_EVERY_SEC", "1.0"))  # heavy pipeline cadence
TRACK_TTL_SEC = float(os.getenv("TRACK_TTL_SEC", "12.0"))
TRACK_IOU_MIN = float(os.getenv("TRACK_IOU_MIN", "0.12"))
TRACK_CENTER_MAX_PX = float(os.getenv("TRACK_CENTER_MAX_PX", "140"))
TRACK_MOVE_PX = float(os.getenv("TRACK_MOVE_PX", "45"))

TRACKED_CLASSES = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "dog", "cat",
}
CLASS_CATEGORY = {
    "person": "human",
    "dog": "dog",
    "cat": "animal",
    "bicycle": "bike",
    "motorcycle": "bike_scooter",
    "car": "car",
    "bus": "vehicle",
    "truck": "vehicle",
}

_state = {"sam": None, "yolo": None, "device": None,
          "last_caption_ts": 0.0, "caption_inflight": False,
          "last_process_ts": 0.0, "process_inflight": False,
          "tracks": []}

_native_upload_queue: "queue.Queue[dict | None]" = queue.Queue()
_native_upload_worker: threading.Thread | None = None

# 1x1 transparent PNG for "no overlay" replies to phone
_EMPTY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS frames (
            ts REAL PRIMARY KEY,
            iso TEXT,
            path TEXT,
            yolo_summary TEXT,
            caption TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            ts REAL,
            iso TEXT,
            cls TEXT,
            conf REAL,
            box TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_cls_ts ON events(cls, ts);
        CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts);
        CREATE TABLE IF NOT EXISTS object_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cls TEXT NOT NULL,
            category TEXT NOT NULL,
            first_ts REAL NOT NULL,
            first_iso TEXT NOT NULL,
            last_ts REAL NOT NULL,
            last_iso TEXT NOT NULL,
            first_box TEXT NOT NULL,
            last_box TEXT NOT NULL,
            best_conf REAL NOT NULL,
            hits INTEGER NOT NULL DEFAULT 1,
            max_displacement REAL NOT NULL DEFAULT 0,
            moving INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_object_tracks_cls_last ON object_tracks(cls, last_ts);
        CREATE INDEX IF NOT EXISTS idx_object_tracks_category_last ON object_tracks(category, last_ts);
        CREATE INDEX IF NOT EXISTS idx_object_tracks_moving_last ON object_tracks(moving, last_ts);
        CREATE TABLE IF NOT EXISTS say_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            text TEXT
        );
        """
    )
    cols = {row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()}
    if "track_id" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN track_id INTEGER")
    con.commit()
    con.close()


def _load_models():
    from sam2.build_sam import build_sam2
    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from ultralytics import YOLO

    dev = _device()
    print(f"[init] device={dev}")
    ckpt = ROOT / "checkpoints" / "sam2.1_hiera_tiny.pt"
    sam = build_sam2("configs/sam2.1/sam2.1_hiera_t.yaml", str(ckpt), device=dev, apply_postprocessing=False)
    _state["sam"] = SAM2AutomaticMaskGenerator(
        model=sam, points_per_side=12, points_per_batch=64,
        pred_iou_thresh=0.7, stability_score_thresh=0.85,
        crop_n_layers=0, min_mask_region_area=300,
    )
    yolo = YOLO(str(ROOT / "checkpoints" / "yolov8n.pt"))
    _ = yolo.predict(np.zeros((320, 320, 3), dtype=np.uint8), device=dev, verbose=False)
    _state["yolo"] = yolo
    _state["device"] = dev
    print("[init] models loaded")


def _load_active_tracks():
    t0 = time.time() - TRACK_TTL_SEC
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT id, cls, category, first_ts, last_ts, first_box, last_box,
               best_conf, hits, max_displacement, moving
        FROM object_tracks
        WHERE active=1 AND last_ts>=?
        ORDER BY last_ts ASC
        """,
        (t0,),
    ).fetchall()
    con.close()
    _state["tracks"] = [
        {
            "id": r[0],
            "cls": r[1],
            "category": r[2],
            "first_ts": r[3],
            "last_ts": r[4],
            "first_box": json.loads(r[5]),
            "last_box": json.loads(r[6]),
            "best_conf": r[7],
            "hits": r[8],
            "max_displacement": r[9],
            "moving": r[10],
        }
        for r in rows
    ]
    print(f"[init] active tracks loaded={len(_state['tracks'])}")


def _native_upload_loop():
    """Background processor for MP4 chunks uploaded by the Android app."""
    native_cam.init_db()
    model, device = native_cam.load_model()
    tracker = native_cam.GlobalTracker()
    print("[native-upload] worker ready")
    while True:
        item = _native_upload_queue.get()
        if item is None:
            _native_upload_queue.task_done()
            break
        job = item["job"]
        try:
            native_cam.process_clip(job, model, device, tracker, item["sample_fps"], item["conf"])
            print(f"[native-upload] processed clip_id={job.clip_id}")
        except Exception as exc:
            native_cam.mark_clip_error(job.clip_id, str(exc))
            print(f"[native-upload] clip_id={job.clip_id} error={exc}")
        finally:
            _native_upload_queue.task_done()


def _ensure_native_upload_worker():
    global _native_upload_worker
    if _native_upload_worker is not None and _native_upload_worker.is_alive():
        return
    _native_upload_worker = threading.Thread(target=_native_upload_loop, name="native-upload-worker", daemon=True)
    _native_upload_worker.start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _init_db()
    _load_active_tracks()
    _load_models()
    native_cam.init_db()
    yield


app = FastAPI(title="AiCam", lifespan=lifespan)


_PALETTE = np.array([[231,76,60],[46,204,113],[52,152,219],[241,196,15],[155,89,182],
                     [26,188,156],[230,126,34],[149,165,166]], dtype=np.uint8)


def _segment_overlay(arr_small: np.ndarray, w_full: int, h_full: int) -> bytes:
    with torch.inference_mode():
        masks = _state["sam"].generate(arr_small)
    H, W = arr_small.shape[:2]
    overlay = np.zeros((H, W, 4), dtype=np.uint8)
    for i, m in enumerate(sorted(masks, key=lambda m: -m["area"])):
        c = _PALETTE[i % len(_PALETTE)]
        overlay[m["segmentation"], 0] = c[0]
        overlay[m["segmentation"], 1] = c[1]
        overlay[m["segmentation"], 2] = c[2]
        overlay[m["segmentation"], 3] = 200
    out = Image.fromarray(overlay, mode="RGBA").resize((w_full, h_full), Image.NEAREST)
    buf = io.BytesIO(); out.save(buf, "PNG")
    return buf.getvalue()


def _yolo_detect(img: Image.Image):
    arr = np.array(img)
    res = _state["yolo"].predict(arr, conf=YOLO_CONF, device=_state["device"], verbose=False)[0]
    out = []
    names = res.names
    if res.boxes is not None:
        for b, c, cls in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
            out.append({"cls": names[int(cls)], "conf": float(c), "box": [float(x) for x in b]})
    return out


def _track_category(cls: str) -> str:
    return CLASS_CATEGORY.get(cls, cls)


def _box_center(box: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _box_diag(box: list[float]) -> float:
    x1, y1, x2, y2 = box
    return math.hypot(max(1.0, x2 - x1), max(1.0, y2 - y1))


def _box_iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def _center_distance(a: list[float], b: list[float]) -> float:
    ax, ay = _box_center(a)
    bx, by = _box_center(b)
    return math.hypot(ax - bx, ay - by)


def _track_match_ok(track: dict, det: dict, ts: float) -> tuple[bool, float]:
    if track["cls"] != det["cls"]:
        return False, 0.0
    if ts - track["last_ts"] > TRACK_TTL_SEC:
        return False, 0.0

    iou = _box_iou(track["last_box"], det["box"])
    dist = _center_distance(track["last_box"], det["box"])
    dist_limit = max(TRACK_CENTER_MAX_PX, _box_diag(det["box"]) * 1.25, _box_diag(track["last_box"]) * 1.25)
    if iou >= TRACK_IOU_MIN:
        return True, 2.0 + iou
    if dist <= dist_limit:
        return True, 1.0 - min(0.99, dist / dist_limit)
    return False, 0.0


def _new_track(con: sqlite3.Connection, ts: float, iso: str, det: dict) -> dict:
    cls = det["cls"]
    category = _track_category(cls)
    box = det["box"]
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO object_tracks(
            cls, category, first_ts, first_iso, last_ts, last_iso,
            first_box, last_box, best_conf, hits, max_displacement, moving, active
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (cls, category, ts, iso, ts, iso, json.dumps(box), json.dumps(box), det["conf"], 1, 0.0, 0, 1),
    )
    track = {
        "id": cur.lastrowid,
        "cls": cls,
        "category": category,
        "first_ts": ts,
        "last_ts": ts,
        "first_box": box,
        "last_box": box,
        "best_conf": det["conf"],
        "hits": 1,
        "max_displacement": 0.0,
        "moving": 0,
    }
    _state["tracks"].append(track)
    return track


def _update_track(con: sqlite3.Connection, track: dict, ts: float, iso: str, det: dict) -> dict:
    first_center = _box_center(track["first_box"])
    now_center = _box_center(det["box"])
    displacement = math.hypot(now_center[0] - first_center[0], now_center[1] - first_center[1])
    track["last_ts"] = ts
    track["last_box"] = det["box"]
    track["hits"] += 1
    track["best_conf"] = max(track["best_conf"], det["conf"])
    track["max_displacement"] = max(track["max_displacement"], displacement)
    track["moving"] = 1 if track["max_displacement"] >= TRACK_MOVE_PX and track["hits"] >= 2 else 0
    con.execute(
        """
        UPDATE object_tracks
        SET last_ts=?, last_iso=?, last_box=?, best_conf=?, hits=?,
            max_displacement=?, moving=?, active=1
        WHERE id=?
        """,
        (
            ts,
            iso,
            json.dumps(det["box"]),
            track["best_conf"],
            track["hits"],
            track["max_displacement"],
            track["moving"],
            track["id"],
        ),
    )
    return track


def _expire_tracks(con: sqlite3.Connection, ts: float) -> None:
    active = []
    expired_ids = []
    for track in _state["tracks"]:
        if ts - track["last_ts"] > TRACK_TTL_SEC:
            expired_ids.append(track["id"])
        else:
            active.append(track)
    _state["tracks"] = active
    if expired_ids:
        con.executemany("UPDATE object_tracks SET active=0 WHERE id=?", [(track_id,) for track_id in expired_ids])


def _update_object_tracks(ts: float, iso: str, dets: list) -> list[dict]:
    """Assign stable track IDs to YOLO detections so counts are unique objects, not per-frame boxes."""
    tracked_dets = [
        det for det in dets
        if det.get("cls") in TRACKED_CLASSES and det.get("conf", 0.0) >= YOLO_CONF
    ]
    if not tracked_dets:
        return []

    con = sqlite3.connect(DB_PATH)
    try:
        _expire_tracks(con, ts)
        matched_track_ids: set[int] = set()
        assigned_tracks = []

        for det in sorted(tracked_dets, key=lambda d: d["conf"], reverse=True):
            best_track = None
            best_score = 0.0
            for track in _state["tracks"]:
                if track["id"] in matched_track_ids:
                    continue
                ok, score = _track_match_ok(track, det, ts)
                if ok and score > best_score:
                    best_track = track
                    best_score = score

            if best_track is None:
                best_track = _new_track(con, ts, iso, det)
            else:
                _update_track(con, best_track, ts, iso, det)

            matched_track_ids.add(best_track["id"])
            det["track_id"] = best_track["id"]
            det["category"] = best_track["category"]
            det["moving"] = bool(best_track["moving"])
            assigned_tracks.append(best_track)

        con.commit()
        return assigned_tracks
    finally:
        con.close()


def _annotate(img: Image.Image, dets: list, caption: str | None) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
        small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 14)
    except Exception:
        font = ImageFont.load_default(); small = font
    for det in dets:
        x1, y1, x2, y2 = det["box"]
        d.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=3)
        track_suffix = f'#{det["track_id"]}' if det.get("track_id") else ""
        moving_suffix = " moving" if det.get("moving") else ""
        d.text((x1 + 4, y1 + 4), f'{det["cls"]}{track_suffix} {det["conf"]:.2f}{moving_suffix}', fill=(255, 255, 255), font=small)
    bar_h = 40 if caption else 24
    d.rectangle([0, 0, out.width, bar_h], fill=(0, 0, 0))
    d.text((6, 4), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fill=(0, 255, 200), font=font)
    if caption:
        d.text((6, 22), caption[:120], fill=(255, 255, 255), font=small)
    return out


async def _caption_async(img: Image.Image) -> str | None:
    """Vision call to gpt-4o-mini. Returns short caption or None."""
    try:
        from openai import AsyncAzureOpenAI
    except Exception as e:
        print(f"[caption] openai sdk missing: {e}")
        return None
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    key = os.getenv("AZURE_OPENAI_API_KEY")
    deploy = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini")
    api_ver = os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
    if not (endpoint and key):
        return None
    small = img.copy()
    small.thumbnail((512, 512))
    buf = io.BytesIO(); small.save(buf, "JPEG", quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode()
    client = AsyncAzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_ver)
    try:
        r = await client.chat.completions.create(
            model=deploy,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this CCTV-style frame in ONE short sentence (max 20 words). Mention people/vehicles/movement if visible."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
                ],
            }],
            max_tokens=60, temperature=0.3,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f"[caption] error: {e}")
        return None
    finally:
        await client.close()


def _save_frame(ts: float, img: Image.Image, dets: list, caption: str | None):
    iso = datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")
    path = SNAPS / f"{int(ts)}.jpg"
    img.save(path, "JPEG", quality=80)
    img.save(DATA / "latest.jpg", "JPEG", quality=80)
    yolo_summary = {}
    for d in dets:
        yolo_summary[d["cls"]] = yolo_summary.get(d["cls"], 0) + 1
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT OR REPLACE INTO frames(ts,iso,path,yolo_summary,caption) VALUES(?,?,?,?,?)",
                (ts, iso, str(path), json.dumps(yolo_summary), caption))
    for d in dets:
        con.execute("INSERT INTO events(ts,iso,cls,conf,box,track_id) VALUES(?,?,?,?,?,?)",
                    (ts, iso, d["cls"], d["conf"], json.dumps(d["box"]), d.get("track_id")))
    con.commit(); con.close()


def _heavy_process(img: Image.Image):
    """Synchronous SAM2 + YOLO + caption-save pipeline. Runs in a thread."""
    w, h = img.size
    small = img.copy()
    scale = SAM_MAX_SIDE / max(w, h)
    if scale < 1.0:
        small = small.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    arr_small = np.array(small)
    overlay_png = _segment_overlay(arr_small, w, h)  # not used by phone; for future composite
    dets = _yolo_detect(img)
    return overlay_png, dets


async def _process_frame(jpeg: bytes) -> bytes:
    """FAST path: rotate -> save raw to data/live.jpg -> kick off heavy pipeline async -> return tiny PNG.

    Heavy SAM+YOLO+caption runs at most once per PROCESS_EVERY_SEC.
    """
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        if ROTATE_DEG:
            img = img.rotate(ROTATE_DEG, expand=True)
        # write live preview immediately (fast — no processing)
        img.save(DATA / "live.jpg", "JPEG", quality=70)
    except Exception as e:
        print(f"[fast] {e}")
        return _EMPTY_PNG

    now = time.time()
    if (not _state["process_inflight"]) and (now - _state["last_process_ts"] >= PROCESS_EVERY_SEC):
        _state["last_process_ts"] = now
        _state["process_inflight"] = True

        async def _bg(img_for_proc):
            try:
                overlay, dets = await asyncio.to_thread(_heavy_process, img_for_proc)
                ts = time.time()
                iso = datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")
                _update_object_tracks(ts, iso, dets)
                # caption logic
                caption = None
                if (not _state["caption_inflight"]) and (ts - _state["last_caption_ts"] >= CAPTION_EVERY_SEC):
                    _state["last_caption_ts"] = ts
                    _state["caption_inflight"] = True
                    try:
                        caption = await _caption_async(img_for_proc)
                    finally:
                        _state["caption_inflight"] = False
                annotated = _annotate(img_for_proc, dets, caption)
                _save_frame(time.time(), annotated, dets, caption)
            except Exception as e:
                print(f"[bg] {e}")
            finally:
                _state["process_inflight"] = False

        asyncio.create_task(_bg(img))

    return _EMPTY_PNG  # phone discards / draws transparently


@app.websocket("/ws/segment")
async def ws_segment(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            data = msg.get("bytes")
            if data is None:
                continue
            try:
                png = await _process_frame(data)
                await ws.send_bytes(png)
            except Exception as e:
                print(f"[ws] err: {e}")
                await ws.send_json({"error": str(e)})
    except WebSocketDisconnect:
        return


@app.get("/healthz")
def healthz():
    n_frames = 0
    last = None
    try:
        con = sqlite3.connect(DB_PATH)
        n_frames = con.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        row = con.execute("SELECT iso, caption FROM frames ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            last = {"iso": row[0], "caption": row[1]}
        con.close()
    except Exception:
        pass
    return {"ok": True, "device": _state.get("device"), "frames": n_frames, "latest": last}


def _parse_since(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("h"):
        return time.time() - float(s[:-1]) * 3600
    if s.endswith("m"):
        return time.time() - float(s[:-1]) * 60
    if s.endswith("s"):
        return time.time() - float(s[:-1])
    return float(s)


@app.get("/api/latest")
def api_latest():
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT ts, iso, path, yolo_summary, caption FROM frames ORDER BY ts DESC LIMIT 1").fetchone()
    con.close()
    if not row:
        return JSONResponse({"empty": True})
    return {"ts": row[0], "iso": row[1], "path": row[2], "yolo": json.loads(row[3] or "{}"), "caption": row[4]}


@app.post("/api/native/upload")
async def api_native_upload(
    file: UploadFile = File(...),
    start_ts: float = Form(...),
    end_ts: float = Form(...),
    sample_fps: float = Form(2.0),
    conf: float = Form(0.35),
    chunk_index: int = Form(0),
    device_id: str = Form("android"),
):
    """Accept one CameraX/Flutter MP4 chunk and process it with the native-camera pipeline."""
    native_cam.init_db()
    if end_ts <= start_ts:
        return JSONResponse({"ok": False, "error": "end_ts must be greater than start_ts"}, status_code=400)
    clip_id, local_path, frames_dir = native_cam.create_clip(start_ts, end_ts)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with local_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        await file.close()

    if not native_cam.local_video_ok(local_path):
        native_cam.mark_clip_error(clip_id, "uploaded MP4 is not playable")
        return JSONResponse(
            {"ok": False, "clip_id": clip_id, "error": "uploaded MP4 is not playable", "local_path": str(local_path)},
            status_code=400,
        )

    phone_path = f"android-upload:{device_id}:{chunk_index}:{file.filename or local_path.name}"
    native_cam.mark_clip_pulled(clip_id, phone_path, local_path, frames_dir)
    _ensure_native_upload_worker()
    _native_upload_queue.put(
        {
            "job": native_cam.ClipJob(clip_id=clip_id, phone_path=phone_path, local_path=local_path, frames_dir=frames_dir),
            "sample_fps": sample_fps,
            "conf": conf,
        }
    )
    return {
        "ok": True,
        "accepted": True,
        "clip_id": clip_id,
        "chunk_index": chunk_index,
        "local_path": str(local_path),
        "frames_dir": str(frames_dir),
        "queue_depth": _native_upload_queue.qsize(),
    }


@app.get("/api/native/status")
def api_native_status():
    native_cam.init_db()
    status = native_cam.latest_status()
    status["queue_depth"] = _native_upload_queue.qsize()
    status["worker_alive"] = bool(_native_upload_worker and _native_upload_worker.is_alive())
    return status


@app.get("/api/native/summary")
def api_native_summary(since: str = "10m"):
    """Postgres-backed durable summary; survives MP4/JPG cleanup."""
    try:
        import postgres_store

        return postgres_store.summary_data(since)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/native/clips")
def api_native_clips(start: float = 0, end: float = 0):
    """List clips whose time overlaps a given range. Returns clip metadata + frame paths."""
    native_cam.init_db()
    con = sqlite3.connect(native_cam.DB_PATH)
    if end <= 0:
        end = time.time()
    if start <= 0:
        start = end - 3600
    rows = con.execute(
        """
        SELECT id, start_ts, end_ts, start_iso, end_iso,
               local_path, frames_dir, duration_sec, sampled_frames, status
        FROM clips
        WHERE end_ts >= ? AND start_ts <= ?
        ORDER BY start_ts
        """,
        (start, end),
    ).fetchall()
    clips = []
    for r in rows:
        clips.append({
            "id": r[0], "start_ts": r[1], "end_ts": r[2],
            "start_iso": r[3], "end_iso": r[4],
            "local_path": r[5], "frames_dir": r[6],
            "duration_sec": r[7], "sampled_frames": r[8], "status": r[9],
        })
    con.close()
    return {"start": start, "end": end, "count": len(clips), "clips": clips}


@app.get("/api/native/clips/{clip_id}/frames")
def api_native_clip_frames(clip_id: int):
    """Return frame paths for a specific clip."""
    native_cam.init_db()
    con = sqlite3.connect(native_cam.DB_PATH)
    rows = con.execute(
        "SELECT id, frame_index, video_time_sec, abs_ts, iso, path FROM sampled_frames WHERE clip_id=? ORDER BY frame_index",
        (clip_id,),
    ).fetchall()
    con.close()
    return {
        "clip_id": clip_id,
        "count": len(rows),
        "frames": [
            {"id": r[0], "frame_index": r[1], "video_time_sec": r[2], "abs_ts": r[3], "iso": r[4], "path": r[5]}
            for r in rows
        ],
    }


@app.get("/api/native/clips/range")
def api_native_clips_range():
    """Return the min/max timestamps of all clips — for the timeline widget."""
    native_cam.init_db()
    con = sqlite3.connect(native_cam.DB_PATH)
    row = con.execute("SELECT MIN(start_ts), MAX(end_ts), COUNT(*) FROM clips").fetchone()
    con.close()
    return {"min_ts": row[0], "max_ts": row[1], "total_clips": row[2]}


from fastapi.responses import FileResponse as _FR  # noqa: E402


@app.get("/media/clip/{clip_id}")
def serve_clip(clip_id: int):
    """Serve an mp4 clip by ID."""
    native_cam.init_db()
    con = sqlite3.connect(native_cam.DB_PATH)
    row = con.execute("SELECT local_path FROM clips WHERE id=?", (clip_id,)).fetchone()
    con.close()
    if not row or not row[0] or not Path(row[0]).exists():
        return JSONResponse({"error": "clip not found"}, status_code=404)
    return _FR(row[0], media_type="video/mp4")


@app.get("/media/frame/{frame_id}")
def serve_frame(frame_id: int):
    """Serve a sampled frame JPEG by ID."""
    native_cam.init_db()
    con = sqlite3.connect(native_cam.DB_PATH)
    row = con.execute("SELECT path FROM sampled_frames WHERE id=?", (frame_id,)).fetchone()
    con.close()
    if not row or not row[0] or not Path(row[0]).exists():
        return JSONResponse({"error": "frame not found"}, status_code=404)
    return _FR(row[0], media_type="image/jpeg")


@app.get("/snap/latest.jpg")
def snap_latest():
    p = DATA / "latest.jpg"
    if not p.exists():
        return JSONResponse({"empty": True}, status_code=404)
    return FileResponse(p)


@app.get("/viewer")
def viewer_page():
    """Single-page clip/frame timeline browser."""
    from fastapi.responses import HTMLResponse
    html_path = ROOT / "viewer.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>viewer.html not found at repo root</h1>", status_code=404)


@app.get("/api/counts")
def api_counts(since: str = "1h"):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT cls, COUNT(*) FROM events WHERE ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall()
    n_frames = con.execute("SELECT COUNT(*) FROM frames WHERE ts>=?", (t0,)).fetchone()[0]
    unique_rows = con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall()
    moving_rows = con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? AND moving=1 GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall()
    category_rows = con.execute("SELECT category, COUNT(*) FROM object_tracks WHERE last_ts>=? GROUP BY category ORDER BY 2 DESC", (t0,)).fetchall()
    con.close()
    return {
        "since": since,
        "since_epoch": t0,
        "frames": n_frames,
        "events_by_class": dict(rows),  # old behavior: repeated per-frame detections
        "unique_by_class": dict(unique_rows),
        "moving_unique_by_class": dict(moving_rows),
        "unique_by_category": dict(category_rows),
        "unique_tracks": sum(count for _, count in unique_rows),
        "moving_unique_tracks": sum(count for _, count in moving_rows),
    }


@app.get("/api/object_counts")
def api_object_counts(since: str = "1h"):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    by_class = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall())
    moving_by_class = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? AND moving=1 GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall())
    by_category = dict(con.execute("SELECT category, COUNT(*) FROM object_tracks WHERE last_ts>=? GROUP BY category ORDER BY 2 DESC", (t0,)).fetchall())
    moving_by_category = dict(con.execute("SELECT category, COUNT(*) FROM object_tracks WHERE last_ts>=? AND moving=1 GROUP BY category ORDER BY 2 DESC", (t0,)).fetchall())
    active_by_class = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE active=1 GROUP BY cls ORDER BY 2 DESC").fetchall())
    con.close()
    return {
        "since": since,
        "since_epoch": t0,
        "unique_by_class": by_class,
        "moving_unique_by_class": moving_by_class,
        "unique_by_category": by_category,
        "moving_unique_by_category": moving_by_category,
        "active_by_class": active_by_class,
        "unique_tracks": sum(by_class.values()),
        "moving_unique_tracks": sum(moving_by_class.values()),
    }


@app.get("/api/events")
def api_events(since: str = "10m", limit: int = 500):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT ts, iso, cls, conf FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?", (t0, limit)).fetchall()
    con.close()
    return [{"ts": r[0], "iso": r[1], "cls": r[2], "conf": r[3]} for r in rows]


@app.get("/api/tracks")
def api_tracks(since: str = "1h", limit: int = 500):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT id, cls, category, first_iso, last_iso, best_conf, hits,
               max_displacement, moving, active, first_box, last_box
        FROM object_tracks
        WHERE last_ts>=?
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (t0, limit),
    ).fetchall()
    con.close()
    return [
        {
            "id": r[0],
            "cls": r[1],
            "category": r[2],
            "first_iso": r[3],
            "last_iso": r[4],
            "best_conf": r[5],
            "hits": r[6],
            "max_displacement": r[7],
            "moving": bool(r[8]),
            "active": bool(r[9]),
            "first_box": json.loads(r[10]),
            "last_box": json.loads(r[11]),
        }
        for r in rows
    ]


@app.get("/api/captions")
def api_captions(since: str = "1h", limit: int = 200):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT ts, iso, caption, yolo_summary FROM frames WHERE caption IS NOT NULL AND ts>=? ORDER BY ts DESC LIMIT ?", (t0, limit)).fetchall()
    con.close()
    return [{"ts": r[0], "iso": r[1], "caption": r[2], "yolo": json.loads(r[3] or "{}")} for r in rows]


@app.get("/api/summary")
async def api_summary(since: str = "24h"):
    """Text-only LLM summary over the last `since` of captions + counts."""
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    counts = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall())
    moving_counts = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE last_ts>=? AND moving=1 GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall())
    n_frames = con.execute("SELECT COUNT(*) FROM frames WHERE ts>=?", (t0,)).fetchone()[0]
    caps = con.execute("SELECT iso, caption, yolo_summary FROM frames WHERE caption IS NOT NULL AND ts>=? ORDER BY ts ASC", (t0,)).fetchall()
    con.close()
    if n_frames == 0:
        return {"empty": True, "since": since}

    # Bucket captions by hour to avoid blowing context
    by_hour: dict[str, list[str]] = {}
    for iso, cap, _y in caps:
        hr = iso[:13]
        by_hour.setdefault(hr, []).append(cap)
    # cap each hour to first 6 to stay cheap
    digest_lines = []
    for hr in sorted(by_hour.keys()):
        sample = by_hour[hr][:6]
        digest_lines.append(f"{hr}: " + " | ".join(sample))
    digest = "\n".join(digest_lines[-200:])  # safety

    try:
        from openai import AsyncAzureOpenAI
    except Exception:
        return {"error": "openai sdk missing"}
    client = AsyncAzureOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_API_KEY"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    deploy = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o-mini")
    prompt = (
        f"You are a CCTV log analyst. The camera ran for the last {since}. "
        f"Total frames analysed: {n_frames}. Unique object tracks: {counts}. Moving object tracks: {moving_counts}.\n"
        f"Hour-bucketed scene captions (sampled):\n{digest}\n\n"
        "Write a concise narrative summary for the home owner, structured as:\n"
        "1) **Overview** (1–2 lines)\n2) **Notable events** (bulleted, with times)\n"
        "3) **People & vehicles** (with rough counts)\n4) **Quiet periods**\n"
        "5) **Anything unusual** (or 'nothing unusual')."
    )
    try:
        r = await client.chat.completions.create(
            model=deploy,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600, temperature=0.4,
        )
        text = r.choices[0].message.content.strip()
    finally:
        await client.close()
    return {"since": since, "frames": n_frames, "counts": counts, "moving_counts": moving_counts, "summary": text}


class SayBody(BaseModel):
    text: str


@app.post("/api/say")
def api_say(body: SayBody):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT INTO say_queue(ts, text) VALUES(?, ?)", (time.time(), body.text))
    con.commit()
    new_id = cur.lastrowid
    con.close()
    return {"id": new_id, "text": body.text}


@app.get("/api/say/pending")
def api_say_pending(since: int = 0):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id, ts, text FROM say_queue WHERE id>? ORDER BY id ASC", (since,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "text": r[2]} for r in rows]


@app.get("/stream")
def stream():
    """High-FPS MJPEG of raw rotated camera frames (data/live.jpg)."""
    return _mjpeg_stream(DATA / "live.jpg")


@app.get("/stream/annotated")
def stream_annotated():
    """1 FPS MJPEG of YOLO+caption-annotated frames (data/latest.jpg)."""
    return _mjpeg_stream(DATA / "latest.jpg")


def _mjpeg_stream(path: Path):
    from fastapi.responses import StreamingResponse

    boundary = "frame"

    async def gen():
        last_mtime = 0.0
        while True:
            try:
                if path.exists():
                    mt = path.stat().st_mtime
                    if mt != last_mtime:
                        last_mtime = mt
                        b = path.read_bytes()
                        yield (
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(b)}\r\n\r\n"
                        ).encode() + b + b"\r\n"
            except Exception:
                pass
            await asyncio.sleep(0.05)  # poll fast for live stream

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")


@app.get("/view")
def view_page():
    from fastapi.responses import HTMLResponse

    html = """<!doctype html>
<html><head><meta charset=utf-8><title>AiCam Live</title>
<style>
  body{margin:0;background:#111;color:#eee;font-family:-apple-system,Segoe UI,sans-serif}
  .wrap{display:flex;flex-direction:column;height:100vh}
  .top{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;background:#000}
  .top h1{margin:0;font-size:16px;font-weight:500}
  .pill{padding:3px 10px;border-radius:10px;background:#222;font-size:12px}
  .main{flex:1;display:grid;grid-template-columns:2fr 1fr;gap:8px;padding:8px;overflow:hidden}
  .video{background:#000;display:flex;align-items:center;justify-content:center;border-radius:6px;overflow:hidden}
  .video img{max-width:100%;max-height:100%;object-fit:contain}
  .side{display:flex;flex-direction:column;gap:8px;overflow:hidden}
  .card{background:#1c1c1c;border-radius:6px;padding:10px;overflow:auto}
  .card h2{margin:0 0 6px;font-size:13px;color:#7cf}
  .cap{font-size:14px;line-height:1.4;margin-bottom:8px}
  .meta{color:#aaa;font-size:11px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  td{padding:3px 6px;border-bottom:1px solid #2a2a2a}
  .qty{text-align:right;color:#ffd166}
  .row{display:flex;gap:6px;align-items:center}
  input,button,select{background:#222;color:#eee;border:1px solid #333;padding:6px 8px;border-radius:4px;font-size:12px}
  button{cursor:pointer}
</style></head>
<body>
<div class=wrap>
  <div class=top>
    <h1>AiCam — Live</h1>
    <div class=row>
      <select id=streamSel>
        <option value="/stream">Live (raw)</option>
        <option value="/stream/annotated">Annotated (1 FPS)</option>
      </select>
      <span id=device class=pill>—</span>
      <span id=fps class=pill>0 fps</span>
    </div>
  </div>
  <div class=main>
    <div class=video><img id=stream src="/stream" alt="live"/></div>
    <div class=side>
      <div class=card>
        <h2>Latest caption</h2>
        <div id=cap class=cap>…</div>
        <div id=capMeta class=meta></div>
      </div>
      <div class=card>
        <h2>Unique counts (last <select id=since><option>10m</option><option selected>1h</option><option>6h</option><option>24h</option></select>)</h2>
        <table id=tbl><tbody></tbody></table>
        <div id=tblMeta class=meta style="margin-top:6px"></div>
      </div>
      <div class=card>
        <h2>Speak on phone</h2>
        <div class=row>
          <input id=sayTxt placeholder="text…" style="flex:1"/>
          <button onclick=doSay()>Say</button>
        </div>
        <div id=sayMeta class=meta style="margin-top:4px"></div>
      </div>
    </div>
  </div>
</div>
<script>
let lastFrameTs=0,frames=0;
const img=document.getElementById('stream');
img.addEventListener('load',()=>{frames++});
setInterval(()=>{document.getElementById('fps').textContent=frames+' fps';frames=0},1000);
document.getElementById('streamSel').addEventListener('change',e=>{img.src=e.target.value+'?'+Date.now()});

async function refresh(){
  try{
    const h=await(await fetch('/healthz')).json();
    document.getElementById('device').textContent=h.device||'—';
    const lat=await(await fetch('/api/latest')).json();
    if(lat&&!lat.empty){
      document.getElementById('cap').textContent=lat.caption||'(no caption yet)';
      const yolo=Object.entries(lat.yolo||{}).map(([k,v])=>k+':'+v).join(', ');
      document.getElementById('capMeta').textContent=lat.iso+(yolo?' · '+yolo:'');
    }
    const since=document.getElementById('since').value;
    const c=await(await fetch('/api/counts?since='+since)).json();
    const tb=document.querySelector('#tbl tbody');
    tb.innerHTML='';
    const e=c.unique_by_class||{};
    Object.entries(e).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{
      tb.insertAdjacentHTML('beforeend','<tr><td>'+k+'</td><td class=qty>'+v+'</td></tr>');
    });
    if(!Object.keys(e).length){tb.innerHTML='<tr><td colspan=2 class=meta>(no unique tracks yet)</td></tr>';}
    document.getElementById('tblMeta').textContent=(c.unique_tracks||0)+' unique · '+(c.moving_unique_tracks||0)+' moving · '+c.frames+' frames';
  }catch(e){}
}
async function doSay(){
  const t=document.getElementById('sayTxt').value.trim();
  if(!t)return;
  const r=await fetch('/api/say',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  const j=await r.json();
  document.getElementById('sayMeta').textContent='queued #'+j.id;
  document.getElementById('sayTxt').value='';
}
document.getElementById('since').addEventListener('change',refresh);
refresh();setInterval(refresh,2000);
</script></body></html>"""
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8100)

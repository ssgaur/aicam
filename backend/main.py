"""AiCam backend — SAM 2 + YOLOv8 + per-frame logging + periodic captioning + TTS messages."""
import asyncio
import base64
import io
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont

load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snaps"
DB_PATH = DATA / "events.db"
SNAPS.mkdir(parents=True, exist_ok=True)

CAPTION_EVERY_SEC = 3.0  # ~$2-3/day at 1 FPS, well under $10
SAM_MAX_SIDE = 320
YOLO_CONF = 0.35

_state = {"sam": None, "yolo": None, "device": None, "last_caption_ts": 0.0, "caption_inflight": False}


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
        CREATE TABLE IF NOT EXISTS say_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            text TEXT
        );
        """
    )
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _init_db()
    _load_models()
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
        d.text((x1 + 4, y1 + 4), f'{det["cls"]} {det["conf"]:.2f}', fill=(255, 255, 255), font=small)
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
        con.execute("INSERT INTO events(ts,iso,cls,conf,box) VALUES(?,?,?,?,?)",
                    (ts, iso, d["cls"], d["conf"], json.dumps(d["box"])))
    con.commit(); con.close()


async def _process_frame(jpeg: bytes) -> bytes:
    """Heavy processing in a thread; returns SAM overlay PNG to send to phone."""
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size

    def cpu_work():
        small = img.copy()
        scale = SAM_MAX_SIDE / max(w, h)
        if scale < 1.0:
            small = small.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
        arr_small = np.array(small)
        overlay = _segment_overlay(arr_small, w, h)
        dets = _yolo_detect(img)
        return overlay, dets

    overlay, dets = await asyncio.to_thread(cpu_work)
    ts = time.time()

    # Schedule caption every CAPTION_EVERY_SEC, non-blocking
    caption = None
    if (not _state["caption_inflight"]) and (ts - _state["last_caption_ts"] >= CAPTION_EVERY_SEC):
        _state["last_caption_ts"] = ts
        _state["caption_inflight"] = True
        async def _do_caption(img_for_cap):
            try:
                cap = await _caption_async(img_for_cap)
                if cap:
                    annotated = _annotate(img_for_cap, dets, cap)
                    _save_frame(time.time(), annotated, dets, cap)
            finally:
                _state["caption_inflight"] = False
        asyncio.create_task(_do_caption(img))
    else:
        # save annotated even without caption (so we never lose a frame)
        annotated = _annotate(img, dets, None)
        _save_frame(ts, annotated, dets, None)

    return overlay


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


@app.get("/snap/latest.jpg")
def snap_latest():
    p = DATA / "latest.jpg"
    if not p.exists():
        return JSONResponse({"empty": True}, status_code=404)
    return FileResponse(p)


@app.get("/api/counts")
def api_counts(since: str = "1h"):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT cls, COUNT(*) FROM events WHERE ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall()
    n_frames = con.execute("SELECT COUNT(*) FROM frames WHERE ts>=?", (t0,)).fetchone()[0]
    con.close()
    return {"since": since, "since_epoch": t0, "frames": n_frames, "events_by_class": dict(rows)}


@app.get("/api/events")
def api_events(since: str = "10m", limit: int = 500):
    t0 = _parse_since(since)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT ts, iso, cls, conf FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?", (t0, limit)).fetchall()
    con.close()
    return [{"ts": r[0], "iso": r[1], "cls": r[2], "conf": r[3]} for r in rows]


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
    counts = dict(con.execute("SELECT cls, COUNT(*) FROM events WHERE ts>=? GROUP BY cls ORDER BY 2 DESC", (t0,)).fetchall())
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
        f"Total frames analysed: {n_frames}. Object counts (YOLO, total detections): {counts}.\n"
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
    return {"since": since, "frames": n_frames, "counts": counts, "summary": text}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8100)
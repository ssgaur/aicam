#!/usr/bin/env python3
"""Native OnePlus camera chunk recorder + local object counting pipeline.

Records high-quality MP4 chunks using the phone's native Camera app over ADB,
pulls them to this Mac, samples frames, runs YOLO, tracks unique objects, and
stores everything in SQLite.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "native_camera"
CLIPS = DATA / "clips"
FRAMES = DATA / "frames"
DB_PATH = DATA / "native_camera.db"
YOLO_MODEL = ROOT / "checkpoints" / "yolov8n.pt"

TRACKED_CLASSES = {"person", "bicycle", "car", "motorcycle", "bus", "truck", "dog", "cat"}
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
TRACK_TTL_SEC = 12.0
TRACK_IOU_MIN = 0.12
TRACK_CENTER_MAX_PX = 160.0
TRACK_MOVE_PX = 50.0


@dataclass
class ClipJob:
    clip_id: int
    phone_path: str
    local_path: Path
    frames_dir: Path


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def run_cmd(args: list[str], *, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"command failed: {' '.join(args)}").strip())
    return proc


def adb(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    if not shutil.which("adb"):
        raise RuntimeError("adb not found. Install android-platform-tools.")
    return run_cmd(["adb", *args], check=check)


def device_ready() -> None:
    out = adb(["devices"]).stdout.splitlines()[1:]
    devices = [line.split()[0] for line in out if len(line.split()) >= 2 and line.split()[1] == "device"]
    if not devices:
        raise RuntimeError("No authorized Android device found via adb.")
    if len(devices) > 1 and not os.environ.get("ANDROID_SERIAL"):
        raise RuntimeError(f"Multiple adb devices found; set ANDROID_SERIAL. Devices: {devices}")


def launch_video_camera() -> None:
    adb(["shell", "am", "start", "-a", "android.media.action.VIDEO_CAPTURE", "--ei", "android.intent.extra.durationLimit", "10"])
    time.sleep(2.5)


def tap(x: int, y: int) -> None:
    adb(["shell", "input", "tap", str(x), str(y)])


def list_phone_videos() -> list[tuple[float, str]]:
    script = (
        "find /sdcard/DCIM/Camera -maxdepth 1 -type f "
        "\\( -iname '*.mp4' -o -iname '*.3gp' \\) "
        "-printf '%T@ %p\\n' 2>/dev/null | sort -nr | head -n 100"
    )
    proc = adb(["shell", script], check=False)
    videos: list[tuple[float, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            videos.append((float(parts[0]), parts[1]))
        except ValueError:
            continue
    return videos


def wait_for_new_video(before: set[str], min_mtime: float, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    newest_candidate = ""
    while time.time() < deadline:
        videos = list_phone_videos()
        for mtime, path in videos:
            if path not in before and mtime >= min_mtime - 5:
                return path
        if videos:
            mtime, path = videos[0]
            if path not in before:
                newest_candidate = path
        time.sleep(0.5)
    if newest_candidate:
        return newest_candidate
    videos = list_phone_videos()
    if videos:
        return videos[0][1]
    raise RuntimeError("No recorded video found on phone after stopping recording.")


def pull_video(phone_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    adb(["pull", phone_path, str(local_path)])
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise RuntimeError(f"Pulled file is missing/empty: {local_path}")


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CLIPS.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts REAL NOT NULL,
            end_ts REAL NOT NULL,
            start_iso TEXT NOT NULL,
            end_iso TEXT NOT NULL,
            phone_path TEXT,
            local_path TEXT,
            frames_dir TEXT,
            duration_sec REAL,
            video_fps REAL,
            sampled_fps REAL,
            sampled_frames INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sampled_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL REFERENCES clips(id),
            frame_index INTEGER NOT NULL,
            video_time_sec REAL NOT NULL,
            abs_ts REAL NOT NULL,
            iso TEXT NOT NULL,
            path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id INTEGER NOT NULL REFERENCES clips(id),
            frame_id INTEGER NOT NULL REFERENCES sampled_frames(id),
            track_id INTEGER,
            video_time_sec REAL NOT NULL,
            abs_ts REAL NOT NULL,
            cls TEXT NOT NULL,
            category TEXT NOT NULL,
            conf REAL NOT NULL,
            box TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS object_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cls TEXT NOT NULL,
            category TEXT NOT NULL,
            first_clip_id INTEGER NOT NULL,
            last_clip_id INTEGER NOT NULL,
            first_frame_id INTEGER,
            last_frame_id INTEGER,
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
        CREATE INDEX IF NOT EXISTS idx_native_clips_start ON clips(start_ts);
        CREATE INDEX IF NOT EXISTS idx_native_detections_cls_ts ON detections(cls, abs_ts);
        CREATE INDEX IF NOT EXISTS idx_native_tracks_cls_last ON object_tracks(cls, last_ts);
        CREATE INDEX IF NOT EXISTS idx_native_tracks_category_last ON object_tracks(category, last_ts);
        """
    )
    con.commit()
    con.close()


def create_clip(start_ts: float, end_ts: float) -> tuple[int, Path, Path]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO clips(start_ts,end_ts,start_iso,end_iso,status)
        VALUES(?,?,?,?,?)
        """,
        (start_ts, end_ts, iso(start_ts), iso(end_ts), "recorded"),
    )
    clip_id = int(cur.lastrowid)
    con.commit()
    con.close()
    stamp = datetime.fromtimestamp(start_ts).strftime("%Y%m%d-%H%M%S")
    local_path = CLIPS / f"clip_{clip_id:06d}_{stamp}.mp4"
    frames_dir = FRAMES / f"clip_{clip_id:06d}_{stamp}"
    return clip_id, local_path, frames_dir


def mark_clip_pulled(clip_id: int, phone_path: str, local_path: Path, frames_dir: Path) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE clips SET phone_path=?, local_path=?, frames_dir=?, status=? WHERE id=?",
        (phone_path, str(local_path), str(frames_dir), "pulled", clip_id),
    )
    con.commit()
    con.close()


def mark_clip_error(clip_id: int, error: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE clips SET status=?, error=? WHERE id=?", ("error", error, clip_id))
    con.commit()
    con.close()


def yolo_device() -> str:
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def box_center(box: list[float]) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def box_diag(box: list[float]) -> float:
    x1, y1, x2, y2 = box
    return math.hypot(max(1.0, x2 - x1), max(1.0, y2 - y1))


def box_iou(a: list[float], b: list[float]) -> float:
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


def center_distance(a: list[float], b: list[float]) -> float:
    ax, ay = box_center(a)
    bx, by = box_center(b)
    return math.hypot(ax - bx, ay - by)


class GlobalTracker:
    def __init__(self) -> None:
        self.active: list[dict[str, Any]] = []

    def match_score(self, track: dict[str, Any], det: dict[str, Any]) -> float:
        if track["cls"] != det["cls"]:
            return 0.0
        if det["abs_ts"] - track["last_ts"] > TRACK_TTL_SEC:
            return 0.0
        iou = box_iou(track["last_box"], det["box"])
        dist = center_distance(track["last_box"], det["box"])
        dist_limit = max(TRACK_CENTER_MAX_PX, box_diag(det["box"]) * 1.25, box_diag(track["last_box"]) * 1.25)
        if iou >= TRACK_IOU_MIN:
            return 2.0 + iou
        if dist <= dist_limit:
            return 1.0 - min(0.99, dist / dist_limit)
        return 0.0

    def expire(self, con: sqlite3.Connection, now_ts: float) -> None:
        keep = []
        expired = []
        for track in self.active:
            if now_ts - track["last_ts"] > TRACK_TTL_SEC:
                expired.append(track["id"])
            else:
                keep.append(track)
        self.active = keep
        if expired:
            con.executemany("UPDATE object_tracks SET active=0 WHERE id=?", [(track_id,) for track_id in expired])

    def create_track(self, con: sqlite3.Connection, det: dict[str, Any]) -> dict[str, Any]:
        category = det["category"]
        cur = con.execute(
            """
            INSERT INTO object_tracks(
                cls, category, first_clip_id, last_clip_id, first_frame_id, last_frame_id,
                first_ts, first_iso, last_ts, last_iso, first_box, last_box,
                best_conf, hits, max_displacement, moving, active
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                det["cls"],
                category,
                det["clip_id"],
                det["clip_id"],
                det["frame_id"],
                det["frame_id"],
                det["abs_ts"],
                iso(det["abs_ts"]),
                det["abs_ts"],
                iso(det["abs_ts"]),
                json.dumps(det["box"]),
                json.dumps(det["box"]),
                det["conf"],
                1,
                0.0,
                0,
                1,
            ),
        )
        track = {
            "id": int(cur.lastrowid),
            "cls": det["cls"],
            "category": category,
            "first_ts": det["abs_ts"],
            "last_ts": det["abs_ts"],
            "first_box": det["box"],
            "last_box": det["box"],
            "best_conf": det["conf"],
            "hits": 1,
            "max_displacement": 0.0,
            "moving": 0,
        }
        self.active.append(track)
        return track

    def update_track(self, con: sqlite3.Connection, track: dict[str, Any], det: dict[str, Any]) -> None:
        first_center = box_center(track["first_box"])
        now_center = box_center(det["box"])
        displacement = math.hypot(now_center[0] - first_center[0], now_center[1] - first_center[1])
        track["last_ts"] = det["abs_ts"]
        track["last_box"] = det["box"]
        track["best_conf"] = max(track["best_conf"], det["conf"])
        track["hits"] += 1
        track["max_displacement"] = max(track["max_displacement"], displacement)
        track["moving"] = 1 if track["hits"] >= 2 and track["max_displacement"] >= TRACK_MOVE_PX else 0
        con.execute(
            """
            UPDATE object_tracks
            SET last_clip_id=?, last_frame_id=?, last_ts=?, last_iso=?, last_box=?,
                best_conf=?, hits=?, max_displacement=?, moving=?, active=1
            WHERE id=?
            """,
            (
                det["clip_id"],
                det["frame_id"],
                det["abs_ts"],
                iso(det["abs_ts"]),
                json.dumps(det["box"]),
                track["best_conf"],
                track["hits"],
                track["max_displacement"],
                track["moving"],
                track["id"],
            ),
        )

    def assign(self, con: sqlite3.Connection, detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assigned: list[dict[str, Any]] = []
        used_tracks: set[int] = set()
        for det in sorted(detections, key=lambda item: item["conf"], reverse=True):
            self.expire(con, det["abs_ts"])
            best = None
            best_score = 0.0
            for track in self.active:
                if track["id"] in used_tracks:
                    continue
                score = self.match_score(track, det)
                if score > best_score:
                    best = track
                    best_score = score
            if best is None:
                best = self.create_track(con, det)
            else:
                self.update_track(con, best, det)
            used_tracks.add(best["id"])
            det["track_id"] = best["id"]
            assigned.append(det)
        return assigned


def load_model() -> tuple[YOLO, str]:
    model = YOLO(str(YOLO_MODEL))
    device = yolo_device()
    _ = model.predict(np.zeros((320, 320, 3), dtype=np.uint8), device=device, verbose=False)
    return model, device


def sample_times(duration: float, sample_fps: float) -> list[float]:
    if duration <= 0:
        return []
    step = 1.0 / sample_fps
    times = []
    t = 0.0
    while t < duration:
        times.append(t)
        t += step
    return times


def run_yolo(model: YOLO, device: str, frame: np.ndarray, conf: float) -> list[dict[str, Any]]:
    result = model.predict(frame, conf=conf, device=device, verbose=False)[0]
    names = result.names
    detections: list[dict[str, Any]] = []
    if result.boxes is None:
        return detections
    for box, score, cls_id in zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist()):
        cls = names[int(cls_id)]
        if cls not in TRACKED_CLASSES:
            continue
        detections.append(
            {
                "cls": cls,
                "category": CLASS_CATEGORY.get(cls, cls),
                "conf": float(score),
                "box": [float(x) for x in box],
            }
        )
    return detections


def process_clip(job: ClipJob, model: YOLO, device: str, tracker: GlobalTracker, sample_fps: float, conf: float) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        clip = con.execute("SELECT start_ts FROM clips WHERE id=?", (job.clip_id,)).fetchone()
        if not clip:
            raise RuntimeError(f"clip missing in DB: {job.clip_id}")
        start_ts = float(clip[0])

        cap = cv2.VideoCapture(str(job.local_path))
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video: {job.local_path}")
        video_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = frame_count / video_fps if video_fps > 0 and frame_count > 0 else 0.0
        if duration <= 0:
            duration = 10.0
        job.frames_dir.mkdir(parents=True, exist_ok=True)

        sampled_count = 0
        for frame_index, video_t in enumerate(sample_times(duration, sample_fps)):
            cap.set(cv2.CAP_PROP_POS_MSEC, video_t * 1000.0)
            ok, frame_bgr = cap.read()
            if not ok:
                continue
            frame_path = job.frames_dir / f"frame_{frame_index:04d}_{video_t:05.2f}s.jpg"
            cv2.imwrite(str(frame_path), frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            abs_ts = start_ts + video_t
            cur = con.execute(
                """
                INSERT INTO sampled_frames(clip_id,frame_index,video_time_sec,abs_ts,iso,path)
                VALUES(?,?,?,?,?,?)
                """,
                (job.clip_id, frame_index, video_t, abs_ts, iso(abs_ts), str(frame_path)),
            )
            frame_id = int(cur.lastrowid)
            detections = run_yolo(model, device, frame_bgr, conf)
            for det in detections:
                det.update({"clip_id": job.clip_id, "frame_id": frame_id, "video_time_sec": video_t, "abs_ts": abs_ts})
            assigned = tracker.assign(con, detections)
            for det in assigned:
                con.execute(
                    """
                    INSERT INTO detections(clip_id,frame_id,track_id,video_time_sec,abs_ts,cls,category,conf,box)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job.clip_id,
                        frame_id,
                        det["track_id"],
                        video_t,
                        abs_ts,
                        det["cls"],
                        det["category"],
                        det["conf"],
                        json.dumps(det["box"]),
                    ),
                )
            sampled_count += 1
            con.commit()
        cap.release()

        con.execute(
            """
            UPDATE clips
            SET status='processed', duration_sec=?, video_fps=?, sampled_fps=?,
                sampled_frames=?, processed_at=datetime('now')
            WHERE id=?
            """,
            (duration, video_fps, sample_fps, sampled_count, job.clip_id),
        )
        con.commit()
    except Exception as exc:
        con.execute("UPDATE clips SET status='error', error=? WHERE id=?", (str(exc), job.clip_id))
        con.commit()
        raise
    finally:
        con.close()


def clip_counts(clip_id: int) -> dict[str, Any]:
    con = sqlite3.connect(DB_PATH)
    by_class = dict(
        con.execute(
            """
            SELECT cls, COUNT(DISTINCT track_id)
            FROM detections
            WHERE clip_id=? AND track_id IS NOT NULL
            GROUP BY cls ORDER BY 2 DESC
            """,
            (clip_id,),
        ).fetchall()
    )
    by_category = dict(
        con.execute(
            """
            SELECT category, COUNT(DISTINCT track_id)
            FROM detections
            WHERE clip_id=? AND track_id IS NOT NULL
            GROUP BY category ORDER BY 2 DESC
            """,
            (clip_id,),
        ).fetchall()
    )
    moving = dict(
        con.execute(
            """
            SELECT t.cls, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id=? AND t.moving=1
            GROUP BY t.cls ORDER BY 2 DESC
            """,
            (clip_id,),
        ).fetchall()
    )
    con.close()
    return {"unique_by_class": by_class, "unique_by_category": by_category, "moving_unique_by_class": moving}


def latest_status() -> dict[str, Any]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    clip = con.execute("SELECT * FROM clips ORDER BY id DESC LIMIT 1").fetchone()
    if not clip:
        return {"empty": True, "now": iso(time.time())}
    clip_dict = dict(clip)
    frames = con.execute("SELECT COUNT(*) FROM sampled_frames WHERE clip_id=?", (clip["id"],)).fetchone()[0]
    detections = con.execute("SELECT COUNT(*) FROM detections WHERE clip_id=?", (clip["id"],)).fetchone()[0]
    counts = clip_counts(int(clip["id"]))
    con.close()
    return {
        "now": iso(time.time()),
        "last_clip": clip_dict,
        "sampled_frames": frames,
        "detections": detections,
        **counts,
    }


def print_status(*, json_mode: bool = False) -> None:
    status = latest_status()
    if json_mode:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return
    if status.get("empty"):
        print(f"Current time: {status['now']}")
        print("No native camera chunks recorded yet.")
        return

    clip = status["last_clip"]
    print(f"Current time: {status['now']}")
    print(f"Last chunk: #{clip['id']} · {clip['start_iso']} → {clip['end_iso']} · status={clip['status']}")
    print(f"Phone file: {clip.get('phone_path')}")
    print(f"Local clip: {clip.get('local_path')}")
    print(f"Processed images folder: {clip.get('frames_dir')}")
    print(f"Sampled images: {status['sampled_frames']} at {clip.get('sampled_fps')} fps")
    print(f"Detection rows: {status['detections']}")
    print("Unique object counts: " + (json.dumps(status["unique_by_class"], sort_keys=True) if status["unique_by_class"] else "{}"))
    print("Moving object counts: " + (json.dumps(status["moving_unique_by_class"], sort_keys=True) if status["moving_unique_by_class"] else "{}"))


def worker_loop(work_q: "queue.Queue[ClipJob | None]", sample_fps: float, conf: float) -> None:
    model, device = load_model()
    tracker = GlobalTracker()
    while True:
        job = work_q.get()
        if job is None:
            work_q.task_done()
            break
        try:
            process_clip(job, model, device, tracker, sample_fps, conf)
            counts = clip_counts(job.clip_id)
            print(
                json.dumps(
                    {
                        "processed_clip": job.clip_id,
                        "file": str(job.local_path),
                        "frames_dir": str(job.frames_dir),
                        **counts,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            mark_clip_error(job.clip_id, str(exc))
            print(json.dumps({"processed_clip": job.clip_id, "error": str(exc)}, ensure_ascii=False), flush=True)
        finally:
            work_q.task_done()


def run_chunks(args: argparse.Namespace) -> None:
    init_db()
    device_ready()
    if args.open_camera:
        launch_video_camera()

    work_q: "queue.Queue[ClipJob | None]" = queue.Queue()
    worker = threading.Thread(target=worker_loop, args=(work_q, args.sample_fps, args.conf), daemon=True)
    worker.start()

    known = {path for _, path in list_phone_videos()}
    recording_already_started = False
    start_ts = 0.0
    for index in range(args.chunks):
        if recording_already_started:
            print(json.dumps({"recording_chunk": index + 1, "start_iso": iso(start_ts), "pipelined": True}, ensure_ascii=False), flush=True)
        else:
            start_ts = time.time()
            print(json.dumps({"recording_chunk": index + 1, "start_iso": iso(start_ts), "pipelined": False}, ensure_ascii=False), flush=True)
            tap(args.record_x, args.record_y)

        time.sleep(args.duration)
        stop_ts = time.time()
        tap(args.record_x, args.record_y)
        clip_id, local_path, frames_dir = create_clip(start_ts, stop_ts)

        time.sleep(args.save_settle)
        phone_path = wait_for_new_video(known, stop_ts, timeout=args.find_timeout)
        known.add(phone_path)

        next_start_ts = 0.0
        start_next = index < args.chunks - 1
        if start_next:
            time.sleep(args.restart_settle)
            next_start_ts = time.time()
            tap(args.record_x, args.record_y)
            print(
                json.dumps(
                    {
                        "started_next_chunk": index + 2,
                        "start_iso": iso(next_start_ts),
                        "gap_sec": round(next_start_ts - stop_ts, 3),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # Pull/process previous clip while the next chunk is already recording.
        pull_video(phone_path, local_path)
        mark_clip_pulled(clip_id, phone_path, local_path, frames_dir)
        work_q.put(ClipJob(clip_id=clip_id, phone_path=phone_path, local_path=local_path, frames_dir=frames_dir))
        print(
            json.dumps(
                {
                    "pulled_clip": clip_id,
                    "chunk": index + 1,
                    "from": iso(start_ts),
                    "to": iso(stop_ts),
                    "phone_path": phone_path,
                    "local_path": str(local_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if start_next:
            recording_already_started = True
            start_ts = next_start_ts
        else:
            recording_already_started = False

    work_q.put(None)
    work_q.join()
    print_status(json_mode=args.json_status)


def reset_db() -> None:
    if DB_PATH.exists():
        backup = DB_PATH.with_name(f"{DB_PATH.stem}.backup-{int(time.time())}{DB_PATH.suffix}")
        shutil.copy2(DB_PATH, backup)
        DB_PATH.unlink()
        print(f"backup={backup}")
    init_db()
    print(f"db={DB_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Native Android Camera 10-second chunk object counter.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Record, pull, process chunks")
    run.add_argument("--chunks", type=int, default=1)
    run.add_argument("--duration", type=float, default=10.0)
    run.add_argument("--sample-fps", type=float, default=3.0)
    run.add_argument("--conf", type=float, default=0.35)
    run.add_argument("--record-x", type=int, default=540)
    run.add_argument("--record-y", type=int, default=2037)
    run.add_argument("--save-settle", type=float, default=1.0)
    run.add_argument("--restart-settle", type=float, default=0.8, help="Delay after stop before starting next chunk")
    run.add_argument("--find-timeout", type=float, default=8.0)
    run.add_argument("--open-camera", action=argparse.BooleanOptionalAction, default=True)

    status = sub.add_parser("status", help="Print latest chunk status")
    status.add_argument("--json", action="store_true", dest="json_status")
    sub.add_parser("reset-db", help="Backup and reset native camera SQLite DB")

    args = parser.parse_args()
    if args.cmd == "run":
        args.json_status = getattr(args, "json_status", True)
        run_chunks(args)
    elif args.cmd == "status":
        init_db()
        print_status(json_mode=args.json_status)
    elif args.cmd == "reset-db":
        reset_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

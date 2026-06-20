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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("AICAM_NATIVE_DATA", str(ROOT / "data" / "native_camera"))).expanduser()
CLIPS = DATA / "clips"
FRAMES = DATA / "frames"
AUDITS = DATA / "audits"
TO_BE_DELETED = DATA / "to-be-deleted"
TO_BE_DELETED_CLIPS = TO_BE_DELETED / "clips"
TO_BE_DELETED_FRAMES = TO_BE_DELETED / "frames"
DB_PATH = DATA / "native_camera.db"
PROCESSING_LOG = ROOT / "data" / "processing.log"
YOLO_MODEL = ROOT / "checkpoints" / "yolov8n.pt"


def _plog(msg: str) -> None:
    """Append a line to data/processing.log (tail -f friendly)."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with PROCESSING_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

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

# ─── Azure Blob Storage helper ───
_blob_service = None

def _get_blob_service():
    """Lazy-init Azure BlobServiceClient from env vars."""
    global _blob_service
    if _blob_service is None:
        account = os.environ.get("AZURE_STORAGE_ACCOUNT")
        key = os.environ.get("AZURE_STORAGE_KEY")
        if not account or not key:
            raise RuntimeError("AZURE_STORAGE_ACCOUNT/KEY not set")
        from azure.storage.blob import BlobServiceClient
        _blob_service = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=key,
        )
    return _blob_service


def _upload_to_blob(local_path: Path, frames_dir: Path) -> tuple[str, list[str]]:
    """Upload clip MP4 + frame JPEGs to Azure Blob Storage. Returns (clip_blob_url, [frame_blob_urls])."""
    svc = _get_blob_service()
    clips_container = os.environ.get("AZURE_STORAGE_CLIPS_CONTAINER", "clips")
    frames_container = os.environ.get("AZURE_STORAGE_FRAMES_CONTAINER", "frames")

    # Upload MP4 — use relative path from DATA as blob name
    clip_blob_name = str(local_path.relative_to(DATA)) if local_path.is_relative_to(DATA) else local_path.name
    clip_client = svc.get_blob_client(clips_container, clip_blob_name)
    with open(local_path, "rb") as f:
        clip_client.upload_blob(f, overwrite=True, content_settings=_content_settings("video/mp4"))
    clip_url = clip_client.url

    # Upload frames
    frame_urls = []
    if frames_dir.exists():
        for jpg in sorted(frames_dir.glob("*.jpg")):
            frame_blob_name = str(jpg.relative_to(DATA)) if jpg.is_relative_to(DATA) else f"{frames_dir.name}/{jpg.name}"
            frame_client = svc.get_blob_client(frames_container, frame_blob_name)
            with open(jpg, "rb") as f:
                frame_client.upload_blob(f, overwrite=True, content_settings=_content_settings("image/jpeg"))
            frame_urls.append(frame_client.url)

    return clip_url, frame_urls


def _content_settings(content_type: str):
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type=content_type)


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
    adb(["shell", "am", "start", "-a", "android.media.action.VIDEO_CAPTURE"])
    time.sleep(2.5)


def tap(x: int, y: int) -> None:
    adb(["shell", "input", "tap", str(x), str(y)])


def camera_ui_root() -> ET.Element | None:
    adb(["shell", "uiautomator", "dump", "/sdcard/window.xml"], check=False)
    proc = adb(["exec-out", "cat", "/sdcard/window.xml"], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return ET.fromstring(proc.stdout)
    except ET.ParseError:
        return None


def camera_is_recording() -> bool:
    root = camera_ui_root()
    if root is None:
        return False
    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        if rid in {
            "com.oneplus.camera:id/recording_timer_main_container",
            "com.oneplus.camera:id/video_recording_pause_resume_button",
            "com.oneplus.camera:id/video_recording_pause_resume_button_container",
        }:
            return True
    return False


def wait_recording_state(want_recording: bool, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if camera_is_recording() == want_recording:
            return True
        time.sleep(0.25)
    return camera_is_recording() == want_recording


def ensure_camera_idle(audit_log: Path | None = None) -> None:
    if camera_is_recording():
        audit_write(audit_log, {"event": "camera_was_recording_before_run", "action": "tap_stop"})
        tap(540, 2037)
        wait_recording_state(False, timeout=8.0)
        time.sleep(3.0)


def start_recording(args: argparse.Namespace, audit_log: Path | None) -> float:
    if camera_is_recording():
        raise RuntimeError("Camera is already recording before start; refusing to toggle blindly.")
    last_tap_ts = 0.0
    for attempt in range(1, args.start_retries + 1):
        last_tap_ts = time.time()
        tap(args.record_x, args.record_y)
        if wait_recording_state(True, timeout=args.start_confirm_timeout):
            if attempt > 1:
                audit_write(audit_log, {"event": "record_start_retry_succeeded", "attempt": attempt})
            return last_tap_ts
        audit_write(audit_log, {"event": "record_start_retry", "attempt": attempt, "warning": True})
        time.sleep(args.start_retry_delay)
        if camera_is_recording():
            return last_tap_ts
    raise RuntimeError("Tapped record, but OnePlus Camera did not enter recording state.")


def stop_recording(args: argparse.Namespace, audit_log: Path | None) -> float:
    if not camera_is_recording():
        audit_write(audit_log, {"event": "camera_not_recording_at_stop", "warning": True})
    stop_ts = time.time()
    tap(args.record_x, args.record_y)
    if not wait_recording_state(False, timeout=8.0):
        raise RuntimeError("Tapped stop, but OnePlus Camera still appears to be recording.")
    return stop_ts


def sleep_until_recording_deadline(start_ts: float, duration: float) -> None:
    remaining = (start_ts + duration) - time.time()
    if remaining > 0:
        time.sleep(remaining)


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


def phone_file_size(path: str) -> int:
    quoted = path.replace("'", "'\\''")
    proc = adb(["shell", f"stat -c %s '{quoted}'"], check=False)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def wait_phone_file_stable(phone_path: str, timeout: float = 20.0, min_size: int = 1_000_000) -> int:
    deadline = time.time() + timeout
    last_size = -1
    stable_count = 0
    while time.time() < deadline:
        size = phone_file_size(phone_path)
        if size >= min_size and size == last_size:
            stable_count += 1
        else:
            stable_count = 0
        if stable_count >= 2:
            return size
        last_size = size
        time.sleep(1.0)
    return phone_file_size(phone_path)


def local_video_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1_000_000:
        return False
    cap = cv2.VideoCapture(str(path))
    try:
        return bool(cap.isOpened() and cap.get(cv2.CAP_PROP_FRAME_COUNT) > 0 and cap.get(cv2.CAP_PROP_FPS) > 0)
    finally:
        cap.release()


def pull_video(phone_path: str, local_path: Path, *, validate: bool = True) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    wait_phone_file_stable(phone_path)
    last_error = ""
    for attempt in range(1, 6):
        adb(["pull", phone_path, str(local_path)])
        if not local_path.exists() or local_path.stat().st_size == 0:
            last_error = f"Pulled file is missing/empty: {local_path}"
        elif (not validate) or local_video_ok(local_path):
            return
        else:
            last_error = f"Pulled MP4 is not playable yet: {local_path}"
        time.sleep(2.0 * attempt)
        wait_phone_file_stable(phone_path)
    raise RuntimeError(last_error or f"Could not pull valid video: {phone_path}")


def delete_phone_video(phone_path: str) -> bool:
    quoted = phone_path.replace("'", "'\\''")
    proc = adb(["shell", f"rm -f '{quoted}' && test ! -e '{quoted}'"], check=False)
    return proc.returncode == 0


def seed_sqlite_sequences_from_postgres(con: sqlite3.Connection) -> None:
    """Keep fresh SQLite IDs above durable Postgres IDs after data/native_camera deletion."""
    local_counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("clips", "sampled_frames", "detections", "object_tracks")
    }
    if any(count > 0 for count in local_counts.values()):
        return
    try:
        import postgres_store

        postgres_store.ensure_schema()
        with postgres_store.connect() as pg:
            max_ids = {
                "clips": pg.execute("SELECT COALESCE(MAX(id), 0) FROM native_clips").fetchone()[0],
                "sampled_frames": pg.execute("SELECT COALESCE(MAX(id), 0) FROM native_sampled_frames").fetchone()[0],
                "detections": pg.execute("SELECT COALESCE(MAX(id), 0) FROM native_detections").fetchone()[0],
                "object_tracks": pg.execute("SELECT COALESCE(MAX(id), 0) FROM native_object_tracks").fetchone()[0],
            }
    except Exception as exc:
        print(f"[sqlite-seed] postgres unavailable, starting local ids normally: {exc}")
        return

    for table, max_id in max_ids.items():
        if max_id and max_id > 0:
            cur = con.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (int(max_id), table))
            if cur.rowcount == 0:
                con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)", (table, int(max_id)))
    if any(max_id > 0 for max_id in max_ids.values()):
        print(f"[sqlite-seed] seeded sqlite ids from postgres max ids: {max_ids}")


def init_db() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CLIPS.mkdir(parents=True, exist_ok=True)
    FRAMES.mkdir(parents=True, exist_ok=True)
    AUDITS.mkdir(parents=True, exist_ok=True)
    TO_BE_DELETED_CLIPS.mkdir(parents=True, exist_ok=True)
    TO_BE_DELETED_FRAMES.mkdir(parents=True, exist_ok=True)
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
    # Migrations: add blob_url columns if missing
    cols_clips = {r[1] for r in con.execute("PRAGMA table_info(clips)").fetchall()}
    if "blob_url" not in cols_clips:
        con.execute("ALTER TABLE clips ADD COLUMN blob_url TEXT")
    cols_frames = {r[1] for r in con.execute("PRAGMA table_info(sampled_frames)").fetchall()}
    if "blob_url" not in cols_frames:
        con.execute("ALTER TABLE sampled_frames ADD COLUMN blob_url TEXT")
    seed_sqlite_sequences_from_postgres(con)
    con.commit()
    con.close()


def _time_subdir(ts: float) -> Path:
    """Return year/month/Www/day/hour/minute relative subdir for a clip start ts.

    The leaf "minute" folder holds ~6 ten-second clips and their frames.
    """
    dt = datetime.fromtimestamp(ts)
    year, week, _ = dt.isocalendar()
    return Path(f"{dt.year:04d}") / f"{dt.month:02d}" / f"W{week:02d}" / \
        f"{dt.day:02d}" / f"{dt.hour:02d}" / f"{dt.minute:02d}"


def clip_path_for(clip_id: int, start_ts: float) -> Path:
    stamp = datetime.fromtimestamp(start_ts).strftime("%Y%m%d-%H%M%S")
    return CLIPS / _time_subdir(start_ts) / f"clip_{clip_id:06d}_{stamp}.mp4"


def frames_dir_for(clip_id: int, start_ts: float) -> Path:
    stamp = datetime.fromtimestamp(start_ts).strftime("%Y%m%d-%H%M%S")
    return FRAMES / _time_subdir(start_ts) / f"clip_{clip_id:06d}_{stamp}"


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
    local_path = clip_path_for(clip_id, start_ts)
    frames_dir = frames_dir_for(clip_id, start_ts)
    local_path.parent.mkdir(parents=True, exist_ok=True)
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
    result = model.predict(frame, conf=conf, device=device, imgsz=640, verbose=False)[0]
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


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique destination for {path}")


def maybe_quarantine_empty_clip(con: sqlite3.Connection, clip_id: int, local_path: Path, frames_dir: Path) -> None:
    detection_count = con.execute("SELECT COUNT(*) FROM detections WHERE clip_id=?", (clip_id,)).fetchone()[0]
    if detection_count > 0:
        return
    if not local_path.exists():
        return

    # Mirror the time-bucketed subdir under to-be-deleted/clips and to-be-deleted/frames.
    rel_clip = _relative_to_or_none(local_path, CLIPS) or Path(local_path.name)
    new_clip_path = unique_destination(TO_BE_DELETED_CLIPS / rel_clip)
    new_clip_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(local_path), str(new_clip_path))

    new_frames_dir = frames_dir
    if frames_dir.exists():
        rel_frames = _relative_to_or_none(frames_dir, FRAMES) or Path(frames_dir.name)
        new_frames_dir = unique_destination(TO_BE_DELETED_FRAMES / rel_frames)
        new_frames_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(frames_dir), str(new_frames_dir))
        con.execute(
            "UPDATE sampled_frames SET path = REPLACE(path, ?, ?) WHERE clip_id=?",
            (str(frames_dir), str(new_frames_dir), clip_id),
        )

    con.execute(
        "UPDATE clips SET local_path=?, frames_dir=? WHERE id=?",
        (str(new_clip_path), str(new_frames_dir), clip_id),
    )


def _relative_to_or_none(p: Path, base: Path) -> Path | None:
    try:
        return p.relative_to(base)
    except ValueError:
        return None


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
        _plog(f"━━━ clip #{job.clip_id} START ━━━ duration={duration:.1f}s fps={video_fps:.0f} path={job.local_path}")
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
            obj_names = ", ".join(d["cls"] for d in assigned) if assigned else "—"
            _plog(f"  frame {frame_index:02d} | {frame_path.name} | objects={len(assigned)} | {obj_names}")
            sampled_count += 1
            con.commit()
        cap.release()
        _plog(f"━━━ clip #{job.clip_id} DONE ━━━ frames={sampled_count} stored={job.frames_dir}")
        counts = clip_counts(job.clip_id)
        uniq = counts.get("unique_by_class", {})
        moving = counts.get("moving_unique_by_class", {})
        if uniq:
            _plog(f"    🎯 unique objects: {', '.join(f'{k}:{v}' for k,v in uniq.items())}")
        if moving:
            _plog(f"    🚶 moving: {', '.join(f'{k}:{v}' for k,v in moving.items())}")

        con.execute(
            """
            UPDATE clips
            SET status='processed', duration_sec=?, video_fps=?, sampled_fps=?,
                sampled_frames=?, processed_at=datetime('now')
            WHERE id=?
            """,
            (duration, video_fps, sample_fps, sampled_count, job.clip_id),
        )
        maybe_quarantine_empty_clip(con, job.clip_id, job.local_path, job.frames_dir)
        con.commit()
        try:
            import postgres_store

            postgres_store.sync_clip(DB_PATH, job.clip_id)
        except Exception as pg_exc:
            print(f"[postgres-sync] clip_id={job.clip_id} error={pg_exc}")

        # Cloud mode: upload ALL clips to Azure Blob, then delete locally
        if os.environ.get("AICAM_CLOUD") == "1":
            try:
                clip_url, frame_urls = _upload_to_blob(job.local_path, job.frames_dir)
                con.execute("UPDATE clips SET blob_url=? WHERE id=?", (clip_url, job.clip_id))
                for furl in frame_urls:
                    fname = furl.rsplit("/", 1)[-1]
                    con.execute(
                        "UPDATE sampled_frames SET blob_url=? WHERE clip_id=? AND path LIKE ?",
                        (furl, job.clip_id, f"%{fname}%"),
                    )
                con.commit()
                if job.local_path.exists():
                    job.local_path.unlink()
                if job.frames_dir.exists():
                    shutil.rmtree(job.frames_dir)
                has_det = con.execute(
                    "SELECT COUNT(*) FROM detections WHERE clip_id=?", (job.clip_id,)
                ).fetchone()[0] > 0
                tag = "🎯" if has_det else "📦"
                _plog(f"    {tag} blob uploaded + local deleted ({len(frame_urls)} frames)")
            except Exception as blob_exc:
                _plog(f"    ⚠️  blob upload failed, keeping local: {blob_exc}")
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


def counts_for_clip_ids(clip_ids: list[int]) -> dict[str, Any]:
    if not clip_ids:
        return {
            "unique_by_class": {},
            "unique_by_category": {},
            "moving_unique_by_class": {},
            "moving_unique_by_category": {},
            "unique_tracks": 0,
            "moving_unique_tracks": 0,
        }
    placeholders = ",".join("?" for _ in clip_ids)
    con = sqlite3.connect(DB_PATH)
    by_class = dict(
        con.execute(
            f"""
            SELECT t.cls, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id IN ({placeholders})
            GROUP BY t.cls ORDER BY 2 DESC
            """,
            clip_ids,
        ).fetchall()
    )
    by_category = dict(
        con.execute(
            f"""
            SELECT t.category, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id IN ({placeholders})
            GROUP BY t.category ORDER BY 2 DESC
            """,
            clip_ids,
        ).fetchall()
    )
    moving_by_class = dict(
        con.execute(
            f"""
            SELECT t.cls, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id IN ({placeholders}) AND t.moving=1
            GROUP BY t.cls ORDER BY 2 DESC
            """,
            clip_ids,
        ).fetchall()
    )
    moving_by_category = dict(
        con.execute(
            f"""
            SELECT t.category, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id IN ({placeholders}) AND t.moving=1
            GROUP BY t.category ORDER BY 2 DESC
            """,
            clip_ids,
        ).fetchall()
    )
    con.close()
    return {
        "unique_by_class": by_class,
        "unique_by_category": by_category,
        "moving_unique_by_class": moving_by_class,
        "moving_unique_by_category": moving_by_category,
        "unique_tracks": sum(by_class.values()),
        "moving_unique_tracks": sum(moving_by_class.values()),
    }


def clip_rows(clip_ids: list[int]) -> list[dict[str, Any]]:
    if not clip_ids:
        return []
    placeholders = ",".join("?" for _ in clip_ids)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT * FROM clips WHERE id IN ({placeholders}) ORDER BY id ASC",
        clip_ids,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["counts"] = clip_counts(int(row["id"]))
        out.append(item)
    con.close()
    return out


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num_bytes} B"


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def make_run_report(
    *,
    run_id: str,
    started_ts: float,
    finished_ts: float,
    clip_ids: list[int],
    args: argparse.Namespace,
    audit_log: Path,
) -> dict[str, Any]:
    clips = clip_rows(clip_ids)
    totals = counts_for_clip_ids(clip_ids)
    clip_paths = [Path(c["local_path"]) for c in clips if c.get("local_path")]
    frame_dirs = [Path(c["frames_dir"]) for c in clips if c.get("frames_dir")]
    storage = {
        "clips_bytes": sum(dir_size(p) for p in clip_paths),
        "frames_bytes": sum(dir_size(p) for p in frame_dirs),
    }
    storage["total_bytes"] = storage["clips_bytes"] + storage["frames_bytes"]

    report = {
        "run_id": run_id,
        "started_iso": iso(started_ts),
        "finished_iso": iso(finished_ts),
        "duration_sec": round(finished_ts - started_ts, 3),
        "requested_chunks": args.chunks,
        "requested_chunk_duration_sec": args.duration,
        "sample_fps": args.sample_fps,
        "confidence": args.conf,
        "audit_log": str(audit_log),
        "db_path": str(DB_PATH),
        "clips_dir": str(CLIPS),
        "frames_dir": str(FRAMES),
        "clips_total": len(clips),
        "clips_processed": sum(1 for c in clips if c.get("status") == "processed"),
        "clips_error": sum(1 for c in clips if c.get("status") == "error"),
        "sampled_frames_total": sum(int(c.get("sampled_frames") or 0) for c in clips),
        "storage": {
            **storage,
            "clips": human_size(storage["clips_bytes"]),
            "frames": human_size(storage["frames_bytes"]),
            "total": human_size(storage["total_bytes"]),
        },
        "totals": totals,
        "clips": clips,
        "azure_vision_calls": 0,
        "azure_vision_cost": 0.0,
        "copilot_required": False,
        "internet_required_after_setup": False,
    }
    return report


def write_report_files(report: dict[str, Any]) -> tuple[Path, Path]:
    report_json = AUDITS / f"{report['run_id']}_summary.json"
    report_md = AUDITS / f"{report['run_id']}_summary.md"
    report_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# Native Camera Run {report['run_id']}",
        "",
        f"- Started: `{report['started_iso']}`",
        f"- Finished: `{report['finished_iso']}`",
        f"- Clips: `{report['clips_processed']}/{report['clips_total']}` processed",
        f"- Sampled frames: `{report['sampled_frames_total']}`",
        f"- Storage: `{report['storage']['total']}` (clips `{report['storage']['clips']}`, frames `{report['storage']['frames']}`)",
        f"- Azure Vision calls: `{report['azure_vision_calls']}`",
        f"- Azure Vision cost: `${report['azure_vision_cost']:.2f}`",
        "",
        "## Overall counts",
        "",
        f"- Unique: `{format_counts(report['totals']['unique_by_class'])}`",
        f"- Moving: `{format_counts(report['totals']['moving_unique_by_class'])}`",
        "",
        "## Per-clip table",
        "",
        "| Clip | Time | Frames | Counts | Moving | File |",
        "| ---: | --- | ---: | --- | --- | --- |",
    ]
    for c in report["clips"]:
        counts = c["counts"]
        lines.append(
            "| {id} | {start} → {end} | {frames} | {counts} | {moving} | `{file}` |".format(
                id=c["id"],
                start=c["start_iso"],
                end=c["end_iso"],
                frames=c.get("sampled_frames") or 0,
                counts=format_counts(counts["unique_by_class"]),
                moving=format_counts(counts["moving_unique_by_class"]),
                file=c.get("local_path") or "",
            )
        )
    lines.extend(["", f"Audit log: `{report['audit_log']}`", f"SQLite DB: `{report['db_path']}`", ""])
    report_md.write_text("\n".join(lines), encoding="utf-8")
    return report_json, report_md


def print_run_summary(report: dict[str, Any], report_json: Path, report_md: Path) -> None:
    print("\n=== FINAL RUN SUMMARY ===")
    print(f"Current time: {iso(time.time())}")
    print(f"Run: {report['started_iso']} → {report['finished_iso']}")
    print(f"Clips processed: {report['clips_processed']}/{report['clips_total']}")
    print(f"Sampled images: {report['sampled_frames_total']}")
    print(f"Storage: {report['storage']['total']} (clips {report['storage']['clips']}, frames {report['storage']['frames']})")
    print(f"Azure Vision API calls: {report['azure_vision_calls']} · cost ${report['azure_vision_cost']:.2f}")
    print(f"Overall unique counts: {format_counts(report['totals']['unique_by_class'])}")
    print(f"Overall moving counts: {format_counts(report['totals']['moving_unique_by_class'])}")
    print(f"Clips folder: {CLIPS}")
    print(f"Frames folder: {FRAMES}")
    print(f"SQLite DB: {DB_PATH}")
    print(f"Audit log: {report['audit_log']}")
    print(f"Summary JSON: {report_json}")
    print(f"Summary Markdown: {report_md}")
    print("\nPer-clip object table:")
    print(f"{'Clip':>4}  {'Time':<43}  {'Frames':>6}  {'Counts':<28}  {'Moving'}")
    print("-" * 104)
    for c in report["clips"]:
        counts = c["counts"]
        print(
            f"{c['id']:>4}  {c['start_iso']} → {c['end_iso']:<25}  "
            f"{int(c.get('sampled_frames') or 0):>6}  "
            f"{format_counts(counts['unique_by_class']):<28}  "
            f"{format_counts(counts['moving_unique_by_class'])}"
        )


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


def worker_loop(work_q: "queue.Queue[ClipJob | None]", sample_fps: float, conf: float, audit_log: Path | None) -> None:
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
            audit_write(
                audit_log,
                {
                    "event": "processed_clip",
                    "clip_id": job.clip_id,
                    "file": str(job.local_path),
                    "frames_dir": str(job.frames_dir),
                    **counts,
                },
            )
        except Exception as exc:
            mark_clip_error(job.clip_id, str(exc))
            audit_write(audit_log, {"event": "processed_clip_error", "clip_id": job.clip_id, "error": str(exc)})
        finally:
            work_q.task_done()


def audit_write(audit_log: Path | None, event: dict[str, Any]) -> None:
    event = {"ts": iso(time.time()), **event}
    line = json.dumps(event, ensure_ascii=False)
    print(line, flush=True)
    if audit_log:
        with audit_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_chunks(args: argparse.Namespace) -> None:
    init_db()
    device_ready()
    run_started_ts = time.time()
    run_id = datetime.fromtimestamp(run_started_ts).strftime("run_%Y%m%d_%H%M%S")
    audit_log = AUDITS / f"{run_id}.jsonl" if args.audit else None
    clip_ids: list[int] = []
    if audit_log:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        audit_log.write_text("", encoding="utf-8")
    audit_write(
        audit_log,
        {
            "event": "run_start",
            "run_id": run_id,
            "chunks": args.chunks,
            "duration": args.duration,
            "sample_fps": args.sample_fps,
            "conf": args.conf,
            "offline_local": True,
        },
    )
    if args.open_camera:
        launch_video_camera()
    ensure_camera_idle(audit_log)

    work_q: "queue.Queue[ClipJob | None]" = queue.Queue()
    worker = threading.Thread(target=worker_loop, args=(work_q, args.sample_fps, args.conf, audit_log), daemon=True)
    worker.start()

    known = {path for _, path in list_phone_videos()}
    recording_already_started = False
    start_ts = 0.0
    for index in range(args.chunks):
        if recording_already_started:
            audit_write(audit_log, {"event": "recording_chunk", "chunk": index + 1, "start_iso": iso(start_ts), "pipelined": True})
        else:
            start_ts = start_recording(args, audit_log)
            audit_write(audit_log, {"event": "recording_chunk", "chunk": index + 1, "start_iso": iso(start_ts), "pipelined": False})

        sleep_until_recording_deadline(start_ts, args.duration)
        stop_ts = stop_recording(args, audit_log)
        clip_id, local_path, frames_dir = create_clip(start_ts, stop_ts)
        clip_ids.append(clip_id)

        time.sleep(args.save_settle)
        phone_path = wait_for_new_video(known, stop_ts, timeout=args.find_timeout)
        known.add(phone_path)

        # Pull and validate the completed MP4 before the next toggle. OnePlus
        # exposes files before their moov atom is always ready, so validating here
        # is safer than overlapping the next recording immediately.
        pull_video(phone_path, local_path)
        phone_deleted = False
        if args.delete_phone:
            phone_deleted = delete_phone_video(phone_path)
        mark_clip_pulled(clip_id, phone_path, local_path, frames_dir)
        work_q.put(ClipJob(clip_id=clip_id, phone_path=phone_path, local_path=local_path, frames_dir=frames_dir))
        audit_write(
            audit_log,
            {
                "event": "pulled_clip",
                "clip_id": clip_id,
                "chunk": index + 1,
                "from": iso(start_ts),
                "to": iso(stop_ts),
                "phone_path": phone_path,
                "local_path": str(local_path),
                "local_size_bytes": local_path.stat().st_size if local_path.exists() else 0,
                "phone_deleted": phone_deleted,
            },
        )

        next_start_ts = 0.0
        start_next = index < args.chunks - 1
        if start_next:
            time.sleep(args.restart_settle)
            next_start_ts = start_recording(args, audit_log)
            audit_write(
                audit_log,
                {
                    "event": "started_next_chunk",
                    "chunk": index + 2,
                    "start_iso": iso(next_start_ts),
                    "gap_sec": round(next_start_ts - stop_ts, 3),
                },
            )

        if start_next:
            recording_already_started = True
            start_ts = next_start_ts
        else:
            recording_already_started = False

    work_q.put(None)
    work_q.join()
    report = make_run_report(
        run_id=run_id,
        started_ts=run_started_ts,
        finished_ts=time.time(),
        clip_ids=clip_ids,
        args=args,
        audit_log=audit_log or Path(""),
    )
    report_json, report_md = write_report_files(report)
    audit_write(
        audit_log,
        {
            "event": "run_complete",
            "run_id": run_id,
            "summary_json": str(report_json),
            "summary_markdown": str(report_md),
            "unique_by_class": report["totals"]["unique_by_class"],
            "moving_unique_by_class": report["totals"]["moving_unique_by_class"],
        },
    )
    if args.json_status:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_run_summary(report, report_json, report_md)


def prompt_int(prompt: str, default: int) -> int:
    value = input(f"{prompt} [{default}]: ").strip()
    if value.lower() in {"q", "quit", "n", "no", "cancel"}:
        raise SystemExit("Cancelled.")
    return default if not value else int(value)


def prompt_float(prompt: str, default: float) -> float:
    value = input(f"{prompt} [{default}]: ").strip()
    if value.lower() in {"q", "quit", "n", "no", "cancel"}:
        raise SystemExit("Cancelled.")
    return default if not value else float(value)


def wizard() -> argparse.Namespace:
    print("Native Camera Pipeline Wizard")
    print("This uses OnePlus Camera + ADB + local YOLO. No Copilot/Azure needed for processing.")
    chunks = prompt_int("How many clips?", 30)
    duration = prompt_float("Seconds per clip?", 10.0)
    sample_fps = prompt_float("Sample FPS for processing?", 3.0)
    total_sec = chunks * duration
    print(f"Plan: {chunks} clips × {duration:g}s = {total_sec:g}s ({total_sec/60:.1f} min)")
    print("Data will be stored under:", DATA)
    confirm = input("Start now? [Y/n]: ").strip().lower()
    if confirm in {"n", "no"}:
        raise SystemExit("Cancelled.")
    return argparse.Namespace(
        cmd="run",
        chunks=chunks,
        duration=duration,
        sample_fps=sample_fps,
        conf=0.35,
        record_x=540,
        record_y=2037,
        save_settle=0.8,
        restart_settle=0.4,
        find_timeout=8.0,
        start_retries=3,
        start_confirm_timeout=4.0,
        start_retry_delay=2.0,
        open_camera=True,
        json_status=False,
        audit=True,
        delete_phone=True,
    )


def reset_db() -> None:
    if DB_PATH.exists():
        backup = DB_PATH.with_name(f"{DB_PATH.stem}.backup-{int(time.time())}{DB_PATH.suffix}")
        shutil.copy2(DB_PATH, backup)
        DB_PATH.unlink()
        print(f"backup={backup}")
    init_db()
    print(f"db={DB_PATH}")


def doctor() -> None:
    init_db()
    print("Native Camera Pipeline Doctor")
    print(f"repo={ROOT}")
    print(f"data_dir={DATA}")
    print(f"clips_dir={CLIPS}")
    print(f"frames_dir={FRAMES}")
    print(f"audits_dir={AUDITS}")
    print(f"to_be_deleted_dir={TO_BE_DELETED}")
    print(f"db={DB_PATH}")
    print(f"db_exists={DB_PATH.exists()}")
    print(f"yolo_model={YOLO_MODEL}")
    print(f"yolo_model_exists={YOLO_MODEL.exists()}")
    try:
        device_ready()
        print("adb_device=ok")
    except Exception as exc:
        print(f"adb_device=error: {exc}")
    print(f"mp4_count={len(list(CLIPS.rglob('*.mp4'))) if CLIPS.exists() else 0}")
    print(f"jpg_count={len(list(FRAMES.rglob('*.jpg'))) if FRAMES.exists() else 0}")
    print(f"to_be_deleted_mp4_count={len(list(TO_BE_DELETED_CLIPS.rglob('*.mp4'))) if TO_BE_DELETED_CLIPS.exists() else 0}")


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
    run.add_argument("--start-retries", type=int, default=3, help="Retry record tap if OnePlus Camera ignores the first tap")
    run.add_argument("--start-confirm-timeout", type=float, default=4.0, help="Seconds to wait for recording UI after each start tap")
    run.add_argument("--start-retry-delay", type=float, default=2.0, help="Delay before retrying record start")
    run.add_argument("--open-camera", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--audit", action=argparse.BooleanOptionalAction, default=True, help="Write JSONL audit log and summary files")
    run.add_argument("--json-status", action=argparse.BooleanOptionalAction, default=False, help="Print final report as JSON instead of table")
    run.add_argument("--delete-phone", action=argparse.BooleanOptionalAction, default=True, help="Delete phone MP4 after verified pull to Mac")

    status = sub.add_parser("status", help="Print latest chunk status")
    status.add_argument("--json", action="store_true", dest="json_status")
    sub.add_parser("wizard", help="Interactive prompt for clip count/duration, then run")
    sub.add_parser("doctor", help="Verify data folders, DB, YOLO model, and connected Android device")
    sub.add_parser("reset-db", help="Backup and reset native camera SQLite DB")

    args = parser.parse_args()
    if args.cmd == "wizard":
        args = wizard()
        run_chunks(args)
    elif args.cmd == "run":
        run_chunks(args)
    elif args.cmd == "status":
        init_db()
        print_status(json_mode=args.json_status)
    elif args.cmd == "doctor":
        doctor()
    elif args.cmd == "reset-db":
        reset_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

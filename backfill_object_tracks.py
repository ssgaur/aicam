#!/usr/bin/env python3
"""Backfill unique object tracks from existing per-frame YOLO events."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "events.db"

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
TRACK_CENTER_MAX_PX = 140.0
TRACK_MOVE_PX = 45.0


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


def match_score(track: dict, det: dict) -> float:
    if track["cls"] != det["cls"]:
        return 0.0
    if det["ts"] - track["last_ts"] > TRACK_TTL_SEC:
        return 0.0
    iou = box_iou(track["last_box"], det["box"])
    dist = center_distance(track["last_box"], det["box"])
    dist_limit = max(TRACK_CENTER_MAX_PX, box_diag(det["box"]) * 1.25, box_diag(track["last_box"]) * 1.25)
    if iou >= TRACK_IOU_MIN:
        return 2.0 + iou
    if dist <= dist_limit:
        return 1.0 - min(0.99, dist / dist_limit)
    return 0.0


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
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
        """
    )
    cols = {row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()}
    if "track_id" not in cols:
        con.execute("ALTER TABLE events ADD COLUMN track_id INTEGER")


def create_track(con: sqlite3.Connection, det: dict) -> dict:
    category = CLASS_CATEGORY.get(det["cls"], det["cls"])
    cur = con.execute(
        """
        INSERT INTO object_tracks(
            cls, category, first_ts, first_iso, last_ts, last_iso,
            first_box, last_box, best_conf, hits, max_displacement, moving, active
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            det["cls"],
            category,
            det["ts"],
            det["iso"],
            det["ts"],
            det["iso"],
            json.dumps(det["box"]),
            json.dumps(det["box"]),
            det["conf"],
            1,
            0.0,
            0,
            1,
        ),
    )
    return {
        "id": cur.lastrowid,
        "cls": det["cls"],
        "category": category,
        "first_ts": det["ts"],
        "last_ts": det["ts"],
        "first_box": det["box"],
        "last_box": det["box"],
        "best_conf": det["conf"],
        "hits": 1,
        "max_displacement": 0.0,
        "moving": 0,
    }


def update_track(con: sqlite3.Connection, track: dict, det: dict) -> None:
    first_center = box_center(track["first_box"])
    current_center = box_center(det["box"])
    displacement = math.hypot(current_center[0] - first_center[0], current_center[1] - first_center[1])
    track["last_ts"] = det["ts"]
    track["last_box"] = det["box"]
    track["best_conf"] = max(track["best_conf"], det["conf"])
    track["hits"] += 1
    track["max_displacement"] = max(track["max_displacement"], displacement)
    track["moving"] = 1 if track["hits"] >= 2 and track["max_displacement"] >= TRACK_MOVE_PX else 0
    con.execute(
        """
        UPDATE object_tracks
        SET last_ts=?, last_iso=?, last_box=?, best_conf=?, hits=?,
            max_displacement=?, moving=?, active=1
        WHERE id=?
        """,
        (
            det["ts"],
            det["iso"],
            json.dumps(det["box"]),
            track["best_conf"],
            track["hits"],
            track["max_displacement"],
            track["moving"],
            track["id"],
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="Clear object_tracks and rebuild from all events")
    args = parser.parse_args()

    con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    if args.reset:
        con.execute("DELETE FROM object_tracks")
        con.execute("UPDATE events SET track_id=NULL")

    rows = con.execute(
        """
        SELECT rowid, ts, iso, cls, conf, box
        FROM events
        WHERE cls IN ({})
        ORDER BY ts ASC, rowid ASC
        """.format(",".join("?" for _ in TRACKED_CLASSES)),
        tuple(sorted(TRACKED_CLASSES)),
    ).fetchall()

    active: list[dict] = []
    current_ts = None
    used_this_ts: set[int] = set()
    for rowid, ts, iso, cls, conf, box_json in rows:
        if current_ts != ts:
            current_ts = ts
            used_this_ts = set()
            for track in active:
                if ts - track["last_ts"] > TRACK_TTL_SEC:
                    con.execute("UPDATE object_tracks SET active=0 WHERE id=?", (track["id"],))
            active = [track for track in active if ts - track["last_ts"] <= TRACK_TTL_SEC]

        det = {"rowid": rowid, "ts": ts, "iso": iso, "cls": cls, "conf": conf, "box": json.loads(box_json)}
        best = None
        best_score = 0.0
        for track in active:
            if track["id"] in used_this_ts:
                continue
            score = match_score(track, det)
            if score > best_score:
                best = track
                best_score = score
        if best is None:
            best = create_track(con, det)
            active.append(best)
        else:
            update_track(con, best, det)
        used_this_ts.add(best["id"])
        con.execute("UPDATE events SET track_id=? WHERE rowid=?", (best["id"], rowid))

    max_ts = rows[-1][1] if rows else 0.0
    for track in active:
        if max_ts - track["last_ts"] > TRACK_TTL_SEC:
            con.execute("UPDATE object_tracks SET active=0 WHERE id=?", (track["id"],))

    con.commit()
    counts = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks GROUP BY cls ORDER BY 2 DESC").fetchall())
    moving = dict(con.execute("SELECT cls, COUNT(*) FROM object_tracks WHERE moving=1 GROUP BY cls ORDER BY 2 DESC").fetchall())
    total = con.execute("SELECT COUNT(*) FROM object_tracks").fetchone()[0]
    con.close()
    print("tracks_total=", total)
    print("unique_by_class=", json.dumps(counts, sort_keys=True))
    print("moving_by_class=", json.dumps(moving, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

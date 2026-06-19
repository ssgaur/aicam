#!/usr/bin/env python3
"""Postgres metadata store for AiCam native camera data.

SQLite remains the fast local write-ahead store used by the processing pipeline.
This module mirrors durable metadata/report rows into Postgres so MP4/JPG files
can be deleted later without losing object-count history.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parent
SQLITE_DB = ROOT / "data" / "native_camera" / "native_camera.db"
DEFAULT_DSN = os.getenv("AICAM_PG_DSN", "dbname=aicam")


def connect():
    return psycopg.connect(DEFAULT_DSN)


def ensure_schema() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS native_clips (
                id BIGINT PRIMARY KEY,
                start_ts DOUBLE PRECISION NOT NULL,
                end_ts DOUBLE PRECISION NOT NULL,
                start_iso TEXT NOT NULL,
                end_iso TEXT NOT NULL,
                phone_path TEXT,
                local_path TEXT,
                frames_dir TEXT,
                duration_sec DOUBLE PRECISION,
                video_fps DOUBLE PRECISION,
                sampled_fps DOUBLE PRECISION,
                sampled_frames INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT,
                processed_at TEXT,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS native_sampled_frames (
                id BIGINT PRIMARY KEY,
                clip_id BIGINT NOT NULL REFERENCES native_clips(id) ON DELETE CASCADE,
                frame_index INTEGER NOT NULL,
                video_time_sec DOUBLE PRECISION NOT NULL,
                abs_ts DOUBLE PRECISION NOT NULL,
                iso TEXT NOT NULL,
                path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_detections (
                id BIGINT PRIMARY KEY,
                clip_id BIGINT NOT NULL REFERENCES native_clips(id) ON DELETE CASCADE,
                frame_id BIGINT REFERENCES native_sampled_frames(id) ON DELETE SET NULL,
                track_id BIGINT,
                video_time_sec DOUBLE PRECISION NOT NULL,
                abs_ts DOUBLE PRECISION NOT NULL,
                cls TEXT NOT NULL,
                category TEXT NOT NULL,
                conf DOUBLE PRECISION NOT NULL,
                box JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_object_tracks (
                id BIGINT PRIMARY KEY,
                cls TEXT NOT NULL,
                category TEXT NOT NULL,
                first_clip_id BIGINT,
                last_clip_id BIGINT,
                first_frame_id BIGINT,
                last_frame_id BIGINT,
                first_ts DOUBLE PRECISION NOT NULL,
                first_iso TEXT NOT NULL,
                last_ts DOUBLE PRECISION NOT NULL,
                last_iso TEXT NOT NULL,
                first_box JSONB NOT NULL,
                last_box JSONB NOT NULL,
                best_conf DOUBLE PRECISION NOT NULL,
                hits INTEGER NOT NULL,
                max_displacement DOUBLE PRECISION NOT NULL,
                moving BOOLEAN NOT NULL,
                active BOOLEAN NOT NULL
            );
            CREATE TABLE IF NOT EXISTS native_clip_reports (
                clip_id BIGINT PRIMARY KEY REFERENCES native_clips(id) ON DELETE CASCADE,
                start_ts DOUBLE PRECISION NOT NULL,
                end_ts DOUBLE PRECISION NOT NULL,
                start_iso TEXT NOT NULL,
                end_iso TEXT NOT NULL,
                sampled_frames INTEGER NOT NULL DEFAULT 0,
                detection_rows INTEGER NOT NULL DEFAULT 0,
                objects JSONB NOT NULL DEFAULT '{}'::jsonb,
                moving_objects JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL,
                clip_path TEXT,
                frames_dir TEXT,
                report_text TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_native_clips_end_ts ON native_clips(end_ts);
            CREATE INDEX IF NOT EXISTS idx_native_reports_end_ts ON native_clip_reports(end_ts);
            CREATE INDEX IF NOT EXISTS idx_native_tracks_last_ts ON native_object_tracks(last_ts);
            CREATE INDEX IF NOT EXISTS idx_native_detections_clip_cls ON native_detections(clip_id, cls);
            """
        )


def sqlite_connect(sqlite_db: Path = SQLITE_DB) -> sqlite3.Connection:
    con = sqlite3.connect(sqlite_db)
    con.row_factory = sqlite3.Row
    return con


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in counts.items()) if counts else "-"


def clip_counts(sqlite_con: sqlite3.Connection, clip_id: int) -> tuple[dict[str, int], dict[str, int], int]:
    objects = dict(
        sqlite_con.execute(
            """
            SELECT cls, COUNT(DISTINCT track_id)
            FROM detections
            WHERE clip_id=? AND track_id IS NOT NULL
            GROUP BY cls
            ORDER BY 2 DESC, cls ASC
            """,
            (clip_id,),
        ).fetchall()
    )
    moving = dict(
        sqlite_con.execute(
            """
            SELECT t.cls, COUNT(DISTINCT t.id)
            FROM object_tracks t
            JOIN detections d ON d.track_id=t.id
            WHERE d.clip_id=? AND t.moving=1
            GROUP BY t.cls
            ORDER BY 2 DESC, t.cls ASC
            """,
            (clip_id,),
        ).fetchall()
    )
    detection_rows = sqlite_con.execute("SELECT COUNT(*) FROM detections WHERE clip_id=?", (clip_id,)).fetchone()[0]
    return objects, moving, detection_rows


def report_text(clip: sqlite3.Row, objects: dict[str, int], moving: dict[str, int]) -> str:
    return (
        f"{clip['id']:>5}  {clip['start_iso']} → {clip['end_iso']}  "
        f"{clip['sampled_frames'] or 0:>6}  {format_counts(objects)}  {format_counts(moving)}  {clip['local_path']}"
    )


def sync_clip(sqlite_db: Path, clip_id: int) -> None:
    ensure_schema()
    s = sqlite_connect(sqlite_db)
    try:
        clip = s.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            return
        objects, moving, detection_rows = clip_counts(s, clip_id)
        text = report_text(clip, objects, moving)

        frames = s.execute("SELECT * FROM sampled_frames WHERE clip_id=? ORDER BY id", (clip_id,)).fetchall()
        detections = s.execute("SELECT * FROM detections WHERE clip_id=? ORDER BY id", (clip_id,)).fetchall()
        track_ids = [row["track_id"] for row in detections if row["track_id"] is not None]
        tracks = []
        if track_ids:
            placeholders = ",".join("?" for _ in sorted(set(track_ids)))
            tracks = s.execute(f"SELECT * FROM object_tracks WHERE id IN ({placeholders})", tuple(sorted(set(track_ids)))).fetchall()

        with connect() as p:
            p.execute(
                """
                INSERT INTO native_clips(
                    id,start_ts,end_ts,start_iso,end_iso,phone_path,local_path,frames_dir,
                    duration_sec,video_fps,sampled_fps,sampled_frames,status,error,created_at,processed_at,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(id) DO UPDATE SET
                    start_ts=EXCLUDED.start_ts,end_ts=EXCLUDED.end_ts,start_iso=EXCLUDED.start_iso,end_iso=EXCLUDED.end_iso,
                    phone_path=EXCLUDED.phone_path,local_path=EXCLUDED.local_path,frames_dir=EXCLUDED.frames_dir,
                    duration_sec=EXCLUDED.duration_sec,video_fps=EXCLUDED.video_fps,sampled_fps=EXCLUDED.sampled_fps,
                    sampled_frames=EXCLUDED.sampled_frames,status=EXCLUDED.status,error=EXCLUDED.error,
                    created_at=EXCLUDED.created_at,processed_at=EXCLUDED.processed_at,updated_at=now()
                """,
                (
                    clip["id"], clip["start_ts"], clip["end_ts"], clip["start_iso"], clip["end_iso"], clip["phone_path"],
                    clip["local_path"], clip["frames_dir"], clip["duration_sec"], clip["video_fps"], clip["sampled_fps"],
                    clip["sampled_frames"], clip["status"], clip["error"], clip["created_at"], clip["processed_at"],
                ),
            )
            p.execute("DELETE FROM native_sampled_frames WHERE clip_id=%s", (clip_id,))
            p.execute("DELETE FROM native_detections WHERE clip_id=%s", (clip_id,))
            for frame in frames:
                p.execute(
                    """
                    INSERT INTO native_sampled_frames(id,clip_id,frame_index,video_time_sec,abs_ts,iso,path)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                        clip_id=EXCLUDED.clip_id,frame_index=EXCLUDED.frame_index,video_time_sec=EXCLUDED.video_time_sec,
                        abs_ts=EXCLUDED.abs_ts,iso=EXCLUDED.iso,path=EXCLUDED.path
                    """,
                    (frame["id"], frame["clip_id"], frame["frame_index"], frame["video_time_sec"], frame["abs_ts"], frame["iso"], frame["path"]),
                )
            for det in detections:
                p.execute(
                    """
                    INSERT INTO native_detections(id,clip_id,frame_id,track_id,video_time_sec,abs_ts,cls,category,conf,box)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                        clip_id=EXCLUDED.clip_id,frame_id=EXCLUDED.frame_id,track_id=EXCLUDED.track_id,
                        video_time_sec=EXCLUDED.video_time_sec,abs_ts=EXCLUDED.abs_ts,cls=EXCLUDED.cls,
                        category=EXCLUDED.category,conf=EXCLUDED.conf,box=EXCLUDED.box
                    """,
                    (
                        det["id"], det["clip_id"], det["frame_id"], det["track_id"], det["video_time_sec"], det["abs_ts"],
                        det["cls"], det["category"], det["conf"], Jsonb(json.loads(det["box"])),
                    ),
                )
            for tr in tracks:
                p.execute(
                    """
                    INSERT INTO native_object_tracks(
                        id,cls,category,first_clip_id,last_clip_id,first_frame_id,last_frame_id,
                        first_ts,first_iso,last_ts,last_iso,first_box,last_box,best_conf,hits,max_displacement,moving,active
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                        cls=EXCLUDED.cls,category=EXCLUDED.category,first_clip_id=EXCLUDED.first_clip_id,
                        last_clip_id=EXCLUDED.last_clip_id,first_frame_id=EXCLUDED.first_frame_id,last_frame_id=EXCLUDED.last_frame_id,
                        first_ts=EXCLUDED.first_ts,first_iso=EXCLUDED.first_iso,last_ts=EXCLUDED.last_ts,last_iso=EXCLUDED.last_iso,
                        first_box=EXCLUDED.first_box,last_box=EXCLUDED.last_box,best_conf=EXCLUDED.best_conf,
                        hits=EXCLUDED.hits,max_displacement=EXCLUDED.max_displacement,moving=EXCLUDED.moving,active=EXCLUDED.active
                    """,
                    (
                        tr["id"], tr["cls"], tr["category"], tr["first_clip_id"], tr["last_clip_id"], tr["first_frame_id"],
                        tr["last_frame_id"], tr["first_ts"], tr["first_iso"], tr["last_ts"], tr["last_iso"],
                        Jsonb(json.loads(tr["first_box"])), Jsonb(json.loads(tr["last_box"])), tr["best_conf"], tr["hits"],
                        tr["max_displacement"], bool(tr["moving"]), bool(tr["active"]),
                    ),
                )
            p.execute(
                """
                INSERT INTO native_clip_reports(
                    clip_id,start_ts,end_ts,start_iso,end_iso,sampled_frames,detection_rows,
                    objects,moving_objects,status,clip_path,frames_dir,report_text,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT(clip_id) DO UPDATE SET
                    start_ts=EXCLUDED.start_ts,end_ts=EXCLUDED.end_ts,start_iso=EXCLUDED.start_iso,end_iso=EXCLUDED.end_iso,
                    sampled_frames=EXCLUDED.sampled_frames,detection_rows=EXCLUDED.detection_rows,
                    objects=EXCLUDED.objects,moving_objects=EXCLUDED.moving_objects,status=EXCLUDED.status,
                    clip_path=EXCLUDED.clip_path,frames_dir=EXCLUDED.frames_dir,report_text=EXCLUDED.report_text,updated_at=now()
                """,
                (
                    clip["id"], clip["start_ts"], clip["end_ts"], clip["start_iso"], clip["end_iso"],
                    clip["sampled_frames"] or 0, detection_rows, Jsonb(objects), Jsonb(moving), clip["status"],
                    clip["local_path"], clip["frames_dir"], text,
                ),
            )
    finally:
        s.close()


def sync_all(sqlite_db: Path = SQLITE_DB) -> None:
    ensure_schema()
    s = sqlite_connect(sqlite_db)
    try:
        ids = [row[0] for row in s.execute("SELECT id FROM clips ORDER BY id").fetchall()]
    finally:
        s.close()
    for clip_id in ids:
        sync_clip(sqlite_db, clip_id)
    print(f"synced_clips={len(ids)}")


def parse_since(value: str) -> float:
    value = value.strip().lower()
    if value.endswith("m"):
        return time.time() - float(value[:-1]) * 60
    if value.endswith("h"):
        return time.time() - float(value[:-1]) * 3600
    if value.endswith("s"):
        return time.time() - float(value[:-1])
    return float(value)


def summary_data(since: str) -> dict[str, Any]:
    ensure_schema()
    t0 = parse_since(since)
    with connect() as con:
        reports = con.execute(
            """
            SELECT clip_id,start_iso,end_iso,sampled_frames,objects,moving_objects,clip_path,report_text
            FROM native_clip_reports
            WHERE end_ts >= %s
            ORDER BY clip_id
            """,
            (t0,),
        ).fetchall()
        unique = con.execute(
            """
            SELECT cls, COUNT(*)
            FROM native_object_tracks
            WHERE last_ts >= %s
            GROUP BY cls
            ORDER BY 2 DESC, cls
            """,
            (t0,),
        ).fetchall()
        moving = con.execute(
            """
            SELECT cls, COUNT(*)
            FROM native_object_tracks
            WHERE last_ts >= %s AND moving = true
            GROUP BY cls
            ORDER BY 2 DESC, cls
            """,
            (t0,),
        ).fetchall()
    return {
        "since": since,
        "since_epoch": t0,
        "unique": dict(unique),
        "moving": dict(moving),
        "clip_count": len(reports),
        "clips": [
            {
                "clip_id": row[0],
                "start_iso": row[1],
                "end_iso": row[2],
                "sampled_frames": row[3],
                "objects": row[4],
                "moving_objects": row[5],
                "clip_path": row[6],
                "report_text": row[7],
            }
            for row in reports
        ],
    }


def summary(since: str) -> None:
    data = summary_data(since)
    print(f"since={data['since']}")
    print("unique=" + format_counts(data["unique"]))
    print("moving=" + format_counts(data["moving"]))
    print(f"clips={data['clip_count']}")
    print("--- clips ---")
    for row in data["clips"]:
        print(row["report_text"])


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sync = sub.add_parser("sync", help="Sync SQLite metadata into Postgres")
    sync.add_argument("--sqlite-db", type=Path, default=SQLITE_DB)
    summ = sub.add_parser("summary", help="Query Postgres clip summary")
    summ.add_argument("--since", default="10m")
    sub.add_parser("schema", help="Create Postgres schema")
    args = parser.parse_args()
    if args.cmd == "schema":
        ensure_schema()
        print("schema=ok")
    elif args.cmd == "sync":
        sync_all(args.sqlite_db)
    elif args.cmd == "summary":
        summary(args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

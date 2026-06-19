"""Migrate clips/frames into year/month/Wweek/day/hour/minute layout.

Idempotent. Dry-run by default. Updates SQLite + Postgres path columns to
match the new on-disk location. Quarantined clips (under to-be-deleted/) are
also re-bucketed into the same time tree.

Usage:
    python scripts/migrate_storage_layout.py            # dry run
    python scripts/migrate_storage_layout.py --apply    # actually move files
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import native_camera_pipeline as ncam  # noqa: E402

DB = ncam.DB_PATH
CLIPS = ncam.CLIPS
FRAMES = ncam.FRAMES
QC = ncam.TO_BE_DELETED_CLIPS
QF = ncam.TO_BE_DELETED_FRAMES


def desired_clip_path(clip_id: int, start_ts: float, quarantined: bool) -> Path:
    base = QC if quarantined else CLIPS
    rel = ncam._time_subdir(start_ts) / ncam.clip_path_for(clip_id, start_ts).name
    return base / rel


def desired_frames_dir(clip_id: int, start_ts: float, quarantined: bool) -> Path:
    base = QF if quarantined else FRAMES
    rel = ncam._time_subdir(start_ts) / ncam.frames_dir_for(clip_id, start_ts).name
    return base / rel


def is_quarantined(p: Path) -> bool:
    parts = set(p.parts)
    return "to-be-deleted" in parts


def move(src: Path, dst: Path, apply: bool) -> str:
    if not src.exists():
        return "missing"
    if src.resolve() == dst.resolve():
        return "ok"
    if dst.exists():
        return "skip-dst-exists"
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return "moved"


def update_pg_paths(clip_id: int, new_clip: str, new_frames: str) -> None:
    try:
        import postgres_store as pg
        with pg.connect() as con:
            con.execute(
                "UPDATE native_clips SET local_path=%s, frames_dir=%s WHERE id=%s",
                (new_clip, new_frames, clip_id),
            )
            con.execute(
                "UPDATE native_clip_reports SET clip_path=%s WHERE clip_id=%s",
                (new_clip, clip_id),
            )
    except Exception as exc:
        print(f"  [pg] clip {clip_id}: {exc}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry-run)")
    args = ap.parse_args()

    if not DB.exists():
        print(f"No SQLite DB at {DB}")
        return

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, start_ts, local_path, frames_dir FROM clips ORDER BY id"
    ).fetchall()

    moved = skipped = errors = 0
    for clip_id, start_ts, local_path, frames_dir in rows:
        if not local_path:
            continue
        old_clip = Path(local_path)
        old_frames = Path(frames_dir) if frames_dir else None
        quarantined = is_quarantined(old_clip)

        new_clip = desired_clip_path(clip_id, start_ts, quarantined)
        new_frames = desired_frames_dir(clip_id, start_ts, quarantined)

        c_status = move(old_clip, new_clip, args.apply)
        f_status = "n/a"
        if old_frames is not None:
            f_status = move(old_frames, new_frames, args.apply)

        if c_status == "moved" or f_status == "moved":
            moved += 1
            print(f"#{clip_id:>5} clip={c_status} frames={f_status} → {new_clip.relative_to(ncam.DATA)}")
            if args.apply:
                con.execute(
                    "UPDATE clips SET local_path=?, frames_dir=? WHERE id=?",
                    (str(new_clip), str(new_frames), clip_id),
                )
                con.execute(
                    "UPDATE sampled_frames SET path = REPLACE(path, ?, ?) WHERE clip_id=?",
                    (str(old_frames), str(new_frames), clip_id),
                )
                update_pg_paths(clip_id, str(new_clip), str(new_frames))
        elif c_status in {"ok", "missing"} and f_status in {"ok", "missing", "n/a"}:
            skipped += 1
        else:
            errors += 1
            print(f"#{clip_id:>5} ⚠ clip={c_status} frames={f_status}")

    if args.apply:
        con.commit()
    con.close()

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"\n{mode}: moved={moved} skipped={skipped} errors={errors}")
    if not args.apply and moved:
        print("Re-run with --apply to commit the moves.")


if __name__ == "__main__":
    main()

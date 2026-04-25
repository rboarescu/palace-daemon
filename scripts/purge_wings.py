#!/usr/bin/env python3
"""
Offline wing purge via direct SQLite — bypasses ChromaDB entirely.
Run with daemon STOPPED.

Usage:
    python3 purge_wings.py [--dry-run] wing1 [wing2 ...]

Example:
    python3 purge_wings.py --dry-run -- -home-user -home-user-palace-daemon wing_geminicli
    python3 purge_wings.py -- -home-user -home-user-palace-daemon wing_geminicli
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_DEFAULT_PALACE = Path.home() / ".mempalace" / "palace"


def get_embedding_ids(db: sqlite3.Connection, wing: str) -> list[int]:
    rows = db.execute(
        "SELECT em.id FROM embedding_metadata em WHERE em.key='wing' AND em.string_value=?",
        (wing,),
    ).fetchall()
    return [r[0] for r in rows]


def purge(wings: list[str], dry_run: bool):
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=OFF")

    total = 0
    plan = {}

    for wing in wings:
        ids = get_embedding_ids(db, wing)
        plan[wing] = ids
        print(f"  '{wing}': {len(ids)} drawers")
        total += len(ids)

    print(f"\nTotal to delete: {total}")

    if total == 0 or dry_run:
        db.close()
        if dry_run:
            print("Dry run — no changes made.")
        return

    # Backup first
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.parent / f"chroma.sqlite3.pre-purge-{ts}.bak"
    shutil.copy2(DB_PATH, backup)
    print(f"\nBackup: {backup}")

    for wing, ids in plan.items():
        if not ids:
            continue
        # Delete in batches — cascade handles embedding_metadata automatically
        batch_size = 500
        deleted = 0
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            db.execute(f"DELETE FROM embeddings WHERE id IN ({placeholders})", chunk)
            db.execute(f"DELETE FROM embedding_metadata WHERE id IN ({placeholders})", chunk)
            db.execute(f"DELETE FROM embedding_metadata_array WHERE id IN ({placeholders})", chunk)
            db.commit()
            deleted += len(chunk)
            print(f"\r  '{wing}': deleted {deleted}/{len(ids)}", end="", flush=True)
        print(f"\r  '{wing}': deleted {deleted} drawers ✓")

    # Clear HNSW binary segments so daemon rebuilds clean index on next start
    hnsw_cleared = 0
    for seg_dir in PALACE_PATH.iterdir():
        if seg_dir.is_dir() and not seg_dir.name.startswith("."):
            shutil.rmtree(seg_dir, ignore_errors=True)
            hnsw_cleared += 1
    print(f"\nCleared {hnsw_cleared} HNSW segment dir(s) — daemon will rebuild index on next start.")

    remaining = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"Remaining drawers: {remaining}")
    db.close()


def main():
    parser = argparse.ArgumentParser(description="Offline wing purge (daemon must be stopped)")
    parser.add_argument("wings", nargs="+", help="Wing names to purge")
    parser.add_argument("--dry-run", action="store_true", help="Count only, no deletion")
    parser.add_argument("--palace", type=Path, default=_DEFAULT_PALACE, help="Path to palace directory")
    args = parser.parse_args()

    global PALACE_PATH, DB_PATH
    PALACE_PATH = args.palace
    DB_PATH = PALACE_PATH / "chroma.sqlite3"

    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"Palace: {PALACE_PATH}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE DELETE'}")
    print(f"Wings:  {args.wings}\n")

    purge(args.wings, args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()

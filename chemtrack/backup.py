"""Safe database backups using SQLite's online backup (works while ChemTrack is in use)."""

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config


def backup_now(db_path=None, label="daily"):
    """Copy the database into the backups folder and keep only the newest copies."""
    src_path = Path(db_path or config.DB_PATH)
    if not src_path.exists():
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = config.BACKUP_DIR / f"chemicals-{datetime.now():%Y%m%d-%H%M%S}-{label}.db"
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)  # consistent snapshot, even if someone is saving at this moment
    finally:
        dst.close()
        src.close()
    prune()
    return target


def prune():
    copies = sorted(config.BACKUP_DIR.glob("chemicals-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in copies[config.BACKUP_KEEP:]:
        old.unlink(missing_ok=True)


def latest_backup():
    copies = sorted(config.BACKUP_DIR.glob("chemicals-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return copies[0] if copies else None


def start_daily_backups():
    """Back up now, then every BACKUP_EVERY_HOURS, in the background while the server runs."""
    def loop():
        while True:
            try:
                path = backup_now()
                print(f" * Backup saved: {path}")
            except Exception as exc:  # never stop the server because of a backup problem
                print(f" * Backup FAILED: {exc}")
            time.sleep(config.BACKUP_EVERY_HOURS * 3600)

    threading.Thread(target=loop, name="chemtrack-backup", daemon=True).start()

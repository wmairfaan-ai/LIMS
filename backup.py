"""
Back up the ChemTrack database now.

    python backup.py

Copies go to the `backups` folder; the newest 30 are kept.
To restore: stop ChemTrack, copy a backup file over chemicals.db, start ChemTrack again.
"""

from chemtrack.backup import backup_now

if __name__ == "__main__":
    path = backup_now(label="manual")
    print(f"Backup saved: {path}" if path else "No database found to back up.")

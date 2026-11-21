"""
Run ChemTrack on waitress, a production-quality WSGI server, with daily backups.

    python serve.py

Settings (environment variables, all optional):
    CHEMTRACK_HOST     default 127.0.0.1 (only this PC). Use 0.0.0.0 ONLY after IT approval + HTTPS.
    CHEMTRACK_PORT     default 5000
    CHEMTRACK_THREADS  default 8 (requests handled at the same time)
"""

import os

from waitress import serve

from chemtrack import app
from chemtrack.backup import start_daily_backups

if __name__ == "__main__":
    host = os.environ.get("CHEMTRACK_HOST", "127.0.0.1")
    port = int(os.environ.get("CHEMTRACK_PORT", "5000"))
    threads = int(os.environ.get("CHEMTRACK_THREADS", "8"))
    start_daily_backups()
    print(f" * ChemTrack running on http://{host}:{port}  (waitress, {threads} threads)")
    print(" * Press Ctrl+C to stop")
    serve(app, host=host, port=port, threads=threads)

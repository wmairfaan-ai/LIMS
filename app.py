"""
ChemTrack entry point.

    python serve.py   run ChemTrack (production server, daily backups)   <- use this normally
    python app.py     run Flask's development server (for changing the code)

The application itself lives in the `chemtrack` package.
"""

import os

from chemtrack import app  # noqa: F401  (waitress and tests import `app` from here)

if __name__ == "__main__":
    # Debug mode reloads code and shows error details, but its error page can run code,
    # so it is OFF unless you set CHEMTRACK_DEBUG=1 while developing on your own PC.
    app.run(debug=os.environ.get("CHEMTRACK_DEBUG") == "1")

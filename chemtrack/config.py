"""Settings you can change. Paths can be overridden with environment variables."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DB_PATH = Path(os.environ.get("CHEMTRACK_DB", ROOT / "chemicals.db"))
SECRET_KEY_PATH = ROOT / ".secret_key"
UPLOAD_DIR = Path(os.environ.get("CHEMTRACK_UPLOADS", ROOT / "uploads"))
BACKUP_DIR = Path(os.environ.get("CHEMTRACK_BACKUP_DIR", ROOT / "backups"))
BACKUP_KEEP = 30                       # number of backup copies kept
BACKUP_EVERY_HOURS = 24

# Address printed inside QR labels, e.g. https://chemtrack.lab.local
# Empty = use the address the page was opened with.
BASE_URL = os.environ.get("CHEMTRACK_BASE_URL", "").rstrip("/")

# Turn on in stage 3, once ChemTrack is served over HTTPS behind a proxy
HTTPS_ONLY = os.environ.get("CHEMTRACK_HTTPS") == "1"
BEHIND_PROXY = os.environ.get("CHEMTRACK_BEHIND_PROXY") == "1"

# ---- Chemicals ----
GHS_HAZARDS = ["Explosive", "Flammable", "Oxidising", "Compressed Gas", "Corrosive",
               "Acute Toxic", "Irritant", "Health Hazard", "Environmental"]
# Hazard classes that make a chemical "hazardous to health" (USECHH chemical register)
HEALTH_HAZARDS = {"Acute Toxic", "Corrosive", "Irritant", "Health Hazard"}
UNITS = ["g", "kg", "mL", "L", "pcs"]
INCOMPATIBLE_PAIRS = [("Flammable", "Oxidising"), ("Explosive", "Flammable"), ("Explosive", "Oxidising")]

STORAGE_TYPES = ["General shelf", "Flammable cabinet", "Acid cabinet", "Base cabinet",
                 "Oxidiser cabinet", "Toxic / poison cabinet (locked)", "Refrigerator",
                 "Flammable-safe refrigerator", "Freezer", "Gas cylinder area", "Waste area"]

# ---- Alerts ----
EXPIRY_WARNING_DAYS = 30
SDS_MAX_AGE_DAYS = 5 * 365      # CLASS 2013: SDS should be current; review at least every 5 years
CHRA_REVIEW_YEARS = 5           # USECHH 2000: review CHRA at intervals not exceeding 5 years
CHRA_WARNING_DAYS = 90
STOCKTAKE_WARNING_DAYS = 365    # container not seen in a stocktake for this long

# ---- Analysis ----
LEAD_TIME_DAYS = 14
COVER_DAYS = 60
ANALYSIS_PERIODS = [30, 90, 180, 365]

# ---- Accounts ----
ROLES = {"admin": "Admin", "staff": "Staff", "viewer": "Viewer"}
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 5
SESSION_HOURS = 8
MIN_PASSWORD_LENGTH = 8

MAX_SDS_MB = 10

# Show today's safety status (counts only, never chemical names) on the sign-in page
LOGIN_STATUS = os.environ.get("CHEMTRACK_LOGIN_STATUS", "1") == "1"

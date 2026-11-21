"""
ChemTrack - a simple chemical inventory system for labs.

Features
- Staff accounts with sign in (Admin / Staff / Viewer roles)
- Chemical register (name, CAS no., GHS hazards, location, quantity, supplier...)
- Stock in / stock out log (who used what, when, and why)
- Alerts: low stock, expired / expiring soon, SDS older than 5 years
- Storage check: warns when incompatible chemicals share a location
- Stock movements dashboard and data analysis
- Export the chemical register to CSV (useful for USECHH / CLASS records)

Run:
    pip install -r requirements.txt
    python seed.py      (optional - loads sample chemicals)
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import csv
import hmac
import io
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
# CHEMTRACK_DB lets you point at another database file (e.g. for testing)
DB_PATH = Path(os.environ.get("CHEMTRACK_DB", BASE_DIR / "chemicals.db"))
SECRET_KEY_PATH = BASE_DIR / ".secret_key"

ROLES = {
    "admin": "Admin",    # everything, including staff accounts
    "staff": "Staff",    # record stock, add / edit chemicals, fix own movements today
    "viewer": "Viewer",  # read-only (e.g. lecturer, auditor, SHE committee)
}
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 5
SESSION_HOURS = 8
MIN_PASSWORD_LENGTH = 8

# The 9 GHS pictogram hazard classes
GHS_HAZARDS = [
    "Explosive",
    "Flammable",
    "Oxidising",
    "Compressed Gas",
    "Corrosive",
    "Acute Toxic",
    "Irritant",
    "Health Hazard",
    "Environmental",
]
UNITS = ["g", "kg", "mL", "L", "pcs"]
EXPIRY_WARNING_DAYS = 30
SDS_MAX_AGE_DAYS = 5 * 365  # SDS should be reviewed at least every 5 years
LEAD_TIME_DAYS = 14         # typical days from placing an order to receiving it
COVER_DAYS = 60             # suggested orders should last this many days
ANALYSIS_PERIODS = [30, 90, 180, 365]

# Hazard pairs that must not be stored together in the same location
INCOMPATIBLE_PAIRS = [
    ("Flammable", "Oxidising"),
    ("Explosive", "Flammable"),
    ("Explosive", "Oxidising"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS chemicals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    cas_no        TEXT,
    formula       TEXT,
    hazards       TEXT    DEFAULT '',      -- comma separated GHS classes
    location      TEXT,
    quantity      REAL    NOT NULL DEFAULT 0,
    unit          TEXT    NOT NULL DEFAULT 'g',
    min_quantity  REAL    DEFAULT 0,       -- reorder level
    supplier      TEXT,
    lot_no        TEXT,
    received_date TEXT,                    -- YYYY-MM-DD
    expiry_date   TEXT,
    sds_date      TEXT,                    -- date printed on the SDS
    notes         TEXT,
    created_at    TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chemical_id   INTEGER NOT NULL REFERENCES chemicals(id) ON DELETE CASCADE,
    type          TEXT    NOT NULL CHECK (type IN ('IN', 'OUT')),
    amount        REAL    NOT NULL,
    balance_after REAL,                    -- stock left after this movement
    user_name     TEXT,
    purpose       TEXT,
    reference     TEXT,                    -- PO / DO / invoice no.
    created_at    TEXT    DEFAULT (datetime('now', 'localtime')),
    edited_at     TEXT,                    -- set when a movement is corrected
    edited_by     TEXT,                    -- who corrected it
    user_id       INTEGER                  -- staff account that recorded it
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name                 TEXT    NOT NULL,
    role                 TEXT    NOT NULL CHECK (role IN ('admin', 'staff', 'viewer')),
    password_hash        TEXT    NOT NULL, -- never the password itself
    must_change_password INTEGER NOT NULL DEFAULT 1,
    active               INTEGER NOT NULL DEFAULT 1,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    last_login           TEXT,
    created_at           TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS login_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id   TEXT,
    user_id    INTEGER,
    success    INTEGER NOT NULL,
    note       TEXT,
    created_at TEXT    DEFAULT (datetime('now', 'localtime'))
);
"""


def load_secret_key():
    """A random key that signs the sign-in cookie. Created once and kept in .secret_key."""
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),  # auto sign-out
    SESSION_COOKIE_HTTPONLY=True,   # page scripts can't read the cookie
    SESSION_COOKIE_SAMESITE="Lax",  # other websites can't send it
)


# ---------------------------------------------------------------- database --

def get_db():
    """Open one database connection per request."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        # Upgrade older databases that were created before these columns existed
        existing = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        for column, kind in (("balance_after", "REAL"), ("reference", "TEXT"), ("edited_at", "TEXT"),
                             ("edited_by", "TEXT"), ("user_id", "INTEGER")):
            if column not in existing:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {kind}")
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- helpers --

def parse_date(text):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def hazard_list(row):
    return [h for h in (row["hazards"] or "").split(",") if h]


def chemical_alerts(row):
    """Return a list of (level, message) alerts for one chemical."""
    today = date.today()
    alerts = []

    if row["quantity"] <= 0:
        alerts.append(("danger", "Out of stock"))
    elif row["min_quantity"] and row["quantity"] <= row["min_quantity"]:
        alerts.append(("warn", "Low stock"))

    expiry = parse_date(row["expiry_date"])
    if expiry:
        days_left = (expiry - today).days
        if days_left < 0:
            alerts.append(("danger", "Expired"))
        elif days_left <= EXPIRY_WARNING_DAYS:
            alerts.append(("warn", f"Expires in {days_left} days"))

    sds = parse_date(row["sds_date"])
    if not sds:
        alerts.append(("warn", "No SDS date"))
    elif (today - sds).days > SDS_MAX_AGE_DAYS:
        alerts.append(("warn", "SDS over 5 years old"))

    return alerts


def storage_conflicts(rows):
    """Find incompatible chemicals stored in the same location."""
    by_location = {}
    for row in rows:
        loc = (row["location"] or "").strip()
        if loc:
            by_location.setdefault(loc, []).append(row)

    conflicts = []
    for loc, chems in by_location.items():
        for a_hazard, b_hazard in INCOMPATIBLE_PAIRS:
            group_a = [c["name"] for c in chems if a_hazard in hazard_list(c)]
            group_b = [c["name"] for c in chems if b_hazard in hazard_list(c)]
            if group_a and group_b:
                conflicts.append({
                    "location": loc,
                    "a_hazard": a_hazard,
                    "a_names": group_a,
                    "b_hazard": b_hazard,
                    "b_names": group_b,
                })
    return conflicts


def read_chemical_form(form):
    """Validate the add/edit form. Returns (data, errors)."""
    errors = []
    data = {
        "name": form.get("name", "").strip(),
        "cas_no": form.get("cas_no", "").strip(),
        "formula": form.get("formula", "").strip(),
        "hazards": ",".join(h for h in form.getlist("hazards") if h in GHS_HAZARDS),
        "location": form.get("location", "").strip(),
        "unit": form.get("unit", "g"),
        "supplier": form.get("supplier", "").strip(),
        "lot_no": form.get("lot_no", "").strip(),
        "received_date": form.get("received_date", "").strip(),
        "expiry_date": form.get("expiry_date", "").strip(),
        "sds_date": form.get("sds_date", "").strip(),
        "notes": form.get("notes", "").strip(),
    }

    if not data["name"]:
        errors.append("Chemical name is required.")
    if data["unit"] not in UNITS:
        errors.append("Please choose a valid unit.")

    for field in ("quantity", "min_quantity"):
        raw = form.get(field, "0").strip() or "0"
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
            data[field] = value
        except ValueError:
            errors.append(f"{field.replace('_', ' ').title()} must be a number of 0 or more.")
            data[field] = 0

    for field in ("received_date", "expiry_date", "sds_date"):
        if data[field] and not parse_date(data[field]):
            errors.append(f"{field.replace('_', ' ').title()} is not a valid date.")

    return data, errors


FIELDS = ["name", "cas_no", "formula", "hazards", "location", "quantity", "unit",
          "min_quantity", "supplier", "lot_no", "received_date", "expiry_date",
          "sds_date", "notes"]


# ------------------------------------------------------- sign in & security --

PUBLIC_ENDPOINTS = {"login", "setup", "static"}


def password_marker(user):
    """Part of the password hash kept in the cookie: a password change signs out old sessions."""
    return user["password_hash"][-16:]


@app.before_request
def load_user_and_protect():
    """Runs before every page: who is signed in, and are they allowed here?"""
    g.user = None
    db = get_db()
    user_id = session.get("user_id")
    if user_id:
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user and user["active"] and session.get("pw") == password_marker(user):
            g.user = user
        else:
            session.clear()  # disabled, deleted or password changed elsewhere

    # Form protection (CSRF): every POST must carry the token from this session
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not sent or not expected or not hmac.compare_digest(sent, expected):
            abort(400, "This form expired. Go back, refresh the page, and try again.")

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return redirect(url_for("setup"))  # first run: create the admin account
    if g.user is None:
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
    if g.user["must_change_password"] and request.endpoint not in ("change_password", "logout"):
        flash("Please choose your own password before continuing.", "warn")
        return redirect(url_for("change_password"))
    return None


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_field():
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def roles_required(*roles):
    """Only let these roles open the page."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None or g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def is_admin():
    return g.user is not None and g.user["role"] == "admin"


def can_edit():
    """Admin and Staff can record stock and add / edit chemicals. Viewers can't."""
    return g.user is not None and g.user["role"] in ("admin", "staff")


def can_edit_movement(tx):
    """Admin: any movement. Staff: only their own, on the same day."""
    if is_admin():
        return True
    return (can_edit() and tx["user_id"] == g.user["id"]
            and (tx["created_at"] or "")[:10] == date.today().isoformat())


def initials(name):
    parts = [p for p in (name or "").split() if p[0].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def validate_new_password(password, confirm, staff_id):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords don't match."
    if password.lower() == (staff_id or "").lower():
        return "Password can't be the same as the Staff ID."
    return None


def temporary_password():
    """Easy to read out, hard to guess, e.g. Lab-7G4Q-29XK."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    pick = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))
    return f"Lab-{pick(4)}-{pick(4)}"


def log_login(staff_id, user_id, success, note=""):
    get_db().execute("INSERT INTO login_log (staff_id, user_id, success, note) VALUES (?, ?, ?, ?)",
                     (staff_id, user_id, 1 if success else 0, note))


def start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pw"] = password_marker(user)


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("index")


# A real hash used when the Staff ID doesn't exist, so a wrong ID takes as long as a wrong password
_DUMMY_HASH = generate_password_hash("not-a-real-password")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error = None
    staff_id = ""
    if request.method == "POST":
        db = get_db()
        staff_id = request.form.get("staff_id", "").strip()
        password = request.form.get("password", "")
        user = db.execute("SELECT * FROM users WHERE staff_id = ?", (staff_id,)).fetchone()
        now = datetime.now()
        locked_until = (datetime.strptime(user["locked_until"], "%Y-%m-%d %H:%M:%S")
                        if user and user["locked_until"] else None)

        if locked_until and locked_until > now:
            minutes = max(1, round((locked_until - now).total_seconds() / 60))
            error = f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
            log_login(staff_id, user["id"], False, "Locked")
        elif user and check_password_hash(user["password_hash"], password):
            if not user["active"]:
                error = "This account is disabled. Ask the lab admin."
                log_login(staff_id, user["id"], False, "Disabled account")
            else:
                db.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, "
                           "last_login = datetime('now', 'localtime') WHERE id = ?", (user["id"],))
                log_login(user["staff_id"], user["id"], True)
                db.commit()
                start_session(user)
                return redirect(safe_next(request.form.get("next")))
        else:
            if user is None:
                check_password_hash(_DUMMY_HASH, password)
                log_login(staff_id, None, False, "Unknown Staff ID")
            else:
                fails = user["failed_attempts"] + 1
                if fails >= MAX_FAILED_LOGINS:
                    until = (now + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute("UPDATE users SET failed_attempts = 0, locked_until = ? WHERE id = ?",
                               (until, user["id"]))
                    log_login(staff_id, user["id"], False, f"Wrong password, locked {LOCKOUT_MINUTES} min")
                else:
                    db.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (fails, user["id"]))
                    log_login(staff_id, user["id"], False, "Wrong password")
            error = "Staff ID or password is wrong."
        db.commit()

    return render_template("login.html", error=error, staff_id=staff_id,
                           next=request.values.get("next", ""))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the first admin account."""
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    error = None
    form = request.form
    if request.method == "POST":
        staff_id = form.get("staff_id", "").strip()
        name = form.get("name", "").strip()
        password = form.get("password", "")
        if not staff_id or not name:
            error = "Enter your Staff ID and name."
        else:
            error = validate_new_password(password, form.get("confirm", ""), staff_id)
        if not error:
            cur = db.execute(
                "INSERT INTO users (staff_id, name, role, password_hash, must_change_password, last_login) "
                "VALUES (?, ?, 'admin', ?, 0, datetime('now', 'localtime'))",
                (staff_id, name, generate_password_hash(password)))
            log_login(staff_id, cur.lastrowid, True, "Admin account created")
            db.commit()
            start_session(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
            flash(f"Welcome, {name}. Your admin account is ready. Add your lab staff under Staff.", "ok")
            return redirect(url_for("index"))
    return render_template("setup.html", error=error, form=form)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You're signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    error = None
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("password", "")
        if not check_password_hash(g.user["password_hash"], current):
            error = "Your current password is wrong."
        elif new == current:
            error = "Choose a password different from the current one."
        else:
            error = validate_new_password(new, request.form.get("confirm", ""), g.user["staff_id"])
        if not error:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            start_session(db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone())
            flash("Password changed.", "ok")
            return redirect(url_for("index"))
    return render_template("change_password.html", error=error)


# ------------------------------------------------------------ staff accounts --

def active_admin_count(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]


def render_staff_page(new_login=None):
    users = get_db().execute(
        "SELECT * FROM users ORDER BY active DESC, role, name COLLATE NOCASE").fetchall()
    return render_template("staff.html", users=users, new_login=new_login, now=datetime.now())


@app.route("/staff")
@roles_required("admin")
def staff_list():
    return render_staff_page()


@app.route("/staff/add", methods=["POST"])
@roles_required("admin")
def staff_add():
    db = get_db()
    staff_id = request.form.get("staff_id", "").strip()
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "staff")
    if not staff_id or not name or role not in ROLES:
        flash("Enter a Staff ID, name and role.", "danger")
    elif db.execute("SELECT 1 FROM users WHERE staff_id = ?", (staff_id,)).fetchone():
        flash(f"Staff ID {staff_id} already has an account.", "danger")
    else:
        password = temporary_password()
        db.execute("INSERT INTO users (staff_id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (staff_id, name, role, generate_password_hash(password)))
        db.commit()
        # Shown once on this page only. Never saved in plain text or in the cookie.
        return render_staff_page(new_login={"name": name, "staff_id": staff_id,
                                            "password": password, "action": "Account created"})
    return redirect(url_for("staff_list"))


def get_user_or_404(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


@app.route("/staff/<int:user_id>/update", methods=["POST"])
@roles_required("admin")
def staff_update(user_id):
    db = get_db()
    user = get_user_or_404(user_id)
    name = request.form.get("name", "").strip() or user["name"]
    role = request.form.get("role", user["role"])
    if role not in ROLES:
        abort(400)
    if user["role"] == "admin" and role != "admin" and user["active"] and active_admin_count(db) <= 1:
        flash("ChemTrack needs at least one admin. Make someone else admin first.", "danger")
    else:
        db.execute("UPDATE users SET name = ?, role = ? WHERE id = ?", (name, role, user_id))
        db.commit()
        flash(f"Saved {name} as {ROLES[role]}.", "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/<int:user_id>/reset", methods=["POST"])
@roles_required("admin")
def staff_reset(user_id):
    db = get_db()
    user = get_user_or_404(user_id)
    password = temporary_password()
    db.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0, "
               "locked_until = NULL WHERE id = ?", (generate_password_hash(password), user_id))
    db.commit()
    if user_id == g.user["id"]:
        session.clear()
        flash(f"Your password was reset. Sign in with {password} and choose a new one.", "warn")
        return redirect(url_for("login"))
    return render_staff_page(new_login={"name": user["name"], "staff_id": user["staff_id"],
                                        "password": password, "action": "Password reset"})


@app.route("/staff/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def staff_toggle(user_id):
    db = get_db()
    user = get_user_or_404(user_id)
    if user_id == g.user["id"]:
        flash("You can't disable your own account.", "danger")
    elif user["active"] and user["role"] == "admin" and active_admin_count(db) <= 1:
        flash("ChemTrack needs at least one active admin.", "danger")
    else:
        db.execute("UPDATE users SET active = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
                   (0 if user["active"] else 1, user_id))
        db.commit()
        flash(f"{user['name']} {'disabled' if user['active'] else 'enabled'}.", "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/log")
@roles_required("admin")
def login_history():
    rows = get_db().execute(
        "SELECT l.*, u.name FROM login_log l LEFT JOIN users u ON u.id = l.user_id "
        "ORDER BY l.id DESC LIMIT 200").fetchall()
    return render_template("login_log.html", rows=rows)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", title="Not allowed",
                           message="Your role doesn't allow this. Ask the lab admin if you need access."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", title="Not found",
                           message="That page or record doesn't exist."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Something went wrong",
                           message=getattr(e, "description", "Bad request.")), 400


# ---------------------------------------------------------- page helpers --

def attention_count():
    """How many chemicals have an alert (for the bell in the top bar)."""
    rows = get_db().execute("SELECT * FROM chemicals").fetchall()
    return sum(1 for r in rows if chemical_alerts(r))


@app.context_processor
def inject_globals():
    data = {"GHS_HAZARDS": GHS_HAZARDS, "UNITS": UNITS, "hazard_list": hazard_list,
            "ROLES": ROLES, "csrf_field": csrf_field, "current_user": g.get("user"),
            "is_admin": is_admin, "can_edit": can_edit, "can_edit_movement": can_edit_movement,
            "initials": initials}
    if g.get("user"):
        data["attention"] = attention_count()
    return data


def fmt_qty(value):
    """Show 500.0 as 500 and 2.5 as 2.5."""
    return f"{value:g}"


app.jinja_env.filters["qty"] = fmt_qty
# Readable IDs: chemical 7 -> CH-0007, movement 42 -> TX-00042
app.jinja_env.filters["chem_code"] = lambda i: f"CH-{i:04d}"
app.jinja_env.filters["tx_code"] = lambda i: f"TX-{i:05d}"


# ------------------------------------------------------------------ routes --

@app.route("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "all")

    rows = db.execute("SELECT * FROM chemicals ORDER BY name COLLATE NOCASE").fetchall()
    all_rows = rows

    if q:
        like = q.lower()
        rows = [r for r in rows if like in r["name"].lower()
                or like in (r["cas_no"] or "").lower()
                or like in (r["location"] or "").lower()]

    chemicals = [{"row": r, "alerts": chemical_alerts(r)} for r in rows]
    if show == "alerts":
        chemicals = [c for c in chemicals if c["alerts"]]

    every = [chemical_alerts(r) for r in all_rows]
    summary = {
        "total": len(all_rows),
        "low": sum(any("stock" in m for _, m in a) for a in every),
        "expiry": sum(any("xpire" in m for _, m in a) for a in every),
        "sds": sum(any("SDS" in m for _, m in a) for a in every),
    }

    return render_template(
        "index.html",
        chemicals=chemicals,
        summary=summary,
        conflicts=storage_conflicts(all_rows),
        q=q,
        show=show,
        now_hour=datetime.now().hour,
    )


@app.route("/chemical/new", methods=["GET", "POST"])
@roles_required("admin", "staff")
def add_chemical():
    if request.method == "POST":
        data, errors = read_chemical_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("form.html", chem=data, title="Add chemical")

        db = get_db()
        cur = db.execute(
            f"INSERT INTO chemicals ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
            [data[f] for f in FIELDS],
        )
        if data["quantity"] > 0:
            db.execute(
                "INSERT INTO transactions (chemical_id, type, amount, balance_after, user_name, "
                "user_id, purpose) VALUES (?, 'IN', ?, ?, ?, ?, ?)",
                (cur.lastrowid, data["quantity"], data["quantity"], g.user["name"], g.user["id"],
                 "Opening stock"),
            )
        db.commit()
        flash(f"Added {data['name']}.", "ok")
        return redirect(url_for("view_chemical", chem_id=cur.lastrowid))

    return render_template("form.html", chem={"unit": "g"}, title="Add chemical")


def get_chemical_or_404(chem_id):
    row = get_db().execute("SELECT * FROM chemicals WHERE id = ?", (chem_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/chemical/<int:chem_id>")
def view_chemical(chem_id):
    chem = get_chemical_or_404(chem_id)
    log = get_db().execute(
        "SELECT * FROM transactions WHERE chemical_id = ? ORDER BY created_at DESC, id DESC", (chem_id,)
    ).fetchall()
    return render_template("detail.html", chem=chem, alerts=chemical_alerts(chem), log=log)


@app.route("/chemical/<int:chem_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "staff")
def edit_chemical(chem_id):
    chem = get_chemical_or_404(chem_id)

    if request.method == "POST":
        data, errors = read_chemical_form(request.form)
        # Quantity is changed through stock in/out, not by editing
        data["quantity"] = chem["quantity"]
        if errors:
            for e in errors:
                flash(e, "danger")
            data["id"] = chem_id
            return render_template("form.html", chem=data, title="Edit chemical", editing=True)

        db = get_db()
        db.execute(
            f"UPDATE chemicals SET {', '.join(f + ' = ?' for f in FIELDS)} WHERE id = ?",
            [data[f] for f in FIELDS] + [chem_id],
        )
        db.commit()
        flash("Changes saved.", "ok")
        return redirect(url_for("view_chemical", chem_id=chem_id))

    return render_template("form.html", chem=dict(chem), title="Edit chemical", editing=True)


# ------------------------------------------------------- stock in / out --

def signed(move_type, amount):
    """Stock in adds (+), stock out takes away (-)."""
    return amount if move_type == "IN" else -amount


def read_movement_form(form):
    """Validate the stock in/out fields. Returns (data, error)."""
    data = {
        "type": form.get("type"),
        "purpose": form.get("purpose", "").strip(),
        "reference": form.get("reference", "").strip(),
    }
    try:
        data["amount"] = float(form.get("amount", ""))
    except ValueError:
        data["amount"] = 0

    if data["type"] not in ("IN", "OUT"):
        return data, "Choose Stock in or Stock out."
    if data["amount"] <= 0:
        return data, "Enter an amount greater than 0."
    return data, None


def record_movement(chem, form):
    """Save one stock in/out. Returns (ok, message)."""
    data, error = read_movement_form(form)
    if error:
        return False, error
    if data["type"] == "OUT" and data["amount"] > chem["quantity"]:
        return False, f"Not enough {chem['name']}. Only {fmt_qty(chem['quantity'])} {chem['unit']} left."

    db = get_db()
    db.execute("UPDATE chemicals SET quantity = quantity + ? WHERE id = ?",
               (signed(data["type"], data["amount"]), chem["id"]))
    balance = db.execute("SELECT quantity FROM chemicals WHERE id = ?", (chem["id"],)).fetchone()[0]
    # The name comes from the signed-in account, so nobody can record under someone else's name
    db.execute(
        "INSERT INTO transactions (chemical_id, type, amount, balance_after, user_name, user_id, "
        "purpose, reference) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chem["id"], data["type"], data["amount"], balance, g.user["name"], g.user["id"],
         data["purpose"], data["reference"]),
    )
    db.commit()
    word = "Stock in" if data["type"] == "IN" else "Stock out"
    return True, (f"{word}: {fmt_qty(data['amount'])} {chem['unit']} {chem['name']}. "
                  f"Balance now {fmt_qty(balance)} {chem['unit']}.")


@app.route("/chemical/<int:chem_id>/move", methods=["POST"])
@roles_required("admin", "staff")
def move_stock(chem_id):
    """Stock in/out from a chemical's own page."""
    chem = get_chemical_or_404(chem_id)
    ok, message = record_movement(chem, request.form)
    flash(message, "ok" if ok else "danger")
    return redirect(url_for("view_chemical", chem_id=chem_id))


@app.route("/stock", methods=["GET", "POST"])
@roles_required("admin", "staff")
def stock_page():
    """One page to record stock in/out for any chemical."""
    db = get_db()
    if request.method == "POST":
        chem_id = request.form.get("chemical_id", "")
        chem = db.execute("SELECT * FROM chemicals WHERE id = ?",
                          (int(chem_id) if chem_id.isdigit() else 0,)).fetchone()
        if chem is None:
            flash("Please choose a chemical.", "danger")
            return redirect(url_for("stock_page"))
        ok, message = record_movement(chem, request.form)
        flash(message, "ok" if ok else "danger")
        if ok:
            # Keep the same type selected for fast entry of the next one
            return redirect(url_for("stock_page", type=request.form.get("type")))
        return redirect(url_for("stock_page", chemical=chem["id"], type=request.form.get("type")))

    chemicals = db.execute(
        "SELECT id, name, quantity, unit, location FROM chemicals ORDER BY name COLLATE NOCASE"
    ).fetchall()
    recent = db.execute(
        "SELECT t.*, c.name, c.unit FROM transactions t JOIN chemicals c ON c.id = t.chemical_id "
        "ORDER BY t.id DESC LIMIT 10"
    ).fetchall()
    return render_template(
        "stock.html",
        chemicals=chemicals,
        recent=recent,
        selected_chem=request.args.get("chemical", ""),
        selected_type=request.args.get("type", "OUT"),
    )


def get_movement_or_404(tx_id):
    tx = get_db().execute(
        "SELECT t.*, c.name, c.unit, c.quantity AS stock FROM transactions t "
        "JOIN chemicals c ON c.id = t.chemical_id WHERE t.id = ?", (tx_id,)
    ).fetchone()
    if tx is None:
        abort(404)
    return tx


def shift_stock(db, tx, diff, include_self=True):
    """
    A movement was corrected or deleted, so the chemical's stock changes by `diff`.
    Shift the current quantity and the balance of this and every later movement.
    Returns an error message if that would make stock negative at any point.
    """
    op = ">=" if include_self else ">"
    later = db.execute(
        f"SELECT id, created_at, balance_after FROM transactions WHERE chemical_id = ? "
        f"AND (created_at > ? OR (created_at = ? AND id {op} ?))",
        (tx["chemical_id"], tx["created_at"], tx["created_at"], tx["id"]),
    ).fetchall()

    if tx["stock"] + diff < -1e-9:
        return "This change would make the current stock negative."
    for row in later:
        if row["balance_after"] is not None and row["balance_after"] + diff < -1e-9:
            return f"This change would make the stock negative on {row['created_at'][:16]}."

    db.execute("UPDATE chemicals SET quantity = round(quantity + ?, 6) WHERE id = ?",
               (diff, tx["chemical_id"]))
    for row in later:
        if row["balance_after"] is not None:
            db.execute("UPDATE transactions SET balance_after = round(balance_after + ?, 6) WHERE id = ?",
                       (diff, row["id"]))
    return None


def back_url():
    """Go back to where the user came from (only pages inside ChemTrack)."""
    target = request.form.get("next") or request.args.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else url_for("movements")


@app.route("/movement/<int:tx_id>/edit", methods=["GET", "POST"])
def edit_movement(tx_id):
    tx = get_movement_or_404(tx_id)
    if not can_edit_movement(tx):
        abort(403)

    if request.method == "POST":
        data, error = read_movement_form(request.form)
        if not error:
            db = get_db()
            diff = signed(data["type"], data["amount"]) - signed(tx["type"], tx["amount"])
            error = shift_stock(db, tx, diff) if diff else None
            if not error:
                db.execute(
                    "UPDATE transactions SET type = ?, amount = ?, purpose = ?, reference = ?, "
                    "edited_at = datetime('now', 'localtime'), edited_by = ? WHERE id = ?",
                    (data["type"], data["amount"], data["purpose"], data["reference"],
                     g.user["name"], tx_id),
                )
                db.commit()
                flash(f"Movement TX-{tx_id:05d} updated.", "ok")
                return redirect(back_url())
            db.rollback()
        flash(error, "danger")
        return render_template("movement_form.html", tx=tx, form=request.form, next=back_url())

    return render_template("movement_form.html", tx=tx, form=tx, next=back_url())


@app.route("/movement/<int:tx_id>/delete", methods=["POST"])
def delete_movement(tx_id):
    tx = get_movement_or_404(tx_id)
    if not can_edit_movement(tx):
        abort(403)
    db = get_db()
    error = shift_stock(db, tx, -signed(tx["type"], tx["amount"]), include_self=False)
    if error:
        db.rollback()
        flash(f"Cannot delete: {error}", "danger")
        return redirect(url_for("edit_movement", tx_id=tx_id, next=back_url()))
    db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    db.commit()
    flash(f"Movement TX-{tx_id:05d} deleted and stock recalculated.", "ok")
    return redirect(back_url())


@app.route("/chemical/<int:chem_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_chemical(chem_id):
    chem = get_chemical_or_404(chem_id)
    db = get_db()
    db.execute("DELETE FROM chemicals WHERE id = ?", (chem_id,))
    db.commit()
    flash(f"Deleted {chem['name']}.", "ok")
    return redirect(url_for("index"))


# ------------------------------------------------ stock movements dashboard --

def query_movements(args):
    """Fetch stock movements matching the dashboard filters."""
    today = date.today()
    start = parse_date(args.get("start", "")) or today - timedelta(days=30)
    end = parse_date(args.get("end", "")) or today
    if start > end:
        start, end = end, start
    move_type = args.get("type", "")
    chem_id = args.get("chemical", "")
    q = args.get("q", "").strip()

    sql = """
        SELECT t.id, t.created_at, t.type, t.amount, t.balance_after,
               t.user_name, t.user_id, t.purpose, t.reference, t.edited_at, t.edited_by,
               c.id AS chemical_id, c.name, c.cas_no, c.unit,
               c.supplier, c.lot_no, c.location
        FROM transactions t
        JOIN chemicals c ON c.id = t.chemical_id
        WHERE date(t.created_at) BETWEEN ? AND ?
    """
    params = [start.isoformat(), end.isoformat()]

    if move_type in ("IN", "OUT"):
        sql += " AND t.type = ?"
        params.append(move_type)
    if chem_id.isdigit():
        sql += " AND c.id = ?"
        params.append(int(chem_id))
    if q:
        sql += (" AND (c.name LIKE ? OR c.supplier LIKE ? OR t.user_name LIKE ?"
                " OR t.purpose LIKE ? OR t.reference LIKE ?)")
        params += [f"%{q}%"] * 5

    sql += " ORDER BY t.created_at DESC, t.id DESC"
    rows = get_db().execute(sql, params).fetchall()
    filters = {"start": start.isoformat(), "end": end.isoformat(),
               "type": move_type, "chemical": chem_id, "q": q}
    return rows, filters


@app.route("/movements")
def movements():
    rows, filters = query_movements(request.args)

    summary = {
        "in": sum(r["type"] == "IN" for r in rows),
        "out": sum(r["type"] == "OUT" for r in rows),
        "chemicals": len({r["chemical_id"] for r in rows}),
        "people": len({r["user_name"] for r in rows if r["user_name"]}),
    }

    # Most used chemicals (stock out) in the selected period
    usage = {}
    for r in rows:
        if r["type"] == "OUT":
            u = usage.setdefault(r["chemical_id"], {
                "id": r["chemical_id"], "name": r["name"], "unit": r["unit"],
                "total": 0, "count": 0})
            u["total"] += r["amount"]
            u["count"] += 1
    top_used = sorted(usage.values(), key=lambda u: (-u["count"], -u["total"]))[:5]

    # Movements per day for the bar chart (oldest on the left)
    daily = {}
    for r in rows:
        day = daily.setdefault(r["created_at"][:10], {"IN": 0, "OUT": 0})
        day[r["type"]] += 1
    days = sorted(daily.items())[-31:]
    busiest = max((d["IN"] + d["OUT"] for _, d in days), default=1)

    chemicals = get_db().execute(
        "SELECT id, name FROM chemicals ORDER BY name COLLATE NOCASE").fetchall()

    return render_template(
        "movements.html", rows=rows, filters=filters, summary=summary,
        top_used=top_used, days=days, busiest=busiest, chemicals=chemicals,
    )


@app.route("/movements.csv")
def export_movements():
    """Download the filtered stock movements as CSV."""
    rows, filters = query_movements(request.args)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Movement ID", "Date & time", "Type", "Chemical ID", "Chemical",
                     "CAS No.", "Quantity", "Unit", "Balance after", "Supplier",
                     "Lot No.", "Reference", "Name", "Purpose", "Location"])
    for r in rows:
        writer.writerow([
            f"TX-{r['id']:05d}", r["created_at"], "Stock in" if r["type"] == "IN" else "Stock out",
            f"CH-{r['chemical_id']:04d}", r["name"], r["cas_no"], fmt_qty(r["amount"]), r["unit"],
            "" if r["balance_after"] is None else fmt_qty(r["balance_after"]),
            r["supplier"], r["lot_no"], r["reference"], r["user_name"], r["purpose"], r["location"],
        ])

    filename = f"stock_movements_{filters['start']}_to_{filters['end']}.csv"
    return Response(
        "﻿" + out.getvalue(),  # BOM so Excel reads it correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ------------------------------------------------------------ data analysis --

@app.route("/analysis")
def analysis():
    period = request.args.get("period", "90")
    period = int(period) if period.isdigit() and int(period) in ANALYSIS_PERIODS else 90
    today = date.today()
    start = today - timedelta(days=period)
    db = get_db()

    chems = db.execute("SELECT * FROM chemicals ORDER BY name COLLATE NOCASE").fetchall()
    txs = db.execute(
        "SELECT t.*, c.name, c.unit FROM transactions t JOIN chemicals c ON c.id = t.chemical_id "
        "WHERE date(t.created_at) >= ? ORDER BY t.created_at", (start.isoformat(),)
    ).fetchall()
    outs = [t for t in txs if t["type"] == "OUT"]

    # 1. Usage forecast: how fast is each chemical used, and when will it run out?
    used = {}
    for t in outs:
        u = used.setdefault(t["chemical_id"], {"total": 0, "count": 0})
        u["total"] += t["amount"]
        u["count"] += 1

    forecast, slow_moving = [], []
    for c in chems:
        u = used.get(c["id"])
        if not u:
            slow_moving.append(c)
            continue
        per_day = u["total"] / period
        days_left = c["quantity"] / per_day if per_day else None
        if days_left is not None and days_left <= LEAD_TIME_DAYS:
            status = ("danger", "Order now")
        elif days_left is not None and days_left <= LEAD_TIME_DAYS + 30:
            status = ("warn", "Order soon")
        else:
            status = ("ok", "Enough")
        forecast.append({
            "chem": c,
            "total": u["total"],
            "count": u["count"],
            "per_week": per_day * 7,
            "days_left": days_left,
            "runout": today + timedelta(days=int(days_left)) if days_left is not None else None,
            "suggest": max(0.0, per_day * COVER_DAYS - c["quantity"]),
            "status": status,
        })
    forecast.sort(key=lambda f: f["days_left"] if f["days_left"] is not None else 1e9)

    # 2. Trend: movements per week (short periods) or per month (long periods)
    weekly = period <= 90
    trend = {}
    for t in txs:
        d = parse_date(t["created_at"][:10])
        if weekly:
            monday = d - timedelta(days=d.weekday())
            key, label = monday.isoformat(), f"Wk {monday:%d/%m}"
        else:
            key, label = d.strftime("%Y-%m"), d.strftime("%b %y")
        bucket = trend.setdefault(key, {"label": label, "IN": 0, "OUT": 0})
        bucket[t["type"]] += 1
    trend = [v for _, v in sorted(trend.items())]
    trend_max = max((max(b["IN"], b["OUT"]) for b in trend), default=1) or 1

    # 3. Who uses the most (stock out by person)
    people = {}
    for t in outs:
        name = t["user_name"] or "Unknown"
        p = people.setdefault(name, {"name": name, "count": 0, "chems": set()})
        p["count"] += 1
        p["chems"].add(t["chemical_id"])
    people = sorted(people.values(), key=lambda p: -p["count"])

    # 4. What is it used for (stock out by purpose)
    purposes = {}
    for t in outs:
        key = t["purpose"] or "Not stated"
        purposes[key] = purposes.get(key, 0) + 1
    purposes = sorted(purposes.items(), key=lambda kv: -kv[1])[:8]

    # 5. Hazard profile of the whole inventory
    hazard_counts = {h: 0 for h in GHS_HAZARDS}
    for c in chems:
        for h in hazard_list(c):
            hazard_counts[h] = hazard_counts.get(h, 0) + 1
    hazards = sorted(((h, n) for h, n in hazard_counts.items() if n), key=lambda x: -x[1])

    # 6. Expiry outlook
    buckets = [("Expired", "danger"), ("Within 30 days", "danger"), ("31–90 days", "warn"),
               ("91–180 days", "warn"), ("Over 180 days", "ok"), ("No expiry date", "muted")]
    expiry = {label: [] for label, _ in buckets}
    for c in chems:
        exp = parse_date(c["expiry_date"])
        if not exp:
            expiry["No expiry date"].append(c)
            continue
        left = (exp - today).days
        label = ("Expired" if left < 0 else "Within 30 days" if left <= 30 else
                 "31–90 days" if left <= 90 else "91–180 days" if left <= 180 else "Over 180 days")
        expiry[label].append(c)
    expiry = [(label, level, expiry[label]) for label, level in buckets]

    # 7. Suppliers
    suppliers = {}
    for c in chems:
        s = suppliers.setdefault(c["supplier"] or "Unknown", {"chems": 0, "restocks": 0})
        s["chems"] += 1
    for t in txs:
        if t["type"] == "IN":
            supplier = next((c["supplier"] for c in chems if c["id"] == t["chemical_id"]), None)
            suppliers.setdefault(supplier or "Unknown", {"chems": 0, "restocks": 0})["restocks"] += 1
    suppliers = sorted(suppliers.items(), key=lambda kv: (-kv[1]["chems"], kv[0]))

    summary = {
        "outs": len(outs),
        "ins": len(txs) - len(outs),
        "active": len(used),
        "slow": len(slow_moving),
        "reorder": sum(f["status"][1] != "Enough" for f in forecast),
    }

    return render_template(
        "analysis.html", period=period, periods=ANALYSIS_PERIODS, start=start, summary=summary,
        forecast=forecast, slow_moving=slow_moving, trend=trend, trend_max=trend_max,
        weekly=weekly, people=people, purposes=purposes, hazards=hazards,
        hazard_max=max((n for _, n in hazards), default=1), expiry=expiry,
        expiry_max=max((len(c) for _, _, c in expiry), default=1) or 1,
        suppliers=suppliers, total_chems=len(chems),
        lead_time=LEAD_TIME_DAYS, cover_days=COVER_DAYS,
    )


@app.route("/export.csv")
def export_csv():
    """Download the chemical register as a CSV file (opens in Excel)."""
    rows = get_db().execute("SELECT * FROM chemicals ORDER BY name COLLATE NOCASE").fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["No.", "Chemical name", "CAS No.", "Formula", "GHS hazards", "Location",
                     "Quantity", "Unit", "Supplier", "Lot No.", "Received", "Expiry",
                     "SDS date", "Alerts"])
    for i, r in enumerate(rows, start=1):
        writer.writerow([
            i, r["name"], r["cas_no"], r["formula"], "; ".join(hazard_list(r)), r["location"],
            fmt_qty(r["quantity"]), r["unit"], r["supplier"], r["lot_no"], r["received_date"],
            r["expiry_date"], r["sds_date"], "; ".join(m for _, m in chemical_alerts(r)),
        ])

    filename = f"chemical_register_{date.today():%Y%m%d}.csv"
    return Response(
        "﻿" + out.getvalue(),  # BOM so Excel reads it correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


init_db()

if __name__ == "__main__":
    # Debug mode auto-reloads code and shows error details, but its error page can run code,
    # so it is OFF unless you set CHEMTRACK_DEBUG=1 while developing on your own PC.
    app.run(debug=os.environ.get("CHEMTRACK_DEBUG") == "1")


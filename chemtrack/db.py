"""
Database connection and versioned migrations.

The database version is stored in SQLite's `user_version`. Each migration runs once,
in order, inside a transaction. Before upgrading an existing database, a backup copy
is saved automatically, so an upgrade can always be undone.
"""

import sqlite3
from datetime import datetime

from flask import g

from . import config

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name                 TEXT    NOT NULL,
    role                 TEXT    NOT NULL CHECK (role IN ('admin', 'staff', 'viewer')),
    password_hash        TEXT    NOT NULL,
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

-- Where things are stored: room + storage unit
CREATE TABLE locations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    room              TEXT    NOT NULL,
    unit_name         TEXT    NOT NULL DEFAULT '',
    storage_type      TEXT    NOT NULL DEFAULT 'General shelf',
    flammable_limit_l REAL,                          -- e.g. Bomba permit limit for this cabinet/room
    active            INTEGER NOT NULL DEFAULT 1,
    notes             TEXT,
    created_at        TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- What a chemical IS (one row per chemical product)
CREATE TABLE substances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    cas_no       TEXT,
    formula      TEXT,
    hazards      TEXT    NOT NULL DEFAULT '',        -- comma separated GHS classes
    unit         TEXT    NOT NULL DEFAULT 'g',
    min_quantity REAL    NOT NULL DEFAULT 0,         -- reorder level, total of all containers
    is_cr        INTEGER NOT NULL DEFAULT 0,         -- USECHH: chemical hazardous to health
    is_poison    INTEGER NOT NULL DEFAULT 0,         -- Poisons Act 1952 list
    is_precursor INTEGER NOT NULL DEFAULT 0,         -- controlled precursor / scheduled chemical
    sds_date     TEXT,
    sds_file     TEXT,
    notes        TEXT,
    created_at   TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- What we physically HAVE (one row per bottle / bag / cylinder, each with its own QR code)
CREATE TABLE containers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,            -- printed in the QR label, e.g. CT-00042
    substance_id  INTEGER NOT NULL REFERENCES substances(id) ON DELETE CASCADE,
    location_id   INTEGER REFERENCES locations(id),
    size          REAL    NOT NULL,                   -- amount when received
    amount        REAL    NOT NULL,                   -- amount left now
    lot_no        TEXT,
    supplier      TEXT,
    received_date TEXT,
    opened_date   TEXT,
    expiry_date   TEXT,
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'empty', 'disposed', 'missing')),
    owner_id      INTEGER REFERENCES users(id),
    last_seen     TEXT,                               -- last confirmed in a stocktake
    notes         TEXT,
    created_at    TEXT    DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_containers_substance ON containers(substance_id);
CREATE INDEX idx_containers_location ON containers(location_id);

-- Everything that happens to a container
CREATE TABLE movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id  INTEGER NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    type          TEXT    NOT NULL
                  CHECK (type IN ('RECEIVE', 'USE', 'ADJUST', 'TRANSFER', 'DISPOSE', 'STOCKTAKE')),
    change        REAL    NOT NULL DEFAULT 0,         -- + added, - taken away
    balance_after REAL,
    user_id       INTEGER,
    user_name     TEXT,
    purpose       TEXT,
    reference     TEXT,
    created_at    TEXT    DEFAULT (datetime('now', 'localtime')),
    edited_at     TEXT,
    edited_by     TEXT
);
CREATE INDEX idx_movements_container ON movements(container_id);
CREATE INDEX idx_movements_created ON movements(created_at);

-- Scheduled waste (Environmental Quality (Scheduled Wastes) Regulations 2005)
CREATE TABLE waste_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id   INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    description    TEXT    NOT NULL,
    sw_code        TEXT,
    quantity       REAL,
    unit           TEXT,
    disposed_date  TEXT    NOT NULL,
    contractor     TEXT,
    consignment_no TEXT,
    user_id        INTEGER,
    user_name      TEXT,
    notes          TEXT,
    created_at     TEXT    DEFAULT (datetime('now', 'localtime'))
);

-- Chemical Health Risk Assessments (USECHH 2000)
CREATE TABLE assessments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT    NOT NULL,
    assessor        TEXT,
    assessment_date TEXT    NOT NULL,
    next_review     TEXT    NOT NULL,
    report_ref      TEXT,
    notes           TEXT,
    created_at      TEXT    DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE stocktakes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER REFERENCES locations(id),
    started_by  TEXT,
    started_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);

CREATE TABLE stocktake_items (
    stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
    container_id INTEGER NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    checked_at   TEXT    DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (stocktake_id, container_id)
);

-- Who changed what (never edited, only added to)
CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    user_name  TEXT,
    action     TEXT    NOT NULL,
    entity     TEXT    NOT NULL,
    entity_id  INTEGER,
    details    TEXT,
    created_at TEXT    DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_audit_created ON audit_log(created_at);
"""


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _guess_storage_type(text):
    t = text.lower()
    for key, kind in (("flammable", "Flammable cabinet"), ("acid", "Acid cabinet"),
                      ("base", "Base cabinet"), ("oxidi", "Oxidiser cabinet"),
                      ("fridge", "Refrigerator"), ("refrigerator", "Refrigerator")):
        if key in t:
            return kind
    return "General shelf"


def _convert_legacy(conn):
    """Turn the old one-row-per-chemical tables into substances + containers + movements."""
    conn.row_factory = sqlite3.Row
    chems = conn.execute("SELECT * FROM chemicals ORDER BY id").fetchall()

    loc_ids = {}
    for c in chems:
        text = (c["location"] or "").strip()
        if text and text not in loc_ids:
            room, _, unit = text.partition(" – ")
            if not unit:
                room, _, unit = text.partition(" - ")
            cur = conn.execute("INSERT INTO locations (room, unit_name, storage_type) VALUES (?, ?, ?)",
                               (room.strip(), unit.strip(), _guess_storage_type(text)))
            loc_ids[text] = cur.lastrowid

    for c in chems:
        hazards = c["hazards"] or ""
        is_cr = 1 if set(h for h in hazards.split(",") if h) & config.HEALTH_HAZARDS else 0
        conn.execute(
            "INSERT INTO substances (id, name, cas_no, formula, hazards, unit, min_quantity, is_cr, "
            "sds_date, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c["id"], c["name"], c["cas_no"], c["formula"], hazards, c["unit"], c["min_quantity"] or 0,
             is_cr, c["sds_date"], c["notes"], c["created_at"]))
        qty = c["quantity"] or 0
        conn.execute(
            "INSERT INTO containers (id, code, substance_id, location_id, size, amount, lot_no, supplier, "
            "received_date, expiry_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c["id"], f"CT-{c['id']:05d}", c["id"], loc_ids.get((c["location"] or "").strip()), qty, qty,
             c["lot_no"], c["supplier"], c["received_date"], c["expiry_date"],
             "active" if qty > 0 else "empty", c["created_at"]))

    if _table_exists(conn, "transactions"):
        biggest = {}
        for t in conn.execute("SELECT * FROM transactions ORDER BY created_at, id").fetchall():
            keys = t.keys()
            get = lambda k: t[k] if k in keys else None  # noqa: E731
            change = t["amount"] if t["type"] == "IN" else -t["amount"]
            conn.execute(
                "INSERT INTO movements (container_id, type, change, balance_after, user_id, user_name, "
                "purpose, reference, created_at, edited_at, edited_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["chemical_id"], "RECEIVE" if t["type"] == "IN" else "USE", change, get("balance_after"),
                 get("user_id"), t["user_name"], t["purpose"], get("reference"), t["created_at"],
                 get("edited_at"), get("edited_by")))
            if get("balance_after") is not None:
                biggest[t["chemical_id"]] = max(biggest.get(t["chemical_id"], 0), t["balance_after"])
        # A container's size = the most it ever held
        for cid, size in biggest.items():
            conn.execute("UPDATE containers SET size = MAX(size, ?) WHERE id = ?", (size, cid))

    conn.execute("ALTER TABLE chemicals RENAME TO legacy_chemicals")
    if _table_exists(conn, "transactions"):
        conn.execute("ALTER TABLE transactions RENAME TO legacy_transactions")
    conn.row_factory = None


def run_script(conn, script):
    """Run SQL statements one by one. (executescript would COMMIT our open transaction.)"""
    buffer = ""
    for line in script.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            conn.execute(buffer)
            buffer = ""


def migration_1(conn):
    """Container-based structure (and conversion of the old structure if present)."""
    run_script(conn, SCHEMA_V1)
    if _table_exists(conn, "chemicals"):
        _convert_legacy(conn)


def migration_2(conn):
    """Index so a container's full history can find its audit entries quickly."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id)")


MIGRATIONS = [migration_1, migration_2]   # add migration_3 ... here, never edit old ones


def migrate(db_path=None):
    """Bring the database up to the latest version. Safe to call every start."""
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= len(MIGRATIONS):
            return
        has_data = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0
        if has_data:
            from .backup import backup_now  # local import avoids a cycle
            backup_now(path, label=f"before-upgrade-v{version}")
        conn.isolation_level = None  # manage the transaction ourselves
        for number in range(version, len(MIGRATIONS)):
            conn.execute("BEGIN")
            try:
                MIGRATIONS[number](conn)
                conn.execute(f"PRAGMA user_version = {number + 1}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(config.DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

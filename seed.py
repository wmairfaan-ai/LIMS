"""
Load sample data so you can try ChemTrack.

WARNING: this erases all locations, chemicals, containers, movements, waste records,
CHRA records and stocktakes. Staff accounts are kept. A backup is saved first.

Run:  python seed.py
"""

import sqlite3
from datetime import date, timedelta

from chemtrack.backup import backup_now
from chemtrack.config import DB_PATH
from chemtrack.db import migrate


def days(n):
    return (date.today() + timedelta(days=n)).isoformat()


LOCATIONS = [  # room, unit, type, flammable limit (L)
    ("Lab 1", "Flammable cabinet A", "Flammable cabinet", 10),
    ("Lab 1", "Acid cabinet", "Acid cabinet", None),
    ("Lab 1", "Base cabinet", "Base cabinet", None),
    ("Lab 2", "Shelf B", "General shelf", 2),
    ("Lab 2", "Shelf C", "General shelf", None),
    ("Lab 2", "Poison cabinet", "Toxic / poison cabinet (locked)", None),
]

# name, CAS, formula, hazards, unit, reorder, is_cr, poison, precursor, SDS date (None = no SDS)
SUBSTANCES = [
    ("Ethanol absolute", "64-17-5", "C2H5OH", "Flammable,Irritant", "L", 2, 1, 0, 0, days(-400)),
    ("Acetone", "67-64-1", "C3H6O", "Flammable,Irritant", "L", 1, 1, 0, 1, days(-300)),
    ("Methanol HPLC grade", "67-56-1", "CH3OH", "Flammable,Acute Toxic,Health Hazard", "L", 1, 1, 1, 0, days(-900)),
    ("n-Hexane", "110-54-3", "C6H14", "Flammable,Irritant,Health Hazard,Environmental", "L", 1, 1, 0, 0, days(-2100)),
    ("Hydrochloric acid 37%", "7647-01-0", "HCl", "Corrosive,Irritant", "L", 1, 1, 0, 1, days(-500)),
    ("Sulfuric acid 98%", "7664-93-9", "H2SO4", "Corrosive", "L", 1, 1, 0, 1, days(-1000)),
    ("Nitric acid 65%", "7697-37-2", "HNO3", "Oxidising,Corrosive", "L", 0.5, 1, 0, 0, days(-200)),
    ("Sodium hydroxide pellets", "1310-73-2", "NaOH", "Corrosive", "g", 500, 1, 0, 0, days(-150)),
    ("Potassium permanganate", "7722-64-7", "KMnO4", "Oxidising,Irritant,Environmental", "g", 100, 1, 0, 1, days(-100)),
    ("Copper(II) sulfate pentahydrate", "7758-99-8", "CuSO4·5H2O", "Irritant,Environmental", "g", 200, 1, 0, 0, None),
    ("Sodium chloride", "7647-14-5", "NaCl", "", "kg", 0.5, 0, 0, 0, days(-700)),
]

# substance, location index, size, amount left, lot, supplier, received (days ago), expiry (days from now)
CONTAINERS = [
    ("Ethanol absolute", 0, 2.5, 2.5, "K5012", "Merck", 120, 600),
    ("Ethanol absolute", 0, 2.5, 1.0, "K5012", "Merck", 120, 600),
    ("Acetone", 3, 2.5, 1.75, "A221", "Fisher Scientific", 60, 700),
    ("Methanol HPLC grade", 0, 2.5, 0.5, "M8890", "Merck", 200, 20),
    ("n-Hexane", 0, 2.5, 2.0, "H1102", "Sigma-Aldrich", 90, 400),
    ("Hydrochloric acid 37%", 1, 2.5, 2.0, "H3741", "Merck", 150, 500),
    ("Sulfuric acid 98%", 1, 2.5, 1.0, "S0098", "Fisher Scientific", 300, -10),
    ("Nitric acid 65%", 1, 2.5, 1.5, "N6501", "Merck", 100, 450),
    ("Sodium hydroxide pellets", 2, 500, 300, "SH553", "R&M Chemicals", 45, 900),
    ("Sodium hydroxide pellets", 2, 500, 500, "SH560", "R&M Chemicals", 6, 950),
    ("Potassium permanganate", 3, 250, 225, "PP207", "R&M Chemicals", 30, 1000),
    ("Copper(II) sulfate pentahydrate", 4, 500, 450, "CS145", "Systerm", 400, 800),
    ("Sodium chloride", 4, 1, 1, "SC900", "Systerm", 500, 1500),
    ("Sodium chloride", 4, 1, 0, "SC880", "Systerm", 600, 1200),   # empty, waiting for disposal
]

# container index, days ago, time, amount used, who, purpose
USES = [
    (1, 20, "09:15", 0.5, "Afiq", "CHM101 practical"),
    (2, 18, "10:30", 0.5, "Kumar", "Cleaning glassware"),
    (3, 15, "14:05", 1.0, "Kumar", "HPLC mobile phase"),
    (5, 14, "09:40", 0.25, "Siti", "Titration lab"),
    (1, 12, "11:20", 1.0, "Siti", "Extraction practical"),
    (8, 10, "08:50", 200, "Afiq", "Preparing 1 M NaOH"),
    (11, 9, "15:10", 50, "Nurul", "Crystal growing practical"),
    (10, 8, "10:00", 25, "Siti", "Redox titration"),
    (3, 4, "13:30", 1.0, "Kumar", "HPLC mobile phase"),
    (2, 3, "09:05", 0.25, "Siti", "Cleaning glassware"),
    (5, 1, "09:30", 0.25, "Nurul", "Titration lab"),
    (13, 2, "11:00", 1.0, "Nurul", "Brine preparation"),
]

WIPE = ["stocktake_items", "stocktakes", "waste_records", "assessments", "movements", "containers",
        "substances", "locations"]


def main():
    migrate()
    print(f"Backup first: {backup_now(label='before-seed')}")
    conn = sqlite3.connect(DB_PATH)
    with conn:
        for table in WIPE:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" % ",".join("?" * len(WIPE)), WIPE)

        loc_ids = [conn.execute("INSERT INTO locations (room, unit_name, storage_type, flammable_limit_l) "
                                "VALUES (?, ?, ?, ?)", row).lastrowid for row in LOCATIONS]
        sub_ids, units = {}, {}
        for name, cas, formula, haz, unit, reorder, cr, poison, prec, sds in SUBSTANCES:
            sub_ids[name] = conn.execute(
                "INSERT INTO substances (name, cas_no, formula, hazards, unit, min_quantity, is_cr, is_poison, "
                "is_precursor, sds_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, cas, formula, haz, unit, reorder, cr, poison, prec, sds)).lastrowid
            units[name] = unit

        uses_by = {}
        for ci, ago, hhmm, amount, who, purpose in USES:
            uses_by.setdefault(ci, []).append((ago, hhmm, amount, who, purpose))

        for i, (name, li, size, left, lot, supplier, rcv, exp) in enumerate(CONTAINERS):
            moves = sorted(uses_by.get(i, []), key=lambda u: (-u[0], u[1]))
            start = left + sum(m[2] for m in moves)       # work back to what it held on arrival
            size = max(size, start)
            status = "active" if left > 0 else "empty"
            cid = conn.execute(
                "INSERT INTO containers (code, substance_id, location_id, size, amount, lot_no, supplier, "
                "received_date, expiry_date, status, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"CT-{i + 1:05d}", sub_ids[name], loc_ids[li], size, left, lot, supplier, days(-rcv),
                 days(exp), status, days(-rcv))).lastrowid
            balance = start
            conn.execute("INSERT INTO movements (container_id, type, change, balance_after, user_name, purpose, "
                         "reference, created_at) VALUES (?, 'RECEIVE', ?, ?, 'system', 'Received', ?, ?)",
                         (cid, start, start, f"DO-{lot}", f"{days(-rcv)} 09:00:00"))
            for ago, hhmm, amount, who, purpose in moves:
                balance = round(balance - amount, 6)
                conn.execute("INSERT INTO movements (container_id, type, change, balance_after, user_name, "
                             "purpose, created_at) VALUES (?, 'USE', ?, ?, ?, ?, ?)",
                             (cid, -amount, balance, who, purpose, f"{days(-ago)} {hhmm}:00"))

        conn.execute("INSERT INTO assessments (title, assessor, assessment_date, next_review, report_ref) "
                     "VALUES (?, ?, ?, ?, ?)", ("Lab 1 and Lab 2 – teaching laboratories", "Sample assessor (JKKP/HQ/…)",
                                                days(-1750), days(75), "CHRA-2021-07"))
        conn.execute("INSERT INTO waste_records (description, sw_code, quantity, unit, disposed_date, contractor, "
                     "user_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("Sample entry: replace with your real records", "SW (sample)", 5, "L", days(-30),
                      "Licensed contractor", "system"))
    conn.close()
    print(f"Loaded {len(LOCATIONS)} locations, {len(SUBSTANCES)} chemicals, {len(CONTAINERS)} containers.")


if __name__ == "__main__":
    main()

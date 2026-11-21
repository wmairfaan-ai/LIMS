"""
Load sample chemicals so you can try the app.

WARNING: this deletes chemicals.db and starts fresh.
Run:  python seed.py
"""

import sqlite3
from datetime import date, timedelta

from app import DB_PATH, SCHEMA


def days(n):
    """Date n days from today, as YYYY-MM-DD (negative = in the past)."""
    return (date.today() + timedelta(days=n)).isoformat()


# name, CAS, formula, hazards, location, qty, unit, reorder, supplier, lot, received, expiry, sds
CHEMICALS = [
    ("Ethanol absolute", "64-17-5", "C2H5OH", "Flammable,Irritant",
     "Lab 1 – Flammable cabinet A", 4, "L", 2, "Merck", "K5012", days(-120), days(600), days(-400)),
    ("Acetone", "67-64-1", "C3H6O", "Flammable,Irritant",
     "Lab 2 – Shelf B", 2.5, "L", 1, "Fisher Scientific", "A221", days(-60), days(700), days(-300)),
    ("Methanol HPLC grade", "67-56-1", "CH3OH", "Flammable,Acute Toxic,Health Hazard",
     "Lab 1 – Flammable cabinet A", 0.5, "L", 1, "Merck", "M8890", days(-200), days(20), days(-900)),
    ("n-Hexane", "110-54-3", "C6H14", "Flammable,Irritant,Health Hazard,Environmental",
     "Lab 1 – Flammable cabinet A", 2, "L", 1, "Sigma-Aldrich", "H1102", days(-90), days(400), days(-2100)),
    ("Hydrochloric acid 37%", "7647-01-0", "HCl", "Corrosive,Irritant",
     "Lab 1 – Acid cabinet", 2.5, "L", 1, "Merck", "H3741", days(-150), days(500), days(-500)),
    ("Sulfuric acid 98%", "7664-93-9", "H2SO4", "Corrosive",
     "Lab 1 – Acid cabinet", 1, "L", 1, "Fisher Scientific", "S0098", days(-300), days(-10), days(-1000)),
    ("Nitric acid 65%", "7697-37-2", "HNO3", "Oxidising,Corrosive",
     "Lab 1 – Acid cabinet", 1.5, "L", 0.5, "Merck", "N6501", days(-100), days(450), days(-200)),
    ("Sodium hydroxide pellets", "1310-73-2", "NaOH", "Corrosive",
     "Lab 1 – Base cabinet", 800, "g", 500, "R&M Chemicals", "SH553", days(-45), days(900), days(-150)),
    ("Potassium permanganate", "7722-64-7", "KMnO4", "Oxidising,Irritant,Environmental",
     "Lab 2 – Shelf B", 250, "g", 100, "R&M Chemicals", "PP207", days(-30), days(1000), days(-100)),
    ("Copper(II) sulfate pentahydrate", "7758-99-8", "CuSO4·5H2O", "Irritant,Environmental",
     "Lab 2 – Shelf C", 500, "g", 200, "Systerm", "CS145", days(-400), days(800), None),
    ("Sodium chloride", "7647-14-5", "NaCl", "",
     "Lab 2 – Shelf C", 1, "kg", 0.5, "Systerm", "SC900", days(-500), days(1500), days(-700)),
]

# chemical, days ago, time, type, amount, name, purpose, reference
USAGE = [
    ("Ethanol absolute", 20, "09:15", "OUT", 0.5, "Afiq", "CHM101 practical", ""),
    ("Acetone", 18, "10:30", "OUT", 0.5, "Kumar", "Cleaning glassware", ""),
    ("Methanol HPLC grade", 15, "14:05", "OUT", 1, "Kumar", "HPLC mobile phase", ""),
    ("Hydrochloric acid 37%", 14, "09:40", "OUT", 0.25, "Siti", "Titration lab", ""),
    ("Ethanol absolute", 12, "11:20", "OUT", 1, "Siti", "Extraction practical", ""),
    ("Sodium hydroxide pellets", 10, "08:50", "OUT", 200, "Afiq", "Preparing 1 M NaOH", ""),
    ("Copper(II) sulfate pentahydrate", 9, "15:10", "OUT", 50, "Nurul", "Crystal growing practical", ""),
    ("Potassium permanganate", 8, "10:00", "OUT", 25, "Siti", "Redox titration", ""),
    ("Sodium hydroxide pellets", 6, "16:30", "IN", 500, "Afiq", "Restock", "PO-2611-009"),
    ("Ethanol absolute", 5, "16:45", "IN", 2.5, "Afiq", "Restock", "PO-2611-014"),
    ("Methanol HPLC grade", 4, "13:30", "OUT", 0.5, "Kumar", "HPLC mobile phase", ""),
    ("Acetone", 3, "09:05", "OUT", 0.25, "Siti", "Cleaning glassware", ""),
    ("Ethanol absolute", 2, "10:10", "OUT", 0.5, "Kumar", "Extraction practical", ""),
    ("Hydrochloric acid 37%", 1, "09:30", "OUT", 0.25, "Nurul", "Titration lab", ""),
    ("Sodium hydroxide pellets", 1, "11:00", "OUT", 100, "Nurul", "Preparing 0.1 M NaOH", ""),
]


def stamp(days_ago, hhmm):
    return f"{days(-days_ago)} {hhmm}:00"


def main():
    conn = sqlite3.connect(DB_PATH)
    with conn:  # commits at the end of the block
        conn.executescript(
            "DROP TABLE IF EXISTS transactions; DROP TABLE IF EXISTS chemicals;" + SCHEMA)
        for c in CHEMICALS:
            name, final_qty, received = c[0], c[5], c[10]
            cur = conn.execute(
                "INSERT INTO chemicals (name, cas_no, formula, hazards, location, quantity, unit,"
                " min_quantity, supplier, lot_no, received_date, expiry_date, sds_date)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", c)
            chem_id = cur.lastrowid

            # Work backwards from today's quantity to find the opening stock,
            # so every "balance after" in the history adds up correctly.
            moves = [u for u in USAGE if u[0] == name]
            net = sum(amt if kind == "IN" else -amt for _, _, _, kind, amt, *_ in moves)
            balance = final_qty - net

            conn.execute(
                "INSERT INTO transactions (chemical_id, type, amount, balance_after, user_name,"
                " purpose, reference, created_at) VALUES (?, 'IN', ?, ?, 'system', 'Opening stock', ?, ?)",
                (chem_id, balance, balance, f"DO-{c[9]}", f"{received} 09:00:00"))

            for _, ago, hhmm, kind, amt, who, purpose, ref in sorted(moves, key=lambda u: (-u[1], u[2])):
                balance += amt if kind == "IN" else -amt
                conn.execute(
                    "INSERT INTO transactions (chemical_id, type, amount, balance_after, user_name,"
                    " purpose, reference, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (chem_id, kind, amt, round(balance, 3), who, purpose, ref, stamp(ago, hhmm)))
    conn.close()

    print(f"Loaded {len(CHEMICALS)} sample chemicals into {DB_PATH.name}")


if __name__ == "__main__":
    main()

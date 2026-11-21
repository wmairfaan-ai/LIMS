"""Business rules: alerts, storage checks, totals. No web code here, so it is easy to test."""

from datetime import date, datetime, timedelta

from . import config


def parse_date(text):
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def hazard_list(row):
    return [h for h in (row["hazards"] or "").split(",") if h]


def fmt_qty(value):
    if value is None:
        return "–"
    return f"{round(value, 4):g}"


def to_litres(amount, unit):
    """Volume in litres, or None for units that aren't a volume."""
    return {"L": amount, "mL": amount / 1000}.get(unit)


def location_label(row, prefix=""):
    room = row[f"{prefix}room"] if row[f"{prefix}room"] else None
    if not room:
        return "No location"
    unit = row[f"{prefix}unit_name"]
    return f"{room} › {unit}" if unit else room


def chem_code(i):
    return f"CH-{i:04d}"


def tx_code(i):
    return f"TX-{i:05d}"


def next_container_codes(db, count):
    last = db.execute("SELECT MAX(id) FROM containers").fetchone()[0] or 0
    taken = {r[0] for r in db.execute("SELECT code FROM containers")}
    codes, n = [], last
    while len(codes) < count:
        n += 1
        code = f"CT-{n:05d}"
        if code not in taken:
            codes.append(code)
    return codes


# ---------------------------------------------------------------- inventory --

LIVE = ("active", "empty", "missing")   # statuses still physically (or possibly) in the lab


def substances_overview(db):
    """One dict per substance with totals across its containers."""
    subs = db.execute("SELECT * FROM substances ORDER BY name COLLATE NOCASE").fetchall()
    agg = {r["substance_id"]: r for r in db.execute(
        "SELECT substance_id, "
        "SUM(CASE WHEN status = 'active' THEN amount ELSE 0 END) AS total, "
        "SUM(status = 'active') AS n_active, SUM(status = 'empty') AS n_empty, "
        "SUM(status = 'missing') AS n_missing, "
        "MIN(CASE WHEN status = 'active' THEN expiry_date END) AS first_expiry "
        "FROM containers GROUP BY substance_id")}
    locs = {}
    for r in db.execute(
            "SELECT DISTINCT c.substance_id, l.room, l.unit_name FROM containers c "
            "JOIN locations l ON l.id = c.location_id WHERE c.status = 'active'"):
        locs.setdefault(r["substance_id"], []).append(location_label(r))
    out = []
    for s in subs:
        a = agg.get(s["id"])
        item = {
            "s": s,
            "total": (a["total"] or 0) if a else 0,
            "n_active": (a["n_active"] or 0) if a else 0,
            "n_empty": (a["n_empty"] or 0) if a else 0,
            "n_missing": (a["n_missing"] or 0) if a else 0,
            "first_expiry": a["first_expiry"] if a else None,
            "locations": locs.get(s["id"], []),
        }
        item["alerts"] = substance_alerts(item)
        out.append(item)
    return out


def substance_alerts(item):
    s, today, alerts = item["s"], date.today(), []
    if item["total"] <= 0:
        alerts.append(("danger", "Out of stock"))
    elif s["min_quantity"] and item["total"] <= s["min_quantity"]:
        alerts.append(("warn", "Low stock"))
    exp = parse_date(item["first_expiry"])
    if exp:
        days = (exp - today).days
        if days < 0:
            alerts.append(("danger", "Expired container"))
        elif days <= config.EXPIRY_WARNING_DAYS:
            alerts.append(("warn", f"Expires in {days} days"))
    sds = parse_date(s["sds_date"])
    if not s["sds_file"] and not sds:
        alerts.append(("warn", "No SDS"))
    elif sds and (today - sds).days > config.SDS_MAX_AGE_DAYS:
        alerts.append(("warn", "SDS over 5 years old"))
    if item["n_missing"]:
        alerts.append(("danger", f"{item['n_missing']} missing"))
    if item["n_empty"]:
        alerts.append(("warn", f"{item['n_empty']} empty to dispose"))
    return alerts


def container_alerts(c):
    today, alerts = date.today(), []
    if c["status"] == "missing":
        alerts.append(("danger", "Missing"))
    elif c["status"] == "empty":
        alerts.append(("warn", "Empty: dispose"))
    elif c["status"] == "disposed":
        alerts.append(("muted", "Disposed"))
    if c["status"] in ("active", "empty"):
        exp = parse_date(c["expiry_date"])
        if exp:
            days = (exp - today).days
            if days < 0:
                alerts.append(("danger", "Expired"))
            elif days <= config.EXPIRY_WARNING_DAYS:
                alerts.append(("warn", f"Expires in {days} days"))
        seen = parse_date(c["last_seen"]) or parse_date(c["received_date"]) or parse_date(c["created_at"])
        if seen and (today - seen).days > config.STOCKTAKE_WARNING_DAYS:
            alerts.append(("warn", "Not checked in a year"))
    return alerts


# ------------------------------------------------------------------ storage --

def active_containers(db, location_id=None):
    sql = ("SELECT c.*, s.name, s.cas_no, s.hazards, s.unit, s.is_cr, s.is_poison, s.is_precursor, "
           "l.room, l.unit_name, l.storage_type FROM containers c "
           "JOIN substances s ON s.id = c.substance_id LEFT JOIN locations l ON l.id = c.location_id "
           "WHERE c.status IN ('active', 'empty')")
    params = []
    if location_id is not None:
        sql += " AND c.location_id = ?"
        params.append(location_id)
    return db.execute(sql + " ORDER BY s.name COLLATE NOCASE, c.code", params).fetchall()


def storage_conflicts(containers):
    """Incompatible hazard classes kept in the same location."""
    by_loc = {}
    for c in containers:
        if c["location_id"]:
            by_loc.setdefault(c["location_id"], []).append(c)
    conflicts = []
    for loc_id, items in by_loc.items():
        for a, b in config.INCOMPATIBLE_PAIRS:
            names_a = sorted({c["name"] for c in items if a in hazard_list(c)})
            names_b = sorted({c["name"] for c in items if b in hazard_list(c)})
            if names_a and names_b:
                conflicts.append({"location_id": loc_id, "location": location_label(items[0]),
                                  "a": a, "a_names": names_a, "b": b, "b_names": names_b})
    return conflicts


def flammable_totals(db):
    """Litres of flammable liquid per location, compared with the location's limit."""
    totals = {}
    for c in active_containers(db):
        if c["location_id"] and "Flammable" in hazard_list(c):
            litres = to_litres(c["amount"], c["unit"])
            if litres:
                totals[c["location_id"]] = totals.get(c["location_id"], 0) + litres
    out = []
    for loc in db.execute("SELECT * FROM locations WHERE active = 1 ORDER BY room, unit_name"):
        total = round(totals.get(loc["id"], 0), 3)
        limit = loc["flammable_limit_l"]
        out.append({"loc": loc, "litres": total, "limit": limit,
                    "over": bool(limit is not None and total > limit),
                    "pct": min(100, round(total / limit * 100)) if limit else None})
    return out


# --------------------------------------------------------------- compliance --

def chra_status(row):
    due = parse_date(row["next_review"])
    if not due:
        return ("warn", "No review date")
    days = (due - date.today()).days
    if days < 0:
        return ("danger", f"Overdue by {-days} days")
    if days <= config.CHRA_WARNING_DAYS:
        return ("warn", f"Due in {days} days")
    return ("ok", f"Valid until {due:%d %b %Y}")


def add_years(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d + timedelta(days=365 * years)

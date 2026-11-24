"""Business rules: alerts, storage checks, totals. No web code here, so it is easy to test."""

import re
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


def scanned_code(text):
    """The container code from a scan. QR labels hold a web address (…/c/CT-00042), so take its last part."""
    text = (text or "").strip()
    m = re.search(r"/c/([^/?#\s]+)", text, re.I)
    return m.group(1) if m else text


def page_of(text, total, size):
    """(page, pages) for a paged list: anything odd in the address becomes the nearest real page."""
    pages = max(1, -(-total // size))
    try:
        page = int(text)
    except (TypeError, ValueError):
        page = 1
    return min(max(page, 1), pages), pages


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


def usage_rate_days(first_use, period, today=None):
    """
    Number of days to average usage over: the analysis period, or fewer if USE records
    only started inside it. Dividing by the full period when recording began recently
    would understate how fast stock is used (and how soon it runs out).
    """
    first = parse_date(first_use)
    if not first:
        return period
    today = today or date.today()
    return max(1, min(period, (today - first).days + 1))


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


# ---------------------------------------------------------- dashboard score --

def compliance_checklist(db):
    """
    Safety and compliance checks for the dashboard. Each item scores 0..1;
    the compliance score is the average, as a percentage.
    Each item: key, label, score, detail, fix (endpoint, args) for a "Fix" link.
    """
    today = date.today()
    items = substances_overview(db)
    in_use = [i for i in items if i["n_active"]]
    containers = active_containers(db)
    flam = flammable_totals(db)
    checks = []

    def add(key, label, score, detail, fix):
        checks.append({"key": key, "label": label, "score": max(0.0, min(1.0, score)),
                       "ok": score >= 0.999, "detail": detail, "fix": fix})

    n = len(in_use)
    with_sds = sum(1 for i in in_use if i["s"]["sds_file"])
    add("sds", "SDS uploaded for every chemical in use", with_sds / n if n else 1,
        f"{with_sds} of {n}", ("compliance", {}))

    old = sum(1 for i in in_use if any("SDS over" in m for _, m in i["alerts"]))
    add("sds_age", "No SDS older than 5 years", 1 - old / n if n else 1,
        f"{old} too old" if old else "All current", ("compliance", {}))

    chra = db.execute("SELECT next_review FROM assessments").fetchall()
    valid = [a for a in chra if (parse_date(a["next_review"]) or today) >= today]
    add("chra", "CHRA recorded and not overdue", 1 if valid else 0,
        "Valid" if valid else ("Overdue" if chra else "None recorded"), ("compliance", {}))

    locs = [l for l in db.execute("SELECT id FROM locations WHERE active = 1")]
    used_locs = {c["location_id"] for c in containers if c["location_id"]}
    recent = {r[0] for r in db.execute(
        "SELECT DISTINCT location_id FROM stocktakes WHERE finished_at >= ?",
        ((today - timedelta(days=config.STOCKTAKE_WARNING_DAYS)).isoformat(),))}
    checked = len(used_locs & recent)
    add("stocktake", "Stocktake done in the last 12 months", checked / len(used_locs) if used_locs else 1,
        f"{checked} of {len(used_locs)} locations", ("stocktake_list", {}))

    with_flam = [f for f in flam if f["litres"]]
    limited = sum(1 for f in with_flam if f["limit"] is not None)
    add("flam_limits", "Flammable limit set where flammables are stored",
        limited / len(with_flam) if with_flam else 1, f"{limited} of {len(with_flam)} locations", ("locations", {}))

    over = sum(1 for f in flam if f["over"])
    add("flam_over", "No location over its flammable limit", 0 if over else 1,
        f"{over} over limit" if over else "All within limits", ("locations", {}))

    conflicts = storage_conflicts(containers)
    add("segregation", "Incompatible chemicals stored apart", 0 if conflicts else 1,
        f"{len(conflicts)} problem(s)" if conflicts else "OK", ("inventory", {}))

    expired = sum(1 for c in containers if c["status"] == "active"
                  and (parse_date(c["expiry_date"]) or date.max) < today)
    add("expired", "No expired containers in use", 0 if expired else 1,
        f"{expired} expired" if expired else "None", ("inventory", {"show": "expired"}))

    empties = sum(1 for c in containers if c["status"] == "empty")
    add("empties", "Empty containers disposed of", 0 if empties else 1,
        f"{empties} waiting" if empties else "None waiting", ("inventory", {"show": "empty"}))

    missing = db.execute("SELECT COUNT(*) FROM containers WHERE status = 'missing'").fetchone()[0]
    add("missing", "No missing containers", 0 if missing else 1,
        f"{missing} missing" if missing else "None", ("inventory", {"show": "missing"}))

    score = round(sum(c["score"] for c in checks) / len(checks) * 100) if checks else 100
    return score, checks

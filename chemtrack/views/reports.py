"""Stock movements dashboard, data analysis and CSV exports."""

import csv
import io
from datetime import date, timedelta

from flask import Response, render_template, request

from .. import app, config, logic
from ..db import get_db

TYPE_LABELS = {"RECEIVE": "Received", "USE": "Used", "ADJUST": "Corrected", "TRANSFER": "Moved",
               "DISPOSE": "Disposed", "STOCKTAKE": "Stocktake"}
app.jinja_env.globals["TYPE_LABELS"] = TYPE_LABELS
MOVEMENTS_PAGE_SIZE = 50


def movement_filter(args):
    """The chosen filters, and the FROM / WHERE part (with its values) that every movements query shares."""
    today = date.today()
    start = logic.parse_date(args.get("start")) or today - timedelta(days=30)
    end = logic.parse_date(args.get("end")) or today
    if start > end:
        start, end = end, start
    mtype, sid, q = args.get("type", ""), args.get("substance", ""), args.get("q", "").strip()
    sql = ("FROM movements m JOIN containers c ON c.id = m.container_id "
           "JOIN substances s ON s.id = c.substance_id WHERE date(m.created_at) BETWEEN ? AND ?")
    params = [start.isoformat(), end.isoformat()]
    if mtype in TYPE_LABELS:
        sql += " AND m.type = ?"
        params.append(mtype)
    if sid.isdigit():
        sql += " AND s.id = ?"
        params.append(int(sid))
    if q:
        sql += (" AND (s.name LIKE ? OR c.code LIKE ? OR c.supplier LIKE ? OR m.user_name LIKE ? "
                "OR m.purpose LIKE ? OR m.reference LIKE ?)")
        params += [f"%{q}%"] * 6
    return {"start": start.isoformat(), "end": end.isoformat(), "type": mtype, "substance": sid, "q": q}, sql, params


def movement_rows(db, sql, params, limit=-1, offset=0):
    """Movements newest first (limit -1 = all of them)."""
    sql = sql.replace(" WHERE ", " LEFT JOIN locations l ON l.id = c.location_id WHERE ", 1)
    return db.execute(
        "SELECT m.*, c.code, c.lot_no, c.supplier, s.id AS substance_id, s.name, s.cas_no, s.unit, "
        f"l.room, l.unit_name {sql} ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()


@app.route("/movements")
def movements():
    db = get_db()
    filters, sql, params = movement_filter(request.args)
    # The cards, chart and most used table cover every matching movement, not only the page shown
    t = db.execute("SELECT COUNT(*) AS total, SUM(m.type = 'RECEIVE') AS received, SUM(m.type = 'USE') AS used, "
                   "COUNT(DISTINCT s.id) AS substances, COUNT(DISTINCT NULLIF(m.user_name, '')) AS people " + sql,
                   params).fetchone()
    summary = {"in": t["received"] or 0, "out": t["used"] or 0, "substances": t["substances"], "people": t["people"]}
    top_used = db.execute("SELECT s.id, s.name, s.unit, -SUM(m.change) AS total, COUNT(*) AS count " + sql +
                          " AND m.type = 'USE' GROUP BY s.id ORDER BY count DESC, total DESC, s.name LIMIT 5",
                          params).fetchall()
    daily = db.execute("SELECT substr(m.created_at, 1, 10) AS day, SUM(m.type = 'RECEIVE') AS n_in, "
                       "SUM(m.type = 'USE') AS n_out " + sql + " AND m.type IN ('RECEIVE', 'USE') "
                       "GROUP BY day ORDER BY day DESC LIMIT 31", params).fetchall()
    days = [(d["day"], {"IN": d["n_in"], "OUT": d["n_out"]}) for d in reversed(daily)]
    busiest = max((d["IN"] + d["OUT"] for _, d in days), default=1) or 1
    page, pages = logic.page_of(request.args.get("page"), t["total"], MOVEMENTS_PAGE_SIZE)
    rows = movement_rows(db, sql, params, MOVEMENTS_PAGE_SIZE, (page - 1) * MOVEMENTS_PAGE_SIZE)
    subs = db.execute("SELECT id, name FROM substances ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("movements.html", rows=rows, total=t["total"], page=page, pages=pages, filters=filters,
                           summary=summary, top_used=top_used, days=days, busiest=busiest, substances=subs)


@app.route("/movements.csv")
def movements_csv():
    f, sql, params = movement_filter(request.args)
    rows = movement_rows(get_db(), sql, params)   # the export is never paged
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Movement ID", "Date & time", "Type", "Container", "Chemical", "CAS No.", "Change", "Unit",
                "Balance after", "Location", "Supplier", "Lot No.", "Reference", "Name", "Purpose", "Edited by"])
    for r in rows:
        w.writerow([logic.tx_code(r["id"]), r["created_at"], TYPE_LABELS[r["type"]], r["code"], r["name"],
                    r["cas_no"], logic.fmt_qty(r["change"]), r["unit"], logic.fmt_qty(r["balance_after"]),
                    logic.location_label(r), r["supplier"], r["lot_no"], r["reference"], r["user_name"],
                    r["purpose"], r["edited_by"] or ""])
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=stock_movements_{f['start']}_to_{f['end']}.csv"})


@app.route("/analysis")
def analysis():
    period = request.args.get("period", "90")
    period = int(period) if period.isdigit() and int(period) in config.ANALYSIS_PERIODS else 90
    today = date.today()
    start = today - timedelta(days=period)
    db = get_db()
    items = logic.substances_overview(db)
    txs = db.execute(
        "SELECT m.*, c.substance_id, c.supplier FROM movements m JOIN containers c ON c.id = m.container_id "
        "WHERE date(m.created_at) >= ? ORDER BY m.created_at", (start.isoformat(),)).fetchall()
    uses = [t for t in txs if t["type"] == "USE"]

    used = {}
    for t in uses:
        u = used.setdefault(t["substance_id"], {"total": 0, "count": 0})
        u["total"] -= t["change"]
        u["count"] += 1

    first_use = db.execute("SELECT MIN(created_at) FROM movements WHERE type = 'USE'").fetchone()[0]
    rate_days = logic.usage_rate_days(first_use, period, today)

    forecast, slow = [], []
    for i in items:
        s, u = i["s"], used.get(i["s"]["id"])
        if not u:
            if i["n_active"]:
                slow.append(i)
            continue
        per_day = u["total"] / rate_days
        days_left = i["total"] / per_day if per_day else None
        status = (("danger", "Order now") if days_left is not None and days_left <= config.LEAD_TIME_DAYS else
                  ("warn", "Order soon") if days_left is not None and days_left <= config.LEAD_TIME_DAYS + 30 else
                  ("ok", "Enough"))
        forecast.append({"s": s, "stock": i["total"], "total": u["total"], "count": u["count"],
                         "per_week": per_day * 7, "days_left": days_left,
                         "runout": today + timedelta(days=int(days_left)) if days_left is not None else None,
                         "suggest": max(0.0, per_day * config.COVER_DAYS - i["total"]), "status": status})
    forecast.sort(key=lambda f: f["days_left"] if f["days_left"] is not None else 1e9)

    weekly, trend = period <= 90, {}
    for t in txs:
        if t["type"] not in ("RECEIVE", "USE"):
            continue
        d = logic.parse_date(t["created_at"])
        if weekly:
            monday = d - timedelta(days=d.weekday())
            key, label = monday.isoformat(), f"Wk {monday:%d/%m}"
        else:
            key, label = d.strftime("%Y-%m"), d.strftime("%b %y")
        b = trend.setdefault(key, {"label": label, "IN": 0, "OUT": 0})
        b["IN" if t["type"] == "RECEIVE" else "OUT"] += 1
    trend = [v for _, v in sorted(trend.items())]
    trend_max = max((max(b["IN"], b["OUT"]) for b in trend), default=1) or 1

    people, purposes = {}, {}
    for t in uses:
        p = people.setdefault(t["user_name"] or "Unknown", {"name": t["user_name"] or "Unknown", "count": 0, "subs": set()})
        p["count"] += 1
        p["subs"].add(t["substance_id"])
        purposes[t["purpose"] or "Not stated"] = purposes.get(t["purpose"] or "Not stated", 0) + 1
    people = sorted(people.values(), key=lambda p: -p["count"])
    purposes = sorted(purposes.items(), key=lambda kv: -kv[1])[:8]

    hazard_counts = {}
    for i in items:
        if i["n_active"]:
            for h in logic.hazard_list(i["s"]):
                hazard_counts[h] = hazard_counts.get(h, 0) + 1
    hazards = sorted(hazard_counts.items(), key=lambda x: -x[1])

    buckets = [("Expired", "danger"), ("Within 30 days", "danger"), ("31–90 days", "warn"),
               ("91–180 days", "warn"), ("Over 180 days", "ok"), ("No expiry date", "muted")]
    expiry = {label: [] for label, _ in buckets}
    for c in logic.active_containers(db):
        if c["status"] != "active":
            continue
        exp = logic.parse_date(c["expiry_date"])
        if not exp:
            expiry["No expiry date"].append(c)
            continue
        left = (exp - today).days
        expiry["Expired" if left < 0 else "Within 30 days" if left <= 30 else "31–90 days" if left <= 90
               else "91–180 days" if left <= 180 else "Over 180 days"].append(c)
    expiry = [(label, level, expiry[label]) for label, level in buckets]

    suppliers = {}
    for c in db.execute("SELECT supplier, status FROM containers"):
        s = suppliers.setdefault(c["supplier"] or "Unknown", {"containers": 0, "received": 0})
        if c["status"] in ("active", "empty"):
            s["containers"] += 1
    for t in txs:
        if t["type"] == "RECEIVE":
            suppliers.setdefault(t["supplier"] or "Unknown", {"containers": 0, "received": 0})["received"] += 1
    suppliers = sorted(suppliers.items(), key=lambda kv: (-kv[1]["containers"], kv[0]))

    summary = {"outs": len(uses), "ins": sum(t["type"] == "RECEIVE" for t in txs), "active": len(used),
               "slow": len(slow), "reorder": sum(f["status"][1] != "Enough" for f in forecast)}
    return render_template(
        "analysis.html", period=period, periods=config.ANALYSIS_PERIODS, start=start, summary=summary,
        rate_days=rate_days, first_use=logic.parse_date(first_use),
        forecast=forecast, slow=slow, trend=trend, trend_max=trend_max, weekly=weekly, people=people,
        purposes=purposes, hazards=hazards, hazard_max=max((n for _, n in hazards), default=1),
        expiry=expiry, expiry_max=max((len(c) for _, _, c in expiry), default=1) or 1,
        suppliers=suppliers, n_substances=sum(1 for i in items if i["n_active"]),
        lead_time=config.LEAD_TIME_DAYS, cover_days=config.COVER_DAYS)

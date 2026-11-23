"""Malaysian compliance: USECHH chemical inventory / register, CHRA, scheduled waste, stocktake, audit log."""

import csv
import io
from datetime import date

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

from .. import app, config, logic
from ..db import get_db, now_str
from ..security import audit, roles_required


@app.route("/compliance")
def compliance():
    db = get_db()
    items = logic.substances_overview(db)
    in_use = [i for i in items if i["n_active"]]
    chra = db.execute("SELECT * FROM assessments ORDER BY next_review").fetchall()
    return render_template(
        "compliance.html",
        n_ci=len(in_use),
        n_cr=sum(1 for i in in_use if i["s"]["is_cr"]),
        no_sds=[i for i in in_use if not i["s"]["sds_file"]],
        old_sds=[i for i in in_use if any("SDS over" in m for _, m in i["alerts"])],
        controlled=[i for i in in_use if i["s"]["is_poison"] or i["s"]["is_precursor"]],
        chra=[(a, logic.chra_status(a)) for a in chra],
        flam=logic.flammable_totals(db),
        waste=db.execute("SELECT * FROM waste_records ORDER BY disposed_date DESC, id DESC LIMIT 5").fetchall(),
        empties=db.execute("SELECT c.code, s.name FROM containers c JOIN substances s ON s.id = c.substance_id "
                           "WHERE c.status = 'empty' ORDER BY c.code").fetchall(),
        stocktake=db.execute("SELECT * FROM stocktakes WHERE finished_at IS NOT NULL "
                             "ORDER BY finished_at DESC LIMIT 1").fetchone(),
    )


# ------------------------------------------------- USECHH inventory / register --

def register_rows(kind):
    db = get_db()
    rows = []
    for i in logic.substances_overview(db):
        s = i["s"]
        if not i["n_active"] or (kind == "CR" and not s["is_cr"]):
            continue
        rows.append({"s": s, "total": i["total"], "n": i["n_active"], "locations": i["locations"],
                     "hazards": logic.hazard_list(s)})
    return rows


@app.route("/register/<kind>")
def register_report(kind):
    kind = kind.upper()
    if kind not in ("CI", "CR"):
        abort(404)
    return render_template("register.html", kind=kind, rows=register_rows(kind))


@app.route("/register/<kind>.csv")
def register_csv(kind):
    kind = kind.upper()
    if kind not in ("CI", "CR"):
        abort(404)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["No.", "Chemical name", "CAS No.", "GHS hazard classes", "Hazardous to health (CR)",
                "Poison", "Precursor / controlled", "Quantity in stock", "Unit", "Containers",
                "Locations", "SDS available", "SDS date"])
    for n, r in enumerate(register_rows(kind), 1):
        s = r["s"]
        w.writerow([n, s["name"], s["cas_no"], "; ".join(r["hazards"]), "Yes" if s["is_cr"] else "No",
                    "Yes" if s["is_poison"] else "No", "Yes" if s["is_precursor"] else "No",
                    logic.fmt_qty(r["total"]), s["unit"], r["n"], "; ".join(r["locations"]),
                    "Yes" if s["sds_file"] else "No", s["sds_date"] or ""])
    name = "chemical_register" if kind == "CR" else "chemical_inventory"
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}_{date.today():%Y%m%d}.csv"})


# --------------------------------------------------------------------- CHRA --

def read_chra_form(form):
    data = {k: form.get(k, "").strip() for k in ("title", "assessor", "assessment_date", "next_review",
                                                  "report_ref", "notes")}
    errors = []
    if not data["title"]:
        errors.append("Describe what the assessment covers.")
    done = logic.parse_date(data["assessment_date"])
    if not done:
        errors.append("Enter the assessment date.")
    if data["next_review"] and not logic.parse_date(data["next_review"]):
        errors.append("Next review date is not valid.")
    if done and not data["next_review"]:
        data["next_review"] = logic.add_years(done, config.CHRA_REVIEW_YEARS).isoformat()
    return data, errors


@app.route("/chra/new", methods=["GET", "POST"])
@app.route("/chra/<int:aid>/edit", methods=["GET", "POST"])
@roles_required("admin")
def chra_form(aid=None):
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id = ?", (aid,)).fetchone() if aid else None
    if aid and a is None:
        abort(404)
    if request.method == "POST":
        data, errors = read_chra_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("chra_form.html", a=dict(data, id=aid))
        cols = list(data)
        if a:
            db.execute(f"UPDATE assessments SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                       [data[c] for c in cols] + [aid])
            audit("update", "CHRA", aid, **data)
        else:
            cur = db.execute(f"INSERT INTO assessments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [data[c] for c in cols])
            audit("create", "CHRA", cur.lastrowid, **data)
        db.commit()
        flash("CHRA saved.", "ok")
        return redirect(url_for("compliance"))
    return render_template("chra_form.html", a=dict(a) if a else {})


# ----------------------------------------------------------- scheduled waste --

@app.route("/waste", methods=["GET", "POST"])
def waste():
    db = get_db()
    if request.method == "POST":
        if g.user["role"] not in ("admin", "staff"):
            abort(403)
        f = request.form
        try:
            qty = float(f.get("quantity") or 0) or None
        except ValueError:
            qty = -1
        errors = []
        if not f.get("description", "").strip():
            errors.append("Describe the waste.")
        if not f.get("sw_code", "").strip():
            errors.append("Enter the SW code.")
        if qty is not None and qty < 0:
            errors.append("Quantity must be a positive number.")
        if not logic.parse_date(f.get("disposed_date")):
            errors.append("Enter the date.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            cur = db.execute(
                "INSERT INTO waste_records (description, sw_code, quantity, unit, disposed_date, contractor, "
                "consignment_no, user_id, user_name, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f["description"].strip(), f["sw_code"].strip().upper(), qty, f.get("unit") or None,
                 f["disposed_date"], f.get("contractor", "").strip(), f.get("consignment_no", "").strip(),
                 g.user["id"], g.user["name"], f.get("notes", "").strip()))
            audit("create", "waste", cur.lastrowid, sw_code=f["sw_code"], quantity=qty)
            db.commit()
            flash("Waste record saved.", "ok")
        return redirect(url_for("waste"))
    rows = db.execute("SELECT w.*, c.code FROM waste_records w LEFT JOIN containers c ON c.id = w.container_id "
                      "ORDER BY w.disposed_date DESC, w.id DESC").fetchall()
    used_codes = [r[0] for r in db.execute("SELECT DISTINCT sw_code FROM waste_records WHERE sw_code != '' "
                                            "ORDER BY sw_code")]
    return render_template("waste.html", rows=rows, used_codes=used_codes)


# ---------------------------------------------------------------- stocktake --

@app.route("/stocktake", methods=["GET", "POST"])
def stocktake_list():
    db = get_db()
    if request.method == "POST":
        if g.user["role"] not in ("admin", "staff"):
            abort(403)
        lid = request.form.get("location_id", "")
        if not lid.isdigit():
            flash("Choose the location to check.", "danger")
            return redirect(url_for("stocktake_list"))
        cur = db.execute("INSERT INTO stocktakes (location_id, started_by) VALUES (?, ?)", (int(lid), g.user["name"]))
        audit("start", "stocktake", cur.lastrowid, location_id=int(lid))
        db.commit()
        return redirect(url_for("stocktake_view", tid=cur.lastrowid))
    rows = db.execute(
        "SELECT t.*, l.room, l.unit_name, (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id = t.id) AS found "
        "FROM stocktakes t LEFT JOIN locations l ON l.id = t.location_id ORDER BY t.id DESC LIMIT 50").fetchall()
    locs = db.execute("SELECT * FROM locations WHERE active = 1 ORDER BY room, unit_name").fetchall()
    return render_template("stocktake_list.html", rows=rows, locations=locs)


def get_stocktake_or_404(tid):
    t = get_db().execute("SELECT t.*, l.room, l.unit_name FROM stocktakes t "
                         "LEFT JOIN locations l ON l.id = t.location_id WHERE t.id = ?", (tid,)).fetchone()
    if t is None:
        abort(404)
    return t


@app.route("/stocktake/<int:tid>")
def stocktake_view(tid):
    db, t = get_db(), get_stocktake_or_404(tid)
    found_ids = {r[0] for r in db.execute("SELECT container_id FROM stocktake_items WHERE stocktake_id = ?", (tid,))}
    expected = db.execute(
        "SELECT c.*, s.name, s.unit FROM containers c JOIN substances s ON s.id = c.substance_id "
        "WHERE c.location_id = ? AND c.status IN ('active', 'empty', 'missing') ORDER BY s.name, c.code",
        (t["location_id"],)).fetchall()
    return render_template("stocktake_view.html", t=t, expected=expected, found_ids=found_ids)


@app.route("/stocktake/<int:tid>/scan", methods=["POST"])
@roles_required("admin", "staff")
def stocktake_scan(tid):
    db, t = get_db(), get_stocktake_or_404(tid)
    if t["finished_at"]:
        flash("This stocktake is finished.", "danger")
        return redirect(url_for("stocktake_view", tid=tid))
    code = request.form.get("code", "").strip()
    c = db.execute("SELECT c.*, s.name, l.room, l.unit_name FROM containers c JOIN substances s ON s.id = c.substance_id "
                   "LEFT JOIN locations l ON l.id = c.location_id WHERE c.code = ? COLLATE NOCASE", (code,)).fetchone()
    if c is None:
        flash(f"No container with code {code}.", "danger")
    elif c["status"] == "disposed":
        flash(f"{c['code']} ({c['name']}) is recorded as disposed. Check it and correct the record.", "danger")
    else:
        db.execute("INSERT OR IGNORE INTO stocktake_items (stocktake_id, container_id) VALUES (?, ?)", (tid, c["id"]))
        db.execute("UPDATE containers SET last_seen = ? WHERE id = ?", (date.today().isoformat(), c["id"]))
        notes = []
        if c["location_id"] != t["location_id"]:
            old = logic.location_label(c)
            new_amount = c["amount"]
            db.execute("UPDATE containers SET location_id = ? WHERE id = ?", (t["location_id"], c["id"]))
            db.execute("INSERT INTO movements (container_id, type, change, balance_after, user_id, user_name, purpose) "
                       "VALUES (?, 'TRANSFER', 0, ?, ?, ?, ?)",
                       (c["id"], new_amount, g.user["id"], g.user["name"],
                        f"Found in stocktake #{tid}: {old} → {logic.location_label(t)}"))
            notes.append(f"moved here from {old}")
        if c["status"] == "missing":
            db.execute("UPDATE containers SET status = ? WHERE id = ?",
                       ("active" if c["amount"] > 0 else "empty", c["id"]))
            notes.append("no longer missing")
        db.commit()
        flash(f"✓ {c['code']} {c['name']}" + (f" ({', '.join(notes)})" if notes else ""), "ok")
    return redirect(url_for("stocktake_view", tid=tid))


@app.route("/stocktake/<int:tid>/finish", methods=["POST"])
@roles_required("admin", "staff")
def stocktake_finish(tid):
    db, t = get_db(), get_stocktake_or_404(tid)
    if t["finished_at"]:
        return redirect(url_for("stocktake_view", tid=tid))
    missing = db.execute(
        "SELECT * FROM containers WHERE location_id = ? AND status IN ('active', 'empty') AND id NOT IN "
        "(SELECT container_id FROM stocktake_items WHERE stocktake_id = ?)", (t["location_id"], tid)).fetchall()
    for c in missing:
        db.execute("UPDATE containers SET status = 'missing' WHERE id = ?", (c["id"],))
        db.execute("INSERT INTO movements (container_id, type, change, balance_after, user_id, user_name, purpose) "
                   "VALUES (?, 'STOCKTAKE', 0, ?, ?, ?, ?)",
                   (c["id"], c["amount"], g.user["id"], g.user["name"], f"Not found in stocktake #{tid}"))
    db.execute("UPDATE stocktakes SET finished_at = ? WHERE id = ?", (now_str(), tid))
    audit("finish", "stocktake", tid, missing=[c["code"] for c in missing])
    db.commit()
    flash(f"Stocktake finished. {len(missing)} container(s) marked missing." if missing
          else "Stocktake finished. Everything was found.", "warn" if missing else "ok")
    return redirect(url_for("stocktake_view", tid=tid))

# The audit log page lives in views/trace.py with the other traceability pages.

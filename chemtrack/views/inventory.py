"""Chemicals (substances), containers, QR labels, stock in / out, SDS files."""

import re
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for

from .. import app, config, logic
from ..db import get_db
from ..qr import container_url, qr_svg
from ..security import audit, can_edit_movement, roles_required
from .dashboard import remember_scan

CODE_RE = re.compile(r"^CT-\d{5,}$", re.I)


# ------------------------------------------------------------------ helpers --

def get_substance_or_404(sid):
    s = get_db().execute("SELECT * FROM substances WHERE id = ?", (sid,)).fetchone()
    if s is None:
        abort(404)
    return s


def get_container_or_404(code):
    c = get_db().execute(
        "SELECT c.*, s.name, s.cas_no, s.formula, s.hazards, s.unit, s.is_cr, s.is_poison, s.is_precursor, "
        "s.sds_file, s.sds_date, l.room, l.unit_name, l.storage_type, u.name AS owner_name "
        "FROM containers c JOIN substances s ON s.id = c.substance_id "
        "LEFT JOIN locations l ON l.id = c.location_id LEFT JOIN users u ON u.id = c.owner_id "
        "WHERE c.code = ? COLLATE NOCASE", (code,)).fetchone()
    if c is None:
        abort(404)
    return c


def active_locations():
    return get_db().execute("SELECT * FROM locations WHERE active = 1 ORDER BY room, unit_name").fetchall()


def parse_amount(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def record(db, container, mtype, change, purpose="", reference="", status=None, location_id=None):
    """Apply one movement to a container and log it. Returns the new balance."""
    new_amount = round(container["amount"] + change, 6)
    new_status = status or ("empty" if new_amount <= 0 and container["status"] == "active"
                            else "active" if new_amount > 0 and container["status"] == "empty"
                            else container["status"])
    db.execute("UPDATE containers SET amount = ?, status = ?, location_id = COALESCE(?, location_id) WHERE id = ?",
               (max(new_amount, 0), new_status, location_id, container["id"]))
    db.execute(
        "INSERT INTO movements (container_id, type, change, balance_after, user_id, user_name, purpose, reference) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (container["id"], mtype, change, max(new_amount, 0), g.user["id"], g.user["name"], purpose, reference))
    return max(new_amount, 0)


def read_substance_form(form):
    errors = []
    data = {
        "name": form.get("name", "").strip(),
        "cas_no": form.get("cas_no", "").strip(),
        "formula": form.get("formula", "").strip(),
        "hazards": ",".join(h for h in form.getlist("hazards") if h in config.GHS_HAZARDS),
        "unit": form.get("unit", "g"),
        "is_cr": 1 if form.get("is_cr") else 0,
        "is_poison": 1 if form.get("is_poison") else 0,
        "is_precursor": 1 if form.get("is_precursor") else 0,
        "sds_date": form.get("sds_date", "").strip() or None,
        "notes": form.get("notes", "").strip(),
    }
    if not data["name"]:
        errors.append("Chemical name is required.")
    if data["unit"] not in config.UNITS:
        errors.append("Choose a valid unit.")
    if data["cas_no"] and not re.fullmatch(r"\d{2,7}-\d{2}-\d", data["cas_no"]):
        errors.append("CAS No. should look like 64-17-5.")
    if data["sds_date"] and not logic.parse_date(data["sds_date"]):
        errors.append("SDS date is not a valid date.")
    try:
        data["min_quantity"] = float(form.get("min_quantity") or 0)
        if data["min_quantity"] < 0:
            raise ValueError
    except ValueError:
        errors.append("Reorder level must be 0 or more.")
        data["min_quantity"] = 0
    return data, errors


SUBSTANCE_FIELDS = ["name", "cas_no", "formula", "hazards", "unit", "min_quantity",
                    "is_cr", "is_poison", "is_precursor", "sds_date", "notes"]


# ---------------------------------------------------------------- inventory --

# Filters for the inventory list: key -> (label, test on an alert message)
FILTERS = {
    "alerts": ("Needs attention", lambda m: True),
    "low": ("Low / out of stock", lambda m: "stock" in m),
    "expired": ("Expired / expiring", lambda m: "xpire" in m),
    "nosds": ("SDS to fix", lambda m: "SDS" in m),
    "missing": ("Missing containers", lambda m: "missing" in m),
    "empty": ("Empty, to dispose", lambda m: "empty" in m),
}


@app.route("/inventory")
def inventory():
    db = get_db()
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "all")
    if CODE_RE.match(logic.scanned_code(q)):
        q = logic.scanned_code(q)
        c = db.execute("SELECT code FROM containers WHERE code = ? COLLATE NOCASE", (q,)).fetchone()
        if c:
            return redirect(url_for("container_view", code=c["code"]))

    items = logic.substances_overview(db)
    everything = items
    if q:
        needle = q.lower()
        items = [i for i in items if needle in i["s"]["name"].lower() or needle in (i["s"]["cas_no"] or "")
                 or needle in (i["s"]["formula"] or "").lower()
                 or any(needle in loc.lower() for loc in i["locations"])]
    if show in FILTERS:
        test = FILTERS[show][1]
        items = [i for i in items if any(test(m) for _, m in i["alerts"])]

    containers = logic.active_containers(db)
    summary = {
        "substances": len(everything),
        "containers": sum(1 for c in containers if c["status"] == "active"),
        "low": sum(1 for i in everything if any("stock" in m for _, m in i["alerts"])),
        "expiry": sum(1 for i in everything if any("xpire" in m for _, m in i["alerts"])),
        "sds": sum(1 for i in everything if any("SDS" in m for _, m in i["alerts"])),
    }
    return render_template("inventory.html", items=items, summary=summary, q=q, show=show, filters=FILTERS,
                           conflicts=logic.storage_conflicts(containers),
                           over_limit=[f for f in logic.flammable_totals(db) if f["over"]])


ACTIONS = ("use", "move", "adjust", "dispose")


@app.route("/find")
def find():
    """Scan box: open the container, optionally with one action ready (?action=move)."""
    code = request.args.get("code", "").strip()
    if CODE_RE.match(logic.scanned_code(code)):   # otherwise it's a search, so leave it as typed
        code = logic.scanned_code(code)
    action = request.args.get("action", "")
    c = get_db().execute("SELECT code FROM containers WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    if c:
        return redirect(url_for("container_view", code=c["code"], open=action if action in ACTIONS else None))
    if code and not CODE_RE.match(code):  # not a code: treat it as a search
        return redirect(url_for("inventory", q=code))
    flash(f"No container with code {code}." if code else "Scan or type a container code.", "danger")
    return redirect(url_for("index"))


@app.route("/substance/new", methods=["GET", "POST"])
@roles_required("admin", "staff")
def substance_new():
    if request.method == "POST":
        data, errors = read_substance_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("substance_form.html", s=data, title="Add chemical")
        db = get_db()
        cur = db.execute(f"INSERT INTO substances ({', '.join(SUBSTANCE_FIELDS)}) "
                         f"VALUES ({', '.join('?' * len(SUBSTANCE_FIELDS))})", [data[f] for f in SUBSTANCE_FIELDS])
        audit("create", "substance", cur.lastrowid, name=data["name"], cas=data["cas_no"])
        db.commit()
        flash(f"Added {data['name']}. Now receive its first container.", "ok")
        return redirect(url_for("receive", substance=cur.lastrowid))
    return render_template("substance_form.html", s={"unit": "g"}, title="Add chemical")


@app.route("/substance/<int:sid>")
def substance_view(sid):
    db = get_db()
    s = get_substance_or_404(sid)
    item = next(i for i in logic.substances_overview(db) if i["s"]["id"] == sid)
    containers = db.execute(
        "SELECT c.*, l.room, l.unit_name FROM containers c LEFT JOIN locations l ON l.id = c.location_id "
        "WHERE c.substance_id = ? ORDER BY c.status = 'disposed', c.expiry_date IS NULL, c.expiry_date, c.code",
        (sid,)).fetchall()
    log = db.execute(
        "SELECT m.*, c.code FROM movements m JOIN containers c ON c.id = m.container_id "
        "WHERE c.substance_id = ? ORDER BY m.created_at DESC, m.id DESC LIMIT 50", (sid,)).fetchall()
    return render_template("substance_view.html", s=s, item=item, containers=containers, log=log)


@app.route("/substance/<int:sid>/edit", methods=["GET", "POST"])
@roles_required("admin", "staff")
def substance_edit(sid):
    s = get_substance_or_404(sid)
    if request.method == "POST":
        data, errors = read_substance_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("substance_form.html", s=dict(data, id=sid), title="Edit chemical", editing=True)
        db = get_db()
        changed = {f: [s[f], data[f]] for f in SUBSTANCE_FIELDS if str(s[f] or "") != str(data[f] or "")}
        db.execute(f"UPDATE substances SET {', '.join(f + ' = ?' for f in SUBSTANCE_FIELDS)} WHERE id = ?",
                   [data[f] for f in SUBSTANCE_FIELDS] + [sid])
        if changed:
            audit("update", "substance", sid, **changed)
        db.commit()
        flash("Changes saved.", "ok")
        return redirect(url_for("substance_view", sid=sid))
    return render_template("substance_form.html", s=dict(s), title="Edit chemical", editing=True)


@app.route("/substance/<int:sid>/delete", methods=["POST"])
@roles_required("admin")
def substance_delete(sid):
    db, s = get_db(), get_substance_or_404(sid)
    live = db.execute("SELECT COUNT(*) FROM containers WHERE substance_id = ? AND status != 'disposed'",
                      (sid,)).fetchone()[0]
    if live:
        flash(f"{s['name']} still has {live} container(s) in the lab. Dispose of them first.", "danger")
        return redirect(url_for("substance_view", sid=sid))
    audit("delete", "substance", sid, name=s["name"])
    db.execute("DELETE FROM substances WHERE id = ?", (sid,))
    db.commit()
    flash(f"Deleted {s['name']}.", "ok")
    return redirect(url_for("inventory"))


# --------------------------------------------------------------- SDS files --

@app.route("/substance/<int:sid>/sds", methods=["POST"])
@roles_required("admin", "staff")
def sds_upload(sid):
    s = get_substance_or_404(sid)
    f = request.files.get("sds")
    if not f or not f.filename:
        flash("Choose a PDF file.", "danger")
        return redirect(url_for("substance_view", sid=sid))
    head = f.stream.read(5)
    f.stream.seek(0)
    if head != b"%PDF-":
        flash("The SDS must be a PDF file.", "danger")
        return redirect(url_for("substance_view", sid=sid))
    folder = config.UPLOAD_DIR / "sds"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"sds-{sid}.pdf"  # our own name: never trust the uploaded file name
    f.save(folder / name)
    db = get_db()
    sds_date = request.form.get("sds_date") or s["sds_date"]
    if sds_date and not logic.parse_date(sds_date):
        sds_date = s["sds_date"]
    db.execute("UPDATE substances SET sds_file = ?, sds_date = ? WHERE id = ?", (name, sds_date, sid))
    audit("upload SDS", "substance", sid, sds_date=sds_date)
    db.commit()
    flash("SDS uploaded.", "ok")
    return redirect(url_for("substance_view", sid=sid))


@app.route("/substance/<int:sid>/sds.pdf")
def sds_file(sid):
    s = get_substance_or_404(sid)
    path = config.UPLOAD_DIR / "sds" / (s["sds_file"] or "")
    if not s["sds_file"] or not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", download_name=f"SDS {s['name']}.pdf")


# --------------------------------------------------------------- containers --

@app.route("/receive", methods=["GET", "POST"])
@roles_required("admin", "staff")
def receive():
    db = get_db()
    form = request.form
    if request.method == "POST":
        sid = form.get("substance_id", "")
        s = db.execute("SELECT * FROM substances WHERE id = ?", (int(sid) if sid.isdigit() else 0,)).fetchone()
        size = parse_amount(form.get("size"))
        count = int(form.get("count") or 1) if (form.get("count") or "1").isdigit() else 0
        loc = form.get("location_id", "")
        errors = []
        if s is None:
            errors.append("Choose a chemical.")
        if size is None:
            errors.append("Enter the container size (more than 0).")
        if not 1 <= count <= 50:
            errors.append("Number of containers must be 1 to 50.")
        if not loc.isdigit():
            errors.append("Choose where it will be stored.")
        for field in ("received_date", "expiry_date"):
            if form.get(field) and not logic.parse_date(form.get(field)):
                errors.append(f"{field.replace('_', ' ').capitalize()} is not a valid date.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            codes = logic.next_container_codes(db, count)
            for code in codes:
                cur = db.execute(
                    "INSERT INTO containers (code, substance_id, location_id, size, amount, lot_no, supplier, "
                    "received_date, expiry_date, owner_id, last_seen) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                    (code, s["id"], int(loc), size, form.get("lot_no", "").strip(),
                     form.get("supplier", "").strip(), form.get("received_date") or date.today().isoformat(),
                     form.get("expiry_date") or None, g.user["id"], date.today().isoformat()))
                container = db.execute("SELECT * FROM containers WHERE id = ?", (cur.lastrowid,)).fetchone()
                record(db, container, "RECEIVE", size, "Received", form.get("reference", "").strip(), status="active")
            audit("receive", "substance", s["id"], name=s["name"], containers=codes, size=size)
            db.commit()
            flash(f"Received {count} × {logic.fmt_qty(size)} {s['unit']} {s['name']}. Print the labels now.", "ok")
            return redirect(url_for("labels", codes=",".join(codes)))

    subs = db.execute("SELECT id, name, cas_no, unit FROM substances ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("receive.html", subs=subs, locations=active_locations(), form=form,
                           selected=request.values.get("substance", ""))


@app.route("/c/<code>")
def container_view(code):
    from .trace import container_history  # trace.py imports this module, so import here
    c = get_container_or_404(code)
    db = get_db()
    history = container_history(db, c)
    waste = db.execute("SELECT * FROM waste_records WHERE container_id = ?", (c["id"],)).fetchone()
    remember_scan(c["code"])
    opened = request.args.get("open", "")
    if opened not in ACTIONS:
        opened = "use" if c["status"] == "active" else ""
    return render_template("container_view.html", c=c, history=history, waste=waste, locations=active_locations(),
                           qr=qr_svg(container_url(c["code"])), alerts=logic.container_alerts(c), opened=opened)


@app.route("/c/<code>/edit", methods=["GET", "POST"])
@roles_required("admin", "staff")
def container_edit(code):
    c = get_container_or_404(code)
    if request.method == "POST":
        f = request.form
        bad = [k for k in ("received_date", "opened_date", "expiry_date") if f.get(k) and not logic.parse_date(f.get(k))]
        if bad:
            flash("Check the dates.", "danger")
            return render_template("container_form.html", c=c)
        db = get_db()
        fields = {k: (f.get(k, "").strip() or None) for k in
                  ("lot_no", "supplier", "received_date", "opened_date", "expiry_date", "notes")}
        changed = {k: [c[k], v] for k, v in fields.items() if (c[k] or None) != v}
        db.execute("UPDATE containers SET lot_no=?, supplier=?, received_date=?, opened_date=?, expiry_date=?, "
                   "notes=? WHERE id=?", (*fields.values(), c["id"]))
        if changed:
            audit("update", "container", c["id"], code=c["code"], **changed)
        db.commit()
        flash("Saved.", "ok")
        return redirect(url_for("container_view", code=c["code"]))
    return render_template("container_form.html", c=c)


@app.route("/c/<code>/<action>", methods=["POST"])
@roles_required("admin", "staff")
def container_action(code, action):
    c = get_container_or_404(code)
    db, f = get_db(), request.form
    purpose, reference = f.get("purpose", "").strip(), f.get("reference", "").strip()
    back = redirect(f.get("next") if (f.get("next") or "").startswith("/") else url_for("container_view", code=c["code"]))

    if c["status"] == "disposed":
        flash("This container was disposed of. No more changes.", "danger")
        return back

    if action == "use":
        amount = parse_amount(f.get("amount"))
        if amount is None:
            flash("Enter an amount greater than 0.", "danger")
        elif c["status"] != "active":
            flash(f"{c['code']} is {c['status']}. Only active containers can be used.", "danger")
        elif amount > c["amount"] + 1e-9:
            flash(f"Not enough in {c['code']}. Only {logic.fmt_qty(c['amount'])} {c['unit']} left.", "danger")
        else:
            if not c["opened_date"]:
                db.execute("UPDATE containers SET opened_date = ? WHERE id = ?", (date.today().isoformat(), c["id"]))
            bal = record(db, c, "USE", -amount, purpose, reference)
            db.commit()
            msg = f"Used {logic.fmt_qty(amount)} {c['unit']} from {c['code']} ({c['name']}). Left: {logic.fmt_qty(bal)} {c['unit']}."
            flash(msg + (" Container is now empty: dispose of it when ready." if bal <= 0 else ""), "ok")
        return back

    if action == "move":
        loc = f.get("location_id", "")
        new = db.execute("SELECT * FROM locations WHERE id = ?", (int(loc) if loc.isdigit() else 0,)).fetchone()
        if new is None or new["id"] == c["location_id"]:
            flash("Choose a different location.", "danger")
        else:
            old = logic.location_label(c)
            record(db, c, "TRANSFER", 0, f"{old} → {logic.location_label(new)}" + (f" ({purpose})" if purpose else ""),
                   reference, location_id=new["id"])
            db.commit()
            flash(f"{c['code']} moved to {logic.location_label(new)}.", "ok")
        return back

    if action == "adjust":
        try:
            new_amount = float(f.get("new_amount", ""))
        except ValueError:
            new_amount = -1
        if new_amount < 0:
            flash("Enter the measured amount (0 or more).", "danger")
        elif not purpose:
            flash("Give a reason for the correction.", "danger")
        else:
            status = "active" if new_amount > 0 else "empty"  # also brings back a 'missing' container
            record(db, c, "ADJUST", round(new_amount - c["amount"], 6), purpose, reference, status=status)
            db.commit()
            flash(f"{c['code']} corrected to {logic.fmt_qty(new_amount)} {c['unit']}.", "ok")
        return back

    if action == "dispose":
        if f.get("waste") and not f.get("sw_code", "").strip():
            flash("Enter the SW code for the scheduled waste record.", "danger")
            return back
        left = c["amount"]
        record(db, c, "DISPOSE", -left, purpose or "Disposed", reference, status="disposed")
        if f.get("waste"):
            qty = parse_amount(f.get("waste_qty")) or (left if left > 0 else None)
            db.execute(
                "INSERT INTO waste_records (container_id, description, sw_code, quantity, unit, disposed_date, "
                "contractor, consignment_no, user_id, user_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["id"], f"{c['name']} ({c['code']})", f.get("sw_code").strip().upper(), qty, c["unit"],
                 date.today().isoformat(), f.get("contractor", "").strip(), reference, g.user["id"], g.user["name"]))
        audit("dispose", "container", c["id"], code=c["code"], amount_left=left, waste=bool(f.get("waste")))
        db.commit()
        flash(f"{c['code']} disposed." + (" Scheduled waste recorded." if f.get("waste") else ""), "ok")
        return back

    abort(404)


@app.route("/labels")
def labels():
    codes = [x.strip() for x in request.args.get("codes", "").split(",") if x.strip()][:100]
    db = get_db()
    rows = []
    for code in codes:
        c = db.execute(
            "SELECT c.*, s.name, s.cas_no, s.hazards, s.unit, s.formula, l.room, l.unit_name "
            "FROM containers c JOIN substances s ON s.id = c.substance_id "
            "LEFT JOIN locations l ON l.id = c.location_id WHERE c.code = ? COLLATE NOCASE", (code,)).fetchone()
        if c:
            rows.append({"c": c, "qr": qr_svg(container_url(c["code"]))})
    if not rows:
        abort(404)
    return render_template("labels.html", rows=rows)


# ------------------------------------------------------ quick stock in / out --

@app.route("/stock", methods=["GET", "POST"])
@roles_required("admin", "staff")
def stock_page():
    db = get_db()
    if request.method == "POST":
        code = logic.scanned_code(request.form.get("code")) or request.form.get("container", "")
        c = db.execute("SELECT code FROM containers WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
        if c is None:
            flash("Scan or choose a container.", "danger")
            return redirect(url_for("stock_page"))
        # Reuse the container 'use' action (it shows the result message), then come back here
        container_action(c["code"], "use")
        return redirect(url_for("stock_page"))
    containers = logic.active_containers(db)
    recent = db.execute(
        "SELECT m.*, c.code, s.name, s.unit FROM movements m JOIN containers c ON c.id = m.container_id "
        "JOIN substances s ON s.id = c.substance_id ORDER BY m.id DESC LIMIT 10").fetchall()
    return render_template("stock.html", containers=[c for c in containers if c["status"] == "active"],
                           recent=recent, selected=request.args.get("container", ""))


# --------------------------------------------------------- edit a movement --

def get_movement_or_404(mid):
    m = get_db().execute(
        "SELECT m.*, c.code, c.amount AS current, c.status, s.name, s.unit FROM movements m "
        "JOIN containers c ON c.id = m.container_id JOIN substances s ON s.id = c.substance_id "
        "WHERE m.id = ?", (mid,)).fetchone()
    if m is None:
        abort(404)
    return m


def shift_container(db, m, diff, include_self=True):
    """Shift the container's amount and the balance of this and later movements by diff."""
    op = ">=" if include_self else ">"
    later = db.execute(
        f"SELECT id, created_at, balance_after FROM movements WHERE container_id = ? "
        f"AND (created_at > ? OR (created_at = ? AND id {op} ?))",
        (m["container_id"], m["created_at"], m["created_at"], m["id"])).fetchall()
    if m["current"] + diff < -1e-9:
        return "This would make the container's amount negative."
    for row in later:
        if row["balance_after"] is not None and row["balance_after"] + diff < -1e-9:
            return f"This would make the amount negative on {row['created_at'][:16]}."
    new_amount = round(m["current"] + diff, 6)
    status = "empty" if new_amount <= 0 else "active"
    db.execute("UPDATE containers SET amount = ?, status = ? WHERE id = ?", (new_amount, status, m["container_id"]))
    for row in later:
        if row["balance_after"] is not None:
            db.execute("UPDATE movements SET balance_after = round(balance_after + ?, 6) WHERE id = ?",
                       (diff, row["id"]))
    return None


def back_url():
    target = request.form.get("next") or request.args.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else url_for("movements")


@app.route("/movement/<int:mid>/edit", methods=["GET", "POST"])
def movement_edit(mid):
    m = get_movement_or_404(mid)
    if not can_edit_movement(m) or m["status"] == "disposed":
        abort(403)
    if request.method == "POST":
        amount = parse_amount(request.form.get("amount"))
        error = None if amount else "Enter an amount greater than 0."
        if not error:
            db = get_db()
            diff = -amount - m["change"]  # new change minus old change (uses are negative)
            error = shift_container(db, m, diff) if diff else None
            if not error:
                db.execute("UPDATE movements SET change = ?, purpose = ?, reference = ?, "
                           "edited_at = datetime('now', 'localtime'), edited_by = ? WHERE id = ?",
                           (-amount, request.form.get("purpose", "").strip(), request.form.get("reference", "").strip(),
                            g.user["name"], mid))
                audit("edit movement", "movement", mid, code=m["code"], old=-m["change"], new=amount)
                db.commit()
                flash(f"Movement {logic.tx_code(mid)} updated.", "ok")
                return redirect(back_url())
            db.rollback()
        flash(error, "danger")
    return render_template("movement_form.html", m=m, next=back_url())


@app.route("/movement/<int:mid>/delete", methods=["POST"])
def movement_delete(mid):
    m = get_movement_or_404(mid)
    if not can_edit_movement(m) or m["status"] == "disposed":
        abort(403)
    db = get_db()
    error = shift_container(db, m, -m["change"], include_self=False)
    if error:
        db.rollback()
        flash(f"Can't delete: {error}", "danger")
        return redirect(url_for("movement_edit", mid=mid, next=back_url()))
    db.execute("DELETE FROM movements WHERE id = ?", (mid,))
    audit("delete movement", "movement", mid, code=m["code"], amount=-m["change"])
    db.commit()
    flash(f"Movement {logic.tx_code(mid)} deleted and amount recalculated.", "ok")
    return redirect(back_url())

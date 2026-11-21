"""Storage locations, flammable limits and the printable emergency list."""

from flask import abort, flash, redirect, render_template, request, url_for

from .. import app, config, logic
from ..db import get_db
from ..security import audit, is_admin, roles_required


def read_location_form(form):
    data = {
        "room": form.get("room", "").strip(),
        "unit_name": form.get("unit_name", "").strip(),
        "storage_type": form.get("storage_type", "General shelf"),
        "notes": form.get("notes", "").strip(),
    }
    errors = []
    if not data["room"]:
        errors.append("Enter the room or lab name.")
    if data["storage_type"] not in config.STORAGE_TYPES:
        errors.append("Choose a storage type.")
    limit = form.get("flammable_limit_l", "").strip()
    try:
        data["flammable_limit_l"] = float(limit) if limit else None
        if data["flammable_limit_l"] is not None and data["flammable_limit_l"] < 0:
            raise ValueError
    except ValueError:
        errors.append("Flammable limit must be a number of litres.")
        data["flammable_limit_l"] = None
    return data, errors


@app.route("/locations", methods=["GET", "POST"])
def locations():
    db = get_db()
    if request.method == "POST":
        if not is_admin():
            abort(403)
        data, errors = read_location_form(request.form)
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            cur = db.execute("INSERT INTO locations (room, unit_name, storage_type, flammable_limit_l, notes) "
                             "VALUES (?, ?, ?, ?, ?)", (data["room"], data["unit_name"], data["storage_type"],
                                                         data["flammable_limit_l"], data["notes"]))
            audit("create", "location", cur.lastrowid, **data)
            db.commit()
            flash(f"Added {logic.location_label(data)}.", "ok")
        return redirect(url_for("locations"))

    counts = {r[0]: r[1] for r in db.execute(
        "SELECT location_id, COUNT(*) FROM containers WHERE status IN ('active', 'empty') GROUP BY location_id")}
    flam = {f["loc"]["id"]: f for f in logic.flammable_totals(db)}
    rows = db.execute("SELECT * FROM locations ORDER BY active DESC, room, unit_name").fetchall()
    conflicts = {c["location_id"] for c in logic.storage_conflicts(logic.active_containers(db))}
    return render_template("locations.html", rows=rows, counts=counts, flam=flam, conflicts=conflicts)


def get_location_or_404(lid):
    loc = get_db().execute("SELECT * FROM locations WHERE id = ?", (lid,)).fetchone()
    if loc is None:
        abort(404)
    return loc


@app.route("/location/<int:lid>")
def location_view(lid):
    db, loc = get_db(), get_location_or_404(lid)
    containers = logic.active_containers(db, lid)
    flam = next((f for f in logic.flammable_totals(db) if f["loc"]["id"] == lid), None)
    return render_template("location_view.html", loc=loc, containers=containers, flam=flam,
                           conflicts=logic.storage_conflicts(containers))


@app.route("/location/<int:lid>/edit", methods=["GET", "POST"])
@roles_required("admin")
def location_edit(lid):
    db, loc = get_db(), get_location_or_404(lid)
    if request.method == "POST":
        data, errors = read_location_form(request.form)
        active = 1 if request.form.get("active") else 0
        if not active and db.execute("SELECT COUNT(*) FROM containers WHERE location_id = ? AND status IN "
                                     "('active', 'empty')", (lid,)).fetchone()[0]:
            errors.append("Move the containers out before closing this location.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("location_form.html", loc=dict(data, id=lid, active=active))
        db.execute("UPDATE locations SET room=?, unit_name=?, storage_type=?, flammable_limit_l=?, notes=?, active=? "
                   "WHERE id=?", (data["room"], data["unit_name"], data["storage_type"], data["flammable_limit_l"],
                                  data["notes"], active, lid))
        audit("update", "location", lid, **data, active=active)
        db.commit()
        flash("Location saved.", "ok")
        return redirect(url_for("location_view", lid=lid))
    return render_template("location_form.html", loc=dict(loc))


@app.route("/emergency")
def emergency():
    """Printable list of what is stored where, for emergency responders (Bomba) and audits."""
    db = get_db()
    lid = request.args.get("location", "")
    locs = db.execute("SELECT * FROM locations WHERE active = 1 " + ("AND id = ? " if lid.isdigit() else "") +
                      "ORDER BY room, unit_name", ((int(lid),) if lid.isdigit() else ())).fetchall()
    flam = {f["loc"]["id"]: f for f in logic.flammable_totals(db)}
    sections = [{"loc": loc, "containers": logic.active_containers(db, loc["id"]), "flam": flam.get(loc["id"])}
                for loc in locs]
    return render_template("emergency.html", sections=[s for s in sections if s["containers"]])

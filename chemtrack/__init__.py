"""
ChemTrack: chemical inventory for a teaching / research lab.

Structure
    config.py      settings
    db.py          database connection + versioned migrations
    backup.py      safe backups
    security.py    sign-in, roles, CSRF, audit log
    logic.py       alerts, storage checks, totals (no web code)
    qr.py          QR codes for container labels
    views/         the pages, one module per area
"""

import secrets
from datetime import datetime, timedelta

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from . import config, logic
from .db import close_db, get_db, migrate
from .security import (can_edit, can_edit_movement, csrf_field, csrf_ok, is_admin, password_marker)


def _secret_key():
    if config.SECRET_KEY_PATH.exists():
        return config.SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    config.SECRET_KEY_PATH.write_text(key)
    return key


app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = _secret_key()
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=config.SESSION_HOURS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.HTTPS_ONLY,
    MAX_CONTENT_LENGTH=(config.MAX_SDS_MB + 2) * 1024 * 1024,
)
if config.BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.teardown_appcontext(close_db)

PUBLIC_ENDPOINTS = {"login", "setup", "static", "favicon"}


@app.route("/favicon.ico")
def favicon():
    return "", 204  # no icon file; stops browsers being sent to the sign-in page for it


@app.before_request
def load_user_and_protect():
    g.user = None
    db = get_db()
    uid = session.get("user_id")
    if uid:
        user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if user and user["active"] and session.get("pw") == password_marker(user):
            g.user = user
        else:
            session.clear()

    if request.method == "POST" and not csrf_ok(request.form.get("csrf_token", "")):
        abort(400, "This form expired. Go back, refresh the page, and try again.")

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return redirect(url_for("setup"))
    if g.user is None:
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
    if g.user["must_change_password"] and request.endpoint not in ("change_password", "logout"):
        flash("Please choose your own password before continuing.", "warn")
        return redirect(url_for("change_password"))
    return None


def initials(name):
    parts = [p for p in (name or "").split() if p[0].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def attention_count():
    db = get_db()
    subs = sum(1 for item in logic.substances_overview(db) if item["alerts"])
    over = sum(1 for f in logic.flammable_totals(db) if f["over"])
    return subs + over


@app.context_processor
def inject_globals():
    data = {"config": config, "ROLES": config.ROLES, "csrf_field": csrf_field,
            "current_user": g.get("user"), "is_admin": is_admin, "can_edit": can_edit,
            "can_edit_movement": can_edit_movement, "initials": initials,
            "hazard_list": logic.hazard_list, "location_label": logic.location_label,
            "container_alerts": logic.container_alerts, "today": datetime.now().date().isoformat()}
    if g.get("user"):
        data["attention"] = attention_count()
    return data


app.jinja_env.filters.update(qty=logic.fmt_qty, chem_code=logic.chem_code, tx_code=logic.tx_code)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", title="Not allowed",
                           message="Your role doesn't allow this. Ask the lab admin if you need access."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", title="Not found", message="That page or record doesn't exist."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Something went wrong",
                           message=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", title="File too large",
                           message=f"SDS files can be up to {config.MAX_SDS_MB} MB."), 413


migrate()

# The pages (imported last: they use `app` from this module)
from .views import auth, inventory, locations, compliance, reports  # noqa: E402,F401

"""Sign in, first-run setup, passwords, staff accounts and the sign-in log."""

import secrets
from datetime import datetime, timedelta

from flask import abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .. import app, config
from ..db import get_db
from ..security import audit, roles_required, start_session

_DUMMY_HASH = generate_password_hash("not-a-real-password")


def validate_new_password(password, confirm, staff_id):
    if len(password) < config.MIN_PASSWORD_LENGTH:
        return f"Password must be at least {config.MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords don't match."
    if password.lower() == (staff_id or "").lower():
        return "Password can't be the same as the Staff ID."
    return None


def temporary_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    pick = lambda n: "".join(secrets.choice(alphabet) for _ in range(n))  # noqa: E731
    return f"Lab-{pick(4)}-{pick(4)}"


def log_login(staff_id, user_id, success, note=""):
    get_db().execute("INSERT INTO login_log (staff_id, user_id, success, note) VALUES (?, ?, ?, ?)",
                     (staff_id, user_id, 1 if success else 0, note))


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error, staff_id = None, ""
    if request.method == "POST":
        db = get_db()
        staff_id = request.form.get("staff_id", "").strip()
        password = request.form.get("password", "")
        user = db.execute("SELECT * FROM users WHERE staff_id = ?", (staff_id,)).fetchone()
        now = datetime.now()
        locked_until = (datetime.strptime(user["locked_until"], "%Y-%m-%d %H:%M:%S")
                        if user and user["locked_until"] else None)

        if locked_until and locked_until > now:
            minutes = max(1, round((locked_until - now).total_seconds() / 60))
            error = f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
            log_login(staff_id, user["id"], False, "Locked")
        elif user and check_password_hash(user["password_hash"], password):
            if not user["active"]:
                error = "This account is disabled. Ask the lab admin."
                log_login(staff_id, user["id"], False, "Disabled account")
            else:
                db.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, "
                           "last_login = datetime('now', 'localtime') WHERE id = ?", (user["id"],))
                log_login(user["staff_id"], user["id"], True)
                db.commit()
                start_session(user)
                return redirect(safe_next(request.form.get("next")))
        else:
            if user is None:
                check_password_hash(_DUMMY_HASH, password)  # same time as a wrong password
                log_login(staff_id, None, False, "Unknown Staff ID")
            else:
                fails = user["failed_attempts"] + 1
                if fails >= config.MAX_FAILED_LOGINS:
                    until = (now + timedelta(minutes=config.LOCKOUT_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                    db.execute("UPDATE users SET failed_attempts = 0, locked_until = ? WHERE id = ?",
                               (until, user["id"]))
                    log_login(staff_id, user["id"], False, f"Wrong password, locked {config.LOCKOUT_MINUTES} min")
                else:
                    db.execute("UPDATE users SET failed_attempts = ? WHERE id = ?", (fails, user["id"]))
                    log_login(staff_id, user["id"], False, "Wrong password")
            error = "Staff ID or password is wrong."
        db.commit()
    from .dashboard import public_status  # counts only, never chemical names
    return render_template("login.html", error=error, staff_id=staff_id, next=request.values.get("next", ""),
                           status=public_status(get_db()))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    error, form = None, request.form
    if request.method == "POST":
        staff_id, name = form.get("staff_id", "").strip(), form.get("name", "").strip()
        password = form.get("password", "")
        error = ("Enter your Staff ID and name." if not staff_id or not name
                 else validate_new_password(password, form.get("confirm", ""), staff_id))
        if not error:
            cur = db.execute(
                "INSERT INTO users (staff_id, name, role, password_hash, must_change_password, last_login) "
                "VALUES (?, ?, 'admin', ?, 0, datetime('now', 'localtime'))",
                (staff_id, name, generate_password_hash(password)))
            log_login(staff_id, cur.lastrowid, True, "Admin account created")
            db.commit()
            start_session(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())
            flash(f"Welcome, {name}. Your admin account is ready. Next: add your storage locations.", "ok")
            return redirect(url_for("locations"))
    return render_template("setup.html", error=error, form=form)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You're signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    error = None
    if request.method == "POST":
        current, new = request.form.get("current", ""), request.form.get("password", "")
        if not check_password_hash(g.user["password_hash"], current):
            error = "Your current password is wrong."
        elif new == current:
            error = "Choose a password different from the current one."
        else:
            error = validate_new_password(new, request.form.get("confirm", ""), g.user["staff_id"])
        if not error:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            start_session(db.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone())
            flash("Password changed.", "ok")
            return redirect(url_for("index"))
    return render_template("change_password.html", error=error)


# ------------------------------------------------------------ staff accounts --

def active_admin_count(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]


def render_staff_page(new_login=None):
    users = get_db().execute("SELECT * FROM users ORDER BY active DESC, role, name COLLATE NOCASE").fetchall()
    return render_template("staff.html", users=users, new_login=new_login, now=datetime.now())


def get_user_or_404(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


@app.route("/staff")
@roles_required("admin")
def staff_list():
    return render_staff_page()


@app.route("/staff/add", methods=["POST"])
@roles_required("admin")
def staff_add():
    db = get_db()
    staff_id, name = request.form.get("staff_id", "").strip(), request.form.get("name", "").strip()
    role = request.form.get("role", "staff")
    if not staff_id or not name or role not in config.ROLES:
        flash("Enter a Staff ID, name and role.", "danger")
    elif db.execute("SELECT 1 FROM users WHERE staff_id = ?", (staff_id,)).fetchone():
        flash(f"Staff ID {staff_id} already has an account.", "danger")
    else:
        password = temporary_password()
        cur = db.execute("INSERT INTO users (staff_id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                         (staff_id, name, role, generate_password_hash(password)))
        audit("create", "user", cur.lastrowid, staff_id=staff_id, name=name, role=role)
        db.commit()
        return render_staff_page(new_login={"name": name, "staff_id": staff_id,
                                            "password": password, "action": "Account created"})
    return redirect(url_for("staff_list"))


@app.route("/staff/<int:user_id>/update", methods=["POST"])
@roles_required("admin")
def staff_update(user_id):
    db, user = get_db(), get_user_or_404(user_id)
    name = request.form.get("name", "").strip() or user["name"]
    role = request.form.get("role", user["role"])
    if role not in config.ROLES:
        abort(400)
    if user["role"] == "admin" and role != "admin" and user["active"] and active_admin_count(db) <= 1:
        flash("ChemTrack needs at least one admin. Make someone else admin first.", "danger")
    else:
        db.execute("UPDATE users SET name = ?, role = ? WHERE id = ?", (name, role, user_id))
        audit("update", "user", user_id, name=name, role=role, old_role=user["role"])
        db.commit()
        flash(f"Saved {name} as {config.ROLES[role]}.", "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/<int:user_id>/reset", methods=["POST"])
@roles_required("admin")
def staff_reset(user_id):
    db, user = get_db(), get_user_or_404(user_id)
    password = temporary_password()
    db.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_attempts = 0, "
               "locked_until = NULL WHERE id = ?", (generate_password_hash(password), user_id))
    audit("reset password", "user", user_id, staff_id=user["staff_id"])
    db.commit()
    if user_id == g.user["id"]:
        session.clear()
        flash(f"Your password was reset. Sign in with {password} and choose a new one.", "warn")
        return redirect(url_for("login"))
    return render_staff_page(new_login={"name": user["name"], "staff_id": user["staff_id"],
                                        "password": password, "action": "Password reset"})


@app.route("/staff/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def staff_toggle(user_id):
    db, user = get_db(), get_user_or_404(user_id)
    if user_id == g.user["id"]:
        flash("You can't disable your own account.", "danger")
    elif user["active"] and user["role"] == "admin" and active_admin_count(db) <= 1:
        flash("ChemTrack needs at least one active admin.", "danger")
    else:
        db.execute("UPDATE users SET active = ?, failed_attempts = 0, locked_until = NULL WHERE id = ?",
                   (0 if user["active"] else 1, user_id))
        audit("disable" if user["active"] else "enable", "user", user_id, staff_id=user["staff_id"])
        db.commit()
        flash(f"{user['name']} {'disabled' if user['active'] else 'enabled'}.", "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/log")
@roles_required("admin")
def login_history():
    rows = get_db().execute(
        "SELECT l.*, u.name FROM login_log l LEFT JOIN users u ON u.id = l.user_id "
        "ORDER BY l.id DESC LIMIT 200").fetchall()
    return render_template("login_log.html", rows=rows)

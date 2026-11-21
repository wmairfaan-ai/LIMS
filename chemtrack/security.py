"""Sign-in checks, roles, form protection (CSRF) and the audit log."""

import hmac
import json
import secrets
from datetime import date
from functools import wraps

from flask import abort, g, session
from markupsafe import Markup

from .db import get_db


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_field():
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def csrf_ok(sent):
    expected = session.get("csrf_token", "")
    return bool(sent and expected and hmac.compare_digest(sent, expected))


def password_marker(user):
    """Part of the hash kept in the cookie: changing a password signs out old sessions."""
    return user["password_hash"][-16:]


def start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pw"] = password_marker(user)


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None or g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def is_admin():
    return g.get("user") is not None and g.user["role"] == "admin"


def can_edit():
    return g.get("user") is not None and g.user["role"] in ("admin", "staff")


def can_edit_movement(m):
    """Only 'use' movements can be corrected. Admin: any. Staff: own, same day."""
    if m["type"] != "USE" or not can_edit():
        return False
    if is_admin():
        return True
    return m["user_id"] == g.user["id"] and (m["created_at"] or "")[:10] == date.today().isoformat()


def audit(action, entity, entity_id=None, **details):
    """Record who changed what. Call before db.commit() so it saves together with the change."""
    user = g.get("user")
    get_db().execute(
        "INSERT INTO audit_log (user_id, user_name, action, entity, entity_id, details) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"] if user else None, user["name"] if user else "system", action, entity, entity_id,
         json.dumps(details, ensure_ascii=False, default=str) if details else None))

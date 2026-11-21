"""Dashboard: scan-first quick actions (A) + safety control room (B)."""

from datetime import date, datetime, timedelta

from flask import g, render_template, session

from .. import app, config, logic
from ..db import get_db

QUICK_ACTIONS = [  # key, label, icon, what happens after scanning
    ("use", "Use", "➖", "Record how much you used"),
    ("move", "Move", "↔️", "Put it in another location"),
    ("adjust", "Correct", "⚖️", "Fix the amount after weighing"),
    ("dispose", "Dispose", "🗑️", "Remove it and log the waste"),
]


def action_counts(db):
    """What needs attention, with a link to the filtered list for each."""
    today = date.today()
    soon = today + timedelta(days=config.EXPIRY_WARNING_DAYS)
    items = logic.substances_overview(db)
    containers = logic.active_containers(db)
    exp = [logic.parse_date(c["expiry_date"]) for c in containers if c["status"] == "active"]
    return [
        ("danger", "Expired", sum(1 for d in exp if d and d < today), "expired"),
        ("warn", "Expiring in 30 days", sum(1 for d in exp if d and today <= d <= soon), "expired"),
        ("warn", "Low / out of stock", sum(1 for i in items if any("stock" in m for _, m in i["alerts"])), "low"),
        ("warn", "SDS to fix", sum(1 for i in items if i["n_active"] and any("SDS" in m for _, m in i["alerts"])), "nosds"),
        ("danger", "Missing", db.execute("SELECT COUNT(*) FROM containers WHERE status='missing'").fetchone()[0], "missing"),
        ("warn", "Empty, to dispose", sum(1 for c in containers if c["status"] == "empty"), "empty"),
    ]


def remember_scan(code):
    """Keep the last 8 containers this person opened (stored in their own session)."""
    recent = [c for c in session.get("recent", []) if c != code]
    session["recent"] = ([code] + recent)[:8]


def public_status(db):
    """Counts only, for the sign-in page. Never chemical names or amounts."""
    if not config.LOGIN_STATUS:
        return None
    score, _ = logic.compliance_checklist(db)
    flam = [f for f in logic.flammable_totals(db) if f["limit"]]
    fullest = max((round(f["litres"] / f["limit"] * 100) for f in flam), default=None)
    next_chra = db.execute("SELECT MIN(next_review) FROM assessments").fetchone()[0]
    chra_days = (logic.parse_date(next_chra) - date.today()).days if next_chra else None
    attention = sum(1 for i in logic.substances_overview(db) if i["alerts"])
    return {"score": score, "fullest": fullest, "chra_days": chra_days, "attention": attention}


@app.route("/")
def index():
    db = get_db()
    score, checklist = logic.compliance_checklist(db)
    recent_codes = session.get("recent", [])
    recent = []
    if recent_codes:
        rows = {r["code"]: r for r in db.execute(
            "SELECT c.code, c.amount, c.status, s.name, s.unit FROM containers c "
            "JOIN substances s ON s.id = c.substance_id WHERE c.code IN (%s)" % ",".join("?" * len(recent_codes)),
            recent_codes)}
        recent = [rows[c] for c in recent_codes if c in rows]
    mine = db.execute(
        "SELECT c.code, c.amount, c.size, s.name, s.unit FROM containers c JOIN substances s ON s.id = c.substance_id "
        "WHERE c.owner_id = ? AND c.status = 'active' ORDER BY c.id DESC LIMIT 6", (g.user["id"],)).fetchall()
    today_moves = db.execute("SELECT COUNT(*) FROM movements WHERE date(created_at) = date('now', 'localtime')"
                             ).fetchone()[0]
    flam = [f for f in logic.flammable_totals(db) if f["litres"] or f["limit"] is not None]
    return render_template("dashboard.html", score=score, checklist=checklist, actions=action_counts(db),
                           flam=flam, recent=recent, mine=mine, today_moves=today_moves,
                           quick=QUICK_ACTIONS, conflicts=logic.storage_conflicts(logic.active_containers(db)),
                           now_hour=datetime.now().hour)

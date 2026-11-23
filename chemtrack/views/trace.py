"""Traceability: full container history, lot / recall trace, and the searchable audit log."""

import csv
import io
import json

from flask import Response, render_template, request, url_for

from .. import app, logic
from ..db import get_db
from ..security import roles_required
from .inventory import get_container_or_404
from .reports import TYPE_LABELS

AUDIT_PAGE_SIZE = 50


def csv_response(filename, headers, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(headers)
    w.writerows(rows)
    return Response("﻿" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ------------------------------------------------------------- audit helpers --

DETAIL_LABELS = {"code": "Container", "cas": "CAS No.", "sw_code": "SW code", "amount_left": "Amount left",
                 "old": "Old amount", "new": "New amount", "old_role": "Old role", "staff_id": "Staff ID",
                 "lot_no": "Lot No.", "location_id": "Location", "sds_date": "SDS date"}


def audit_details(row):
    """Audit details as readable (label, text) pairs. [old, new] pairs become 'old → new'."""
    if not row["details"]:
        return []
    try:
        data = json.loads(row["details"])
    except ValueError:
        return [("Details", row["details"])]
    if not isinstance(data, dict):
        return [("Details", str(data))]
    out = []
    for key, value in data.items():
        label = DETAIL_LABELS.get(key, key.replace("_", " ").capitalize())
        if isinstance(value, list) and len(value) == 2 and key not in ("containers", "missing"):
            text = f"{value[0] if value[0] not in (None, '') else '(blank)'} → {value[1] if value[1] not in (None, '') else '(blank)'}"
        elif isinstance(value, list):
            text = ", ".join(str(v) for v in value) or "none"
        elif isinstance(value, bool):
            text = "yes" if value else "no"
        else:
            text = "(blank)" if value in (None, "") else str(value)
        out.append((label, text))
    return out


def audit_link(row):
    """Page for the record an audit entry is about, if it still makes sense to link to."""
    entity, eid = row["entity"], row["entity_id"]
    try:
        code = (json.loads(row["details"] or "{}") or {}).get("code")
    except (ValueError, AttributeError):
        code = None
    if entity in ("container", "movement") and code:
        return url_for("container_view", code=code)
    if not eid:
        return None
    return {"substance": lambda: url_for("substance_view", sid=eid),
            "location": lambda: url_for("location_view", lid=eid),
            "stocktake": lambda: url_for("stocktake_view", tid=eid),
            "CHRA": lambda: url_for("chra_form", aid=eid),
            "waste": lambda: url_for("waste"),
            "user": lambda: url_for("staff_list")}.get(entity, lambda: None)()


def first_upper(text):
    """'upload SDS' -> 'Upload SDS' (Jinja's capitalize would give 'Upload sds')."""
    text = str(text or "")
    return text[:1].upper() + text[1:]


app.jinja_env.globals.update(audit_details=audit_details, audit_link=audit_link)
app.jinja_env.filters["first_upper"] = first_upper


# --------------------------------------------------------- container history --

def container_history(db, c):
    """
    Everything that happened to one container, newest first: stock movements, edits to its
    details, corrected or deleted uses (from the audit log), stocktake checks and its waste record.
    """
    events = []
    for m in db.execute("SELECT m.*, c.status FROM movements m JOIN containers c ON c.id = m.container_id "
                        "WHERE m.container_id = ?", (c["id"],)):
        change = m["change"] or 0
        amount = f"{'+' if change > 0 else '−'}{logic.fmt_qty(abs(change))} {c['unit']}" if change else ""
        events.append({"when": m["created_at"], "order": (0, m["id"]), "kind": m["type"].lower(),
                       "label": TYPE_LABELS[m["type"]], "who": m["user_name"], "amount": amount,
                       "balance": f"{logic.fmt_qty(m['balance_after'])} {c['unit']}",
                       "text": m["purpose"] or "", "ref": m["reference"] or "", "code": logic.tx_code(m["id"]), "m": m})
    audits = db.execute(
        "SELECT * FROM audit_log WHERE (entity = 'container' AND entity_id = ? AND action != 'dispose') "
        "OR (entity = 'movement' AND CASE WHEN json_valid(details) THEN json_extract(details, '$.code') END "
        "= ? COLLATE NOCASE)",
        (c["id"], c["code"]))
    for a in audits:
        if a["action"] == "update":
            label, kind = "Details edited", "edit"
            text = "; ".join(f"{k}: {v}" for k, v in audit_details(a) if k != "Container")
        elif a["action"] == "edit movement":
            d = json.loads(a["details"])
            label, kind = "Use corrected", "edit"
            text = (f"{logic.tx_code(a['entity_id'])}: {logic.fmt_qty(d.get('old'))} → "
                    f"{logic.fmt_qty(d.get('new'))} {c['unit']}")
        elif a["action"] == "delete movement":
            d = json.loads(a["details"])
            label, kind = "Use deleted", "delete"
            text = f"{logic.tx_code(a['entity_id'])}: {logic.fmt_qty(d.get('amount'))} {c['unit']} put back"
        else:
            label, kind, text = first_upper(a["action"]), "edit", "; ".join(f"{k}: {v}" for k, v in audit_details(a))
        events.append({"when": a["created_at"], "order": (1, a["id"]), "kind": kind, "label": label,
                       "who": a["user_name"], "amount": "", "balance": "", "text": text, "ref": "", "code": ""})
    for s in db.execute(
            "SELECT i.checked_at, t.id, t.started_by, l.room, l.unit_name FROM stocktake_items i "
            "JOIN stocktakes t ON t.id = i.stocktake_id LEFT JOIN locations l ON l.id = t.location_id "
            "WHERE i.container_id = ?", (c["id"],)):
        events.append({"when": s["checked_at"], "order": (2, s["id"]), "kind": "check", "label": "Found in stocktake",
                       "who": f"Stocktake by {s['started_by']}" if s["started_by"] else "", "amount": "", "balance": "",
                       "text": f"Stocktake #{s['id']} · {logic.location_label(s)}", "ref": "", "code": "",
                       "link": url_for("stocktake_view", tid=s["id"])})
    for w in db.execute("SELECT * FROM waste_records WHERE container_id = ?", (c["id"],)):
        qty = f"{logic.fmt_qty(w['quantity'])} {w['unit'] or ''}".strip() if w["quantity"] is not None else ""
        parts = [p for p in (f"SW code {w['sw_code']}" if w["sw_code"] else "", qty,
                             w["contractor"], f"note {w['consignment_no']}" if w["consignment_no"] else "") if p]
        events.append({"when": w["created_at"], "order": (3, w["id"]), "kind": "waste", "label": "Scheduled waste",
                       "who": w["user_name"], "amount": "", "balance": "", "text": " · ".join(parts),
                       "ref": "", "code": ""})
    events.sort(key=lambda e: (e["when"] or "", e["order"]), reverse=True)
    return events


@app.route("/c/<code>/history.csv")
def container_history_csv(code):
    c = get_container_or_404(code)
    events = container_history(get_db(), c)
    return csv_response(
        f"history_{c['code']}.csv",
        ["Date & time", "Event", "ID", "Change", "Left after", "Who", "Details", "Reference"],
        [[e["when"], e["label"], e["code"], e["amount"], e["balance"], e["who"] or "", e["text"], e["ref"]]
         for e in reversed(events)])


# ------------------------------------------------------------------- trace --

def trace_query(args):
    """Containers matching a lot / supplier / chemical / person, and every movement on them."""
    f = {k: args.get(k, "").strip() for k in ("lot", "supplier", "substance", "person", "start", "end")}
    f["start"] = f["start"] if logic.parse_date(f["start"]) else ""
    f["end"] = f["end"] if logic.parse_date(f["end"]) else ""
    if not any(f[k] for k in ("lot", "supplier", "substance", "person")):
        return f, None, None
    where, params = [], []
    if f["lot"]:
        where.append("c.lot_no LIKE ?")
        params.append(f"%{f['lot']}%")
    if f["supplier"]:
        where.append("c.supplier LIKE ?")
        params.append(f"%{f['supplier']}%")
    if f["substance"].isdigit():
        where.append("c.substance_id = ?")
        params.append(int(f["substance"]))
    if f["person"]:
        where.append("c.id IN (SELECT container_id FROM movements WHERE user_name LIKE ?)")
        params.append(f"%{f['person']}%")
    db = get_db()
    containers = db.execute(
        "SELECT c.*, s.name, s.cas_no, s.unit, l.room, l.unit_name FROM containers c "
        "JOIN substances s ON s.id = c.substance_id LEFT JOIN locations l ON l.id = c.location_id "
        f"WHERE {' AND '.join(where) or '1'} ORDER BY s.name COLLATE NOCASE, c.code", params).fetchall()
    ids = [c["id"] for c in containers]
    if not ids:
        return f, containers, []
    msql = ("SELECT m.*, c.code, c.lot_no, c.supplier, s.name, s.unit FROM movements m "
            "JOIN containers c ON c.id = m.container_id JOIN substances s ON s.id = c.substance_id "
            f"WHERE m.container_id IN ({','.join('?' * len(ids))})")
    mparams = list(ids)
    if f["person"]:
        msql += " AND m.user_name LIKE ?"
        mparams.append(f"%{f['person']}%")
    if f["start"]:
        msql += " AND date(m.created_at) >= ?"
        mparams.append(f["start"])
    if f["end"]:
        msql += " AND date(m.created_at) <= ?"
        mparams.append(f["end"])
    movements = db.execute(msql + " ORDER BY m.created_at DESC, m.id DESC", mparams).fetchall()
    return f, containers, movements


def trace_summary(containers, movements):
    received, used = {}, {}
    for c in containers:
        received[c["unit"]] = received.get(c["unit"], 0) + (c["size"] or 0)
    for m in movements:
        if m["type"] == "USE":
            used[m["unit"]] = used.get(m["unit"], 0) - m["change"]
    fmt = lambda d: ", ".join(f"{logic.fmt_qty(v)} {u}" for u, v in sorted(d.items())) or "–"  # noqa: E731
    return {"containers": len(containers),
            "in_lab": sum(c["status"] in ("active", "empty", "missing") for c in containers),
            "missing": sum(c["status"] == "missing" for c in containers),
            "received": fmt(received), "used": fmt(used),
            "uses": sum(m["type"] == "USE" for m in movements),
            "people": len({m["user_name"] for m in movements if m["user_name"]})}


@app.route("/trace")
def trace():
    f, containers, movements = trace_query(request.args)
    subs = get_db().execute("SELECT id, name FROM substances ORDER BY name COLLATE NOCASE").fetchall()
    summary = trace_summary(containers, movements) if containers is not None else None
    return render_template("trace.html", f=f, containers=containers, movements=movements, summary=summary,
                           substances=subs)


@app.route("/trace.csv")
def trace_csv():
    f, containers, movements = trace_query(request.args)
    rows = [[logic.tx_code(m["id"]), m["created_at"], TYPE_LABELS[m["type"]], m["code"], m["name"], m["lot_no"] or "",
             m["supplier"] or "", logic.fmt_qty(m["change"]), m["unit"], logic.fmt_qty(m["balance_after"]),
             m["user_name"] or "", m["purpose"] or "", m["reference"] or ""] for m in (movements or [])]
    return csv_response("trace.csv", ["Movement ID", "Date & time", "Type", "Container", "Chemical", "Lot No.",
                                      "Supplier", "Change", "Unit", "Balance after", "Name", "Purpose", "Reference"], rows)


# --------------------------------------------------------------- audit log --

def audit_query(args):
    f = {k: args.get(k, "").strip() for k in ("start", "end", "who", "action", "entity", "q")}
    where, params = [], []
    if logic.parse_date(f["start"]):
        where.append("date(created_at) >= ?")
        params.append(f["start"])
    else:
        f["start"] = ""
    if logic.parse_date(f["end"]):
        where.append("date(created_at) <= ?")
        params.append(f["end"])
    else:
        f["end"] = ""
    for key, col in (("who", "user_name"), ("action", "action"), ("entity", "entity")):
        if f[key]:
            where.append(f"{col} = ?")
            params.append(f[key])
    if f["q"]:
        where.append("(details LIKE ? OR user_name LIKE ? OR action LIKE ? OR entity LIKE ?)")
        params += [f"%{f['q']}%"] * 4
    return f, (" WHERE " + " AND ".join(where)) if where else "", params


@app.route("/audit")
@roles_required("admin")
def audit_log():
    db = get_db()
    f, where, params = audit_query(request.args)
    total = db.execute(f"SELECT COUNT(*) FROM audit_log{where}", params).fetchone()[0]
    pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    page = request.args.get("page", "1")
    page = min(pages, int(page)) if page.isdigit() and int(page) > 0 else 1
    rows = db.execute(f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                      params + [AUDIT_PAGE_SIZE, (page - 1) * AUDIT_PAGE_SIZE]).fetchall()
    options = {col: [r[0] for r in db.execute(
        f"SELECT DISTINCT {col} FROM audit_log WHERE {col} IS NOT NULL ORDER BY {col} COLLATE NOCASE")]
        for col in ("user_name", "action", "entity")}
    return render_template("audit_log.html", rows=rows, f=f, total=total, page=page, pages=pages, options=options)


@app.route("/audit.csv")
@roles_required("admin")
def audit_csv():
    f, where, params = audit_query(request.args)
    rows = get_db().execute(f"SELECT * FROM audit_log{where} ORDER BY id", params).fetchall()
    return csv_response("audit_log.csv", ["Entry", "Date & time", "Who", "Action", "Record", "Record ID", "Details"],
                        [[r["id"], r["created_at"], r["user_name"], r["action"], r["entity"], r["entity_id"] or "",
                          "; ".join(f"{k}: {v}" for k, v in audit_details(r))] for r in rows])

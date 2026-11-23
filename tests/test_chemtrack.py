"""
Automated checks for ChemTrack. They use a temporary database, never your real data.

Run:  python -m unittest discover -s tests -v

(Use exactly this command. Plain `python -m unittest` would import the chemtrack package
 before these settings, and this file then refuses to run, to protect your real database.)
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

if "chemtrack" in sys.modules:
    raise RuntimeError("chemtrack was imported before the test settings, so it points at the REAL database. "
                       "Run the tests with: python -m unittest discover -s tests -v")

TMP = Path(tempfile.mkdtemp(prefix="chemtrack-test-"))
os.environ["CHEMTRACK_DB"] = str(TMP / "test.db")
os.environ["CHEMTRACK_UPLOADS"] = str(TMP / "uploads")
os.environ["CHEMTRACK_BACKUP_DIR"] = str(TMP / "backups")

from chemtrack import app, db as dbmod  # noqa: E402

app.config["TESTING"] = True
ADMIN = ("TU-LAB-014", "Afiq Irfaan", "Admin-Pass-2026")


def token(client, path):
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).get_data(as_text=True))
    return m.group(1) if m else ""


def post(client, path, data=None, page=None, **kw):
    data = dict(data or {}, csrf_token=token(client, page or path))
    return client.post(path, data=data, **kw)


def sql(query, *args):
    conn = sqlite3.connect(os.environ["CHEMTRACK_DB"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def temp_pw(html):
    m = re.search(r'Temporary password</dt><dd class="mono big">([^<]+)<', html)
    return m.group(1) if m else None


class ChemTrackTests(unittest.TestCase):
    """Runs in order: each test builds on the previous one, like a real day in the lab."""

    @classmethod
    def setUpClass(cls):
        cls.admin = app.test_client()
        post(cls.admin, "/setup", {"staff_id": ADMIN[0], "name": ADMIN[1], "password": ADMIN[2], "confirm": ADMIN[2]})

    # ---------------------------------------------------------------- security
    def test_01_pages_need_sign_in(self):
        r = app.test_client().get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_02_post_without_form_token_refused(self):
        self.assertEqual(self.admin.post("/locations", data={"room": "X"}).status_code, 400)

    def test_03_admin_adds_locations(self):
        post(self.admin, "/locations", {"room": "Lab 1", "unit_name": "Flammable cabinet A",
                                        "storage_type": "Flammable cabinet", "flammable_limit_l": "3"})
        post(self.admin, "/locations", {"room": "Lab 1", "unit_name": "Shelf B", "storage_type": "General shelf"})
        self.assertEqual(len(sql("SELECT * FROM locations")), 2)

    def test_04_add_chemical_validates_cas(self):
        r = post(self.admin, "/substance/new", {"name": "Bad", "cas_no": "12345", "unit": "L"})
        self.assertIn("CAS No. should look like", r.get_data(as_text=True))
        r = post(self.admin, "/substance/new", {"name": "Ethanol absolute", "cas_no": "64-17-5", "unit": "L",
                                                "hazards": ["Flammable", "Irritant"], "is_cr": "1",
                                                "min_quantity": "2"}, page="/substance/new")
        self.assertIn("/receive?substance=", r.headers["Location"])

    def test_05_receive_creates_containers_and_labels(self):
        sid = sql("SELECT id FROM substances WHERE name = 'Ethanol absolute'")[0]["id"]
        lid = sql("SELECT id FROM locations WHERE unit_name = 'Flammable cabinet A'")[0]["id"]
        r = post(self.admin, "/receive", {"substance_id": sid, "size": "2.5", "count": "2", "location_id": lid,
                                          "lot_no": "K5012", "supplier": "Merck",
                                          "expiry_date": (date.today() + timedelta(days=400)).isoformat()})
        self.assertIn("/labels?codes=CT-00001,CT-00002", r.headers["Location"])
        html = self.admin.get(r.headers["Location"]).get_data(as_text=True)
        self.assertEqual(html.count('<svg class="qr"'), 2)
        rows = sql("SELECT amount, status FROM containers ORDER BY id")
        self.assertEqual([(r["amount"], r["status"]) for r in rows], [(2.5, "active"), (2.5, "active")])

    def test_06_flammable_limit_warning(self):
        html = self.admin.get("/inventory").get_data(as_text=True)
        self.assertIn("over the 3 L limit", html)  # 5 L of ethanol in a 3 L cabinet
        dash = self.admin.get("/").get_data(as_text=True)
        self.assertIn("fill-over", dash)  # the dashboard gauge turns red
        self.assertIn("No location over its flammable limit", dash)

    def test_06b_dashboard_scan_and_actions(self):
        r = self.admin.get("/find?code=ct-00001&action=move")
        self.assertIn("/c/CT-00001?open=move", r.headers["Location"])
        html = self.admin.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('id="act-move" open', html)
        title = html.split("<title>", 1)[1].split("</title>", 1)[0]
        self.assertNotIn("<", title)  # nothing but text in the browser tab title
        self.assertEqual(self.admin.get("/find?code=ethanol").headers["Location"], "/inventory?q=ethanol")
        dash = self.admin.get("/").get_data(as_text=True)
        self.assertIn("Compliance score", dash)
        self.assertIn("CT-00001", dash.split("My recent scans", 1)[1])     # just opened
        self.assertIn("CT-00002", dash.split("Containers I received", 1)[1])

    def test_06c_sign_in_page_shows_counts_only(self):
        html = app.test_client().get("/login").get_data(as_text=True)
        self.assertIn("Compliance score", html)
        self.assertIn("Fullest flammable cabinet", html)
        for secret in ("Ethanol", "64-17-5", "Flammable cabinet A", "CT-0000"):
            self.assertNotIn(secret, html)

    def test_07_use_and_refuse_overuse(self):
        post(self.admin, "/c/CT-00001/use", {"amount": "0.5", "purpose": "CHM101"}, page="/c/CT-00001")
        self.assertEqual(sql("SELECT amount FROM containers WHERE code='CT-00001'")[0]["amount"], 2.0)
        post(self.admin, "/c/CT-00001/use", {"amount": "9"}, page="/c/CT-00001")
        self.assertEqual(sql("SELECT amount FROM containers WHERE code='CT-00001'")[0]["amount"], 2.0)
        self.assertIsNotNone(sql("SELECT opened_date FROM containers WHERE code='CT-00001'")[0]["opened_date"])

    def test_08_stock_page_by_scanned_code(self):
        post(self.admin, "/stock", {"code": "ct-00002", "amount": "2.5"})
        row = sql("SELECT amount, status FROM containers WHERE code='CT-00002'")[0]
        self.assertEqual((row["amount"], row["status"]), (0, "empty"))

    def test_09_move_container(self):
        lid = sql("SELECT id FROM locations WHERE unit_name = 'Shelf B'")[0]["id"]
        post(self.admin, "/c/CT-00001/move", {"location_id": lid}, page="/c/CT-00001")
        self.assertEqual(sql("SELECT location_id FROM containers WHERE code='CT-00001'")[0]["location_id"], lid)
        self.assertEqual(sql("SELECT COUNT(*) n FROM movements WHERE type='TRANSFER'")[0]["n"], 1)

    def test_10_edit_use_recalculates(self):
        mid = sql("SELECT m.id FROM movements m JOIN containers c ON c.id=m.container_id "
                  "WHERE c.code='CT-00001' AND m.type='USE'")[0]["id"]
        post(self.admin, f"/movement/{mid}/edit", {"amount": "1", "purpose": "fixed", "next": "/"},
             page=f"/movement/{mid}/edit")
        self.assertEqual(sql("SELECT amount FROM containers WHERE code='CT-00001'")[0]["amount"], 1.5)
        later = sql("SELECT balance_after FROM movements m JOIN containers c ON c.id=m.container_id "
                    "WHERE c.code='CT-00001' ORDER BY m.id")
        self.assertEqual(later[-1]["balance_after"], 1.5)  # the later TRANSFER row was shifted too

    def test_11_dispose_with_waste_record(self):
        post(self.admin, "/c/CT-00002/dispose", {"waste": "1", "sw_code": "sw 322", "purpose": "Empty"},
             page="/c/CT-00002")
        self.assertEqual(sql("SELECT status FROM containers WHERE code='CT-00002'")[0]["status"], "disposed")
        self.assertEqual(sql("SELECT sw_code FROM waste_records")[0]["sw_code"], "SW 322")
        r = post(self.admin, "/c/CT-00002/use", {"amount": "1"}, page="/c/CT-00002")
        self.assertEqual(r.status_code, 302)  # refused politely, nothing changed
        self.assertEqual(sql("SELECT COUNT(*) n FROM movements m JOIN containers c ON c.id=m.container_id "
                             "WHERE c.code='CT-00002' AND m.type='USE'")[0]["n"], 1)

    def test_12_sds_upload_pdf_only(self):
        sid = sql("SELECT id FROM substances")[0]["id"]
        page = f"/substance/{sid}"
        import io
        post(self.admin, f"/substance/{sid}/sds", {"sds": (io.BytesIO(b"not a pdf"), "x.pdf")}, page=page,
             content_type="multipart/form-data")
        self.assertIsNone(sql("SELECT sds_file FROM substances WHERE id=?", sid)[0]["sds_file"])
        post(self.admin, f"/substance/{sid}/sds", {"sds": (io.BytesIO(b"%PDF-1.4 test"), "../../evil.pdf"),
                                                   "sds_date": "2025-01-01"}, page=page,
             content_type="multipart/form-data")
        self.assertEqual(sql("SELECT sds_file FROM substances WHERE id=?", sid)[0]["sds_file"], f"sds-{sid}.pdf")
        r = self.admin.get(f"/substance/{sid}/sds.pdf")
        self.assertEqual(r.data[:5], b"%PDF-")
        r.close()

    def test_13_stocktake_marks_missing(self):
        lid = sql("SELECT id FROM locations WHERE unit_name = 'Shelf B'")[0]["id"]
        sid = sql("SELECT id FROM substances")[0]["id"]
        post(self.admin, "/receive", {"substance_id": sid, "size": "1", "count": "1", "location_id": lid})
        r = post(self.admin, "/stocktake", {"location_id": lid})
        tid = int(r.headers["Location"].rsplit("/", 1)[1])
        post(self.admin, f"/stocktake/{tid}/scan", {"code": "CT-00001"}, page=f"/stocktake/{tid}")
        post(self.admin, f"/stocktake/{tid}/finish", page=f"/stocktake/{tid}")
        statuses = {r["code"]: r["status"] for r in sql("SELECT code, status FROM containers")}
        self.assertEqual(statuses["CT-00001"], "active")
        self.assertEqual(statuses["CT-00003"], "missing")
        missing = self.admin.get("/inventory?show=missing").get_data(as_text=True)
        self.assertIn("Ethanol absolute", missing.split("<tbody>", 1)[1])
        dash = self.admin.get("/").get_data(as_text=True)
        self.assertIn("No missing containers", dash)
        self.assertIn("1 missing", dash)

    def test_14_register_reports(self):
        post(self.admin, "/substance/new", {"name": "Sodium chloride", "unit": "kg"})
        sid = sql("SELECT id FROM substances WHERE name='Sodium chloride'")[0]["id"]
        lid = sql("SELECT id FROM locations")[0]["id"]
        post(self.admin, "/receive", {"substance_id": sid, "size": "1", "location_id": lid})
        table = lambda html: html.split("<tbody>", 1)[1]  # noqa: E731  (ignore the "Received…" message)
        cr = table(self.admin.get("/register/CR").get_data(as_text=True))
        ci = table(self.admin.get("/register/CI").get_data(as_text=True))
        self.assertIn("Ethanol absolute", cr)
        self.assertNotIn("Sodium chloride", cr)
        self.assertIn("Sodium chloride", ci)
        self.assertEqual(self.admin.get("/register/CI.csv").data[:3], b"\xef\xbb\xbf")

    def test_15_chra_default_review_five_years(self):
        post(self.admin, "/chra/new", {"title": "Lab 1", "assessment_date": "2024-03-01"})
        self.assertEqual(sql("SELECT next_review FROM assessments")[0]["next_review"], "2029-03-01")

    def test_16_roles(self):
        html = post(self.admin, "/staff/add", {"staff_id": "TU-AUD-002", "name": "Nurul", "role": "viewer"},
                    page="/staff").get_data(as_text=True)
        viewer = app.test_client()
        pw = temp_pw(html)
        post(viewer, "/login", {"staff_id": "TU-AUD-002", "password": pw})
        post(viewer, "/account/password", {"current": pw, "password": "Viewer-Pass-1", "confirm": "Viewer-Pass-1"})
        for page in ["/", "/locations", "/movements", "/compliance", "/analysis", "/c/CT-00001", "/register/CR",
                     "/emergency", "/waste", "/stocktake"]:
            self.assertEqual(viewer.get(page).status_code, 200, page)
        self.assertEqual(viewer.get("/stock").status_code, 403)
        self.assertEqual(viewer.get("/receive").status_code, 403)
        self.assertEqual(post(viewer, "/c/CT-00001/use", {"amount": "1"}, page="/c/CT-00001").status_code, 403)
        self.assertEqual(viewer.get("/audit").status_code, 403)

    def test_17_audit_log_records_changes(self):
        actions = {r["action"] for r in sql("SELECT action FROM audit_log")}
        self.assertTrue({"create", "receive", "dispose", "edit movement", "upload SDS"} <= actions, actions)

    def test_18_every_admin_page_loads(self):
        for page in ["/", "/inventory", "/inventory?show=low", "/inventory?show=empty", "/stock", "/receive", "/locations", "/location/1", "/location/1/edit", "/movements",
                     "/movements.csv", "/compliance", "/register/CI", "/chra/new", "/waste", "/stocktake",
                     "/stocktake/1", "/audit", "/analysis", "/analysis?period=365", "/staff", "/staff/log",
                     "/substance/1", "/substance/1/edit", "/c/CT-00001", "/c/CT-00001/edit",
                     "/labels?codes=CT-00001", "/emergency", "/emergency?location=1", "/account/password"]:
            self.assertEqual(self.admin.get(page).status_code, 200, page)

    def test_19_forecast_averages_over_recorded_days(self):
        from chemtrack import logic
        today = date(2026, 11, 23)
        self.assertEqual(logic.usage_rate_days("2026-11-01 09:15:00", 90, today), 23)   # records started recently
        self.assertEqual(logic.usage_rate_days("2025-01-01", 90, today), 90)            # records older than period
        self.assertEqual(logic.usage_rate_days(None, 90, today), 90)                    # no use yet
        self.assertEqual(logic.usage_rate_days("2026-11-30", 90, today), 1)             # clock oddities never divide by 0
        # Uses in these tests are all from today, so the page must say it averaged over 1 day, not 90
        html = self.admin.get("/analysis?period=90").get_data(as_text=True)
        self.assertIn("data-rate-note", html)
        self.assertIn("Average use over the last 1 day,", html)


class LegacyMigrationTest(unittest.TestCase):
    def test_old_database_is_converted(self):
        path = TMP / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE chemicals (id INTEGER PRIMARY KEY, name TEXT, cas_no TEXT, formula TEXT, hazards TEXT,
              location TEXT, quantity REAL, unit TEXT, min_quantity REAL, supplier TEXT, lot_no TEXT,
              received_date TEXT, expiry_date TEXT, sds_date TEXT, notes TEXT, created_at TEXT);
            CREATE TABLE transactions (id INTEGER PRIMARY KEY, chemical_id INTEGER, type TEXT, amount REAL,
              balance_after REAL, user_name TEXT, purpose TEXT, reference TEXT, created_at TEXT,
              edited_at TEXT, edited_by TEXT, user_id INTEGER);
            CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, staff_id TEXT, name TEXT, role TEXT,
              password_hash TEXT, must_change_password INTEGER, active INTEGER, failed_attempts INTEGER,
              locked_until TEXT, last_login TEXT, created_at TEXT);
            INSERT INTO users (staff_id, name, role, password_hash) VALUES ('A1', 'Admin', 'admin', 'x');
            INSERT INTO chemicals VALUES (1, 'Ethanol', '64-17-5', 'C2H5OH', 'Flammable,Irritant',
              'Lab 1 – Flammable cabinet A', 4, 'L', 2, 'Merck', 'K1', '2026-01-01', '2027-01-01', NULL, NULL, '2026-01-01');
            INSERT INTO chemicals VALUES (2, 'NaCl', NULL, NULL, '', 'Lab 2 – Shelf C', 0, 'kg', 0, NULL, NULL,
              NULL, NULL, NULL, NULL, '2026-01-01');
            INSERT INTO transactions VALUES (1, 1, 'IN', 5, 5, 'system', 'Opening stock', NULL, '2026-01-01 09:00:00', NULL, NULL, NULL);
            INSERT INTO transactions VALUES (2, 1, 'OUT', 1, 4, 'Siti', 'Lab', NULL, '2026-02-01 09:00:00', NULL, NULL, NULL);
        """)
        conn.close()
        dbmod.migrate(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        c = conn.execute("SELECT * FROM containers WHERE id = 1").fetchone()
        self.assertEqual((c["code"], c["amount"], c["size"], c["status"]), ("CT-00001", 4, 5, "active"))
        self.assertEqual(conn.execute("SELECT status FROM containers WHERE id = 2").fetchone()[0], "empty")
        self.assertEqual(conn.execute("SELECT storage_type FROM locations WHERE unit_name = 'Flammable cabinet A'")
                         .fetchone()[0], "Flammable cabinet")
        self.assertEqual([r["type"] for r in conn.execute("SELECT type FROM movements ORDER BY id")], ["RECEIVE", "USE"])
        self.assertEqual(conn.execute("SELECT is_cr FROM substances WHERE id = 1").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)  # accounts kept
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(dbmod.MIGRATIONS))
        conn.close()
        self.assertTrue(any(Path(os.environ["CHEMTRACK_BACKUP_DIR"]).glob("*before-upgrade*")))
        dbmod.migrate(path)  # running again changes nothing


if __name__ == "__main__":
    unittest.main()

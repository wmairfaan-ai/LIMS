# ChemTrack – Chemical Inventory System

A web app for managing a lab's chemicals **bottle by bottle**: every container gets its own
**QR label**, location, owner and full life cycle (received → used → moved → disposed), with
Malaysian compliance records built in.

Built with Python (Flask), SQLite and waitress. Runs on your own computer, with no internet needed.

The design follows a comparison of university chemical systems in 6 Asian countries
(NUS LMMS, CityU CCS, HKUST, CUHK, HKU, Kyoto KUCRS, U Tokyo UTCIMS, KAIST, NTU Taiwan, UTM/UKM/USM):
**one record per container, QR/barcode identity, proper locations with legal limits,
life-cycle tracking, stocktakes, and local-law reports.**

---

## How it's organised

| Idea | What it means in ChemTrack |
|---|---|
| **Chemical (substance)** | *What* it is: name, CAS No., GHS hazards, unit, reorder level, regulatory flags, SDS |
| **Container** | *A physical bottle/bag* with its own code (`CT-00042`) and QR label: size, amount left, lot, supplier, expiry, location, status |
| **Location** | Room › storage unit (e.g. *Lab 1 › Flammable cabinet A*), storage type, flammable limit |
| **Movement** | Everything that happens to a container: Received, Used, Moved, Corrected, Disposed, Stocktake |

A chemical's stock = the total of its active containers.

## Daily use

| Task | Where |
|---|---|
| **New bottles arrive** | **Inventory › Receive containers**: choose the chemical, size, how many, location, lot, expiry → **print the QR labels** and stick them on |
| **Use some** | **Dashboard**: choose *Use* → scan the bottle → amount. Or scan its QR with a phone, or use the green **Stock in / out** button |
| **Move a bottle** | Container page → **Move** |
| **Weighed it and the amount is different** | Container page → **Correct amount** (reason required) |
| **Empty or expired** | Container page → **Dispose** (+ scheduled waste record with SW code) |
| **Typed the wrong amount** | **Edit** next to the "Used" entry: later balances recalculate automatically |
| **Find a bottle** | Type or scan its `CT-` code in the top search box |

## Pages

- **Sign-in page**: split design. The left side shows **today's safety status, as counts only**
  (compliance score, chemicals needing attention, fullest flammable cabinet, days to CHRA review).
  It never shows chemical names or amounts. To hide it, set `CHEMTRACK_LOGIN_STATUS=0`.
- **Dashboard** (first page after sign-in), combining a scan-first layout with a safety control room:
  - **Scan box**: pick **Use / Move / Correct / Dispose**, then scan a QR label (or type a code). The container
    opens with that action ready and the cursor in place. Typing a chemical name searches the inventory instead
  - **Compliance score** (%) and **checklist** of 10 checks (SDS uploaded, SDS age, CHRA, stocktakes,
    flammable limits set and not exceeded, segregation, expired, empties, missing), each with a **Fix** link
  - **Action needed** tiles: expired, expiring, low stock, SDS to fix, missing, empty to dispose. Each opens the filtered inventory
  - **Flammable storage gauges** per location (amber at 80%, red when over the limit)
  - **My recent scans** and **Containers I received**; 🚨 emergency list; ➕ Receive containers
- **Inventory**: chemicals with totals, containers, locations and alerts; filters *Needs attention, Low / out of stock,
  Expired / expiring, SDS to fix, Missing, Empty*; storage problems (incompatible chemicals together, flammable limit exceeded)
- **Chemical page**: details, regulatory flags, **SDS PDF** (upload / open), all its containers with fill gauges, recent activity
- **Container page** (where the QR leads): amount left, location, lot, dates, **QR code**, actions (use, move, correct, dispose), history, print label
- **Locations**: rooms and storage units, storage types, **flammable litres vs limit**, incompatible storage warnings, **🚨 printable emergency list** per room (for Bomba / lab door), start a **stocktake**
- **Movements**: every movement with filters (date, type, chemical, search), 50 per page, charts, most used chemicals, CSV export.
  The charts, totals and CSV always cover every matching movement, not only the page shown
- **Compliance** (one page for audits):
  - **USECHH 2000**: Chemical Inventory (CI) and Chemical Register (CR, hazardous to health): printable and CSV
  - **CLASS 2013**: chemicals in use without an SDS, or with an SDS over 5 years old
  - **CHRA** records with review due dates (default 5 years after assessment)
  - **Fire Services Act 1988**: flammable litres per location vs your permit / cabinet limit
  - **Poisons Act 1952 / precursors**: flagged chemicals and their stock
  - **Scheduled Wastes Regulations 2005**: waste log with SW codes, contractor, consignment note
  - **Stocktake**: scan every container in a location; unscanned ones are marked **missing**. Scans are saved live,
    without reloading the page (`static/stocktake.js`). A scan that could not be saved shows **Retry**, and the
    stocktake can't be finished until it is saved
- **Analysis**: usage forecast and reorder plan, trends, uses by person / purpose, hazard profile, expiry outlook, slow-moving stock, suppliers
- **Staff** (admin): accounts, roles, sign-in log, **audit log** (who changed what, when)

## Roles

| Action | Admin | Staff | Viewer |
|---|:-:|:-:|:-:|
| View everything, print labels and reports, export CSV | ✓ | ✓ | ✓ |
| Receive, use, move, correct, dispose containers | ✓ | ✓ | – |
| Add / edit chemicals, upload SDS, waste records, stocktakes | ✓ | ✓ | – |
| Correct own "used" entries (same day) | ✓ | ✓ | – |
| Correct any entry | ✓ | – | – |
| Locations and limits, CHRA records, delete chemicals | ✓ | – | – |
| Staff accounts, sign-in log, audit log | ✓ | – | – |

## Security
- Sign in with Staff ID + password; passwords stored only as secure hashes
- 5 wrong passwords → locked 5 minutes; every attempt in the sign-in log
- Automatic sign-out after 8 hours; password changes sign out old sessions
- CSRF protection on every form; SDS uploads must be real PDFs and are saved under ChemTrack's own file names
- **Audit log** of every change; movement corrections are marked *edited* with who and when

---

## How to run (Windows)

In PowerShell, inside this folder:

```powershell
pip install -r requirements.txt
```
Installs Flask, waitress and qrcode. Once per computer.

```powershell
python serve.py
```
Starts ChemTrack on the **production server (waitress)** with **automatic daily backups**.
Or just double-click **`start_chemtrack.bat`**. Open **http://127.0.0.1:5000**. Ctrl+C (or close the window) to stop.

The first time, create your admin account, then add **locations**, then **chemicals**, then **receive containers**.

**While changing the code**, use Flask's development server with auto-reload instead:
```powershell
$env:CHEMTRACK_DEBUG = "1"; python app.py
```
Never use debug mode when others can reach the app. Its error page can run code.

**Sample data** to explore (erases all inventory data, keeps accounts, saves a backup first):
```powershell
python seed.py
```

## Backups and upgrades
- `python serve.py` saves a backup **at start and every 24 hours** into `backups/`, keeping the newest 30.
  Backups use SQLite's safe online copy, so they're consistent even while people are working.
- Back up any time: `python backup.py`
- **Restore**: stop ChemTrack → copy a file from `backups/` over `chemicals.db` → start again.
- **Database upgrades are automatic and versioned** (`chemtrack/db.py`). Before any upgrade a
  `before-upgrade` backup is saved. Old data from the first ChemTrack version was converted:
  each chemical became one substance + one container (`CT-` + its old number), all history kept,
  and the original tables kept as `legacy_chemicals` / `legacy_transactions`.
- This folder is in OneDrive, so backups are also synced to the cloud.

## Automated tests
```powershell
python -m unittest discover -s tests -v
```
30 checks (security, dashboard scan actions, sign-in page privacy, receiving, labels, use/over-use, scanning, moving, correcting with
recalculation, disposal + waste, SDS upload safety, stocktake, live stocktake scanning, registers, CHRA dates, roles,
audit log, every page, and conversion of an old database). They use a temporary database.
Use exactly this command: plain `python -m unittest` is blocked on purpose, because it would point at your real data.

## Who can open it?
`127.0.0.1` (**localhost**) means *this computer only*. `:5000` is the port.
To share on the office network later (stage 3 of the deployment roadmap): get **IT approval**, put
**HTTPS** in front (e.g. Caddy), then set:

| Variable | Meaning |
|---|---|
| `CHEMTRACK_HOST=0.0.0.0` | Accept connections from other computers |
| `CHEMTRACK_HTTPS=1` | Sign-in cookie only sent over HTTPS (turn on only once HTTPS works) |
| `CHEMTRACK_BEHIND_PROXY=1` | Trust the HTTPS proxy's headers (real user addresses in logs) |
| `CHEMTRACK_BASE_URL=https://chemtrack.lab.local` | Address printed inside QR labels |

QR labels open the container page on a phone **once ChemTrack is reachable from that phone**
(after stage 3). Until then, scan with a USB/Bluetooth QR scanner into the search box, or type the code.

## Files

| File | What it does |
|---|---|
| `serve.py` | Start ChemTrack (waitress + daily backups). **Use this normally** |
| `start_chemtrack.bat` | Double-click to start |
| `app.py` | Start the development server (for code changes) |
| `backup.py`, `seed.py`, `reset_password.py` | Manual backup · sample data · emergency admin password reset |
| `chemtrack/config.py` | **Settings** (limits, alert days, units, storage types, …) |
| `chemtrack/db.py` | Database structure and versioned migrations |
| `chemtrack/logic.py` | Rules: alerts, storage checks, flammable totals (no web code) |
| `chemtrack/security.py` | Sign-in checks, roles, CSRF, audit log |
| `chemtrack/backup.py`, `chemtrack/qr.py` | Backups · QR codes |
| `chemtrack/views/` | Pages: `auth`, `inventory`, `locations`, `compliance`, `reports` |
| `templates/`, `static/style.css` | Page layouts and styling |
| `tests/` | Automated tests |
| `chemicals.db`, `.secret_key`, `uploads/`, `backups/` | Your data, secret key, SDS files, backups. **Not in git. Keep private** |

## Settings you can change (`chemtrack/config.py`)
`EXPIRY_WARNING_DAYS` (30) · `SDS_MAX_AGE_DAYS` (5 years) · `CHRA_REVIEW_YEARS` (5) · `CHRA_WARNING_DAYS` (90) ·
`STOCKTAKE_WARNING_DAYS` (365) · `LEAD_TIME_DAYS` (14) · `COVER_DAYS` (60) · `INCOMPATIBLE_PAIRS` ·
`STORAGE_TYPES` · `UNITS` · `BACKUP_KEEP` (30) · `MAX_FAILED_LOGINS` (5) · `LOCKOUT_MINUTES` (5) ·
`SESSION_HOURS` (8) · `MIN_PASSWORD_LENGTH` (8) · `MAX_SDS_MB` (10)

## Troubleshooting

| Problem | Fix |
|---|---|
| `No module named 'waitress'` / `'qrcode'` / `'flask'` | `pip install -r requirements.txt` |
| Port 5000 busy | Another copy is running. Close it, or press Ctrl+C in its window |
| Changes to the code don't show | Restart ChemTrack (waitress doesn't reload code) |
| Staff forgot password | Admin: Staff page → **Reset password** |
| Admin forgot password | `python reset_password.py <Staff ID>` on this computer |
| "This form expired" | Refresh the page and try again |
| A container shows **Missing** | It wasn't scanned in a stocktake. Scan it in the next stocktake, or use **Correct amount** when found |
| Something went wrong after an update | Restore the newest `before-upgrade` backup (see Backups) |

## Next steps (deployment roadmap)
1. ✅ App ready: sign-in, roles, audit log, waitress, backups, tests
2. Choose hosting with Taylor's IT (lab PC, university server, or cloud)
3. HTTPS + firewall + friendly address → phones can open QR labels
4. Run as a Windows service (starts with the PC)
5. Pilot in the lab, train staff, go live

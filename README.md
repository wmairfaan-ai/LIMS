# ChemTrack – Chemical Inventory System

A simple web app for keeping a lab's chemical register and tracing every stock movement.
Built with Python (Flask) and SQLite. Runs on your own computer, with no internet or server needed.

## Features

### Staff accounts and sign in
Every page is locked until you sign in with your **Staff ID and password**.

| Action | Admin | Staff | Viewer |
|---|:-:|:-:|:-:|
| View inventory, movements and analysis; export CSV | ✓ | ✓ | ✓ |
| Record stock in / out | ✓ | ✓ | – |
| Add and edit chemicals | ✓ | ✓ | – |
| Edit or delete **own** movements (same day) | ✓ | ✓ | – |
| Edit or delete **any** movement | ✓ | – | – |
| Delete chemicals; manage staff accounts; view sign-in log | ✓ | – | – |

- **First run**: ChemTrack asks you to create the first **admin** account
- **Adding staff** (Staff page): the admin enters the Staff ID, name and role. ChemTrack creates a
  temporary password, shown **once**, to hand over in person. Staff must choose their own password at first sign-in
- **Forgotten password**: the admin clicks **Reset password** on the Staff page
- **Staff who leave** are **disabled**, not deleted, so their stock history stays traceable
- **Your name is filled in automatically** on every stock movement. Nobody can record under someone else's name.
  Corrections show who edited them and when
- **Account menu** (your initials, top right): your role, change password, staff accounts, sign-in log, sign out

**Security built in**
- Passwords are stored only as secure hashes (scrypt), never as text
- 5 wrong passwords lock the account for 5 minutes; every attempt is in the **Sign-in log**
- Automatic sign-out after 8 hours; changing or resetting a password signs out old sessions
- Every form carries a hidden security token (CSRF protection), so other websites can't submit forms in ChemTrack
- A random secret key is created on first run and kept in `.secret_key`

### Top bar
Inventory · Movements · Analysis · Staff (admin) tabs, a **search box** on every page,
the **Stock in / out** button, a **bell** showing how many chemicals need attention, and your account menu.

### Inventory (home page)
- **Chemical register**: name, CAS No., formula, GHS hazard classes, location, supplier, lot No., received / expiry / SDS dates
- **Summary cards**: total chemicals, low or out of stock, expired or expiring, SDS to update
- **Alerts** on each chemical:
  - Low stock (at or below the reorder level) or out of stock
  - Expired, or expiring within 30 days
  - SDS date missing, or SDS older than 5 years
- **Storage check**: warns when incompatible chemicals share a location
  (flammable + oxidiser, explosive + flammable, explosive + oxidiser)
- **Search** (top bar) by name, CAS No. or location; **All chemicals / Needs attention** filter
- **Quick + In / − Out buttons** on every row, which open the Stock in / out page for that chemical
- **+ Add chemical** and **Export register** buttons at the top
- **Export register**: chemical register as CSV (opens in Excel), useful for
  USECHH 2000 / CLASS 2013 chemical register records

### Stock in / out page (green button in the top bar)
The main place to key in stock movements:
1. Choose **Stock out** (used / taken) or **Stock in** (received / restocked)
2. Choose the chemical (the list shows the current stock of each one)
3. Enter the amount, the purpose and the PO / DO / invoice No. (your name is added automatically)
4. Click **Save movement**

- Prevents taking out more than is in stock
- After saving, the type stays selected, so you can key in many movements quickly
- Shows the last 10 movements with an **Edit** link where you're allowed to edit

### Editing or deleting a movement
Keyed in the wrong amount? Click **Edit** next to the movement (on the Stock in / out page,
the Stock movements dashboard or a chemical's history). Staff can fix their own movements on
the same day; admins can fix any movement:
- Change the type, amount, purpose or reference
- If the amount or type changes, the chemical's stock and the **balance of every later movement
  are recalculated automatically**
- Changes that would make the stock negative at any point are refused
- Corrected movements are marked **edited** (with who and when) so corrections stay visible
- **Delete movement** removes it and recalculates the stock

### Chemical page
- All details, alerts and the chemical ID (e.g. `CH-0002`)
- Stock in / out form for that chemical
- Full history for that chemical, with the balance after each movement and Edit links

### Stock movements dashboard
- Every stock in and out across all chemicals, with:
  movement ID (`TX-00027`), date & time, type, chemical ID, quantity, **balance after**,
  supplier, lot No., reference, name and purpose
- **Filters**: date range (last 30 days by default), stock in or out only, one chemical,
  or search by supplier, name, PO No. or purpose
- **Summary**: number of stock-ins, stock-outs, chemicals moved and people involved
- **Movements per day** chart and the **5 most used chemicals**
- **Export CSV** of exactly what the filters show (e.g. for a monthly usage report)

### Data analysis page
Choose a period (30, 90, 180 or 365 days) to analyse:
- **Summary**: stock-outs, stock-ins, chemicals in use, chemicals to reorder, slow-moving chemicals
- **Usage forecast & reorder plan**: for each chemical in use, the amount used, average use per week,
  **days of stock left**, the **date it will run out**, and a **suggested order quantity**
  - *Order now*: runs out within the delivery lead time (14 days)
  - *Order soon*: runs out within lead time + 30 days
  - Suggested order = enough for 60 days of use, minus what is in stock
- **Activity trend**: stock in vs stock out per week (30 / 90 days) or per month (180 / 365 days)
- **Stock-outs by person**, and **what chemicals are used for** (by purpose)
- **Hazard profile**: how many chemicals fall under each GHS hazard class
- **Expiry outlook**: expired, within 30 days, 31–90, 91–180, over 180 days, no date
- **Slow-moving stock**: chemicals not used at all in the period (check before reordering, or plan disposal)
- **Suppliers**: number of chemicals and stock-ins per supplier

The forecast uses a simple average, so it works best with a few weeks of real stock-out records.

## How to run (Windows)

Run these in PowerShell inside the project folder (the one containing `app.py`):

```powershell
pip install -r requirements.txt
```
Installs Flask. Only needed once per computer.

```powershell
python seed.py
```
Optional: loads 11 sample chemicals with about 3 weeks of stock history.
**Warning: this erases all chemicals and stock movements** (staff accounts are kept).

```powershell
python app.py
```
Starts the app. Open **http://127.0.0.1:5000** in your browser.
The first time, create your admin account. Press **Ctrl+C** in the terminal to stop it.

**While changing the code**, start it in debug mode so it reloads automatically:
```powershell
$env:CHEMTRACK_DEBUG = "1"; python app.py
```
Never use debug mode when others can reach the app. Its error page can run code.

## Who can open it?

`127.0.0.1` (also called **localhost**) always means *this same computer*, so only you can
open the app. Other people typing that address would reach their own computer, not yours.
`:5000` is the **port** the app listens on.

To share it with colleagues on the same network, the app would need to run on `0.0.0.0`
and they would open your computer's local IP address (e.g. `http://192.168.1.25:5000`).
Sign-in now protects the data, but before sharing also: keep debug mode **off**, get
approval from your IT department, and use a proper server (e.g. `waitress`) instead of
Flask's built-in development server.

## IDs used in the app

| ID | Meaning | Example |
|---|---|---|
| `CH-0001` | Chemical ID | `CH-0002` = Acetone |
| `TX-00001` | Stock movement (transaction) ID | `TX-00027` = one stock in or out |

## Files

| File | What it does |
|---|---|
| `app.py` | The web app: database setup, sign in and roles, alert rules, storage check, all pages and CSV exports |
| `seed.py` | Loads sample data for a demo (erases chemicals and movements) |
| `reset_password.py` | Emergency password reset from the computer itself |
| `templates/base.html` | Page layout and top bar |
| `templates/login.html` | Sign-in page |
| `templates/setup.html` | First-run "create admin account" page |
| `templates/change_password.html` | Change password (also forced at first sign-in) |
| `templates/staff.html` | Staff accounts (admin) |
| `templates/login_log.html` | Sign-in log (admin) |
| `templates/error.html` | "Not allowed" / "Not found" pages |
| `templates/index.html` | Inventory home page |
| `templates/form.html` | Add / edit chemical form |
| `templates/detail.html` | Chemical page with stock in / out and history |
| `templates/stock.html` | Stock in / out page |
| `templates/movement_form.html` | Edit or delete a stock movement |
| `templates/movements.html` | Stock movements dashboard |
| `templates/analysis.html` | Data analysis page |
| `static/style.css` | Styling |
| `requirements.txt` | Python libraries needed (Flask) |
| `chemicals.db` | Your data, created automatically. **Back up this file** to keep your records |
| `.secret_key` | Random key that protects sign-in cookies. Created automatically. **Keep it private** |
| `.gitignore` | Stops `chemicals.db` and `.secret_key` being uploaded if you put the code on GitHub |

## Database

Four tables in `chemicals.db` (SQLite):

- **chemicals**: one row per chemical (identity, hazards, location, current quantity, reorder level, supplier, dates)
- **transactions**: one row per stock in or out (amount, balance after, who recorded it, purpose, reference,
  date & time, and who last edited it and when)
- **users**: staff accounts (Staff ID, name, role, password hash, active, lockout, last sign-in)
- **login_log**: every sign-in attempt, successful or failed

The current quantity is only changed through stock in / out (or by editing a movement),
so every change is traced.
Older databases are upgraded automatically when the app starts.

## Settings you can change (top of `app.py`)

| Setting | Default | Meaning |
|---|---|---|
| `EXPIRY_WARNING_DAYS` | `30` | Warn this many days before expiry |
| `SDS_MAX_AGE_DAYS` | `5 * 365` | Flag SDS older than this |
| `LEAD_TIME_DAYS` | `14` | Days from ordering to receiving stock (for "Order now") |
| `COVER_DAYS` | `60` | Suggested orders should last this many days |
| `INCOMPATIBLE_PAIRS` | flammable / oxidiser / explosive | Hazard pairs that must not share a location |
| `UNITS` | g, kg, mL, L, pcs | Units in the dropdown |
| `MAX_FAILED_LOGINS` | `5` | Wrong passwords before the account locks |
| `LOCKOUT_MINUTES` | `5` | How long a locked account stays locked |
| `SESSION_HOURS` | `8` | Automatic sign-out after this many hours |
| `MIN_PASSWORD_LENGTH` | `8` | Shortest password allowed |

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'flask'` | Run `pip install -r requirements.txt` |
| `Address already in use` / port 5000 busy | Another copy is running. Stop it with Ctrl+C, or close its terminal |
| Page shows old data or no changes | Refresh the browser (Ctrl+F5) |
| Changes to the code don't show up | Stop the app with Ctrl+C and run `python app.py` again (or use debug mode while developing) |
| A staff member forgot their password | Admin: Staff page → **Reset password**, then hand over the temporary password |
| **The admin** forgot their password, or is locked out | On the computer running ChemTrack: `python reset_password.py <Staff ID>` prints a temporary password |
| "This form expired" | The page was open too long or you signed out in another tab. Refresh and try again |
| "Not allowed" page | Your role can't do that. Ask an admin to change your role on the Staff page |
| Want a fresh start | Stop the app and run `python seed.py`, or delete `chemicals.db` for an empty database (this also removes all accounts) |

## Ideas for next versions
- QR code labels for each bottle (`qrcode` library): scan to open stock in / out
- Upload and attach SDS PDF files
- Email reminders for low stock and expiring chemicals
- Monthly usage report as PDF
- Waste disposal log (scheduled waste SW codes)
- Run with `waitress` for sharing on the office network

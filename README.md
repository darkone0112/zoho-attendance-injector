# zoho-attendance-injector

Cookie-based Zoho People attendance filler using Playwright.

## Setup
1. Create/activate a Python venv.
2. Install dependencies:

```bash
pip install -r requirements.txt
playwright install
```

## Usage
Run and choose from the menu:

```bash
python zoho_attendance.py
```

### Test cookies (opens a headed browser)
Use the menu option **Test cookie login**, or run:

```bash
python zoho_attendance.py --action test-cookies --headed
```

If you want to use a specific browser channel (e.g. system Chrome):

```bash
python zoho_attendance.py --action test-cookies --browser-channel chrome
```

If Zoho shows a **Sign in with Microsoft** button, the script will click it and wait for you to complete the login in the opened browser.

### Fill a date range
Use the menu option **Fill a date range**, or run:

```bash
python zoho_attendance.py --action fill-range \
  --cookies data/zoho-cookies.json \
  --start-date 2026-02-09 \
  --end-date 2026-02-15 \
  --check-in 09:00 \
  --check-out 18:00 \
  --headed
```

By default weekends are skipped. To include weekends:

```bash
python zoho_attendance.py --action fill-range --include-weekends
```

## Notes
- The cookie file must be a JSON array like `data/zoho-cookies.json`.
- Times must be 24h `HH:mm`.
- The script uses Zoho's in-page `Attendance.Entry` API. If the UI changes, selectors may need updates in `zoho_attendance.py`.
- Flow details are documented in `ATTENDANCE_FLOW.md`.

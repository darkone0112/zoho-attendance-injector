# zoho-attendance-injector

Cookie-injected Playwright automation for Zoho People attendance and On Duty (WFH) requests.

Built for scraping practice, DOM automation drills, and absolutely not because some modern companies still run attendance like it is a sacred 2009 ritual of daily manual clicks.

## What This Repo Contains
- `zoho_attendance.py`: main CLI app (menu, browser automation, date logic, retries, logging).
- `ATTENDANCE_FLOW.md`: flow notes for the attendance UI.
- `data/`: local runtime inputs (`zoho-cookies.json`, `remote-work-config.json`).
- `logs/`: run logs (`zoho_attendance.log`, reset on each run).
- `requirements.txt`: Python dependencies.

## Requirements
- Python `3.10+` (tested in this repo with Python `3.13`).
- Linux/macOS shell (examples use `bash`).
- Internet access to Zoho.
- Valid Zoho session cookies exported to JSON.

## Setup
1. Create a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
```
2. Install Python deps:
```bash
pip install -r requirements.txt
```
3. Install Playwright browser runtime:
```bash
playwright install chromium
```
4. Add cookies file:
```text
data/zoho-cookies.json
```
5. Run:
```bash
python zoho_attendance.py
```

## Cookie File Format
`data/zoho-cookies.json` must be a JSON array of cookie objects (name, value, domain, etc.).  
If cookies are expired, Zoho will redirect to login and the script will tell you with zero mercy.

## Interactive Menu
The app loops back to the menu after each action.

1. `Fill Attendance`
2. `Test Cookies`
3. `Add Remote Work`
4. `Remote Config`
5. `Exit`

### 1) Fill Attendance
- Fills check-in/check-out entries per day in a selected range.
- Skips weekends by default.
- Navigates week-by-week in Zoho until target range is covered.
- Uses robust popup/input handling and verifies time values before save.
- Applies summer schedule automatically from June 15 through September 15, inclusive:
  - Check-out is one hour earlier than the configured `--check-out`.
  - Example: `18:00` becomes `17:00` for summer dates.

Range selection behavior:
- If no CLI dates were passed, it asks:
  - `Fill current week (Mon..Fri)?`
- If `yes`: uses current week Monday to Friday.
- If `no`: asks start date, then suggests end date as that start date's Friday.
- If user just presses Enter on prompts: defaults are used.

### 2) Test Cookies
- Always opens a headed browser.
- Injects cookies and opens attendance page.
- If Microsoft SSO is shown, it tries clicking it and waits for manual completion.
- Good for validating that cookie export still works before running actual actions.

### 3) Add Remote Work
- Submits On Duty requests with reason `WFH`.
- Uses same range selection flow as option 1.
- For each configured remote weekday in the range, it submits one single-day request:
  - Start date = End date = target day.
- Includes retry+reload protection: if one day fails, it retries that same day before moving on.

### 4) Remote Config
- Saves defaults in `data/remote-work-config.json`.
- Lets user choose:
  - Start weekday (`Mon`..`Fri`)
  - Number of remote days/week
- Days are contiguous from start day.
- Example: `Mon + 3` => `Mon, Tue, Wed`.
- Default when file is missing:
```json
{
  "start_weekday": "Mon",
  "days_per_week": 1
}
```

## CLI Usage

### Run menu
```bash
python zoho_attendance.py
```

### Run a specific action (skip menu)
```bash
python zoho_attendance.py --action fill-range
python zoho_attendance.py --action test-cookies
python zoho_attendance.py --action remote-work
python zoho_attendance.py --action config
```

### Example: fill attendance directly
```bash
python zoho_attendance.py \
  --action fill-range \
  --cookies data/zoho-cookies.json \
  --start-date 2026-02-09 \
  --end-date 2026-02-13 \
  --check-in 09:00 \
  --check-out 18:00 \
  --headed
```

## Parameters Reference

| Parameter | Default | Used by | What it does |
|---|---|---|---|
| `--cookies` | `data/zoho-cookies.json` | fill, test, remote | Cookie JSON path. |
| `--start-date` | interactive | fill, remote | Range start (`YYYY-MM-DD`). |
| `--end-date` | interactive | fill, remote | Range end (`YYYY-MM-DD`). |
| `--check-in` | prompt default `09:00` | fill | Check-in time (`HH:mm`). |
| `--check-out` | prompt default `18:00` | fill | Standard check-out time (`HH:mm`); summer dates use one hour earlier. |
| `--exclude-weekends` | `false` | fill | Explicitly skip Sat/Sun. |
| `--include-weekends` | `false` | fill | Include Sat/Sun (unless both weekend flags are set; exclude wins). |
| `--headed` | `false` | fill, remote | Run browser with UI. |
| `--browser-channel` | `None` | all browser actions | Browser channel (`chrome`, `msedge`, `chromium`). |
| `--slow-mo` | `0` | fill, test, remote | Playwright slow motion in ms. |
| `--step-delay` | `0.6` | fill, remote | Delay between major UI steps. |
| `--nav-step-delay` | `0.8` | fill | Delay between week navigation actions. |
| `--nav-timeout` | `6.0` | fill | Timeout for week/range navigation reads. |
| `--popup-timeout` | `8.0` | fill, remote | Timeout waiting for popups/inputs/modals. |
| `--max-nav-steps` | `80` | fill | Safety cap while navigating weeks. |
| `--dry-run` | `false` | fill, remote | Navigate and fill fields without final submit/save clicks. |
| `--debug` | `false` | fill, remote | Extra runtime diagnostics. |
| `--attendance-url` | Zoho attendance URL in code | all browser actions | Override target attendance URL if needed. |
| `--action` | `None` | CLI | Action selector: `fill-range`, `test-cookies`, `remote-work`, `config`. |

## Logging
- Log file: `logs/zoho_attendance.log`
- Behavior: file is cleared at each run, then all actions are appended.
- Includes click-level traces for critical On Duty date-picker operations.

## Troubleshooting
- Redirected to login page:
  - Cookies are stale/invalid, or SSO step is required.
  - Run option 2 (`Test Cookies`) in headed mode and complete login manually.
- Microsoft login required in headless:
  - Use `--headed` (headless cannot click your keyboard for you).
- Dates/times not filling:
  - Increase `--step-delay` and `--popup-timeout`.
  - Keep UI headed while tuning selectors/behavior.

## Disclaimer
Use this only where you are authorized to automate.  
This repo exists for scraping practice, DOM resilience drills, and definitely not because repetitive admin tasks are soul-draining.

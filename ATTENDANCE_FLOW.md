# Zoho Attendance Fill Flow

This document describes the exact sequence used to fill attendance for a user-specified date range.

## Inputs
- Start date (YYYY-MM-DD)
- End date (YYYY-MM-DD)
- Check-in time (HH:mm)
- Standard check-out time (HH:mm)

Summer schedule:
- From June 15 through September 15, inclusive, the check-out time is one hour earlier.
- Example: a standard check-out of `18:00` is filled as `17:00` on those dates.

## Page State Assumptions
- The page always opens on the **current week**.
- Only one week is visible at a time, with 7 days in the table.
- The current week range is shown in `#ZPAtt_entryNavigation`, e.g.:
  - `<b>09-Feb-2026 - 15-Feb-2026</b>`
  - `aria-label="... Currently selected date range:09-Feb-2026 - 15-Feb-2026"`

## Algorithm
1. Open the attendance page and wait for `#ZPAtt_entryNavigation` to be present.
2. Read the currently displayed week range from `#ZPAtt_entryNavigation`.
3. If the **start date** is not inside the displayed week:
   - Click **Previous**: `<i class="PI_alft" onclick="Attendance.Entry.setMonthsNavigation(1)">` until the week contains the start date, or
   - Click **Next**: `<i class="PI_argt" onclick="Attendance.Entry.setMonthsNavigation(-1)">` if the start date is after the displayed week.
4. Once the week containing the start date is displayed:
   - Fill days from `max(start_date, week_start)` to `min(end_date, week_end)`.
   - Skip weekends (Saturday/Sunday).
5. For each day to fill:
   - Click the day row (the row with `onclick="Attendance.Entry.consEntriesPopup(this)"`).
   - In the popup, click **Add Check-in / Check-out Entry**.
   - Fill **check-in** and date-adjusted **check-out** time inputs (HH:mm 24h).
   - Click **Save** (`Attendance.Entry.updateEntry(...)`).
6. After finishing the visible week, move to the **next week** and repeat step 4.
7. Stop when the end date has been filled.

## Notes
- The week navigation is always based on the header range shown in `#ZPAtt_entryNavigation`.
- The script never attempts to fill days outside the user-provided start/end range.

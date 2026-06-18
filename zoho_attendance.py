#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_URL = "https://people.zoho.eu/20096346973/"
ATTENDANCE_URL = "https://people.zoho.eu/20096346973/zp#attendance/entry/summary-mode:list"
LOG_PATH = Path("logs/zoho_attendance.log")
REMOTE_CONFIG_PATH = Path("data/remote-work-config.json")
WORKWEEK_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

DEFAULTS = {
    "include_weekends": False,
    "step_delay": 0.6,
    "nav_step_delay": 0.8,
    "nav_timeout": 6.0,
    "popup_timeout": 8.0,
    "max_nav_steps": 80,
}

SUMMER_WORKDAY_START = (6, 15)
SUMMER_WORKDAY_END = (9, 15)
SUMMER_CHECK_OUT_OFFSET_HOURS = -1


@dataclass
class Settings:
    cookies_path: Path
    start_date: date
    end_date: date
    check_in: str
    check_out: str
    include_weekends: bool
    headless: bool
    slow_mo: int
    step_delay: float
    nav_step_delay: float
    nav_timeout: float
    popup_timeout: float
    max_nav_steps: int
    dry_run: bool
    attendance_url: str
    browser_channel: Optional[str]
    debug: bool


@dataclass
class RemoteWorkSettings:
    cookies_path: Path
    start_date: date
    end_date: date
    headless: bool
    slow_mo: int
    step_delay: float
    popup_timeout: float
    dry_run: bool
    attendance_url: str
    browser_channel: Optional[str]
    debug: bool
    remote_weekdays: List[int]


def init_run_log(mode: str, start_date: Optional[date] = None, end_date: Optional[date] = None) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as f:
        f.write("")
    log_action(f"RUN START mode={mode}")
    if start_date and end_date:
        log_action(f"RANGE start={start_date.isoformat()} end={end_date.isoformat()}")


def log_action(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def parse_time(value: str) -> str:
    if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", value):
        raise ValueError("Time must be in HH:mm (24h) format.")
    return value


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Date must be in YYYY-MM-DD format.") from exc


def parse_zoho_date(value: str) -> date:
    match = re.match(r"^(\d{2})-([A-Za-z]{3})-(\d{4})$", value.strip())
    if not match:
        raise ValueError(f"Invalid Zoho date: {value}")
    day = int(match.group(1))
    month = MONTHS.get(match.group(2))
    year = int(match.group(3))
    if not month:
        raise ValueError(f"Unknown month in date: {value}")
    return date(year, month, day)


def format_zoho_date(value: date) -> str:
    month = list(MONTHS.keys())[list(MONTHS.values()).index(value.month)]
    return f"{value.day:02d}-{month}-{value.year}"


def enumerate_days(start: date, end: date) -> List[date]:
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def is_summer_workday_schedule(day: date) -> bool:
    start = date(day.year, *SUMMER_WORKDAY_START)
    end = date(day.year, *SUMMER_WORKDAY_END)
    return start <= day <= end


def shift_time(value: str, hours: int) -> str:
    parsed_hours, parsed_minutes = map(int, parse_time(value).split(":"))
    total_minutes = (parsed_hours * 60 + parsed_minutes + hours * 60) % (24 * 60)
    shifted_hours, shifted_minutes = divmod(total_minutes, 60)
    return f"{shifted_hours:02d}:{shifted_minutes:02d}"


def check_out_for_day(day: date, standard_check_out: str) -> str:
    if is_summer_workday_schedule(day):
        return shift_time(standard_check_out, SUMMER_CHECK_OUT_OFFSET_HOURS)
    return standard_check_out


def load_cookies(path: Path) -> List[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cookies: List[dict] = []
    for item in raw:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not name or value is None or not domain:
            continue

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": item.get("path") or "/",
            "secure": bool(item.get("secure")),
            "httpOnly": bool(item.get("httpOnly")),
        }

        same_site = item.get("sameSite")
        if same_site:
            same_site = same_site.lower()
            if same_site == "strict":
                cookie["sameSite"] = "Strict"
            elif same_site == "lax":
                cookie["sameSite"] = "Lax"
            elif same_site in ("none", "no_restriction"):
                cookie["sameSite"] = "None"

        if not item.get("session") and item.get("expirationDate"):
            cookie["expires"] = float(item.get("expirationDate"))

        cookies.append(cookie)

    return cookies


def load_remote_work_config(path: Path = REMOTE_CONFIG_PATH) -> dict:
    default = {"start_weekday": "Mon", "days_per_week": 1}
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(raw, dict):
        return default

    start_weekday = str(raw.get("start_weekday", default["start_weekday"]))
    days_per_week = raw.get("days_per_week", default["days_per_week"])
    if start_weekday not in WORKWEEK_NAMES:
        start_weekday = default["start_weekday"]
    try:
        days_per_week = int(days_per_week)
    except (TypeError, ValueError):
        days_per_week = default["days_per_week"]

    max_days = 5 - WORKWEEK_NAMES.index(start_weekday)
    days_per_week = max(1, min(days_per_week, max_days))
    return {"start_weekday": start_weekday, "days_per_week": days_per_week}


def save_remote_work_config(config: dict, path: Path = REMOTE_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def remote_weekday_indices(config: dict) -> List[int]:
    start = WORKWEEK_NAMES.index(config["start_weekday"])
    count = int(config["days_per_week"])
    return list(range(start, start + count))




def current_workweek_range(today: Optional[date] = None) -> Tuple[date, date]:
    if today is None:
        today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=4)
    return week_start, week_end


def workweek_end_for(day: date) -> date:
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=4)
    # If user selects weekend, keep end at least on/after start to avoid invalid defaults.
    if week_end < day:
        return day
    return week_end


def prompt_for_range(console: Console, default_start: date, default_end: date) -> Tuple[date, date]:
    start_raw = Prompt.ask("Start date (YYYY-MM-DD)", default=default_start.isoformat())
    start = parse_iso_date(start_raw)
    computed_default_end = workweek_end_for(start)
    end_raw = Prompt.ask("End date (YYYY-MM-DD)", default=computed_default_end.isoformat())
    end = parse_iso_date(end_raw)
    if end < start:
        raise ValueError("End date must be on or after start date.")
    return start, end


def resolve_range_from_args(args: argparse.Namespace, console: Console) -> Tuple[date, date]:
    current_week_start, current_week_end = current_workweek_range()

    if args.start_date and args.end_date:
        start = parse_iso_date(args.start_date)
        end = parse_iso_date(args.end_date)
    elif args.start_date or args.end_date:
        start_raw = args.start_date or Prompt.ask("Start date (YYYY-MM-DD)", default=current_week_start.isoformat())
        start = parse_iso_date(start_raw)
        end_default = workweek_end_for(start)
        end_raw = args.end_date or Prompt.ask("End date (YYYY-MM-DD)", default=end_default.isoformat())
        end = parse_iso_date(end_raw)
    else:
        fill_current_week = Confirm.ask(
            f"Fill current week ({current_week_start.isoformat()} to {current_week_end.isoformat()})?",
            default=True,
        )
        if fill_current_week:
            start, end = current_week_start, current_week_end
        else:
            start, end = prompt_for_range(console, current_week_start, current_week_end)

    if end < start:
        raise ValueError("End date must be on or after start date.")
    return start, end


def prompt_for_time(console: Console, label: str, default_value: str) -> str:
    value = Prompt.ask(label, default=default_value)
    return parse_time(value)


def _extract_range(text: str) -> Optional[Tuple[date, date]]:
    cleaned = (text or "").replace("\u00a0", " ").strip()
    matches = re.findall(r"\d{1,2}-[A-Za-z]{3}-\d{4}", cleaned)
    if len(matches) < 2:
        return None
    return parse_zoho_date(matches[0]), parse_zoho_date(matches[1])


def _range_candidates(scope) -> List[str]:
    script = """
    () => {
      const el = document.querySelector('#ZPAtt_entryNavigation');
      if (!el) return null;
      const aria = el.getAttribute('aria-label') || '';
      const bold = el.querySelector('b');
      const text = (bold || el).innerText || '';
      const content = el.textContent || '';
      return [aria, text, content].filter(Boolean);
    }
    """
    return scope.evaluate(script) or []


def get_range_text(scope) -> str:
    candidates = _range_candidates(scope)
    if not candidates:
        return ""
    return candidates[0].replace("\u00a0", " ").strip()


def get_displayed_range(scope) -> Tuple[date, date]:
    candidates = _range_candidates(scope)
    if not candidates:
        raise RuntimeError("Week navigation element not found.")

    for text in candidates:
        parsed = _extract_range(text)
        if parsed:
            return parsed

    raise RuntimeError(f"Unable to parse week range from: {candidates[0] if candidates else ''}")


def wait_for_week_range(scope, timeout_s: float) -> Tuple[date, date]:
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            return get_displayed_range(scope)
        except RuntimeError:
            time.sleep(0.2)
    raise PlaywrightTimeoutError("Week range not ready")


def navigate_to_week(scope, target: date, settings: Settings, console: Console) -> bool:
    for _ in range(settings.max_nav_steps):
        try:
            start, end = wait_for_week_range(scope, settings.nav_timeout)
        except (PlaywrightTimeoutError, RuntimeError):
            console.log("[red]Week range not ready yet. Retrying...[/red]")
            time.sleep(settings.nav_step_delay)
            continue
        if settings.debug:
            console.log(f"[debug] Header: {get_range_text(scope)}")
            console.log(f"[debug] Parsed range: {format_zoho_date(start)} - {format_zoho_date(end)} | Target: {format_zoho_date(target)}")
        if start <= target <= end:
            return True

        prev_text = get_range_text(scope)
        direction = 1 if target < start else -1
        if settings.debug:
            console.log(f"[debug] Navigating {'previous' if direction == 1 else 'next'} week")
        scope.evaluate("dir => Attendance.Entry.setMonthsNavigation(dir)", direction)
        time.sleep(settings.nav_step_delay)

        try:
            scope.wait_for_function(
                "prev => {"
                "  const el = document.querySelector('#ZPAtt_entryNavigation');"
                "  if (!el) return false;"
                "  const text = (el.querySelector('b') || el).innerText.replace(/\\u00a0/g, ' ').trim();"
                "  return text && text !== prev;"
                "}",
                arg=prev_text,
                timeout=int(settings.nav_timeout * 1000),
            )
        except PlaywrightTimeoutError:
            console.log("[red]Week navigation timed out.[/red]")
            return False

    console.log("[red]Reached max week navigation steps.[/red]")
    return False


def wait_for_day_rows(scope, timeout_s: float) -> None:
    scope.wait_for_function(
        "() => document.querySelectorAll('tr[onclick*=\"consEntriesPopup\"]').length > 0",
        timeout=int(timeout_s * 1000),
    )


def click_day_row(scope, target: date, range_start: Optional[date]) -> bool:
    offset = None
    if range_start:
        offset = (target - range_start).days

    payload = {
        "weekday": WEEKDAYS[target.weekday()],
        "day": target.day,
        "dayPadded": f"{target.day:02d}",
        "formatted": format_zoho_date(target),
        "iso": target.isoformat(),
        "offset": offset,
    }
    script = """
    (args) => {
      const rows = Array.from(document.querySelectorAll('tr[onclick*="consEntriesPopup"]'));
      if (rows.length === 0) return false;

      if (typeof args.offset === 'number' && args.offset >= 0 && args.offset < rows.length) {
        const row = rows[args.offset];
        row.scrollIntoView({ block: 'center' });
        row.click();
        return true;
      }

      for (const row of rows) {
        const attrs = [row.getAttribute('aria-label'), row.getAttribute('title'),
          row.getAttribute('data-date'), row.dataset?.date].filter(Boolean).join(' ');
        const text = `${row.textContent || ''} ${attrs}`;
        if (text.includes(args.formatted) || text.includes(args.iso) ||
            (text.includes(args.weekday) && (new RegExp('\\\\b' + args.dayPadded + '\\\\b').test(text) ||
                                             new RegExp('\\\\b' + args.day + '\\\\b').test(text)))) {
          row.scrollIntoView({ block: 'center' });
          row.click();
          return true;
        }
      }
      return false;
    }
    """
    return bool(scope.evaluate(script, payload))


def wait_for_popup_scope(page, settings: Settings):
    start = time.time()
    selector = "#ZPAtt_entry_allEntriesList"
    while time.time() - start < settings.popup_timeout:
        if _has_selector(page, selector):
            return page
        for frame in page.frames:
            if _has_selector(frame, selector):
                return frame
        time.sleep(0.2)
    return None


def wait_for_add_button(scope, settings: Settings) -> Optional[object]:
    try:
        locator = scope.locator(
            "#ZPAtt_entry_allEntriesList div.zpl_lnkbg",
            has_text=re.compile(r"Add Check-in\s*/\s*Check-out Entry", re.I),
        )
        locator.wait_for(timeout=int(settings.popup_timeout * 1000))
        return locator
    except PlaywrightError:
        return None


def _scope_has_time_inputs(scope) -> bool:
    try:
        root = "#ZPAtt_entry_allEntriesList"
        if scope.query_selector(f'{root} [id^="ZPAtt_entry_editFromTime"][id$="-container"] input') and scope.query_selector(
            f'{root} [id^="ZPAtt_entry_editToTime"][id$="-container"] input'
        ):
            return True
        if scope.query_selector(f'{root} #ZPAtt_entry_editFromTime-container input') and scope.query_selector(
            f'{root} #ZPAtt_entry_editToTime-container input'
        ):
            return True
        hhmm = scope.query_selector_all(f'{root} input[placeholder="hh:mm"]')
        if len(hhmm) >= 2:
            return True
    except PlaywrightError:
        return False
    return False


def wait_for_time_inputs(scope, settings: Settings) -> bool:
    start = time.time()
    while time.time() - start < settings.popup_timeout:
        if _scope_has_time_inputs(scope):
            return True
        time.sleep(0.2)
    return False


def _resolve_time_inputs(scope):
    root = "#ZPAtt_entry_allEntriesList"
    from_sel = f'{root} [id^="ZPAtt_entry_editFromTime"][id$="-container"] input.zinputfield__textbox'
    to_sel = f'{root} [id^="ZPAtt_entry_editToTime"][id$="-container"] input.zinputfield__textbox'
    from_loc = scope.locator(from_sel)
    to_loc = scope.locator(to_sel)
    if from_loc.count() > 0 and to_loc.count() > 0:
        return from_loc.first, to_loc.first

    from_sel = f'{root} #ZPAtt_entry_editFromTime-container input.zinputfield__textbox'
    to_sel = f'{root} #ZPAtt_entry_editToTime-container input.zinputfield__textbox'
    from_loc = scope.locator(from_sel)
    to_loc = scope.locator(to_sel)
    if from_loc.count() > 0 and to_loc.count() > 0:
        return from_loc.first, to_loc.first

    hhmm = scope.locator(f'{root} input[placeholder="hh:mm"]')
    if hhmm.count() >= 2:
        return hhmm.nth(0), hhmm.nth(1)

    return None, None


def _normalize_time(value: str) -> str:
    return value.replace(" ", "")


def _parse_time_value(value: str) -> Optional[Tuple[int, int]]:
    match = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$", value or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _time_matches(value: str, target_hours: int, target_minutes: int) -> bool:
    parsed = _parse_time_value(_normalize_time(value))
    if not parsed:
        return False
    return parsed == (target_hours, target_minutes)


def _type_time(locator, value: str, prefer_space: bool = False) -> bool:
    hours, minutes = value.split(":")

    def _read_value() -> str:
        try:
            return _normalize_time(locator.input_value())
        except PlaywrightError:
            return ""

    def _click_left():
        try:
            box = locator.bounding_box()
            if box:
                locator.click(position={"x": max(2, box["width"] * 0.2), "y": box["height"] / 2})
                return
        except PlaywrightError:
            pass
        locator.click()

    def _method_arrow() -> None:
        _click_left()
        locator.press("Home")
        locator.type(hours, delay=80)
        locator.press("ArrowRight")
        locator.press("ArrowRight")
        locator.type(minutes, delay=80)
        locator.press("Enter")

    def _method_space() -> None:
        _click_left()
        locator.press("Home")
        locator.press(hours[0])
        locator.press("ArrowRight")
        locator.press(hours[1])
        locator.press("Space")
        locator.press(minutes[0])
        locator.press(minutes[1])
        locator.press("Enter")

    methods = [_method_space, _method_arrow] if prefer_space else [_method_arrow, _method_space]
    for _ in range(3):
        for method in methods:
            try:
                method()
            except PlaywrightError:
                pass
            if _read_value() == value:
                return True
        time.sleep(0.2)

    # Fallback: direct set + events.
    try:
        locator.evaluate(
            "(el, val) => {"
            "  el.value = val;"
            "  el.dispatchEvent(new Event('input', { bubbles: true }));"
            "  el.dispatchEvent(new Event('change', { bubbles: true }));"
            "  el.dispatchEvent(new Event('blur', { bubbles: true }));"
            "}",
            value,
        )
    except PlaywrightError:
        pass

    return _read_value() == value


def _set_time_by_arrows(locator, value: str) -> bool:
    target_hours, target_minutes = map(int, value.split(":"))
    if target_minutes != 0:
        return False

    def _read_value() -> str:
        try:
            return _normalize_time(locator.input_value())
        except PlaywrightError:
            return ""

    def _select_segment(segment: str) -> None:
        # segment: "hh" or "mm"
        try:
            locator.evaluate(
                "(el, seg) => {"
                "  const start = seg === 'hh' ? 0 : 3;"
                "  const end = seg === 'hh' ? 2 : 5;"
                "  el.focus();"
                "  if (el.setSelectionRange) {"
                "    el.setSelectionRange(start, end);"
                "  }"
                "}",
                segment,
            )
        except PlaywrightError:
            pass

    def _focus_hours():
        try:
            locator.click()
        except PlaywrightError:
            return
        _select_segment("hh")

    _focus_hours()
    for _ in range(target_hours + 1):
        try:
            locator.press("ArrowUp")
        except PlaywrightError:
            return False
        time.sleep(0.04)

    try:
        locator.press("Space")
        _select_segment("mm")
        locator.press("ArrowUp")
    except PlaywrightError:
        return False

    try:
        locator.press("Enter")
    except PlaywrightError:
        pass

    return _time_matches(_read_value(), target_hours, 0)


def fill_time_entries(scope, check_in: str, check_out: str, dry_run: bool) -> bool:
    in_loc, out_loc = _resolve_time_inputs(scope)
    if not in_loc or not out_loc:
        return False

    if dry_run:
        return True

    ok_in = _set_time_by_arrows(in_loc, check_in)
    ok_out = _set_time_by_arrows(out_loc, check_out)
    if not ok_in or not ok_out:
        return False
    if not ok_in or not ok_out:
        return False

    try:
        save = scope.locator(
            '#ZPAtt_entry_allEntriesList button',
            has_text=re.compile(r"save", re.I),
        )
        if save.count() == 0:
            save = scope.locator('button[onclick*="updateEntry"]')
        if save.count() > 0:
            save.first.click()
            return True
    except PlaywrightError:
        return False
    return False


def _has_selector(scope, selector: str) -> bool:
    try:
        return scope.query_selector(selector) is not None
    except PlaywrightError:
        return False


def _find_scope(page, selector: str):
    if _has_selector(page, selector):
        return page
    for frame in page.frames:
        if _has_selector(frame, selector):
            return frame
    return None


def wait_for_scope(page, selector: str, timeout: float):
    start = time.time()
    while time.time() - start < timeout:
        scope = _find_scope(page, selector)
        if scope:
            return scope
        time.sleep(0.2)
    return None


def _any_scope_has(page, selector: str) -> bool:
    if _has_selector(page, selector):
        return True
    for frame in page.frames:
        if _has_selector(frame, selector):
            return True
    return False


def is_login_page(page) -> bool:
    selectors = [
        "input#login_id",
        "input[name=\"LOGIN_ID\"]",
        "input#email",
        "input[type=\"email\"]",
        "form[action*=\"signin\"]",
        "form[action*=\"login\"]",
    ]
    return any(_any_scope_has(page, sel) for sel in selectors)


def is_federated_login_page(page) -> bool:
    selectors = [
        "span.fed_div.small_box.MS_icon[title*=\"Microsoft\"]",
    ]
    return any(_any_scope_has(page, sel) for sel in selectors)


def try_microsoft_login(page, console: Console) -> bool:
    selector = "span.fed_div.small_box.MS_icon[title*=\"Microsoft\"]"
    scope = wait_for_scope(page, selector, 10.0)
    if not scope:
        console.log("[red]Microsoft login button not found.[/red]")
        return False

    try:
        scope.locator(selector).first.click()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightError:
        return False
    except PlaywrightTimeoutError:
        pass
    return True


def navigate_next_week(scope, settings: Settings, console: Console) -> bool:
    prev_text = get_range_text(scope)
    scope.evaluate("Attendance.Entry.setMonthsNavigation(-1)")
    time.sleep(settings.nav_step_delay)
    try:
        scope.wait_for_function(
            "prev => {"
            "  const el = document.querySelector('#ZPAtt_entryNavigation');"
            "  if (!el) return false;"
            "  const aria = el.getAttribute('aria-label') || '';"
            "  const bold = el.querySelector('b');"
            "  const text = (bold || el).innerText.replace(/\\u00a0/g, ' ').trim();"
            "  const current = text || aria.replace(/\\u00a0/g, ' ');"
            "  return current && current !== prev;"
            "}",
            arg=prev_text,
            timeout=int(settings.nav_timeout * 1000),
        )
    except PlaywrightTimeoutError:
        console.log("[red]Next-week navigation timed out.[/red]")
        return False
    return True


def test_cookies_login(
    console: Console,
    cookies_path: Path,
    attendance_url: str,
    browser_channel: Optional[str],
) -> int:
    init_run_log("test-cookies")
    cookies = load_cookies(cookies_path)
    console.log("Opening Zoho People with injected cookies...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=0, channel=browser_channel)
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.goto(attendance_url, wait_until="domcontentloaded")

        scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 15.0)
        if not scope and is_federated_login_page(page):
            console.log("[yellow]Microsoft login required. Attempting click...[/yellow]")
            clicked = try_microsoft_login(page, console)
            if not clicked:
                console.log("[yellow]Please click 'Sign in with Microsoft' in the opened browser.[/yellow]")
            scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 180.0)

        if scope:
            console.log("[green]Attendance page loaded. Cookies appear valid.[/green]")
        else:
            if is_login_page(page):
                console.log("[red]Login page detected. Cookies are likely expired.[/red]")
            if is_federated_login_page(page):
                console.log("[yellow]Microsoft login still required. Complete it in the browser.[/yellow]")
            console.log(f"[yellow]Attendance navigation not found. Current URL: {page.url}[/yellow]")

        console.input("Press Enter to close the browser...")
        browser.close()

    return 0


def run(settings: Settings) -> int:
    console = Console()
    init_run_log("fill-range", settings.start_date, settings.end_date)

    cookies = load_cookies(settings.cookies_path)
    console.log(
        f"Filling days from {format_zoho_date(settings.start_date)} to {format_zoho_date(settings.end_date)}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=settings.headless,
            slow_mo=settings.slow_mo,
            channel=settings.browser_channel,
        )
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.goto(settings.attendance_url, wait_until="domcontentloaded")

        scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 15.0)
        if not scope and is_federated_login_page(page):
            console.log("[yellow]Microsoft login required. Attempting click...[/yellow]")
            clicked = try_microsoft_login(page, console)
            if not clicked:
                console.log("[yellow]Please click 'Sign in with Microsoft' in the opened browser.[/yellow]")
            scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 180.0)

        if not scope:
            if is_login_page(page):
                console.log("[red]Login page detected. Cookies may be expired or invalid.[/red]")
            if is_federated_login_page(page):
                console.log("[yellow]Microsoft login still required. Complete it in the browser.[/yellow]")
                if settings.headless:
                    console.log("[yellow]Headless mode cannot complete interactive login. Rerun with --headed.[/yellow]")
            console.log(f"[red]Attendance navigation not found. Current URL: {page.url}[/red]")
            browser.close()
            return 1

        scope.wait_for_function(
            "() => window.Attendance && Attendance.Entry && Attendance.Entry.setMonthsNavigation",
            timeout=15000,
        )

        if not navigate_to_week(scope, settings.start_date, settings, console):
            console.log(f"[red]Failed to reach week for {format_zoho_date(settings.start_date)}[/red]")
            browser.close()
            return 1

        while True:
            try:
                week_start, week_end = wait_for_week_range(scope, settings.nav_timeout)
            except (PlaywrightTimeoutError, RuntimeError):
                console.log("[red]Unable to read current week range.[/red]")
                break

            if settings.debug:
                console.log(f"[debug] Active week: {format_zoho_date(week_start)} - {format_zoho_date(week_end)}")

            range_start = max(settings.start_date, week_start)
            range_end = min(settings.end_date, week_end)

            try:
                wait_for_day_rows(scope, settings.popup_timeout)
            except PlaywrightTimeoutError:
                console.log(f"[red]Day rows not loaded for {format_zoho_date(range_start)}[/red]")
                break

            for day in enumerate_days(range_start, range_end):
                if not settings.include_weekends and day.weekday() in (5, 6):
                    continue

                clicked = False
                for attempt in range(3):
                    clicked = click_day_row(scope, day, week_start)
                    if clicked:
                        break
                    time.sleep(0.4)

                if not clicked:
                    console.log(f"[red]Day row not found for {format_zoho_date(day)}[/red]")
                    continue

                popup_scope = wait_for_popup_scope(page, settings)
                if not popup_scope:
                    console.log(f"[red]Popup container not found for {format_zoho_date(day)}[/red]")
                    continue

                add_button = wait_for_add_button(popup_scope, settings)
                if not add_button:
                    console.log(f"[red]Add entry button not found for {format_zoho_date(day)}[/red]")
                    continue

                if not settings.dry_run:
                    add_button.click()

                time.sleep(settings.step_delay)

                if not wait_for_time_inputs(popup_scope, settings):
                    console.log(f"[red]Time inputs not found for {format_zoho_date(day)}[/red]")
                    continue

                check_out = check_out_for_day(day, settings.check_out)
                if settings.debug and check_out != settings.check_out:
                    console.log(
                        f"[debug] Summer schedule for {format_zoho_date(day)}: "
                        f"check-out {settings.check_out} -> {check_out}"
                    )

                ok = fill_time_entries(popup_scope, settings.check_in, check_out, settings.dry_run)
                if not ok:
                    console.log(f"[red]Failed to fill inputs for {format_zoho_date(day)}[/red]")
                else:
                    console.log(f"[green]Filled {format_zoho_date(day)} ({WEEKDAYS[day.weekday()]})[/green]")

                time.sleep(settings.step_delay)

            if settings.end_date <= week_end:
                break

            if not navigate_next_week(scope, settings, console):
                break

        browser.close()

    return 0


def remote_dates_in_range(start_date: date, end_date: date, weekday_indices: List[int]) -> List[date]:
    allowed = set(weekday_indices)
    days: List[date] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() in allowed:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def wait_for_on_duty_modal(scope, timeout_s: float) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        if _has_selector(scope, "#odStartDate-container input.zinputfield__textbox") and _has_selector(
            scope, "#odEndDate-container input.zinputfield__textbox"
        ):
            return True
        time.sleep(0.2)
    return False


def _month_from_picker(scope, picker_id: str) -> Optional[Tuple[int, int]]:
    log_action(f"PICKER READ month/year picker_id={picker_id}")
    try:
        result = scope.evaluate(
            "(pickerId) => {"
            "  const picker = document.getElementById(pickerId);"
            "  if (!picker) return null;"
            "  const m = picker.querySelector('.zdatetimepicker__monthnav');"
            "  const y = picker.querySelector('.zdatetimepicker__yearnav');"
            "  if (!m || !y) return null;"
            "  return { month: (m.textContent || '').trim(), year: Number((y.textContent || '').trim()) };"
            "}",
            picker_id,
        )
    except PlaywrightError:
        return None
    if not result:
        return None
    month_text = str(result.get("month", ""))[:3]
    year_value = int(result.get("year", 0))
    month_value = MONTHS.get(month_text)
    if not month_value:
        log_action(f"PICKER month parse failed month_text={month_text} picker_id={picker_id}")
        return None
    log_action(f"PICKER current month={month_value} year={year_value} picker_id={picker_id}")
    return month_value, year_value


def set_onduty_date(scope, container_id: str, target_day: date, timeout_s: float) -> bool:
    picker_id = f"{container_id}-picker"
    target_text = format_zoho_date(target_day)
    target_month = target_day.month
    target_year = target_day.year

    log_action(f"SET ONDUTY DATE container={container_id} target={target_text}")
    try:
        input_box = scope.locator(f"#{container_id} input.zinputfield__textbox").first
        input_box.click()
        log_action(f"CLICK date input container={container_id}")
    except PlaywrightError:
        log_action(f"FAIL click date input container={container_id}")
        return False

    try:
        scope.wait_for_function(
            "(pickerId) => {"
            "  const picker = document.getElementById(pickerId);"
            "  if (!picker) return false;"
            "  return window.getComputedStyle(picker).display !== 'none';"
            "}",
            arg=picker_id,
            timeout=int(timeout_s * 1000),
        )
    except PlaywrightTimeoutError:
        log_action(f"FAIL picker not visible picker_id={picker_id}")
        return False

    for _ in range(24):
        current = _month_from_picker(scope, picker_id)
        if not current:
            log_action(f"FAIL picker current month/year unavailable picker_id={picker_id}")
            return False
        current_month, current_year = current
        if current_month == target_month and current_year == target_year:
            log_action(f"PICKER reached target month={target_month} year={target_year} picker_id={picker_id}")
            break

        go_left = (current_year, current_month) > (target_year, target_month)
        nav_id = f"{picker_id}-left-month-0" if go_left else f"{picker_id}-right-month-0"
        try:
            scope.locator(f"#{nav_id}").first.click()
            log_action(f"CLICK picker nav id={nav_id}")
        except PlaywrightError:
            log_action(f"FAIL click picker nav id={nav_id}")
            return False
        time.sleep(0.15)
    else:
        log_action(f"FAIL picker navigation exceeded attempts picker_id={picker_id}")
        return False

    try:
        clicked = scope.evaluate(
            "(args) => {"
            "  const picker = document.getElementById(args.pickerId);"
            "  if (!picker) return false;"
            "  const cells = Array.from(picker.querySelectorAll('td.zdatetimepicker__date'));"
            "  const dayStr = String(args.day);"
            "  const cell = cells.find(td => {"
            "    const t = (td.querySelector('.zdatetimepicker__text') || td).textContent || '';"
            "    return t.trim() === dayStr;"
            "  });"
            "  if (!cell) return false;"
            "  cell.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));"
            "  cell.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));"
            "  cell.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));"
            "  return true;"
            "}",
            {"pickerId": picker_id, "day": target_day.day},
        )
    except PlaywrightError:
        log_action(f"FAIL click day cell day={target_day.day} picker_id={picker_id}")
        return False
    if not clicked:
        log_action(f"FAIL day cell not found day={target_day.day} picker_id={picker_id}")
        return False
    log_action(f"CLICK day cell day={target_day.day} picker_id={picker_id}")

    try:
        written = scope.locator(f"#{container_id} input.zinputfield__textbox").first.input_value().strip()
    except PlaywrightError:
        log_action(f"FAIL read written date container={container_id}")
        return False
    log_action(f"VALUE date container={container_id} written={written} expected={target_text}")
    return written == target_text


def submit_remote_work_for_date(scope, target_day: date, settings: RemoteWorkSettings, console: Console) -> bool:
    formatted = format_zoho_date(target_day)
    onduty_btn = scope.locator("#ZPAtt_Quick_AddOptionsBtn_list")
    try:
        onduty_btn.wait_for(timeout=int(settings.popup_timeout * 1000))
        onduty_btn.first.click()
        log_action(f"CLICK On Duty button day={formatted}")
    except PlaywrightError:
        console.log(f"[red]On Duty button not available for {format_zoho_date(target_day)}[/red]")
        log_action(f"FAIL On Duty button unavailable day={target_day.isoformat()}")
        return False

    if not wait_for_on_duty_modal(scope, settings.popup_timeout):
        console.log(f"[red]On Duty modal not loaded for {format_zoho_date(target_day)}[/red]")
        log_action(f"FAIL On Duty modal not loaded day={target_day.isoformat()}")
        return False

    log_action(f"ONDUTY modal loaded day={formatted}")
    start_ok = set_onduty_date(scope, "odStartDate-container", target_day, settings.popup_timeout)
    end_ok = set_onduty_date(scope, "odEndDate-container", target_day, settings.popup_timeout)
    if not start_ok or not end_ok:
        console.log(f"[red]Could not set On Duty date fields for {formatted}[/red]")
        log_action(f"FAIL set date fields day={formatted} start_ok={start_ok} end_ok={end_ok}")
        return False

    # Hard guardrail: never submit unless both dates match exactly.
    try:
        start_val = scope.locator("#odStartDate-container input.zinputfield__textbox").first.input_value().strip()
        end_val = scope.locator("#odEndDate-container input.zinputfield__textbox").first.input_value().strip()
    except PlaywrightError:
        console.log(f"[red]Could not verify On Duty date fields for {formatted}[/red]")
        log_action(f"FAIL verify date fields read exception day={formatted}")
        return False
    if start_val != formatted or end_val != formatted:
        console.log(
            f"[red]Date verification failed for {formatted}. Start='{start_val}' End='{end_val}'[/red]"
        )
        log_action(f"FAIL date verification day={formatted} start={start_val} end={end_val}")
        return False
    log_action(f"VERIFY date fields day={formatted} start={start_val} end={end_val}")

    try:
        reason = scope.locator("#ZPAtt_OD_req_desc").first
        reason.click()
        reason.fill("WFH")
        log_action(f"FILL reason day={formatted} value=WFH")
    except PlaywrightError:
        console.log(f"[red]Could not fill reason for {formatted}[/red]")
        log_action(f"FAIL fill reason day={formatted}")
        return False

    if settings.dry_run:
        log_action(f"DRY RUN skip submit day={formatted}")
        return True

    try:
        submit_btn = scope.locator("#zp_modal_blubtn")
        submit_btn.wait_for(timeout=int(settings.popup_timeout * 1000))
        submit_btn.first.click()
        log_action(f"CLICK submit day={formatted}")
    except PlaywrightError:
        console.log(f"[red]Could not submit On Duty request for {formatted}[/red]")
        log_action(f"FAIL submit day={formatted}")
        return False

    try:
        # Guard against stale modal state: submission is considered successful
        # only after the On Duty form is no longer visible.
        scope.wait_for_function(
            "() => {"
            "  const reason = document.querySelector('#ZPAtt_OD_req_desc');"
            "  if (!reason) return true;"
            "  const style = window.getComputedStyle(reason);"
            "  return style.display === 'none' || style.visibility === 'hidden' || reason.offsetParent === null;"
            "}",
            timeout=int(settings.popup_timeout * 1000),
        )
    except PlaywrightTimeoutError:
        console.log(f"[red]On Duty modal remained open after submit for {formatted}[/red]")
        log_action(f"FAIL modal remained open after submit day={formatted}")
        return False

    time.sleep(settings.step_delay)
    return True


def run_remote_work(settings: RemoteWorkSettings) -> int:
    console = Console()
    init_run_log("remote-work", settings.start_date, settings.end_date)
    cookies = load_cookies(settings.cookies_path)
    target_days = remote_dates_in_range(settings.start_date, settings.end_date, settings.remote_weekdays)
    weekday_labels = ", ".join(WEEKDAYS[i] for i in settings.remote_weekdays)

    if not target_days:
        console.log("[yellow]No configured remote-work days found in selected range. Nothing to submit.[/yellow]")
        return 0

    target_list = ", ".join(d.isoformat() for d in target_days)
    console.log(f"Configured remote weekdays: {weekday_labels}")
    console.log(f"Detected remote-work dates in range: {target_list}")
    log_action(f"REMOTE weekdays={weekday_labels}")
    log_action(f"REMOTE dates={target_list}")

    console.log(
        f"Submitting remote work (WFH) from {format_zoho_date(settings.start_date)} to {format_zoho_date(settings.end_date)}"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=settings.headless,
            slow_mo=settings.slow_mo,
            channel=settings.browser_channel,
        )
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.goto(settings.attendance_url, wait_until="domcontentloaded")

        scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 15.0)
        if not scope and is_federated_login_page(page):
            console.log("[yellow]Microsoft login required. Attempting click...[/yellow]")
            clicked = try_microsoft_login(page, console)
            if not clicked:
                console.log("[yellow]Please click 'Sign in with Microsoft' in the opened browser.[/yellow]")
            scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 180.0)

        if not scope:
            if is_login_page(page):
                console.log("[red]Login page detected. Cookies may be expired or invalid.[/red]")
            console.log(f"[red]Attendance navigation not found. Current URL: {page.url}[/red]")
            browser.close()
            return 1

        for target_day in target_days:
            attempts = 0
            while True:
                attempts += 1
                log_action(f"REMOTE day attempt day={target_day.isoformat()} attempt={attempts}")
                ok = submit_remote_work_for_date(scope, target_day, settings, console)
                if ok:
                    console.log(f"[green]Submitted WFH for {format_zoho_date(target_day)} ({WEEKDAYS[target_day.weekday()]})[/green]")
                    break

                console.log(
                    f"[yellow]Retrying {format_zoho_date(target_day)} after reload (attempt {attempts})[/yellow]"
                )
                log_action(f"RELOAD after failure day={target_day.isoformat()} attempt={attempts}")

                if attempts >= 5:
                    console.log(f"[red]Stopping: could not submit {format_zoho_date(target_day)} after {attempts} attempts[/red]")
                    log_action(f"ABORT day={target_day.isoformat()} attempts={attempts}")
                    browser.close()
                    return 1

                try:
                    page.reload(wait_until="domcontentloaded")
                except PlaywrightError:
                    log_action("FAIL page reload")
                    browser.close()
                    return 1

                scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 30.0)
                if not scope and is_federated_login_page(page):
                    console.log("[yellow]Microsoft login required. Attempting click...[/yellow]")
                    clicked = try_microsoft_login(page, console)
                    if not clicked:
                        console.log("[yellow]Please click 'Sign in with Microsoft' in the opened browser.[/yellow]")
                    scope = wait_for_scope(page, "#ZPAtt_entryNavigation", 180.0)

                if not scope:
                    console.log(f"[red]Attendance page not ready after reload. URL: {page.url}[/red]")
                    log_action(f"FAIL scope after reload url={page.url}")
                    browser.close()
                    return 1

                time.sleep(settings.step_delay)

        browser.close()

    return 0


def build_remote_work_settings(args: argparse.Namespace, console: Console) -> RemoteWorkSettings:
    start, end = resolve_range_from_args(args, console)
    remote_cfg = load_remote_work_config()
    return RemoteWorkSettings(
        cookies_path=Path(args.cookies),
        start_date=start,
        end_date=end,
        headless=not args.headed,
        slow_mo=args.slow_mo,
        step_delay=args.step_delay,
        popup_timeout=args.popup_timeout,
        dry_run=args.dry_run,
        attendance_url=args.attendance_url,
        browser_channel=args.browser_channel,
        debug=args.debug,
        remote_weekdays=remote_weekday_indices(remote_cfg),
    )


def build_settings(args: argparse.Namespace, console: Console) -> Settings:
    start, end = resolve_range_from_args(args, console)

    if args.check_in:
        check_in = parse_time(args.check_in)
    else:
        check_in = prompt_for_time(console, "Check-in time (HH:mm)", "09:00")

    if args.check_out:
        check_out = parse_time(args.check_out)
    else:
        check_out = prompt_for_time(console, "Check-out time (HH:mm)", "18:00")

    if args.include_weekends and args.exclude_weekends:
        console.log("[yellow]Both --include-weekends and --exclude-weekends set. Excluding weekends.[/yellow]")
    include_weekends = args.include_weekends and not args.exclude_weekends
    return Settings(
        cookies_path=Path(args.cookies),
        start_date=start,
        end_date=end,
        check_in=check_in,
        check_out=check_out,
        include_weekends=include_weekends,
        headless=not args.headed,
        slow_mo=args.slow_mo,
        step_delay=args.step_delay,
        nav_step_delay=args.nav_step_delay,
        nav_timeout=args.nav_timeout,
        popup_timeout=args.popup_timeout,
        max_nav_steps=args.max_nav_steps,
        dry_run=args.dry_run,
        attendance_url=args.attendance_url,
        browser_channel=args.browser_channel,
        debug=args.debug,
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Zoho attendance filler (cookie-based)")
    parser.add_argument(
        "--cookies",
        default="data/zoho-cookies.json",
        help="Path to cookie JSON file",
    )
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--check-in", help="Check-in time (HH:mm)")
    parser.add_argument("--check-out", help="Check-out time (HH:mm)")
    parser.add_argument("--exclude-weekends", action="store_true", default=False)
    parser.add_argument("--include-weekends", action="store_true", default=False)
    parser.add_argument("--headed", action="store_true", help="Run browser with UI")
    parser.add_argument(
        "--browser-channel",
        choices=["chrome", "msedge", "chromium"],
        default=None,
        help="Use a specific browser channel if available",
    )
    parser.add_argument("--slow-mo", type=int, default=0, help="Slow motion delay in ms")
    parser.add_argument("--step-delay", type=float, default=DEFAULTS["step_delay"])
    parser.add_argument("--nav-step-delay", type=float, default=DEFAULTS["nav_step_delay"])
    parser.add_argument("--nav-timeout", type=float, default=DEFAULTS["nav_timeout"])
    parser.add_argument("--popup-timeout", type=float, default=DEFAULTS["popup_timeout"])
    parser.add_argument("--max-nav-steps", type=int, default=DEFAULTS["max_nav_steps"])
    parser.add_argument("--dry-run", action="store_true", help="Navigate without saving")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    parser.add_argument(
        "--attendance-url",
        default=ATTENDANCE_URL,
        help="Attendance entry URL",
    )
    parser.add_argument(
        "--action",
        choices=["fill-range", "test-cookies", "remote-work", "config"],
        default=None,
        help="Run a specific action and skip the menu",
    )
    return parser.parse_args(argv)


def render_menu(console: Console) -> str:
    title = Panel.fit(
        "[bold cyan]Zoho Attendance Injector[/bold cyan]",
        subtitle="[dim]Choose an action[/dim]",
        border_style="blue",
    )
    console.print(title)

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Option", style="bold cyan", width=8)
    table.add_column("Action", style="bold white")
    table.add_column("Description", style="dim")
    table.add_row("1", "Fill Attendance", "Fill check-in/out entries for a date range")
    table.add_row("2", "Test Cookies", "Open page and validate cookie login")
    table.add_row("3", "Add Remote Work", "Submit On Duty (WFH) for configured weekdays")
    table.add_row("4", "Remote Config", "Change default remote weekdays (start day + count)")
    table.add_row("5", "Exit", "Close program")
    console.print(table)

    return Prompt.ask("Select option", choices=["1", "2", "3", "4", "5"], default="1")


def configure_remote_work(console: Console) -> int:
    cfg = load_remote_work_config()

    summary = Table(show_header=True, header_style="bold cyan")
    summary.add_column("Setting", style="bold white")
    summary.add_column("Value", style="green")
    summary.add_row("Config file", str(REMOTE_CONFIG_PATH))
    summary.add_row("Start weekday", cfg["start_weekday"])
    summary.add_row("Days per week", str(cfg["days_per_week"]))
    configured_days = ", ".join(WEEKDAYS[i] for i in remote_weekday_indices(cfg))
    summary.add_row("Configured days", configured_days)
    console.print(Panel.fit(summary, title="Remote Work Config", border_style="blue"))

    new_start = Prompt.ask("Start weekday", choices=WORKWEEK_NAMES, default=cfg["start_weekday"])
    max_days = 5 - WORKWEEK_NAMES.index(new_start)
    new_count = int(
        Prompt.ask(
            f"Days per week (1-{max_days})",
            choices=[str(i) for i in range(1, max_days + 1)],
            default=str(min(cfg["days_per_week"], max_days)),
        )
    )

    new_cfg = {"start_weekday": new_start, "days_per_week": new_count}
    save_remote_work_config(new_cfg)

    configured_days = ", ".join(WEEKDAYS[i] for i in remote_weekday_indices(new_cfg))
    console.print(
        Panel.fit(
            f"[bold green]Saved[/bold green]\nDays: [cyan]{configured_days}[/cyan]\nFile: [dim]{REMOTE_CONFIG_PATH}[/dim]",
            border_style="green",
        )
    )
    return 0


def run_selected_action(action: str, args: argparse.Namespace, console: Console) -> int:
    if action == "config":
        return configure_remote_work(console)

    cookies_path = Path(args.cookies)
    if not cookies_path.exists():
        console.print(Text(f"Cookie file not found: {cookies_path}", style="red"))
        return 1

    if action == "test-cookies":
        return test_cookies_login(
            console=console,
            cookies_path=cookies_path,
            attendance_url=args.attendance_url,
            browser_channel=args.browser_channel,
        )

    if action in {"fill-range", "remote-work"} and not args.headed:
        open_ui = Confirm.ask(
            "Open browser UI? (recommended for Microsoft login)",
            default=True,
        )
        if open_ui:
            args.headed = True

    if action == "remote-work":
        try:
            settings = build_remote_work_settings(args, console)
        except ValueError as exc:
            console.print(Text(str(exc), style="red"))
            return 1
        return run_remote_work(settings)

    try:
        settings = build_settings(args, console)
    except ValueError as exc:
        console.print(Text(str(exc), style="red"))
        return 1

    if not settings.include_weekends and settings.start_date == settings.end_date:
        if settings.start_date.weekday() in (5, 6):
            proceed = Confirm.ask("Selected date is a weekend and weekends are excluded. Continue anyway?", default=False)
            if not proceed:
                return 1

    return run(settings)


def main(argv: List[str]) -> int:
    console = Console()
    args = parse_args(argv)

    if args.action:
        return run_selected_action(args.action, args, console)

    while True:
        choice = render_menu(console)
        if choice == "5":
            return 0

        if choice == "1":
            action = "fill-range"
        elif choice == "2":
            action = "test-cookies"
        elif choice == "3":
            action = "remote-work"
        elif choice == "4":
            action = "config"
        else:
            continue

        run_args = argparse.Namespace(**vars(args))
        result = run_selected_action(action, run_args, console)
        if result == 0:
            console.print(Panel.fit("[bold green]Action completed[/bold green]", border_style="green"))
        else:
            console.print(Panel.fit("[bold red]Action failed[/bold red]", border_style="red"))
        console.input("Press Enter to return to menu...")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

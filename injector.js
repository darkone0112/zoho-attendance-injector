(() => {
  'use strict';

  const CONFIG = window.ZOHO_ATTENDANCE_CONFIG || {};

  const STORAGE_KEYS = {
    checkIn: 'zohoAttendance.checkIn',
    checkOut: 'zohoAttendance.checkOut',
  };

  const MONTHS = {
    Jan: 0,
    Feb: 1,
    Mar: 2,
    Apr: 3,
    May: 4,
    Jun: 5,
    Jul: 6,
    Aug: 7,
    Sep: 8,
    Oct: 9,
    Nov: 10,
    Dec: 11,
  };

  const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  const DEFAULTS = {
    includeWeekends: true,
    stepDelayMs: 350,
    navTimeoutMs: 6000,
    popupTimeoutMs: 8000,
    maxNavSteps: 80,
    dryRun: false,
  };

  const SETTINGS = {
    includeWeekends: CONFIG.includeWeekends ?? DEFAULTS.includeWeekends,
    stepDelayMs: CONFIG.stepDelayMs ?? DEFAULTS.stepDelayMs,
    navTimeoutMs: CONFIG.navTimeoutMs ?? DEFAULTS.navTimeoutMs,
    popupTimeoutMs: CONFIG.popupTimeoutMs ?? DEFAULTS.popupTimeoutMs,
    maxNavSteps: CONFIG.maxNavSteps ?? DEFAULTS.maxNavSteps,
    dryRun: CONFIG.dryRun ?? DEFAULTS.dryRun,
  };

  const log = (...args) => console.log('[zoho-attendance]', ...args);

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const waitFor = async (fn, timeoutMs, intervalMs = 200) => {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const value = fn();
      if (value) return value;
      await sleep(intervalMs);
    }
    return null;
  };

  const isValidTime = (value) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);

  const parseIsoDate = (value) => {
    const match = /^\s*(\d{4})-(\d{2})-(\d{2})\s*$/.exec(value || '');
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]) - 1;
    const day = Number(match[3]);
    const date = new Date(year, month, day);
    return Number.isNaN(date.getTime()) ? null : date;
  };

  const parseZohoDate = (value) => {
    const match = /^\s*(\d{2})-([A-Za-z]{3})-(\d{4})\s*$/.exec(value || '');
    if (!match) return null;
    const day = Number(match[1]);
    const month = MONTHS[match[2]];
    const year = Number(match[3]);
    if (month === undefined) return null;
    return new Date(year, month, day);
  };

  const formatZohoDate = (date) => {
    const day = String(date.getDate()).padStart(2, '0');
    const monthName = Object.keys(MONTHS).find((key) => MONTHS[key] === date.getMonth());
    return `${day}-${monthName}-${date.getFullYear()}`;
  };

  const enumerateDays = (start, end) => {
    const days = [];
    const cursor = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    const last = new Date(end.getFullYear(), end.getMonth(), end.getDate());
    while (cursor <= last) {
      days.push(new Date(cursor));
      cursor.setDate(cursor.getDate() + 1);
    }
    return days;
  };

  const getDisplayedRangeText = () => {
    const nav = document.querySelector('#ZPAtt_entryNavigation');
    if (!nav) return null;
    const bold = nav.querySelector('b');
    if (bold && bold.textContent.trim()) return bold.textContent.trim();
    return nav.textContent.trim();
  };

  const getDisplayedRange = () => {
    const text = getDisplayedRangeText();
    if (!text) return null;
    const match = /(\d{2}-[A-Za-z]{3}-\d{4})\s*-\s*(\d{2}-[A-Za-z]{3}-\d{4})/.exec(text.replace(/\u00a0/g, ' '));
    if (!match) return null;
    const start = parseZohoDate(match[1]);
    const end = parseZohoDate(match[2]);
    if (!start || !end) return null;
    return { start, end };
  };

  const navigateToWeekContaining = async (date) => {
    for (let step = 0; step < SETTINGS.maxNavSteps; step += 1) {
      const range = getDisplayedRange();
      if (!range) {
        log('Unable to read current week range.');
        return false;
      }

      if (date >= range.start && date <= range.end) return true;

      const prevStart = range.start.getTime();
      const prevEnd = range.end.getTime();

      if (date < range.start) {
        Attendance.Entry.setMonthsNavigation(1);
      } else {
        Attendance.Entry.setMonthsNavigation(-1);
      }

      const updated = await waitFor(() => {
        const next = getDisplayedRange();
        if (!next) return null;
        if (next.start.getTime() !== prevStart || next.end.getTime() !== prevEnd) return next;
        return null;
      }, SETTINGS.navTimeoutMs);

      if (!updated) {
        log('Week navigation timed out.');
        return false;
      }
    }

    log('Max week navigation steps reached.');
    return false;
  };

  const rowMatchesDate = (row, date) => {
    const formatted = formatZohoDate(date);
    const weekday = WEEKDAYS[date.getDay()];
    const dayNum = String(date.getDate());
    const dayNumPadded = String(date.getDate()).padStart(2, '0');

    const attrs = [
      row.getAttribute('aria-label'),
      row.getAttribute('title'),
      row.getAttribute('data-date'),
      row.dataset?.date,
    ]
      .filter(Boolean)
      .join(' ');

    const text = `${row.textContent || ''} ${attrs}`;

    if (text.includes(formatted)) return true;
    if (!text.includes(weekday)) return false;
    if (new RegExp(`\\b${dayNumPadded}\\b`).test(text)) return true;
    if (new RegExp(`\\b${dayNum}\\b`).test(text)) return true;
    return false;
  };

  const findDayRow = (date) => {
    const rows = Array.from(document.querySelectorAll('tr[onclick*="consEntriesPopup"]'));
    for (const row of rows) {
      if (rowMatchesDate(row, date)) return row;
    }
    return null;
  };

  const findAddEntryButton = (root = document) => {
    const buttons = Array.from(root.querySelectorAll('div.zpl_lnkbg'));
    return buttons.find((el) => /Add Check-in\s*\/\s*Check-out Entry/i.test(el.textContent || '')) || null;
  };

  const findPopupRoot = () => {
    return (
      document.querySelector('.zpl_popup') ||
      document.querySelector('.zpl_attpup') ||
      document.querySelector('[id*="EntryPopup" i]') ||
      document.querySelector('[class*="popup" i]') ||
      document
    );
  };

  const findTimeInputs = (root) => {
    const inputs = Array.from(root.querySelectorAll('input'));
    const matchByHint = (hints) =>
      inputs.find((input) => {
        const haystack = [
          input.getAttribute('aria-label'),
          input.getAttribute('placeholder'),
          input.getAttribute('name'),
          input.getAttribute('id'),
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return hints.some((hint) => haystack.includes(hint));
      });

    const inInput =
      matchByHint(['check-in', 'check in', 'checkin', 'in time', 'intime']) || null;
    const outInput =
      matchByHint(['check-out', 'check out', 'checkout', 'out time', 'outtime']) || null;

    if (inInput && outInput) return { inInput, outInput };

    const candidates = inputs.filter((input) => input.type === 'text' || input.type === 'time');
    if (candidates.length >= 2) {
      return { inInput: inInput || candidates[0], outInput: outInput || candidates[1] };
    }

    return { inInput: inInput || null, outInput: outInput || null };
  };

  const setInputValue = (input, value) => {
    if (!input) return;
    input.focus();
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const clickSaveButton = (root) => {
    const buttons = Array.from(root.querySelectorAll('button'));
    const button =
      buttons.find((el) => /save/i.test(el.textContent || '')) ||
      buttons.find((el) => (el.getAttribute('onclick') || '').includes('updateEntry'));
    if (button) button.click();
    return Boolean(button);
  };

  const openDayPopup = async (date) => {
    const row = findDayRow(date);
    if (!row) return false;
    row.scrollIntoView({ block: 'center' });
    row.click();
    const addButton = await waitFor(() => findAddEntryButton(findPopupRoot()), SETTINGS.popupTimeoutMs);
    return Boolean(addButton);
  };

  const fillDay = async (date, checkIn, checkOut) => {
    const opened = await openDayPopup(date);
    if (!opened) {
      log(`Unable to open entry popup for ${formatZohoDate(date)}.`);
      return false;
    }

    const popupRoot = findPopupRoot();
    const addButton = findAddEntryButton(popupRoot);
    if (!addButton) {
      log(`Add entry button not found for ${formatZohoDate(date)}.`);
      return false;
    }

    if (!SETTINGS.dryRun) addButton.click();

    const inputsReady = await waitFor(() => {
      const inputs = findTimeInputs(findPopupRoot());
      return inputs.inInput && inputs.outInput ? inputs : null;
    }, SETTINGS.popupTimeoutMs);

    if (!inputsReady) {
      log(`Time inputs not found for ${formatZohoDate(date)}.`);
      return false;
    }

    if (!SETTINGS.dryRun) {
      setInputValue(inputsReady.inInput, checkIn);
      setInputValue(inputsReady.outInput, checkOut);
    }

    if (!SETTINGS.dryRun) {
      const saved = clickSaveButton(findPopupRoot());
      if (!saved) {
        log(`Save button not found for ${formatZohoDate(date)}.`);
        return false;
      }
    }

    log(`Filled ${formatZohoDate(date)} (${WEEKDAYS[date.getDay()]}).`);
    return true;
  };

  const promptForTime = (label, key) => {
    const stored = localStorage.getItem(key);
    if (stored && isValidTime(stored)) return stored;

    const value = prompt(label, stored || '09:00');
    if (!value || !isValidTime(value)) {
      throw new Error(`${label} must be in HH:mm (24h) format.`);
    }
    localStorage.setItem(key, value);
    return value;
  };

  const promptForDateRange = () => {
    const startStr = CONFIG.startDate || prompt('Start date (YYYY-MM-DD):', '2026-02-09');
    const endStr = CONFIG.endDate || prompt('End date (YYYY-MM-DD):', '2026-02-15');
    const start = parseIsoDate(startStr);
    const end = parseIsoDate(endStr);
    if (!start || !end) throw new Error('Dates must be in YYYY-MM-DD format.');
    if (end < start) throw new Error('End date must be on or after start date.');
    return { start, end };
  };

  const run = async () => {
    if (!window.Attendance?.Entry?.setMonthsNavigation) {
      throw new Error('Attendance.Entry API not found. Make sure you are on the attendance entry page.');
    }

    const checkIn = CONFIG.checkIn || promptForTime('Check-in time (HH:mm):', STORAGE_KEYS.checkIn);
    const checkOut = CONFIG.checkOut || promptForTime('Check-out time (HH:mm):', STORAGE_KEYS.checkOut);
    if (!isValidTime(checkIn) || !isValidTime(checkOut)) {
      throw new Error('Times must be in HH:mm (24h) format.');
    }

    const { start, end } = promptForDateRange();
    const days = enumerateDays(start, end).filter((day) => {
      if (SETTINGS.includeWeekends) return true;
      const weekday = day.getDay();
      return weekday !== 0 && weekday !== 6;
    });

    log(`Starting fill for ${days.length} day(s) from ${formatZohoDate(start)} to ${formatZohoDate(end)}.`);

    for (const day of days) {
      const navigated = await navigateToWeekContaining(day);
      if (!navigated) {
        log(`Could not navigate to week for ${formatZohoDate(day)}.`);
        break;
      }

      await fillDay(day, checkIn, checkOut);
      await sleep(SETTINGS.stepDelayMs);
    }

    log('Done.');
  };

  run().catch((error) => {
    console.error('[zoho-attendance] Error:', error);
  });
})();

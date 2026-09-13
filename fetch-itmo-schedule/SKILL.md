---
name: fetch-itmo-schedule
description: Use when fetching the user's personal ITMO class schedule from my.itmo.ru via their ISU account — answering "what classes do I have today/this week", or exporting the schedule as an .ics calendar file.
---

# Fetch ITMO Schedule

Fetch the user's personal class schedule from `my.itmo.ru` by logging in with
their ISU (my.itmo.ru) username and password. Login and the schedule API call
are adapted from
[iburakov/my-itmo-ru-to-ical](https://github.com/iburakov/my-itmo-ru-to-ical)
(MIT License). That project runs as a personally hosted Docker service
exposing a public `.ics` URL for calendar apps to poll; this skill instead
performs the same login and fetch on demand, once per run, with no server, no
session file, and no credential cache.

## Safety

- Read credentials only from `ITMO_ISU_USERNAME` and `ITMO_ISU_PASSWORD`.
  Never request, print, log, or commit the username, password, or the
  short-lived API token obtained from them.
- Nothing is cached or written to disk between runs; each invocation logs in
  again. The only file this skill writes is the `.ics` output the user asked
  for (`--format ics`), never credentials or raw login responses.
- Diagnostics go to `stderr` as fixed `stage=... error_type=...` labels, never
  raw exception text, tracebacks, or HTTP response bodies (a login response
  could echo request data back). Schedule data goes only to `stdout` (or the
  `.ics` file for that format).
- This is a read-only fetch: it only requests the schedule and never changes
  anything in ISU or my.itmo.ru.

## Run

Install the isolated dependency once, then run the skill:

```bash
python3 -m venv /private/tmp/itmo-schedule-venv
/private/tmp/itmo-schedule-venv/bin/pip install -r ~/.claude/skills/fetch-itmo-schedule/requirements.txt
export ITMO_ISU_USERNAME='100000'
export ITMO_ISU_PASSWORD='your_isu_password'
/private/tmp/itmo-schedule-venv/bin/python ~/.claude/skills/fetch-itmo-schedule/scripts/fetch_schedule.py
```

`ITMO_ISU_USERNAME`/`ITMO_ISU_PASSWORD` are the same login used at
`my.itmo.ru` (ISU number and password). Keep them out of the repository and
out of shell history where possible (e.g. paste into a `read -s` prompt
instead of typing them on the command line).

### Options

- Date range (pick at most one; default is `--today`):
  `--today`, `--week` (current Monday–Sunday), `--days N` (today + N-1 days),
  `--all-term` (current academic term, Aug 1–Jul 31), or an explicit
  `--date-start YYYY-MM-DD --date-end YYYY-MM-DD` pair.
- `--format text|json|ics` (default `text`). `json` dumps the raw lesson
  objects from the API (fullest detail); `text` is a grouped-by-day human
  summary; `ics` writes an iCalendar file for importing into Google/Apple/etc.
  calendars.
- `--output PATH` — file path for `--format ics` (default `itmo-schedule.ics`
  in the current directory).
- `--log-level LEVEL` — `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`
  (default `INFO`, or `ITMO_LOG_LEVEL` if set).

Run `--help` for the full list before using other options.

## Expected output

`text` and `json` print to `stdout` and cover only the requested date range —
`--all-term` is a lot of output in `text`/`json` form and is really meant for
`--format ics`, to build a calendar file worth subscribing to. `ics` writes
the file and prints a one-line confirmation with the lesson count and path.
An empty range prints "Занятий не найдено." (text) or an empty `lessons` list
(json) rather than an error.

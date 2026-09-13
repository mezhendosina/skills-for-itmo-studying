#!/usr/bin/env python3
"""Fetch the personal ITMO (my.itmo.ru) class schedule using an ISU login.

The ISU login flow (PKCE against id.itmo.ru) and the my.itmo.ru schedule API
call are adapted from iburakov/my-itmo-ru-to-ical (MIT License):
https://github.com/iburakov/my-itmo-ru-to-ical
That project hosts a small Docker service exposing a public .ics URL; this
script instead runs the same login + fetch on demand, without a server,
session file, or credential cache.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import urllib.parse
from base64 import urlsafe_b64encode
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from hashlib import md5, sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import requests
from dateutil.parser import isoparse

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Keycloak client used by my.itmo.ru's own frontend; PKCE flow inspired by
# https://www.stefaanlippens.net/oauth-code-flow-pkce.html (same source the
# reference project credits).
_CLIENT_ID = "student-personal-cabinet"
_REDIRECT_URI = "https://my.itmo.ru/login/callback"
_PROVIDER = "https://id.itmo.ru/auth/realms/itmo"
_API_BASE_URL = "https://my.itmo.ru/api"
_SCHEDULE_PATH = "/schedule/schedule/personal"

_FORM_ACTION_REGEX = re.compile(
    rf'"loginAction":\s*"(?P<action>{re.escape(_PROVIDER)}[^"]*)"', re.DOTALL
)

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_LESSON_TYPE_TAGS = {
    "Лекции": "Лек",
    "Практические занятия": "Прак",
    "Лабораторные занятия": "Лаб",
    "Занятия спортом": "Спорт",
}


class ScheduleAuthError(RuntimeError):
    """A deliberately generic error for a terminal-safe login failure."""

    def __init__(self) -> None:
        super().__init__("ITMO ISU authentication failed.")


def parse_log_level(value: str | None) -> int:
    if not value:
        return logging.INFO
    level = LOG_LEVELS.get(value.upper())
    if level is None:
        raise ValueError("Invalid log level.")
    return level


def configure_logger(level: int) -> logging.Logger:
    logger = logging.getLogger("itmo_schedule")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def credentials() -> tuple[str, str]:
    username = os.environ.get("ITMO_ISU_USERNAME")
    password = os.environ.get("ITMO_ISU_PASSWORD")
    if not username or not password:
        raise ValueError(
            "Set ITMO_ISU_USERNAME and ITMO_ISU_PASSWORD (your ISU / my.itmo.ru login)."
        )
    return username, password


def generate_code_verifier() -> str:
    code_verifier = urlsafe_b64encode(os.urandom(40)).decode("utf-8")
    return re.sub("[^a-zA-Z0-9]+", "", code_verifier)


def get_code_challenge(code_verifier: str) -> str:
    code_challenge_bytes = sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = urlsafe_b64encode(code_challenge_bytes).decode("utf-8")
    return code_challenge.replace("=", "")


def get_access_token(http: requests.Session, username: str, password: str, logger: logging.Logger) -> str:
    """Log in to id.itmo.ru with PKCE and return a my.itmo.ru API bearer token.

    Never logs the username, password, tokens, or raw response bodies.
    """

    code_verifier = generate_code_verifier()
    code_challenge = get_code_challenge(code_verifier)

    try:
        auth_resp = http.get(
            _PROVIDER + "/protocol/openid-connect/auth",
            params=dict(
                protocol="oauth2",
                response_type="code",
                client_id=_CLIENT_ID,
                redirect_uri=_REDIRECT_URI,
                scope="openid",
                state="im_not_a_browser",
                code_challenge_method="S256",
                code_challenge=code_challenge,
            ),
            timeout=30,
        )
        auth_resp.raise_for_status()
    except requests.RequestException:
        logger.error("event=failed stage=authenticate error_type=request")
        raise ScheduleAuthError() from None

    form_action_match = _FORM_ACTION_REGEX.search(auth_resp.text)
    if not form_action_match:
        logger.error("event=failed stage=authenticate error_type=form_not_found")
        raise ScheduleAuthError()
    form_action = html.unescape(form_action_match.group("action"))

    try:
        form_resp = http.post(
            form_action,
            data=dict(username=username, password=password),
            cookies=auth_resp.cookies,
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException:
        logger.error("event=failed stage=authenticate error_type=request")
        raise ScheduleAuthError() from None

    if form_resp.status_code != 302:
        logger.error("event=failed stage=authenticate error_type=invalid_credentials")
        raise ScheduleAuthError()

    location = form_resp.headers.get("Location", "")
    query = urllib.parse.urlparse(location).query
    redirect_params = urllib.parse.parse_qs(query)
    auth_code = redirect_params.get("code", [None])[0]
    if not auth_code:
        logger.error("event=failed stage=authenticate error_type=no_code")
        raise ScheduleAuthError()

    try:
        token_resp = http.post(
            _PROVIDER + "/protocol/openid-connect/token",
            data=dict(
                grant_type="authorization_code",
                client_id=_CLIENT_ID,
                redirect_uri=_REDIRECT_URI,
                code=auth_code,
                code_verifier=code_verifier,
            ),
            allow_redirects=False,
            timeout=30,
        )
        token_resp.raise_for_status()
    except requests.RequestException:
        logger.error("event=failed stage=authenticate error_type=request")
        raise ScheduleAuthError() from None

    token = token_resp.json().get("access_token")
    if not token:
        logger.error("event=failed stage=authenticate error_type=no_token")
        raise ScheduleAuthError()
    logger.info("event=authenticated")
    return token


def fetch_raw_lessons(
    http: requests.Session, token: str, date_start: date, date_end: date, logger: logging.Logger
) -> list[dict[str, Any]]:
    params = {"date_start": date_start.isoformat(), "date_end": date_end.isoformat()}
    logger.info("event=fetch_schedule date_start=%s date_end=%s", params["date_start"], params["date_end"])
    try:
        resp = http.get(
            _API_BASE_URL + _SCHEDULE_PATH,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        logger.error("event=failed stage=fetch error_type=request")
        raise RuntimeError("ITMO schedule request failed.") from None

    try:
        days = resp.json()["data"]
    except (ValueError, KeyError):
        logger.error("event=failed stage=fetch error_type=invalid_response")
        raise RuntimeError("ITMO schedule request failed.") from None

    lessons = [dict(date=day["date"], **lesson) for day in days for lesson in day.get("lessons", [])]
    lessons.sort(key=lambda lesson: (lesson["date"], lesson.get("time_start", "")))
    logger.info("event=fetch_complete lessons=%d", len(lessons))
    return lessons


@dataclass(frozen=True)
class Lesson:
    """A curated view of one raw lesson, for the text and ics renderers."""

    date: str
    time_start: str
    time_end: str
    subject: str
    type: str
    teacher: str | None
    room: str | None
    building: str | None
    zoom_url: str | None
    group: str | None
    note: str | None


def raw_lesson_to_lesson(raw: dict[str, Any]) -> Lesson:
    return Lesson(
        date=raw["date"],
        time_start=raw["time_start"],
        time_end=raw["time_end"],
        subject=raw["subject"],
        type=raw.get("type", ""),
        teacher=raw.get("teacher_name") or raw.get("teacher_fio"),
        room=raw.get("room"),
        building=raw.get("building"),
        zoom_url=raw.get("zoom_url"),
        group=raw.get("group"),
        note=raw.get("note"),
    )


def _lesson_place(lesson: Lesson) -> str:
    place = ", ".join(part for part in (lesson.room, lesson.building) if part)
    if lesson.zoom_url:
        return f"Zoom / {place}" if place else "Zoom"
    return place


def _lesson_uid(lesson: Lesson) -> str:
    raw = ", ".join((lesson.date, lesson.time_start, lesson.subject))
    digest = md5(raw.encode("utf-8")).hexdigest()  # noqa: S324 - stable id, not a security use
    return str(UUID(hex=digest))


def academic_term_bounds(today: date) -> tuple[date, date]:
    pivot = today.replace(month=8, day=1)
    term_start_year = today.year - 1 if today < pivot else today.year
    return date(term_start_year, 8, 1), date(term_start_year + 1, 7, 31)


def week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def resolve_date_range(args: argparse.Namespace, today: date) -> tuple[date, date]:
    if args.date_start or args.date_end:
        if not (args.date_start and args.date_end):
            raise ValueError("--date-start and --date-end must be used together.")
        if args.today or args.week or args.days is not None or args.all_term:
            raise ValueError("--date-start/--date-end cannot be combined with --today/--week/--days/--all-term.")
        if args.date_start > args.date_end:
            raise ValueError("--date-start must not be after --date-end.")
        return args.date_start, args.date_end

    selected = [name for name in ("today", "week", "all_term") if getattr(args, name)]
    if args.days is not None:
        selected.append("days")
    if len(selected) > 1:
        raise ValueError("Use only one of --today, --week, --days, --all-term.")

    if args.all_term:
        return academic_term_bounds(today)
    if args.week:
        return week_bounds(today)
    if args.days is not None:
        if args.days < 1:
            raise ValueError("--days must be a positive integer.")
        return today, today + timedelta(days=args.days - 1)
    return today, today


def render_text(raw_lessons: list[dict[str, Any]], date_start: date, date_end: date) -> str:
    header = f"Расписание {date_start.isoformat()} — {date_end.isoformat()}"
    if not raw_lessons:
        return f"{header}\nЗанятий не найдено."

    lessons_by_date: dict[str, list[Lesson]] = {}
    for raw in raw_lessons:
        lesson = raw_lesson_to_lesson(raw)
        lessons_by_date.setdefault(lesson.date, []).append(lesson)

    lines = [header, ""]
    for day in sorted(lessons_by_date):
        weekday = _WEEKDAYS_RU[date.fromisoformat(day).weekday()]
        lines.append(f"{day} ({weekday})")
        for lesson in sorted(lessons_by_date[day], key=lambda item: item.time_start):
            tag = _LESSON_TYPE_TAGS.get(lesson.type, lesson.type)
            place = _lesson_place(lesson)
            place_part = f" — {place}" if place else ""
            teacher_part = f" ({lesson.teacher})" if lesson.teacher else ""
            lines.append(f"  {lesson.time_start}–{lesson.time_end} [{tag}] {lesson.subject}{teacher_part}{place_part}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_json(raw_lessons: list[dict[str, Any]], date_start: date, date_end: date) -> str:
    payload = {
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "count": len(raw_lessons),
        "lessons": raw_lessons,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_ics(raw_lessons: list[dict[str, Any]]) -> str:
    from ics import Calendar, Event  # imported lazily: only needed for --format ics

    calendar = Calendar(creator="fetch-itmo-schedule")
    for raw in raw_lessons:
        lesson = raw_lesson_to_lesson(raw)
        begin = isoparse(f"{lesson.date}T{lesson.time_start}:00+03:00")
        end = isoparse(f"{lesson.date}T{lesson.time_end}:00+03:00")
        if begin > end:
            begin, end = end, begin

        description_lines = []
        if lesson.teacher:
            description_lines.append(f"Преподаватель: {lesson.teacher}")
        if lesson.group:
            description_lines.append(f"Группа: {lesson.group}")
        if lesson.zoom_url:
            description_lines.append(f"Zoom: {lesson.zoom_url}")
        if lesson.note:
            description_lines.append(f"Примечание: {lesson.note}")

        tag = _LESSON_TYPE_TAGS.get(lesson.type, lesson.type)
        event = Event(
            name=f"[{tag}] {lesson.subject}",
            begin=begin,
            end=end,
            description="\n".join(description_lines) or None,
            location=_lesson_place(lesson) or None,
            uid=_lesson_uid(lesson),
        )
        if lesson.zoom_url:
            event.url = lesson.zoom_url
        calendar.events.add(event)

    return calendar.serialize()


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD.") from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the personal ITMO (my.itmo.ru) class schedule via ISU login."
    )
    parser.add_argument("--today", action="store_true", help="Fetch only today (default).")
    parser.add_argument("--week", action="store_true", help="Fetch the current calendar week (Mon-Sun).")
    parser.add_argument("--days", type=int, help="Fetch N days starting today.")
    parser.add_argument("--all-term", action="store_true", help="Fetch the whole academic term (Aug 1 - Jul 31).")
    parser.add_argument("--date-start", type=parse_date, help="Range start (YYYY-MM-DD); use with --date-end.")
    parser.add_argument("--date-end", type=parse_date, help="Range end (YYYY-MM-DD); use with --date-start.")
    parser.add_argument("--format", choices=("text", "json", "ics"), default="text")
    parser.add_argument(
        "--output", type=Path, default=Path("itmo-schedule.ics"), help="File path for --format ics."
    )
    parser.add_argument(
        "--log-level", metavar="LEVEL", default=None, help="DEBUG, INFO, WARNING, ERROR, or CRITICAL."
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    try:
        logger = configure_logger(parse_log_level(args.log_level or os.environ.get("ITMO_LOG_LEVEL")))
    except ValueError:
        print("Invalid log level.", file=sys.stderr)
        return 2

    try:
        username, password = credentials()
        today = date.today()
        date_start, date_end = resolve_date_range(args, today)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    http = requests.Session()
    try:
        token = get_access_token(http, username, password, logger)
        raw_lessons = fetch_raw_lessons(http, token, date_start, date_end, logger)
    except ScheduleAuthError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    if args.format == "text":
        print(render_text(raw_lessons, date_start, date_end))
    elif args.format == "json":
        print(render_json(raw_lessons, date_start, date_end))
    else:
        args.output.write_text(render_ics(raw_lessons), encoding="utf-8")
        print(f"Saved {len(raw_lessons)} lesson(s) to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Unit tests for the ITMO schedule fetch helpers, without any real network calls."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import requests

SCRIPT = __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "fetch_schedule.py"
SPEC = importlib.util.spec_from_file_location("fetch_schedule", SCRIPT)
assert SPEC and SPEC.loader
fetch_schedule = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetch_schedule
SPEC.loader.exec_module(fetch_schedule)


def make_args(**overrides) -> argparse.Namespace:
    base = dict(today=False, week=False, days=None, all_term=False, date_start=None, date_end=None)
    base.update(overrides)
    return argparse.Namespace(**base)


class DateRangeTest(unittest.TestCase):
    def test_default_is_today_only(self) -> None:
        today = date(2026, 9, 10)
        self.assertEqual(fetch_schedule.resolve_date_range(make_args(), today), (today, today))

    def test_today_flag_matches_default(self) -> None:
        today = date(2026, 9, 10)
        self.assertEqual(fetch_schedule.resolve_date_range(make_args(today=True), today), (today, today))

    def test_week_is_monday_to_sunday_containing_today(self) -> None:
        thursday = date(2026, 9, 10)
        start, end = fetch_schedule.resolve_date_range(make_args(week=True), thursday)
        self.assertEqual(start, date(2026, 9, 7))
        self.assertEqual(end, date(2026, 9, 13))

    def test_days_counts_today_as_first_day(self) -> None:
        today = date(2026, 9, 10)
        start, end = fetch_schedule.resolve_date_range(make_args(days=3), today)
        self.assertEqual((start, end), (date(2026, 9, 10), date(2026, 9, 12)))

    def test_days_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.resolve_date_range(make_args(days=0), date(2026, 9, 10))

    def test_all_term_spans_academic_year_from_august(self) -> None:
        self.assertEqual(
            fetch_schedule.academic_term_bounds(date(2026, 2, 1)),
            (date(2025, 8, 1), date(2026, 7, 31)),
        )
        self.assertEqual(
            fetch_schedule.academic_term_bounds(date(2026, 9, 1)),
            (date(2026, 8, 1), date(2027, 7, 31)),
        )

    def test_explicit_range_is_used_as_is(self) -> None:
        start, end = fetch_schedule.resolve_date_range(
            make_args(date_start=date(2026, 1, 5), date_end=date(2026, 1, 20)), date(2026, 9, 10)
        )
        self.assertEqual((start, end), (date(2026, 1, 5), date(2026, 1, 20)))

    def test_explicit_range_requires_both_ends(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.resolve_date_range(make_args(date_start=date(2026, 1, 5)), date(2026, 9, 10))

    def test_explicit_range_rejects_inverted_bounds(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.resolve_date_range(
                make_args(date_start=date(2026, 1, 20), date_end=date(2026, 1, 5)), date(2026, 9, 10)
            )

    def test_explicit_range_cannot_combine_with_shorthand_flags(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.resolve_date_range(
                make_args(date_start=date(2026, 1, 5), date_end=date(2026, 1, 20), week=True),
                date(2026, 9, 10),
            )

    def test_only_one_shorthand_flag_allowed(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.resolve_date_range(make_args(week=True, all_term=True), date(2026, 9, 10))


class LessonMappingTest(unittest.TestCase):
    def test_prefers_teacher_name_over_teacher_fio(self) -> None:
        lesson = fetch_schedule.raw_lesson_to_lesson(
            {
                "date": "2026-09-10",
                "time_start": "09:00",
                "time_end": "10:30",
                "subject": "Мобильная разработка",
                "type": "Лекции",
                "teacher_name": "Иванов И.И.",
                "teacher_fio": "Иванов Иван Иванович",
            }
        )
        self.assertEqual(lesson.teacher, "Иванов И.И.")

    def test_falls_back_to_teacher_fio(self) -> None:
        lesson = fetch_schedule.raw_lesson_to_lesson(
            {
                "date": "2026-09-10",
                "time_start": "09:00",
                "time_end": "10:30",
                "subject": "Мобильная разработка",
                "type": "Лекции",
                "teacher_fio": "Иванов Иван Иванович",
            }
        )
        self.assertEqual(lesson.teacher, "Иванов Иван Иванович")

    def test_missing_optional_fields_become_none(self) -> None:
        lesson = fetch_schedule.raw_lesson_to_lesson(
            {
                "date": "2026-09-10",
                "time_start": "09:00",
                "time_end": "10:30",
                "subject": "Мобильная разработка",
                "type": "Лекции",
            }
        )
        self.assertIsNone(lesson.teacher)
        self.assertIsNone(lesson.room)
        self.assertIsNone(lesson.zoom_url)

    def test_lesson_place_combines_room_building_and_zoom(self) -> None:
        base = dict(date="2026-09-10", time_start="09:00", time_end="10:30", subject="X", type="Лекции")
        self.assertEqual(
            fetch_schedule._lesson_place(fetch_schedule.raw_lesson_to_lesson({**base, "room": "305", "building": "Кронв. 49"})),
            "305, Кронв. 49",
        )
        self.assertEqual(
            fetch_schedule._lesson_place(fetch_schedule.raw_lesson_to_lesson({**base, "zoom_url": "https://zoom"})),
            "Zoom",
        )
        self.assertEqual(
            fetch_schedule._lesson_place(
                fetch_schedule.raw_lesson_to_lesson({**base, "room": "305", "zoom_url": "https://zoom"})
            ),
            "Zoom / 305",
        )
        self.assertEqual(fetch_schedule._lesson_place(fetch_schedule.raw_lesson_to_lesson(base)), "")

    def test_lesson_uid_is_stable_and_distinct(self) -> None:
        base = dict(date="2026-09-10", time_start="09:00", time_end="10:30", subject="X", type="Лекции")
        lesson_a = fetch_schedule.raw_lesson_to_lesson(base)
        lesson_a_again = fetch_schedule.raw_lesson_to_lesson(base)
        lesson_b = fetch_schedule.raw_lesson_to_lesson({**base, "subject": "Y"})
        self.assertEqual(fetch_schedule._lesson_uid(lesson_a), fetch_schedule._lesson_uid(lesson_a_again))
        self.assertNotEqual(fetch_schedule._lesson_uid(lesson_a), fetch_schedule._lesson_uid(lesson_b))


class RenderTest(unittest.TestCase):
    RAW_LESSONS = [
        {
            "date": "2026-09-11",
            "time_start": "09:00",
            "time_end": "10:30",
            "subject": "Мобильная разработка",
            "type": "Лекции",
            "teacher_name": "Иванов И.И.",
            "room": "305",
            "building": "Кронверкский, 49",
        },
        {
            "date": "2026-09-10",
            "time_start": "13:20",
            "time_end": "14:50",
            "subject": "Базы данных",
            "type": "Практические занятия",
            "zoom_url": "https://itmo.zoom.us/j/123",
        },
    ]

    def test_render_text_groups_by_day_and_sorts_within_day(self) -> None:
        text = fetch_schedule.render_text(self.RAW_LESSONS, date(2026, 9, 10), date(2026, 9, 11))
        lines = text.splitlines()
        self.assertIn("Расписание 2026-09-10 — 2026-09-11", lines[0])
        self.assertLess(lines.index("2026-09-10 (четверг)"), lines.index("2026-09-11 (пятница)"))
        self.assertIn("[Прак] Базы данных — Zoom", text)
        self.assertIn("[Лек] Мобильная разработка (Иванов И.И.) — 305, Кронверкский, 49", text)

    def test_render_text_reports_empty_range(self) -> None:
        text = fetch_schedule.render_text([], date(2026, 9, 10), date(2026, 9, 10))
        self.assertIn("Занятий не найдено.", text)

    def test_render_json_is_raw_passthrough_with_summary(self) -> None:
        payload = json.loads(fetch_schedule.render_json(self.RAW_LESSONS, date(2026, 9, 10), date(2026, 9, 11)))
        self.assertEqual(payload["date_start"], "2026-09-10")
        self.assertEqual(payload["date_end"], "2026-09-11")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["lessons"], self.RAW_LESSONS)

    @unittest.skipUnless(importlib.util.find_spec("ics"), "requires the ics package")
    def test_render_ics_produces_one_vevent_per_lesson_with_stable_uid(self) -> None:
        ics_text = fetch_schedule.render_ics(self.RAW_LESSONS)
        self.assertEqual(ics_text.count("BEGIN:VEVENT"), 2)
        self.assertIn("SUMMARY:[Лек] Мобильная разработка", ics_text)
        self.assertIn("SUMMARY:[Прак] Базы данных", ics_text)


class CredentialsTest(unittest.TestCase):
    def test_reads_both_env_vars(self) -> None:
        with patch.dict(os.environ, {"ITMO_ISU_USERNAME": "100000", "ITMO_ISU_PASSWORD": "secret"}, clear=False):
            self.assertEqual(fetch_schedule.credentials(), ("100000", "secret"))

    def test_raises_when_either_is_missing(self) -> None:
        with patch.dict(os.environ, {"ITMO_ISU_USERNAME": "", "ITMO_ISU_PASSWORD": ""}, clear=False):
            with self.assertRaises(ValueError):
                fetch_schedule.credentials()


class LoggingOptionsTest(unittest.TestCase):
    def test_parse_log_level_defaults_to_info(self) -> None:
        self.assertEqual(fetch_schedule.parse_log_level(None), logging.INFO)

    def test_parse_log_level_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            fetch_schedule.parse_log_level("NOT_A_LEVEL")

    def test_parse_date_rejects_malformed_input(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            fetch_schedule.parse_date("10-09-2026")


def _response(*, status_code: int = 200, text: str = "", json_data=None, headers=None, cookies=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    resp.cookies = cookies or {}
    if json_data is not None:
        resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    return resp


class AccessTokenTest(unittest.TestCase):
    LOGIN_PAGE = '... "loginAction": "https://id.itmo.ru/auth/realms/itmo/login-actions/authenticate?x=1" ...'

    def setUp(self) -> None:
        self.logger = logging.getLogger("test_itmo_schedule")
        self.logger.addHandler(logging.NullHandler())

    def test_success_returns_access_token(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text=self.LOGIN_PAGE)
        http.post.side_effect = [
            _response(status_code=302, headers={"Location": "https://my.itmo.ru/login/callback?code=abc123"}),
            _response(json_data={"access_token": "tok-123"}),
        ]
        token = fetch_schedule.get_access_token(http, "100000", "secret", self.logger)
        self.assertEqual(token, "tok-123")

    def test_missing_form_action_raises_auth_error(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text="no login form here")
        with self.assertRaises(fetch_schedule.ScheduleAuthError):
            fetch_schedule.get_access_token(http, "100000", "secret", self.logger)

    def test_non_redirect_form_response_raises_auth_error(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text=self.LOGIN_PAGE)
        http.post.return_value = _response(status_code=200)
        with self.assertRaises(fetch_schedule.ScheduleAuthError):
            fetch_schedule.get_access_token(http, "100000", "wrong-password", self.logger)

    def test_redirect_without_code_raises_auth_error(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text=self.LOGIN_PAGE)
        http.post.return_value = _response(status_code=302, headers={"Location": "https://my.itmo.ru/login/callback?error=x"})
        with self.assertRaises(fetch_schedule.ScheduleAuthError):
            fetch_schedule.get_access_token(http, "100000", "secret", self.logger)

    def test_missing_access_token_raises_auth_error(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text=self.LOGIN_PAGE)
        http.post.side_effect = [
            _response(status_code=302, headers={"Location": "https://my.itmo.ru/login/callback?code=abc123"}),
            _response(json_data={}),
        ]
        with self.assertRaises(fetch_schedule.ScheduleAuthError):
            fetch_schedule.get_access_token(http, "100000", "secret", self.logger)

    def test_network_error_raises_auth_error(self) -> None:
        http = MagicMock()
        http.get.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(fetch_schedule.ScheduleAuthError):
            fetch_schedule.get_access_token(http, "100000", "secret", self.logger)

    def test_auth_error_message_never_contains_credentials(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(text="no login form here")
        try:
            fetch_schedule.get_access_token(http, "100000", "super-secret-password", self.logger)
            self.fail("expected ScheduleAuthError")
        except fetch_schedule.ScheduleAuthError as error:
            self.assertNotIn("super-secret-password", str(error))


class FetchRawLessonsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test_itmo_schedule")
        self.logger.addHandler(logging.NullHandler())

    def test_flattens_and_sorts_lessons_across_days(self) -> None:
        http = MagicMock()
        http.get.return_value = _response(
            json_data={
                "data": [
                    {
                        "date": "2026-09-11",
                        "lessons": [{"time_start": "09:00", "time_end": "10:30", "subject": "A", "type": "Лекции"}],
                    },
                    {
                        "date": "2026-09-10",
                        "lessons": [
                            {"time_start": "13:20", "time_end": "14:50", "subject": "B", "type": "Лекции"},
                            {"time_start": "09:00", "time_end": "10:30", "subject": "C", "type": "Лекции"},
                        ],
                    },
                ]
            }
        )
        lessons = fetch_schedule.fetch_raw_lessons(http, "tok", date(2026, 9, 10), date(2026, 9, 11), self.logger)
        self.assertEqual([(item["date"], item["subject"]) for item in lessons], [
            ("2026-09-10", "C"),
            ("2026-09-10", "B"),
            ("2026-09-11", "A"),
        ])

    def test_invalid_json_raises_runtime_error(self) -> None:
        http = MagicMock()
        resp = _response()
        resp.json.side_effect = ValueError("bad json")
        http.get.return_value = resp
        with self.assertRaises(RuntimeError):
            fetch_schedule.fetch_raw_lessons(http, "tok", date(2026, 9, 10), date(2026, 9, 10), self.logger)

    def test_network_error_raises_runtime_error(self) -> None:
        http = MagicMock()
        http.get.side_effect = requests.ConnectionError("boom")
        with self.assertRaises(RuntimeError):
            fetch_schedule.fetch_raw_lessons(http, "tok", date(2026, 9, 10), date(2026, 9, 10), self.logger)


class MainTest(unittest.TestCase):
    def test_missing_credentials_exits_with_code_2(self) -> None:
        with patch.dict(os.environ, {"ITMO_ISU_USERNAME": "", "ITMO_ISU_PASSWORD": ""}, clear=False), patch.object(
            sys, "argv", ["fetch_schedule.py"]
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(fetch_schedule.main(), 2)
            self.assertIn("ITMO_ISU_USERNAME", stderr.getvalue())

    def test_successful_text_run_prints_schedule(self) -> None:
        raw_lessons = [
            {"date": date.today().isoformat(), "time_start": "09:00", "time_end": "10:30", "subject": "A", "type": "Лекции"}
        ]
        with patch.dict(
            os.environ, {"ITMO_ISU_USERNAME": "100000", "ITMO_ISU_PASSWORD": "secret"}, clear=False
        ), patch.object(sys, "argv", ["fetch_schedule.py"]), patch.object(
            fetch_schedule, "get_access_token", return_value="tok"
        ), patch.object(
            fetch_schedule, "fetch_raw_lessons", return_value=raw_lessons
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(fetch_schedule.main(), 0)
            self.assertIn("Расписание", stdout.getvalue())
            self.assertIn("[Лек] A", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

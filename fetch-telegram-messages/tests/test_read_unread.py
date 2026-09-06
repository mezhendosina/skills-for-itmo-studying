"""Unit tests for the Telegram folder fetch state helpers."""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import logging
import os
import subprocess
import stat
import sys
import tempfile
import time
import unittest
from datetime import date as calendar_date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_unread.py"
SPEC = importlib.util.spec_from_file_location("read_unread", SCRIPT)
assert SPEC and SPEC.loader
read_unread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = read_unread
SPEC.loader.exec_module(read_unread)


class FetchStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temp_path = Path(self.temporary_directory.name)

    def state_path(self) -> Path:
        return self.temp_path / "state" / "fetch-state.json"

    def require_state_helpers(self) -> None:
        for name in (
            "FetchCursor",
            "cursor_for",
            "fetch_state_path",
            "load_fetch_state",
            "new_fetch_state",
            "set_cursor",
            "write_fetch_state",
        ):
            self.assertTrue(hasattr(read_unread, name), f"missing state helper: {name}")

    def test_missing_state_uses_start_of_yesterday_in_local_time(self) -> None:
        self.require_state_helpers()
        local_now = datetime(
            2026, 9, 5, 10, 15, tzinfo=timezone(timedelta(hours=3))
        )

        cursor = read_unread.cursor_for(
            read_unread.load_fetch_state(self.state_path()),
            account_id=42,
            filter_id=9,
            now=local_now,
        )

        self.assertEqual(cursor.timestamp, datetime(2026, 9, 3, 21, tzinfo=timezone.utc))
        self.assertEqual(cursor.boundary_ids, {})

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone switching")
    def test_first_run_cutoff_uses_yesterdays_dst_offset(self) -> None:
        previous_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()

            cutoff = read_unread.first_run_cutoff(
                datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
            )
        finally:
            if previous_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_timezone
            time.tzset()

        self.assertEqual(cutoff, datetime(2026, 11, 1, 4, tzinfo=timezone.utc))

    def test_state_path_defaults_to_external_xdg_state_home(self) -> None:
        self.require_state_helpers()
        external_state_home = self.temp_path / "external-state"
        with patch.dict(
            os.environ,
            {"TELEGRAM_FETCH_STATE": "", "XDG_STATE_HOME": str(external_state_home)},
            clear=False,
        ):
            path = read_unread.fetch_state_path()

        self.assertEqual(
            path,
            (external_state_home / "telegram-yeba-unread" / "fetch-state.json").resolve(
                strict=False
            ),
        )

    def test_state_path_rejects_relative_repository_and_symlinked_paths(self) -> None:
        self.require_state_helpers()
        repository_root = SCRIPT.resolve().parents[3]
        escaped_link = self.temp_path / "state-link"
        escaped_link.symlink_to(repository_root, target_is_directory=True)

        for environment in (
            {"TELEGRAM_FETCH_STATE": "relative-state.json"},
            {"TELEGRAM_FETCH_STATE": str(repository_root / "fetch-state.json")},
            {"XDG_STATE_HOME": str(escaped_link)},
        ):
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=False
            ):
                with self.assertRaisesRegex(ValueError, "repository|absolute"):
                    read_unread.fetch_state_path()

    def test_worktree_paths_are_rejected_when_running_the_installed_skill(self) -> None:
        worktree = SCRIPT.resolve().parents[3]
        installed_script = Path.home() / ".codex" / "skills" / "fetch-telegram-messages" / "scripts" / "read_unread.py"
        cases = (
            (
                {"TELEGRAM_SESSION": str(worktree / "unsafe.session")},
                read_unread.session_path,
            ),
            (
                {"TELEGRAM_FETCH_STATE": str(worktree / "unsafe-state.json")},
                read_unread.fetch_state_path,
            ),
            (
                {
                    "TELEGRAM_FETCH_STATE": "",
                    "XDG_STATE_HOME": str(worktree / "unsafe-state-home"),
                },
                read_unread.fetch_state_path,
            ),
            (
                {
                    "TELEGRAM_SESSION": "",
                    "XDG_STATE_HOME": str(worktree / "unsafe-session-home"),
                },
                read_unread.session_path,
            ),
        )

        for environment, resolver in cases:
            with self.subTest(environment=environment), patch.object(
                read_unread, "__file__", str(installed_script)
            ), patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(ValueError, "repository"):
                    resolver()

    def test_corrupt_or_incompatible_state_is_rejected_without_overwrite(self) -> None:
        self.require_state_helpers()
        path = self.state_path()
        path.parent.mkdir(parents=True)
        original = b'{"version": 999, "cursors": {}}'
        path.write_bytes(original)

        with self.assertRaisesRegex(ValueError, "version"):
            read_unread.load_fetch_state(path)

        self.assertEqual(path.read_bytes(), original)

    def test_boolean_state_version_is_rejected_without_overwrite(self) -> None:
        path = self.state_path()
        path.parent.mkdir(parents=True)
        original = b'{"version": true, "cursors": {}}'
        path.write_bytes(original)

        with self.assertRaisesRegex(ValueError, "version"):
            read_unread.load_fetch_state(path)

        self.assertEqual(path.read_bytes(), original)

    def test_write_state_is_atomic_and_uses_restrictive_permissions(self) -> None:
        self.require_state_helpers()
        path = self.state_path()
        path.parent.mkdir(parents=True)
        path.write_text('{"old": true}', encoding="utf-8")
        state = read_unread.new_fetch_state()
        read_unread.set_cursor(
            state,
            account_id=42,
            filter_id=9,
            cursor=read_unread.FetchCursor(
                timestamp=datetime(2026, 9, 4, 21, tzinfo=timezone.utc),
                boundary_ids={100: {3, 7}},
            ),
        )

        with patch.object(read_unread.os, "replace", wraps=os.replace) as replace:
            read_unread.write_fetch_state(path, state)

        replace.assert_called_once()

        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "cursors": {
                    "42:9": {
                        "timestamp": "2026-09-04T21:00:00+00:00",
                        "boundary_ids": {"100": [3, 7]},
                    }
                },
            },
        )

    def test_state_directory_permission_failure_is_not_ignored(self) -> None:
        with patch.object(Path, "chmod", side_effect=OSError("chmod denied")):
            with self.assertRaisesRegex(OSError, "chmod denied"):
                read_unread.ensure_secure_state_directory(self.state_path())

    def test_lock_permission_failure_is_not_ignored(self) -> None:
        path = self.state_path()
        with patch.object(read_unread.os, "chmod", side_effect=OSError("lock chmod denied")):
            with self.assertRaisesRegex(OSError, "lock chmod denied"):
                with read_unread.state_transaction(path):
                    self.fail("lock must not be acquired with unknown permissions")


class SafeLoggingTest(unittest.TestCase):
    def test_date_option_parses_strict_iso_calendar_date(self) -> None:
        self.assertEqual(
            read_unread.parse_args(["--date", "2026-09-05"]).date,
            calendar_date(2026, 9, 5),
        )

        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as error:
            read_unread.parse_args(["--date", "20260905"])

        self.assertEqual(error.exception.code, 2)
        self.assertIn("YYYY-MM-DD", stderr.getvalue())

    def test_cli_level_overrides_environment_and_default_is_info(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_LOG_LEVEL": "ERROR"}, clear=False):
            self.assertEqual(
                read_unread.parse_args(["--log-level", "debug"]).log_level,
                logging.DEBUG,
            )
            self.assertEqual(read_unread.parse_args([]).log_level, logging.ERROR)
        with patch.dict(os.environ, {"TELEGRAM_LOG_LEVEL": ""}, clear=False):
            self.assertEqual(read_unread.parse_args([]).log_level, logging.INFO)

    def test_invalid_level_has_fixed_error_without_echoing_value(self) -> None:
        stderr = io.StringIO()
        sentinel = "SENTINEL_INVALID_LOG_LEVEL"
        with patch.object(sys, "stderr", stderr), self.assertRaises(SystemExit) as error:
            read_unread.parse_args(["--log-level", sentinel])

        self.assertEqual(error.exception.code, 2)
        self.assertIn("Invalid log level.", stderr.getvalue())
        self.assertNotIn(sentinel, stderr.getvalue())

    def test_dedicated_logger_is_stderr_only_and_failure_is_allowlisted(self) -> None:
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.DEBUG)
            read_unread.log_failure(logger, "authenticate", "unexpected")

        self.assertEqual(logger.name, "telegram_yeba_unread")
        self.assertFalse(logger.propagate)
        self.assertEqual(len(logger.handlers), 1)
        self.assertIs(logger.handlers[0].stream, stderr)
        self.assertEqual(
            stderr.getvalue(), "event=failed stage=authenticate error_type=unexpected\n"
        )

    def test_logs_never_contaminate_stdout_or_propagate_telethon_records(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        sentinels = (
            "SENTINEL_API_ID",
            "SENTINEL_API_HASH",
            "SENTINEL_PHONE",
            "SENTINEL_CODE",
            "SENTINEL_PASSWORD",
            "SENTINEL_SESSION_PATH",
            "SENTINEL_STATE_PATH",
            "SENTINEL_STATE_CONTENT",
            "SENTINEL_ACCOUNT_ID",
            "SENTINEL_FILTER_ID",
            "SENTINEL_CHAT_ID",
            "SENTINEL_SENDER_ID",
            "SENTINEL_TITLE",
            "SENTINEL_MESSAGE_TEXT",
            "SENTINEL_MEDIA",
            "SENTINEL_RAW_RPC",
            "SENTINEL_EXCEPTION_CLASS",
            "SENTINEL_EXCEPTION_MESSAGE",
            "SENTINEL_EXCEPTION_REPR",
            "SENTINEL_TRACEBACK",
        )
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        root.handlers = [logging.StreamHandler(stdout)]
        root.setLevel(logging.DEBUG)
        try:
            with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
                logger = read_unread.configure_logger(logging.INFO)
                read_unread.log_failure(logger, "authenticate", "unexpected")
                logging.getLogger("telethon.client").warning(" ".join(sentinels))
                print(json.dumps({"chats": []}))
        finally:
            root.handlers = original_handlers
            root.setLevel(original_level)

        self.assertEqual(json.loads(stdout.getvalue()), {"chats": []})
        for sentinel in sentinels:
            self.assertNotIn(sentinel, stdout.getvalue())
            self.assertNotIn(sentinel, stderr.getvalue())
        self.assertNotIn("telethon", stdout.getvalue().casefold())
        self.assertNotIn("telethon", stderr.getvalue().casefold())


class ExplicitAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    def require_authorization_helper(self) -> None:
        self.assertTrue(hasattr(read_unread, "authenticate_client"))
        self.assertTrue(hasattr(read_unread, "AuthenticationFailure"))
        self.assertTrue(hasattr(read_unread, "code_delivery_type"))

    def client(self, *, authorized: bool = False) -> object:
        return SimpleNamespace(
            is_user_authorized=AsyncMock(return_value=authorized),
            send_code_request=AsyncMock(return_value=SimpleNamespace(type=type("SentCodeTypeApp", (), {})())),
            sign_in=AsyncMock(return_value=None),
            send_read_acknowledge=AsyncMock(),
        )

    async def test_authorized_session_is_reused_without_prompts_or_requests(self) -> None:
        self.require_authorization_helper()
        client = self.client(authorized=True)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.INFO)
            await read_unread.authenticate_client(
                client,
                logger,
                input_reader=lambda: self.fail("authorized session must not prompt"),
            )

        client.send_code_request.assert_not_awaited()
        client.sign_in.assert_not_awaited()
        client.send_read_acknowledge.assert_not_awaited()
        self.assertEqual(stderr.getvalue(), "event=authorization status=reused\n")

    async def test_new_session_prompts_on_stderr_logs_delivery_and_signs_in(self) -> None:
        self.require_authorization_helper()
        client = self.client()
        stderr = io.StringIO()
        stdout = io.StringIO()
        phone = "SENTINEL_PHONE"
        code = "SENTINEL_CODE"
        sentinels = (phone, code, "SENTINEL_API_HASH", "SENTINEL_SESSION_PATH")
        with patch.object(sys, "stderr", stderr), patch.object(sys, "stdout", stdout):
            logger = read_unread.configure_logger(logging.INFO)
            answers = iter((phone, code))
            await read_unread.authenticate_client(client, logger, input_reader=lambda: next(answers))

        client.send_code_request.assert_awaited_once_with(phone)
        client.sign_in.assert_awaited_once_with(phone=phone, code=code)
        client.send_read_acknowledge.assert_not_awaited()
        self.assertIn("Phone number:", stderr.getvalue())
        self.assertIn("Telegram code:", stderr.getvalue())
        self.assertIn("event=code_delivery type=app", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        for sentinel in sentinels:
            self.assertNotIn(sentinel, stdout.getvalue())
            self.assertNotIn(sentinel, stderr.getvalue())

    async def test_password_required_retries_with_stderr_only_password_prompt(self) -> None:
        self.require_authorization_helper()

        class SessionPasswordNeededError(Exception):
            pass

        client = self.client()
        client.sign_in.side_effect = (SessionPasswordNeededError(), None)
        stderr = io.StringIO()
        password = "SENTINEL_PASSWORD"
        with patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.INFO)
            answers = iter(("SENTINEL_PHONE", "SENTINEL_CODE"))
            await read_unread.authenticate_client(
                client,
                logger,
                input_reader=lambda: next(answers),
                password_reader=lambda: password,
            )

        self.assertEqual(client.sign_in.await_count, 2)
        self.assertEqual(client.sign_in.await_args_list[1].kwargs, {"password": password})
        self.assertIn("Two-factor password:", stderr.getvalue())
        self.assertNotIn(password, stderr.getvalue())

    async def test_delivery_mapping_and_safe_fixed_failures(self) -> None:
        self.require_authorization_helper()
        for name, expected in (
            ("SentCodeTypeApp", "app"),
            ("SentCodeTypeSms", "sms"),
            ("SentCodeTypeCall", "call"),
            ("SentCodeTypeFlashCall", "flash_call"),
            ("SentCodeTypeUnexpected", "unknown"),
        ):
            with self.subTest(name=name):
                response = SimpleNamespace(type=type(name, (), {})())
                self.assertEqual(read_unread.code_delivery_type(response), expected)

        cases = (
            (lambda client: setattr(client, "send_code_request", AsyncMock(side_effect=RuntimeError("SENTINEL_UNKNOWN"))), lambda: "phone", None, "unexpected"),
            (lambda _client: None, lambda: (_ for _ in ()).throw(EOFError()), None, "input_aborted"),
        )
        for configure, input_reader, password_reader, error_type in cases:
            with self.subTest(error_type=error_type):
                client = self.client()
                configure(client)
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    logger = read_unread.configure_logger(logging.INFO)
                    with self.assertRaises(read_unread.AuthenticationFailure) as error:
                        await read_unread.authenticate_client(
                            client,
                            logger,
                            input_reader=input_reader,
                            password_reader=password_reader,
                        )
                self.assertEqual(str(error.exception), "Telegram authentication failed.")
                self.assertEqual(
                    stderr.getvalue(),
                    "event=authorization status=required\n"
                    f"Phone number: event=failed stage=authenticate error_type={error_type}\n",
                )
                self.assertNotIn("SENTINEL_UNKNOWN", stderr.getvalue())

    async def test_invalid_code_and_failed_two_factor_have_distinct_fixed_labels(self) -> None:
        self.require_authorization_helper()

        class PhoneCodeInvalidError(Exception):
            pass

        class SessionPasswordNeededError(Exception):
            pass

        invalid_client = self.client()
        invalid_client.sign_in.side_effect = PhoneCodeInvalidError()
        password_client = self.client()
        password_client.sign_in.side_effect = (SessionPasswordNeededError(), RuntimeError())
        for client, password_reader, expected in (
            (invalid_client, None, "invalid_code"),
            (password_client, lambda: "password", "two_factor_failed"),
        ):
            with self.subTest(error_type=expected):
                stderr = io.StringIO()
                with patch.object(sys, "stderr", stderr):
                    logger = read_unread.configure_logger(logging.INFO)
                    answers = iter(("phone", "code"))
                    with self.assertRaises(read_unread.AuthenticationFailure):
                        await read_unread.authenticate_client(
                            client,
                            logger,
                            input_reader=lambda: next(answers),
                            password_reader=password_reader,
                        )
                self.assertIn(
                    f"event=failed stage=authenticate error_type={expected}", stderr.getvalue()
                )

    async def test_reader_exceptions_map_to_safe_unexpected_failure(self) -> None:
        class SessionPasswordNeededError(Exception):
            pass

        sentinel = "SENTINEL_READER_OSERROR"
        for site in ("phone", "code", "password"):
            with self.subTest(site=site):
                client = self.client()
                if site == "phone":
                    input_reader = lambda: (_ for _ in ()).throw(OSError(sentinel))
                    password_reader = None
                elif site == "code":
                    answers = iter(("phone", OSError(sentinel)))

                    def input_reader() -> str:
                        value = next(answers)
                        if isinstance(value, Exception):
                            raise value
                        return value

                    password_reader = None
                else:
                    client.sign_in.side_effect = SessionPasswordNeededError()
                    answers = iter(("phone", "code"))
                    input_reader = lambda: next(answers)
                    password_reader = lambda: (_ for _ in ()).throw(OSError(sentinel))

                stderr = io.StringIO()
                stdout = io.StringIO()
                with patch.object(sys, "stderr", stderr), patch.object(sys, "stdout", stdout):
                    logger = read_unread.configure_logger(logging.INFO)
                    try:
                        with self.assertRaises(read_unread.AuthenticationFailure) as error:
                            await read_unread.authenticate_client(
                                client,
                                logger,
                                input_reader=input_reader,
                                password_reader=password_reader,
                            )
                    except OSError:
                        self.fail("reader exception escaped the safe authentication boundary")
                self.assertEqual(str(error.exception), "Telegram authentication failed.")
                self.assertIn(
                    "event=failed stage=authenticate error_type=unexpected", stderr.getvalue()
                )
                self.assertNotIn(sentinel, stderr.getvalue())
                self.assertNotIn(sentinel, stdout.getvalue())
                self.assertEqual(stdout.getvalue(), "")

    async def test_authentication_failure_has_safe_nonzero_main_result(self) -> None:
        args = SimpleNamespace(
            log_level=logging.INFO,
            limit_per_chat=0,
            folder="yeba",
            format="json",
            date=None,
        )
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch.object(read_unread, "parse_args", return_value=args), patch.object(
            read_unread,
            "run_fetch_transaction",
            AsyncMock(side_effect=read_unread.AuthenticationFailure()),
        ), patch.object(sys, "stderr", stderr), patch.object(sys, "stdout", stdout):
            result = await read_unread.main()

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Error: Telegram authentication failed.\n")


class WindowedFolderFetchTest(unittest.IsolatedAsyncioTestCase):
    """Behaviour of the read-only, cursor-bounded Telegram history scan."""

    def message(
        self, message_id: int, timestamp: datetime, *, out: bool = False, text: str = "message"
    ) -> object:
        return type(
            "Message",
            (),
            {
                "id": message_id,
                "date": timestamp,
                "out": out,
                "sender_id": 77,
                "message": text,
                "media": None,
                "action": None,
            },
        )()

    def dialog(
        self,
        chat_id: int,
        title: str,
        unread_count: int,
        messages: list[object],
        *,
        input_entity: object | None = None,
        entity: object | None = None,
        is_user: bool = False,
        is_group: bool = False,
        is_channel: bool = False,
        unread_mentions_count: int = 0,
        unread_mark: bool = False,
        archived: bool = False,
        muted: bool = False,
    ) -> object:
        notify_settings = SimpleNamespace(
            mute_until=int(time.time()) + 3600 if muted else 0
        )
        return SimpleNamespace(
            id=chat_id,
            name=title,
            entity=entity if entity is not None else object(),
            input_entity=input_entity,
            unread_count=unread_count,
            unread_mentions_count=unread_mentions_count,
            messages=messages,
            is_user=is_user,
            is_group=is_group,
            is_channel=is_channel,
            archived=archived,
            dialog=SimpleNamespace(
                notify_settings=notify_settings, unread_mark=unread_mark
            ),
        )

    def telethon_modules(self, client: object) -> dict[str, object]:
        class TelegramClient:
            def __init__(self, *_args: object) -> None:
                pass

            async def __aenter__(self) -> object:
                return client

            async def __aexit__(self, *_args: object) -> None:
                return None

        class GetDialogFiltersRequest:
            pass

        telethon = type("TelethonModule", (), {"TelegramClient": TelegramClient})()
        messages = type("MessagesModule", (), {"GetDialogFiltersRequest": GetDialogFiltersRequest})()
        functions = type("FunctionsModule", (), {"messages": messages})()
        tl = type("TlModule", (), {"functions": functions})()
        return {
            "telethon": telethon,
            "telethon.tl": tl,
            "telethon.tl.functions": functions,
            "telethon.tl.functions.messages": messages,
        }

    async def fetch(
        self,
        dialogs: list[object],
        cursor: object,
        upper_bound: datetime,
        *,
        filter_name: str = "yeba",
        requested_folder: str = "yeba",
        dialog_filter: object | None = None,
        logger: logging.Logger | None = None,
    ) -> object:
        selected_filter = dialog_filter or SimpleNamespace(
            id=8,
            title=filter_name,
            include_peers=[SimpleNamespace(id=dialog.id) for dialog in dialogs],
        )

        class Client:
            acknowledgements: list[object] = []
            mutations: list[object] = []

            async def is_user_authorized(self) -> bool:
                return True

            async def __call__(self, _request: object) -> list[object]:
                return SimpleNamespace(filters=[selected_filter])

            async def get_me(self) -> object:
                return type("Account", (), {"id": 42})()

            async def get_dialogs(
                self, *, limit: object, folder: int | None = None
            ) -> list[object]:
                self.folder_arguments = (limit, folder)
                return dialogs

            async def iter_messages(self, entity: object):
                dialog = next(item for item in dialogs if item.entity is entity)
                for message in dialog.messages:
                    yield message

            async def send_read_acknowledge(self, *args: object, **kwargs: object) -> None:
                self.acknowledgements.append((args, kwargs))

            async def send_message(self, *args: object, **kwargs: object) -> None:
                self.mutations.append(("send", args, kwargs))

            async def edit_message(self, *args: object, **kwargs: object) -> None:
                self.mutations.append(("edit", args, kwargs))

            async def delete_messages(self, *args: object, **kwargs: object) -> None:
                self.mutations.append(("delete", args, kwargs))

            async def forward_messages(self, *args: object, **kwargs: object) -> None:
                self.mutations.append(("forward", args, kwargs))

            async def send_reaction(self, *args: object, **kwargs: object) -> None:
                self.mutations.append(("reaction", args, kwargs))

        client = Client()
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            sys.modules, self.telethon_modules(client), clear=False
        ), patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "test-hash",
                "TELEGRAM_SESSION": str(Path(temporary_directory) / "userbot.session"),
            },
            clear=False,
        ):
            fetch_arguments = {"limit_per_chat": 0}
            if logger is not None:
                fetch_arguments["logger"] = logger
            result = await read_unread.fetch_folder_window(
                requested_folder, cursor, upper_bound, **fetch_arguments
            )

        self.assertEqual(client.folder_arguments, (None, None))
        self.assertEqual(client.acknowledgements, [])
        self.assertEqual(client.mutations, [])
        return result

    async def test_logs_safe_fetch_lifecycle(self) -> None:
        self.assertIn("logger", inspect.signature(read_unread.fetch_folder_window).parameters)
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.INFO)
            await self.fetch([], read_unread.FetchCursor(lower, {}), lower, logger=logger)

        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "event=connected",
                "event=authorization status=reused",
                "event=folder_filters count=1",
                "event=fetch_window start=2026-09-04T12:00:00+00:00 end=2026-09-04T12:00:00+00:00",
                "event=folder_dialogs count=0",
                "event=fetch_complete messages=0",
            ],
        )

    async def test_foreign_exception_keeps_its_safe_folder_classification(self) -> None:
        """Failure labels must not be stored on a third-party exception object."""

        sentinel = "SENTINEL_FOREIGN_EXCEPTION"

        class AttributeRejectingError(Exception):
            def __setattr__(self, name: str, value: object) -> None:
                if name == "_telegram_yeba_failure":
                    raise AttributeError("custom attributes are forbidden")
                super().__setattr__(name, value)

        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        stderr = io.StringIO()
        with patch.object(
            read_unread,
            "dialog_filters",
            side_effect=AttributeRejectingError(sentinel),
        ), patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.INFO)
            with self.assertRaises(AttributeRejectingError):
                await self.fetch([], read_unread.FetchCursor(lower, {}), lower, logger=logger)

        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "event=connected",
                "event=authorization status=reused",
                "event=failed stage=folder error_type=unexpected",
            ],
        )
        self.assertNotIn(sentinel, stderr.getvalue())

    async def test_selects_folder_by_exact_title(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)

        with self.assertRaisesRegex(LookupError, 'Folder "yeba" was not found'):
            await self.fetch(
                [],
                read_unread.FetchCursor(lower, {}),
                lower + timedelta(minutes=1),
                filter_name="YEBA",
            )

    async def test_collects_read_dialogs_and_orders_all_messages_chronologically(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        upper = datetime(2026, 9, 4, 12, 5, tzinfo=timezone.utc)
        dialogs = [
            self.dialog(10, "later", 0, [self.message(3, lower + timedelta(minutes=3))]),
            self.dialog(11, "earlier", 2, [self.message(4, lower + timedelta(minutes=1))]),
        ]

        result = await self.fetch(dialogs, read_unread.FetchCursor(lower, {}), upper)

        self.assertEqual(result.account_id, 42)
        self.assertEqual(result.filter_id, 8)
        self.assertEqual([(item.chat_id, item.id) for item in result.messages], [(11, 4), (10, 3)])

    async def test_uses_strict_timestamp_window_and_per_chat_boundary_ids(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        upper = datetime(2026, 9, 4, 12, 5, tzinfo=timezone.utc)
        dialogs = [
            self.dialog(
                10,
                "first",
                0,
                [
                    self.message(6, upper + timedelta(seconds=1)),
                    self.message(5, upper),
                    self.message(4, lower),
                    self.message(3, lower),
                    self.message(2, lower - timedelta(seconds=1)),
                ],
            ),
            self.dialog(11, "second", 0, [self.message(3, lower)]),
        ]

        result = await self.fetch(
            dialogs, read_unread.FetchCursor(lower, {10: {3}}), upper
        )

        self.assertEqual([(item.chat_id, item.id) for item in result.messages], [(10, 4), (11, 3), (10, 5)])

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone switching")
    async def test_local_day_window_includes_its_midnights_but_not_the_next_day(self) -> None:
        previous_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            start, end = read_unread.local_day_window(
                calendar_date(2026, 9, 4),
                now=datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
            )
            dialogs = [
                self.dialog(
                    10,
                    "chat",
                    0,
                    [
                        self.message(3, end + timedelta(seconds=1)),
                        self.message(2, end),
                        self.message(1, start),
                    ],
                )
            ]
            result = await self.fetch(dialogs, read_unread.FetchCursor(start, {}), end)
        finally:
            if previous_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_timezone
            time.tzset()

        self.assertEqual([message.id for message in result.messages], [1, 2])

    async def test_excludes_outbound_messages_without_acknowledging(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        upper = datetime(2026, 9, 4, 12, 5, tzinfo=timezone.utc)
        dialogs = [
            self.dialog(
                10,
                "chat",
                0,
                [
                    self.message(2, lower + timedelta(seconds=2), out=True),
                    self.message(1, lower + timedelta(seconds=1)),
                ],
            )
        ]

        result = await self.fetch(dialogs, read_unread.FetchCursor(lower, {}), upper)

        self.assertEqual([(item.chat_id, item.id) for item in result.messages], [(10, 1)])

    async def test_evaluates_custom_filter_membership_without_folder_request(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        upper = lower + timedelta(minutes=5)
        dialog_filter = SimpleNamespace(
            id=8,
            title="yeba",
            pinned_peers=[SimpleNamespace(user_id=1)],
            include_peers=[SimpleNamespace(user_id=2)],
            exclude_peers=[SimpleNamespace(user_id=3)],
            contacts=True,
            non_contacts=True,
            groups=True,
            broadcasts=True,
            bots=True,
            exclude_read=True,
            exclude_muted=True,
            exclude_archived=True,
        )
        timestamp = lower + timedelta(seconds=1)
        dialogs = [
            self.dialog(
                1,
                "pinned",
                0,
                [self.message(1, timestamp)],
                input_entity=SimpleNamespace(user_id=1),
                entity=SimpleNamespace(contact=False),
                is_user=True,
                archived=True,
                muted=True,
            ),
            self.dialog(
                2,
                "included",
                0,
                [self.message(2, timestamp)],
                input_entity=SimpleNamespace(user_id=2),
                entity=SimpleNamespace(contact=False),
                is_user=True,
                archived=True,
                muted=True,
            ),
            self.dialog(
                3,
                "explicitly-excluded",
                1,
                [self.message(3, timestamp)],
                input_entity=SimpleNamespace(user_id=3),
                entity=SimpleNamespace(contact=True),
                is_user=True,
            ),
            self.dialog(
                4,
                "contact",
                1,
                [self.message(4, timestamp)],
                input_entity=SimpleNamespace(user_id=4),
                entity=SimpleNamespace(contact=True),
                is_user=True,
            ),
            self.dialog(
                5,
                "non-contact",
                1,
                [self.message(5, timestamp)],
                input_entity=SimpleNamespace(user_id=5),
                entity=SimpleNamespace(contact=False),
                is_user=True,
            ),
            self.dialog(
                -6,
                "group",
                1,
                [self.message(6, timestamp)],
                input_entity=SimpleNamespace(chat_id=6),
                is_group=True,
            ),
            self.dialog(
                -1000000000007,
                "broadcast",
                1,
                [self.message(7, timestamp)],
                input_entity=SimpleNamespace(channel_id=7),
                entity=SimpleNamespace(broadcast=True),
                is_channel=True,
            ),
            self.dialog(
                8,
                "bot",
                1,
                [self.message(8, timestamp)],
                input_entity=SimpleNamespace(user_id=8),
                entity=SimpleNamespace(contact=False, bot=True),
                is_user=True,
            ),
            self.dialog(
                9,
                "read",
                0,
                [self.message(9, timestamp)],
                input_entity=SimpleNamespace(user_id=9),
                entity=SimpleNamespace(contact=True),
                is_user=True,
            ),
            self.dialog(
                10,
                "muted",
                1,
                [self.message(10, timestamp)],
                input_entity=SimpleNamespace(user_id=10),
                entity=SimpleNamespace(contact=True),
                is_user=True,
                muted=True,
            ),
            self.dialog(
                -11,
                "archived",
                1,
                [self.message(11, timestamp)],
                input_entity=SimpleNamespace(chat_id=11),
                is_group=True,
                archived=True,
            ),
        ]

        result = await self.fetch(
            dialogs,
            read_unread.FetchCursor(lower, {}),
            upper,
            dialog_filter=dialog_filter,
        )

        self.assertCountEqual([item.id for item in result.messages], [1, 2, 4, 5, 6, 7, 8])

    async def test_non_contact_rule_does_not_include_bots(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        dialog_filter = SimpleNamespace(
            id=8,
            title="yeba",
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
            contacts=False,
            non_contacts=True,
            groups=False,
            broadcasts=False,
            bots=False,
        )
        dialogs = [
            self.dialog(
                1,
                "non-contact",
                1,
                [self.message(1, lower + timedelta(seconds=1))],
                input_entity=SimpleNamespace(user_id=1),
                entity=SimpleNamespace(contact=False, bot=False),
                is_user=True,
            ),
            self.dialog(
                2,
                "bot",
                1,
                [self.message(2, lower + timedelta(seconds=1))],
                input_entity=SimpleNamespace(user_id=2),
                entity=SimpleNamespace(contact=False, bot=True),
                is_user=True,
            ),
        ]

        result = await self.fetch(
            dialogs,
            read_unread.FetchCursor(lower, {}),
            lower + timedelta(minutes=1),
            dialog_filter=dialog_filter,
        )

        self.assertEqual([item.id for item in result.messages], [1])


class StatefulFetchTransactionTest(unittest.IsolatedAsyncioTestCase):
    """The CLI transaction must not lose messages when any final step fails."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.state_path = Path(self.temporary_directory.name) / "state.json"
        self.started_at = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.message = read_unread.FolderMessage(
            chat_id=10,
            chat_title="chat",
            unread_count=0,
            id=7,
            date=self.started_at.isoformat(),
            sender_id=77,
            text="new message",
        )

    def fetch_result(self, messages: list[object] | None = None) -> object:
        return read_unread.FolderFetch(
            account_id=42,
            filter_id=8,
            fetch_started_at=self.started_at,
            messages=[self.message] if messages is None else messages,
        )

    async def execute_transaction(
        self,
        *,
        limit_per_chat: int = 0,
        output: object | None = None,
        fetch_result: object | None = None,
        logger: logging.Logger | None = None,
        selected_date: calendar_date | None = None,
    ) -> object:
        arguments = {
            "limit_per_chat": limit_per_chat,
            "output_format": "json",
            "state_path": self.state_path,
            "fetch_started_at": self.started_at,
            "stdout": output if output is not None else io.StringIO(),
            "fetcher": AsyncMock(return_value=fetch_result or self.fetch_result()),
        }
        if selected_date is not None:
            arguments["selected_date"] = selected_date
        if logger is not None:
            arguments["logger"] = logger
        return await read_unread.run_fetch_transaction(
            "yeba",
            **arguments,
        )

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone switching")
    async def test_date_mode_uses_full_local_day_without_state_access(self) -> None:
        previous_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "UTC"
            time.tzset()
            selected_date = calendar_date(2026, 9, 4)
            self.state_path.write_text("{", encoding="utf-8")
            observed: dict[str, object] = {}

            async def fetcher(
                _folder: str,
                resolve_cursor: object,
                upper_bound: datetime,
                **_kwargs: object,
            ) -> object:
                observed["cursor"] = resolve_cursor(42, 8)
                observed["upper_bound"] = upper_bound
                return self.fetch_result()

            with patch.object(
                read_unread,
                "validate_external_state_path",
                side_effect=AssertionError("date mode must not resolve state"),
            ), patch.object(
                read_unread,
                "state_transaction",
                side_effect=AssertionError("date mode must not lock state"),
            ):
                advanced = await read_unread.run_fetch_transaction(
                    "yeba",
                    limit_per_chat=0,
                    output_format="json",
                    state_path=self.state_path,
                    fetch_started_at=self.started_at,
                    stdout=io.StringIO(),
                    fetcher=fetcher,
                    selected_date=selected_date,
                )
        finally:
            if previous_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_timezone
            time.tzset()

        self.assertFalse(advanced)
        self.assertEqual(
            observed["cursor"],
            read_unread.FetchCursor(datetime(2026, 9, 4, tzinfo=timezone.utc), {}),
        )
        self.assertEqual(
            observed["upper_bound"],
            datetime(2026, 9, 4, 23, 59, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "{")

    async def test_date_mode_bypasses_default_state_and_marks_output_as_selected_day(self) -> None:
        selected_date = calendar_date(2026, 9, 4)
        output = io.StringIO()
        fetcher = AsyncMock(return_value=self.fetch_result())

        with patch.object(
            read_unread,
            "fetch_state_path",
            side_effect=AssertionError("date mode must not resolve default state"),
        ) as state_path, patch.object(
            read_unread,
            "state_transaction",
            side_effect=AssertionError("date mode must not acquire a lock"),
        ) as transaction, patch.object(
            read_unread,
            "load_fetch_state",
            side_effect=AssertionError("date mode must not load state"),
        ) as load_state, patch.object(
            read_unread,
            "write_fetch_state",
            side_effect=AssertionError("date mode must not write state"),
        ) as write_state:
            advanced = await read_unread.run_fetch_transaction(
                "yeba",
                limit_per_chat=0,
                output_format="json",
                fetch_started_at=self.started_at,
                stdout=output,
                fetcher=fetcher,
                selected_date=selected_date,
            )

        self.assertFalse(advanced)
        state_path.assert_not_called()
        transaction.assert_not_called()
        load_state.assert_not_called()
        write_state.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["date"], "2026-09-04")
        self.assertTrue(payload["preview"])
        self.assertFalse(payload["state_will_advance_on_success"])
        self.assertIn(
            "Messages for 2026-09-04",
            read_unread.render_folder_fetch(
                "yeba",
                self.fetch_result(),
                "text",
                state_advanced=False,
                selected_date=selected_date,
            ),
        )

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone switching")
    def test_local_day_window_uses_dst_midnights_and_rejects_future_date(self) -> None:
        previous_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            start, end = read_unread.local_day_window(
                calendar_date(2026, 11, 1),
                now=datetime(2026, 11, 2, 12, tzinfo=timezone.utc),
            )
            current_start, current_end = read_unread.local_day_window(
                calendar_date(2026, 11, 2),
                now=datetime(2026, 11, 2, 12, 34, 56, tzinfo=timezone.utc),
            )
            with self.assertRaisesRegex(ValueError, "future"):
                read_unread.local_day_window(
                    calendar_date(2026, 11, 3),
                    now=datetime(2026, 11, 2, 12, tzinfo=timezone.utc),
                )
        finally:
            if previous_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_timezone
            time.tzset()

        self.assertEqual(start, datetime(2026, 11, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 2, 4, 59, 59, tzinfo=timezone.utc))
        self.assertEqual(current_start, datetime(2026, 11, 2, 5, tzinfo=timezone.utc))
        self.assertEqual(current_end, datetime(2026, 11, 2, 12, 34, 56, tzinfo=timezone.utc))

    def persisted_cursor(self) -> object | None:
        state = read_unread.load_fetch_state(self.state_path)
        return state["cursors"].get("42:8")

    async def test_transaction_logs_lock_output_preview_and_commit_lifecycle(self) -> None:
        self.assertIn("logger", inspect.signature(read_unread.run_fetch_transaction).parameters)
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            logger = read_unread.configure_logger(logging.INFO)
            advanced = await self.execute_transaction(logger=logger)

        self.assertTrue(advanced)
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                "event=lock_acquired",
                "event=output_flushed",
                "event=cursor_committed",
            ],
        )

        preview_stderr = io.StringIO()
        with patch.object(sys, "stderr", preview_stderr):
            logger = read_unread.configure_logger(logging.INFO)
            advanced = await self.execute_transaction(limit_per_chat=1, logger=logger)

        self.assertFalse(advanced)
        self.assertIn("event=preview_complete", preview_stderr.getvalue())
        self.assertNotIn("event=cursor_committed", preview_stderr.getvalue())

    async def test_failed_transaction_logs_only_fixed_failure_without_commit(self) -> None:
        self.assertIn("logger", inspect.signature(read_unread.run_fetch_transaction).parameters)
        sentinel = "SENTINEL_OUTPUT_EXCEPTION"
        stderr = io.StringIO()
        with patch.object(read_unread, "render_folder_fetch", side_effect=RuntimeError(sentinel)), patch.object(
            sys, "stderr", stderr
        ):
            logger = read_unread.configure_logger(logging.INFO)
            with self.assertRaises(RuntimeError):
                await self.execute_transaction(logger=logger)

        self.assertIn("event=failed stage=output error_type=unexpected", stderr.getvalue())
        self.assertNotIn("event=cursor_committed", stderr.getvalue())
        self.assertNotIn(sentinel, stderr.getvalue())
        self.assertIsNone(self.persisted_cursor())

    async def test_failure_stages_remain_safe_across_transaction_boundaries(self) -> None:
        sentinel = "SENTINEL_PATH_ID_TITLE_MESSAGE_EXCEPTION"

        class AttributeRejectingError(Exception):
            def __setattr__(self, name: str, value: object) -> None:
                if name == "_telegram_yeba_failure":
                    raise AttributeError("custom attributes are forbidden")
                super().__setattr__(name, value)

        async def staged_fetcher(
            _folder: str, _cursor: object, _started_at: datetime, *, logger: logging.Logger, **_kwargs: object
        ) -> object:
            error = AttributeRejectingError(sentinel)
            read_unread.record_failure(logger, staged_fetcher.stage)
            raise error

        async def invoke_fetch_stage(stage: str) -> str:
            staged_fetcher.stage = stage
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                logger = read_unread.configure_logger(logging.INFO)
                with self.assertRaises(AttributeRejectingError):
                    await read_unread.run_fetch_transaction(
                        "yeba",
                        limit_per_chat=0,
                        output_format="json",
                        state_path=self.state_path,
                        fetch_started_at=self.started_at,
                        stdout=io.StringIO(),
                        fetcher=staged_fetcher,
                        logger=logger,
                    )
            return stderr.getvalue()

        for stage in ("connect", "folder", "fetch"):
            with self.subTest(stage=stage):
                output = await invoke_fetch_stage(stage)
                self.assertIn(f"event=failed stage={stage} error_type=unexpected", output)
                if stage != "fetch":
                    self.assertNotIn("event=failed stage=fetch error_type=unexpected", output)
                self.assertEqual(output.count("event=failed "), 1)
                self.assertNotIn(sentinel, output)

        configuration_stderr = io.StringIO()
        with patch.object(sys, "stderr", configuration_stderr):
            logger = read_unread.configure_logger(logging.INFO)
            with self.assertRaises(ValueError):
                await read_unread.run_fetch_transaction(
                    "yeba",
                    limit_per_chat=0,
                    output_format="json",
                    state_path=Path("relative-" + sentinel),
                    stdout=io.StringIO(),
                    logger=logger,
                )
        self.assertIn(
            "event=failed stage=configuration error_type=configuration",
            configuration_stderr.getvalue(),
        )
        self.assertNotIn(sentinel, configuration_stderr.getvalue())

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{", encoding="utf-8")
        state_stderr = io.StringIO()
        with patch.object(sys, "stderr", state_stderr):
            logger = read_unread.configure_logger(logging.INFO)
            with self.assertRaises(ValueError):
                await self.execute_transaction(logger=logger)
        self.assertIn("event=failed stage=state error_type=unexpected", state_stderr.getvalue())
        self.assertNotIn(sentinel, state_stderr.getvalue())

        class BrokenLock:
            def __enter__(self) -> None:
                raise RuntimeError(sentinel)

            def __exit__(self, *_args: object) -> None:
                return None

        lock_stderr = io.StringIO()
        with patch.object(read_unread, "state_transaction", return_value=BrokenLock()), patch.object(
            sys, "stderr", lock_stderr
        ):
            logger = read_unread.configure_logger(logging.INFO)
            with self.assertRaises(RuntimeError):
                await self.execute_transaction(logger=logger)
        self.assertIn("event=failed stage=lock error_type=unexpected", lock_stderr.getvalue())
        self.assertNotIn(sentinel, lock_stderr.getvalue())

    async def test_success_writes_output_then_advances_cursor_and_reports_it(self) -> None:
        output = io.StringIO()

        advanced = await self.execute_transaction(output=output)

        self.assertTrue(advanced)
        self.assertEqual(
            self.persisted_cursor(),
            {
                "timestamp": "2026-09-05T12:00:00+00:00",
                "boundary_ids": {"10": [7]},
            },
        )
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["state_will_advance_on_success"])
        self.assertNotIn("state_advanced", payload)

    async def test_preview_never_advances_cursor_and_reports_it(self) -> None:
        output = io.StringIO()

        advanced = await self.execute_transaction(limit_per_chat=1, output=output)

        self.assertFalse(advanced)
        self.assertIsNone(self.persisted_cursor())
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["state_will_advance_on_success"])
        self.assertNotIn("state_advanced", payload)

    async def test_telegram_failure_does_not_advance_cursor(self) -> None:
        with patch.object(
            read_unread,
            "fetch_folder_window",
            new=AsyncMock(side_effect=RuntimeError("Telegram unavailable")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Telegram unavailable"):
                await read_unread.run_fetch_transaction(
                    "yeba",
                    limit_per_chat=0,
                    output_format="json",
                    state_path=self.state_path,
                    fetch_started_at=self.started_at,
                    stdout=io.StringIO(),
                )

        self.assertIsNone(self.persisted_cursor())

    async def test_serialization_failure_does_not_advance_cursor(self) -> None:
        with patch.object(read_unread.json, "dumps", side_effect=ValueError("bad JSON")):
            with self.assertRaisesRegex(ValueError, "bad JSON"):
                await self.execute_transaction()

        self.assertIsNone(self.persisted_cursor())

    async def test_text_render_failure_does_not_advance_cursor(self) -> None:
        with patch.object(
            read_unread,
            "render_folder_fetch_text",
            side_effect=ValueError("bad text render"),
        ):
            with self.assertRaisesRegex(ValueError, "bad text render"):
                await read_unread.run_fetch_transaction(
                    "yeba",
                    limit_per_chat=0,
                    output_format="text",
                    state_path=self.state_path,
                    fetch_started_at=self.started_at,
                    stdout=io.StringIO(),
                    fetcher=AsyncMock(return_value=self.fetch_result()),
                )

        self.assertIsNone(self.persisted_cursor())

    async def test_stdout_write_and_flush_failures_do_not_advance_cursor(self) -> None:
        class BrokenOutput:
            def __init__(self, fail_at: str) -> None:
                self.fail_at = fail_at

            def write(self, _value: str) -> None:
                if self.fail_at == "write":
                    raise OSError("write failed")

            def flush(self) -> None:
                if self.fail_at == "flush":
                    raise OSError("flush failed")

        for failure in ("write", "flush"):
            with self.subTest(failure=failure), self.assertRaisesRegex(OSError, failure):
                await self.execute_transaction(output=BrokenOutput(failure))
            self.assertIsNone(self.persisted_cursor())

    async def test_state_write_failure_does_not_advance_cursor(self) -> None:
        with patch.object(read_unread, "write_fetch_state", side_effect=OSError("state failed")):
            with self.assertRaisesRegex(OSError, "state failed"):
                await self.execute_transaction()

        self.assertIsNone(self.persisted_cursor())

    async def test_same_second_boundary_ids_prevent_duplicates_and_keep_new_ids(self) -> None:
        first_output = io.StringIO()
        await self.execute_transaction(output=first_output)
        first_cursor = read_unread.cursor_for(
            read_unread.load_fetch_state(self.state_path), 42, 8
        )
        self.assertEqual(first_cursor.boundary_ids, {10: {7}})

        new_at_boundary = read_unread.FolderMessage(
            chat_id=10,
            chat_title="chat",
            unread_count=0,
            id=8,
            date=self.started_at.isoformat(),
            sender_id=77,
            text="arrived in the same second",
        )
        second_output = io.StringIO()
        observed: dict[str, object] = {}

        async def fetcher(*args: object, **_kwargs: object) -> object:
            observed["cursor"] = args[1](42, 8)
            return self.fetch_result([new_at_boundary])

        advanced = await read_unread.run_fetch_transaction(
            "yeba",
            limit_per_chat=0,
            output_format="json",
            state_path=self.state_path,
            fetch_started_at=self.started_at,
            stdout=second_output,
            fetcher=fetcher,
        )

        self.assertTrue(advanced)
        self.assertEqual(observed["cursor"], first_cursor)
        self.assertEqual(
            read_unread.cursor_for(read_unread.load_fetch_state(self.state_path), 42, 8).boundary_ids,
            {10: {7, 8}},
        )
        self.assertEqual(
            [(item["id"], item["text"]) for item in json.loads(second_output.getvalue())["messages"]],
            [(8, "arrived in the same second")],
        )

    def test_second_process_waits_for_lock_and_merges_first_cursor(self) -> None:
        timestamp = "2026-09-05T12:00:00+00:00"
        first_program = """
import importlib.util
import sys
import time
from datetime import datetime
from pathlib import Path
spec = importlib.util.spec_from_file_location('read_unread_child', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
state_path = Path(sys.argv[2])
with module.state_transaction(state_path):
    state = module.load_fetch_state(state_path)
    module.set_cursor(state, 42, 8, module.FetchCursor(datetime.fromisoformat(sys.argv[3]), {10: {1}}))
    print('locked', flush=True)
    time.sleep(0.6)
    module.write_fetch_state(state_path, state)
"""
        second_program = """
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
spec = importlib.util.spec_from_file_location('read_unread_child', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
state_path = Path(sys.argv[2])
started = time.monotonic()
with module.state_transaction(state_path):
    state = module.load_fetch_state(state_path)
    cursor = module.cursor_for(state, 42, 8)
    module.set_cursor(
        state,
        42,
        8,
        module.FetchCursor(datetime.fromisoformat(sys.argv[3]), {10: cursor.boundary_ids[10] | {2}}),
    )
    module.write_fetch_state(state_path, state)
print(json.dumps({'waited': time.monotonic() - started, 'seen': sorted(cursor.boundary_ids[10])}))
"""
        state_path = self.state_path
        command_prefix = [sys.executable, "-c"]
        first = subprocess.Popen(
            [*command_prefix, first_program, str(SCRIPT), str(state_path), timestamp],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(first.stdout.readline().strip(), "locked")
            second = subprocess.run(
                [*command_prefix, second_program, str(SCRIPT), str(state_path), timestamp],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            first_stdout, first_stderr = first.communicate(timeout=5)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_result = json.loads(second.stdout)
        self.assertGreaterEqual(second_result["waited"], 0.4)
        self.assertEqual(second_result["seen"], [1])
        self.assertEqual(
            read_unread.cursor_for(read_unread.load_fetch_state(state_path), 42, 8).boundary_ids,
            {10: {1, 2}},
        )


class WindowedFolderFetchRemainderTest(WindowedFolderFetchTest):
    async def test_unread_mentions_override_read_and_muted_exclusions(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        dialog_filter = SimpleNamespace(
            id=8,
            title="yeba",
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
            contacts=True,
            non_contacts=False,
            groups=False,
            broadcasts=False,
            bots=False,
            exclude_read=True,
            exclude_muted=True,
        )
        timestamp = lower + timedelta(seconds=1)
        dialogs = [
            self.dialog(
                1,
                "read-with-mention",
                0,
                [self.message(1, timestamp)],
                input_entity=SimpleNamespace(user_id=1),
                entity=SimpleNamespace(contact=True),
                is_user=True,
                unread_mentions_count=1,
            ),
            self.dialog(
                2,
                "muted-with-mention",
                1,
                [self.message(2, timestamp)],
                input_entity=SimpleNamespace(user_id=2),
                entity=SimpleNamespace(contact=True),
                is_user=True,
                unread_mentions_count=1,
                muted=True,
            ),
            self.dialog(
                3,
                "read-without-mention",
                0,
                [self.message(3, timestamp)],
                input_entity=SimpleNamespace(user_id=3),
                entity=SimpleNamespace(contact=True),
                is_user=True,
            ),
            self.dialog(
                4,
                "muted-without-mention",
                1,
                [self.message(4, timestamp)],
                input_entity=SimpleNamespace(user_id=4),
                entity=SimpleNamespace(contact=True),
                is_user=True,
                muted=True,
            ),
        ]

        result = await self.fetch(
            dialogs,
            read_unread.FetchCursor(lower, {}),
            lower + timedelta(minutes=1),
            dialog_filter=dialog_filter,
        )

        self.assertEqual([item.id for item in result.messages], [1, 2])

    async def test_saved_messages_is_a_contact_not_a_non_contact(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        saved_messages = self.dialog(
            1,
            "Saved Messages",
            1,
            [self.message(1, lower + timedelta(seconds=1))],
            input_entity=SimpleNamespace(user_id=1),
            entity=SimpleNamespace(self=True, contact=False, bot=False),
            is_user=True,
        )
        base_filter = {
            "id": 8,
            "title": "yeba",
            "pinned_peers": [],
            "include_peers": [],
            "exclude_peers": [],
            "groups": False,
            "broadcasts": False,
            "bots": False,
        }

        contact_result = await self.fetch(
            [saved_messages],
            read_unread.FetchCursor(lower, {}),
            lower + timedelta(minutes=1),
            dialog_filter=SimpleNamespace(
                **base_filter, contacts=True, non_contacts=False
            ),
        )
        non_contact_result = await self.fetch(
            [saved_messages],
            read_unread.FetchCursor(lower, {}),
            lower + timedelta(minutes=1),
            dialog_filter=SimpleNamespace(
                **base_filter, contacts=False, non_contacts=True
            ),
        )

        self.assertEqual([item.id for item in contact_result.messages], [1])
        self.assertEqual(non_contact_result.messages, [])

    async def test_unread_mark_overrides_read_exclusion(self) -> None:
        lower = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        dialog_filter = SimpleNamespace(
            id=8,
            title="yeba",
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
            contacts=True,
            non_contacts=False,
            groups=False,
            broadcasts=False,
            bots=False,
            exclude_read=True,
        )
        dialog = self.dialog(
            1,
            "marked unread",
            0,
            [self.message(1, lower + timedelta(seconds=1))],
            input_entity=SimpleNamespace(user_id=1),
            entity=SimpleNamespace(contact=True),
            is_user=True,
            unread_mark=True,
        )

        result = await self.fetch(
            [dialog],
            read_unread.FetchCursor(lower, {}),
            lower + timedelta(minutes=1),
            dialog_filter=dialog_filter,
        )

        self.assertEqual([item.id for item in result.messages], [1])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Read unread inbound messages from one Telegram chat folder without ACKing them."""

from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import fcntl
import getpass
import json
import logging
import os
import sys
import tempfile
import time as system_time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date as CalendarDate, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, TextIO


FETCH_STATE_VERSION = 1
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
FAILURE_STAGES = frozenset(
    {
        "configuration",
        "lock",
        "connect",
        "authenticate",
        "delivery",
        "folder",
        "fetch",
        "output",
        "state",
    }
)
FAILURE_ERROR_TYPES = frozenset(
    {
        "configuration",
        "input_aborted",
        "invalid_code",
        "two_factor_failed",
        "unexpected",
    }
)
FAILURE_METADATA: ContextVar[tuple[str, str] | None] = ContextVar(
    "telegram_yeba_failure_metadata", default=None
)


def parse_log_level(value: str | None) -> int:
    """Return an allowlisted logging level without reflecting invalid input."""

    if not value:
        return logging.INFO
    level = LOG_LEVELS.get(value.upper())
    if level is None:
        raise ValueError("Invalid log level.")
    return level


def parse_selected_date(value: str) -> CalendarDate:
    """Parse one strict ISO local calendar date for a non-advancing fetch."""

    try:
        selected_date = CalendarDate.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD.") from error
    if selected_date.isoformat() != value:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD.")
    return selected_date


def configure_logger(level: int) -> logging.Logger:
    """Create the isolated diagnostics logger and silence Telethon logging."""

    if level not in LOG_LEVELS.values():
        raise ValueError("Invalid log level.")
    logger = logging.getLogger("telegram_yeba_unread")
    for existing_handler in logger.handlers[:]:
        logger.removeHandler(existing_handler)
        existing_handler.close()
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    telethon_loggers = [logging.getLogger("telethon")]
    telethon_loggers.extend(
        candidate
        for name, candidate in logging.Logger.manager.loggerDict.items()
        if name.startswith("telethon.") and isinstance(candidate, logging.Logger)
    )
    for telethon_logger in telethon_loggers:
        for existing_handler in telethon_logger.handlers[:]:
            telethon_logger.removeHandler(existing_handler)
            existing_handler.close()
        telethon_logger.setLevel(logging.CRITICAL + 1)
        telethon_logger.propagate = False
        telethon_logger.disabled = True
    return logger


def log_failure(logger: logging.Logger, stage: str, error_type: str) -> None:
    """Emit the sole safe failure shape from closed lifecycle allowlists."""

    if stage not in FAILURE_STAGES or error_type not in FAILURE_ERROR_TYPES:
        raise ValueError("Unsupported failure event.")
    logger.error("event=failed stage=%s error_type=%s", stage, error_type)


def record_failure(
    logger: logging.Logger, stage: str, error_type: str = "unexpected"
) -> None:
    """Log fixed metadata and propagate it without modifying foreign exceptions."""

    log_failure(logger, stage, error_type)
    FAILURE_METADATA.set((stage, error_type))


def failure_metadata() -> tuple[str, str] | None:
    metadata = FAILURE_METADATA.get()
    if (
        isinstance(metadata, tuple)
        and len(metadata) == 2
        and metadata[0] in FAILURE_STAGES
        and metadata[1] in FAILURE_ERROR_TYPES
    ):
        return metadata
    return None


class AuthenticationFailure(RuntimeError):
    """A deliberately generic error for a terminal-safe authentication failure."""

    def __init__(self) -> None:
        super().__init__("Telegram authentication failed.")


def code_delivery_type(sent_code: Any) -> str:
    """Map only Telegram's public code-delivery variants to safe labels."""

    name = type(getattr(sent_code, "type", None)).__name__
    return {
        "SentCodeTypeApp": "app",
        "SentCodeTypeSms": "sms",
        "SentCodeTypeCall": "call",
        "SentCodeTypeFlashCall": "flash_call",
    }.get(name, "unknown")


def prompt_stderr(prompt: str, reader: Callable[[], str]) -> str:
    """Prompt locally without putting user input or prompts on stdout."""

    sys.stderr.write(prompt)
    sys.stderr.flush()
    return reader()


def fail_authentication(logger: logging.Logger, error_type: str) -> None:
    log_failure(logger, "authenticate", error_type)
    raise AuthenticationFailure()


async def authenticate_client(
    client: Any,
    logger: logging.Logger,
    *,
    input_reader: Callable[[], str] | None = None,
    password_reader: Callable[[], str] | None = None,
) -> None:
    """Authorize a user session with prompts and diagnostics confined to stderr."""

    try:
        if await client.is_user_authorized():
            logger.info("event=authorization status=reused")
            return
    except Exception:
        fail_authentication(logger, "unexpected")

    logger.info("event=authorization status=required")

    active_input_reader = input if input_reader is None else input_reader
    active_password_reader = (
        (lambda: getpass.getpass("")) if password_reader is None else password_reader
    )
    try:
        phone = prompt_stderr("Phone number: ", active_input_reader)
    except (EOFError, KeyboardInterrupt):
        fail_authentication(logger, "input_aborted")
    except Exception:
        fail_authentication(logger, "unexpected")
    if not phone:
        fail_authentication(logger, "input_aborted")

    try:
        sent_code = await client.send_code_request(phone)
    except Exception:
        fail_authentication(logger, "unexpected")
    logger.info("event=code_delivery type=%s", code_delivery_type(sent_code))

    try:
        code = prompt_stderr("Telegram code: ", active_input_reader)
    except (EOFError, KeyboardInterrupt):
        fail_authentication(logger, "input_aborted")
    except Exception:
        fail_authentication(logger, "unexpected")
    if not code:
        fail_authentication(logger, "input_aborted")

    try:
        await client.sign_in(phone=phone, code=code)
        return
    except Exception as error:
        if type(error).__name__ == "PhoneCodeInvalidError":
            fail_authentication(logger, "invalid_code")
        if type(error).__name__ != "SessionPasswordNeededError":
            fail_authentication(logger, "unexpected")

    try:
        password = prompt_stderr("Two-factor password: ", active_password_reader)
    except (EOFError, KeyboardInterrupt):
        fail_authentication(logger, "two_factor_failed")
    except Exception:
        fail_authentication(logger, "unexpected")
    if not password:
        fail_authentication(logger, "two_factor_failed")
    try:
        await client.sign_in(password=password)
    except Exception:
        fail_authentication(logger, "two_factor_failed")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch new inbound Telegram messages from one chat folder since the "
            "last successful fetch, without marking them read."
        )
    )
    parser.add_argument("--folder", default="yeba", help="Telegram folder title (default: yeba)")
    parser.add_argument(
        "--date",
        type=parse_selected_date,
        help="Fetch one local calendar day (YYYY-MM-DD) without changing fetch state.",
    )
    parser.add_argument(
        "--limit-per-chat",
        type=int,
        default=0,
        help=(
            "Maximum new inbound messages to display per chat; 0 fetches all and "
            "advances the cursor, while a nonzero limit is a preview (default: 0)"
        ),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=None,
        help="Diagnostic level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    )
    args = parser.parse_args(argv)
    try:
        args.log_level = parse_log_level(
            args.log_level if args.log_level is not None else os.environ.get("TELEGRAM_LOG_LEVEL")
        )
    except ValueError:
        parser.error("Invalid log level.")
    return args


def session_path() -> tuple[Path, bool]:
    configured = os.environ.get("TELEGRAM_SESSION")
    if configured:
        candidate = Path(configured).expanduser()
        is_default = False
    else:
        state_home = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
        candidate = state_home / "telegram-yeba-unread" / "userbot.session"
        is_default = True
    return validate_external_user_path(candidate, "Telegram session"), is_default


@dataclass(frozen=True)
class FetchCursor:
    """The inclusive timestamp boundary of one successful folder fetch."""

    timestamp: datetime
    boundary_ids: dict[int, set[int]]


def is_inside_git_worktree(path: Path) -> bool:
    """Whether a canonical target path is located under a Git worktree."""

    return any(
        (ancestor / ".git").is_dir() or (ancestor / ".git").is_file()
        for ancestor in (path, *path.parents)
    )


def validate_external_user_path(candidate: Path, description: str) -> Path:
    """Return a canonical external path, rejecting relative and worktree paths."""

    if not candidate.is_absolute():
        raise ValueError("TELEGRAM_FETCH_STATE and XDG_STATE_HOME must be absolute paths.")
    resolved = candidate.resolve(strict=False)
    if is_inside_git_worktree(resolved):
        raise ValueError(f"{description} must be stored outside a Git repository.")
    return resolved


def validate_external_state_path(candidate: Path) -> Path:
    """Return a canonical state path, rejecting repository and relative paths."""

    return validate_external_user_path(candidate, "Telegram fetch state")


def fetch_state_path() -> Path:
    """Resolve the account cursor location without creating state on disk."""

    configured = os.environ.get("TELEGRAM_FETCH_STATE")
    if configured:
        return validate_external_state_path(Path(configured).expanduser())

    configured_state_home = os.environ.get("XDG_STATE_HOME", "~/.local/state")
    state_home = Path(configured_state_home).expanduser()
    return validate_external_state_path(
        state_home / "telegram-yeba-unread" / "fetch-state.json"
    )


def cursor_key(account_id: int, filter_id: int) -> str:
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        raise ValueError("Telegram account ID must be an integer.")
    if isinstance(filter_id, bool) or not isinstance(filter_id, int):
        raise ValueError("Telegram folder filter ID must be an integer.")
    return f"{account_id}:{filter_id}"


def first_run_cutoff(now: datetime | None = None) -> datetime:
    """Return the start of yesterday in the host's local timezone, in UTC."""

    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    yesterday = local_now.date() - timedelta(days=1)
    local_midnight = datetime.combine(yesterday, time.min)
    return datetime.fromtimestamp(system_time.mktime(local_midnight.timetuple()), timezone.utc)


def local_day_window(
    selected_date: CalendarDate, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Return inclusive UTC bounds for one local calendar day.

    The existing message fetch accepts an inclusive upper bound, so the end is
    one second before the next local midnight. Local midnights are converted
    separately to preserve daylight-saving transitions.
    """

    current = utc_second(now or datetime.now(timezone.utc))
    if selected_date > current.astimezone().date():
        raise ValueError("Selected date must not be in the future.")
    local_start = datetime.combine(selected_date, time.min)
    local_next_start = datetime.combine(selected_date + timedelta(days=1), time.min)
    start = datetime.fromtimestamp(system_time.mktime(local_start.timetuple()), timezone.utc)
    end = datetime.fromtimestamp(system_time.mktime(local_next_start.timetuple()), timezone.utc)
    return start, min(current, end - timedelta(seconds=1))


def new_fetch_state() -> dict[str, Any]:
    return {"version": FETCH_STATE_VERSION, "cursors": {}}


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Fetch state cursor timestamp must be an ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Fetch state cursor timestamp is invalid.") from error
    if parsed.tzinfo is None:
        raise ValueError("Fetch state cursor timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def parse_boundary_ids(value: Any) -> dict[int, set[int]]:
    if not isinstance(value, dict):
        raise ValueError("Fetch state cursor boundary_ids must be an object.")
    parsed: dict[int, set[int]] = {}
    for raw_chat_id, raw_message_ids in value.items():
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Fetch state cursor chat ID is invalid.") from error
        if str(chat_id) != raw_chat_id or not isinstance(raw_message_ids, list):
            raise ValueError("Fetch state cursor boundary is invalid.")
        message_ids: set[int] = set()
        for message_id in raw_message_ids:
            if isinstance(message_id, bool) or not isinstance(message_id, int):
                raise ValueError("Fetch state cursor message ID is invalid.")
            message_ids.add(message_id)
        if len(message_ids) != len(raw_message_ids):
            raise ValueError("Fetch state cursor boundary contains duplicate message IDs.")
        parsed[chat_id] = message_ids
    return parsed


def parse_fetch_cursor(value: Any) -> FetchCursor:
    if not isinstance(value, dict) or set(value) != {"timestamp", "boundary_ids"}:
        raise ValueError("Fetch state cursor has an incompatible schema.")
    return FetchCursor(
        timestamp=parse_timestamp(value["timestamp"]),
        boundary_ids=parse_boundary_ids(value["boundary_ids"]),
    )


def validate_fetch_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != {"version", "cursors"}:
        raise ValueError("Fetch state has an incompatible schema.")
    if (
        isinstance(state["version"], bool)
        or not isinstance(state["version"], int)
        or state["version"] != FETCH_STATE_VERSION
    ):
        raise ValueError(f"Fetch state version must be {FETCH_STATE_VERSION}.")
    if not isinstance(state["cursors"], dict):
        raise ValueError("Fetch state cursors must be an object.")
    for key, cursor in state["cursors"].items():
        if not isinstance(key, str) or key.count(":") != 1:
            raise ValueError("Fetch state cursor key is invalid.")
        raw_account_id, raw_filter_id = key.split(":")
        try:
            if cursor_key(int(raw_account_id), int(raw_filter_id)) != key:
                raise ValueError("Fetch state cursor key is invalid.")
        except ValueError as error:
            raise ValueError("Fetch state cursor key is invalid.") from error
        parse_fetch_cursor(cursor)
    return state


def load_fetch_state(path: Path) -> dict[str, Any]:
    path = validate_external_state_path(path)
    if not path.exists():
        return new_fetch_state()
    try:
        with path.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Unable to read fetch state at {path}: {error}") from error
    return validate_fetch_state(state)


def cursor_for(
    state: dict[str, Any], account_id: int, filter_id: int, now: datetime | None = None
) -> FetchCursor:
    cursor = validate_fetch_state(state)["cursors"].get(cursor_key(account_id, filter_id))
    if cursor is None:
        return FetchCursor(timestamp=first_run_cutoff(now), boundary_ids={})
    return parse_fetch_cursor(cursor)


def set_cursor(
    state: dict[str, Any], account_id: int, filter_id: int, cursor: FetchCursor
) -> None:
    validate_fetch_state(state)
    if cursor.timestamp.tzinfo is None:
        raise ValueError("Fetch cursor timestamp must include a timezone.")
    normalized_boundaries: dict[str, list[int]] = {}
    for chat_id, message_ids in cursor.boundary_ids.items():
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("Fetch cursor chat ID must be an integer.")
        if any(isinstance(message_id, bool) or not isinstance(message_id, int) for message_id in message_ids):
            raise ValueError("Fetch cursor message ID must be an integer.")
        normalized_boundaries[str(chat_id)] = sorted(message_ids)
    state["cursors"][cursor_key(account_id, filter_id)] = {
        "timestamp": cursor.timestamp.astimezone(timezone.utc).isoformat(),
        "boundary_ids": normalized_boundaries,
    }


def ensure_secure_state_directory(path: Path) -> Path:
    path = validate_external_state_path(path)
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    validate_external_state_path(path)
    return directory


@contextmanager
def state_transaction(path: Path) -> Iterator[None]:
    """Hold the fetch-state lock through a read/fetch/render/write transaction."""

    path = validate_external_state_path(path)
    directory = ensure_secure_state_directory(path)
    lock_path = directory / ".fetch-state.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_fetch_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace the validated cursor JSON with user-only permissions."""

    validate_fetch_state(state)
    path = validate_external_state_path(path)
    directory = ensure_secure_state_directory(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fetch-state-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def credentials() -> tuple[int, str]:
    raw_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not raw_id or not api_hash:
        raise ValueError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH from https://my.telegram.org/apps."
        )
    try:
        return int(raw_id), api_hash
    except ValueError as error:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from error


def filter_title(dialog_filter: Any) -> str:
    title = getattr(dialog_filter, "title", "")
    return getattr(title, "text", title) or ""


def dialog_name(dialog: Any) -> str:
    return dialog.name or str(dialog.id)


def message_text(message: Any) -> str:
    if message.message:
        return message.message
    if message.media:
        return "[media without caption]"
    if message.action:
        return "[service message]"
    return "[empty message]"


@dataclass
class UnreadMessage:
    id: int
    date: str
    sender_id: int | None
    text: str


@dataclass
class UnreadChat:
    id: int
    title: str
    unread_count: int
    messages: list[UnreadMessage]


@dataclass(frozen=True)
class FolderMessage:
    """One incoming message, with enough dialog context for chronological output."""

    chat_id: int
    chat_title: str
    unread_count: int
    id: int
    date: str
    sender_id: int | None
    text: str


@dataclass(frozen=True)
class FolderFetch:
    """A read-only, fixed-window fetch for one authenticated account and folder."""

    account_id: int
    filter_id: int
    fetch_started_at: datetime
    messages: list[FolderMessage]


def utc_second(timestamp: datetime) -> datetime:
    """Normalize Telegram cursor boundaries to their one-second UTC resolution."""

    if timestamp.tzinfo is None:
        raise ValueError("Telegram message timestamps must include a timezone.")
    return timestamp.astimezone(timezone.utc).replace(microsecond=0)


def dialog_filters(response: Any) -> list[Any]:
    """Get the filters vector from Telethon's ``messages.DialogFilters`` result."""

    filters = getattr(response, "filters", response)
    if not isinstance(filters, (list, tuple)):
        raise RuntimeError("Telegram returned an invalid dialog-filter response.")
    return list(filters)


def peer_key(peer: Any) -> int | None:
    """Map Telethon input peers to the signed IDs used by custom dialogs."""

    if peer is None:
        return None
    for attribute, multiplier, offset in (
        ("user_id", 1, 0),
        ("chat_id", -1, 0),
        ("channel_id", -1, 1_000_000_000_000),
    ):
        value = getattr(peer, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return multiplier * (value + offset)
    value = getattr(peer, "id", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def filter_peer_keys(dialog_filter: Any, field: str) -> set[int]:
    peers = getattr(dialog_filter, field, None) or ()
    return {key for peer in peers if (key := peer_key(peer)) is not None}


def dialog_is_archived(dialog: Any) -> bool:
    if hasattr(dialog, "archived"):
        return bool(dialog.archived)
    folder_id = getattr(getattr(dialog, "dialog", None), "folder_id", None)
    return folder_id not in (None, 0)


def dialog_is_muted(dialog: Any) -> bool:
    if hasattr(dialog, "muted"):
        return bool(dialog.muted)
    settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = getattr(settings, "mute_until", 0)
    if isinstance(mute_until, datetime):
        return mute_until.astimezone(timezone.utc) > datetime.now(timezone.utc)
    return isinstance(mute_until, int) and not isinstance(mute_until, bool) and mute_until > system_time.time()


def dialog_matches_dynamic_categories(dialog: Any, dialog_filter: Any) -> bool:
    """Implement the category predicates represented by MTProto DialogFilter flags."""

    entity = getattr(dialog, "entity", None)
    entity_type = type(entity).__name__.casefold()
    is_user = bool(getattr(dialog, "is_user", False)) or entity_type == "user"
    is_group = bool(getattr(dialog, "is_group", False)) or entity_type in {
        "chat",
        "chatforbidden",
    } or bool(getattr(entity, "megagroup", False))
    is_channel = bool(getattr(dialog, "is_channel", False)) or entity_type == "channel"
    is_contact = bool(getattr(entity, "self", False)) or bool(
        getattr(entity, "contact", getattr(dialog, "contact", False))
    )
    is_bot = bool(getattr(entity, "bot", getattr(dialog, "bot", False)))
    is_broadcast = bool(getattr(entity, "broadcast", False)) or (
        is_channel and not is_group
    )

    return any(
        (
            bool(getattr(dialog_filter, "contacts", False))
            and is_user
            and not is_bot
            and is_contact,
            bool(getattr(dialog_filter, "non_contacts", False))
            and is_user
            and not is_bot
            and not is_contact,
            bool(getattr(dialog_filter, "groups", False)) and is_group,
            bool(getattr(dialog_filter, "broadcasts", False)) and is_broadcast,
            bool(getattr(dialog_filter, "bots", False)) and is_bot,
        )
    )


def dialog_matches_filter(dialog: Any, dialog_filter: Any) -> bool:
    """Evaluate a custom Telegram folder locally against a normal dialog list."""

    key = peer_key(getattr(dialog, "input_entity", None))
    if key is None:
        key = peer_key(dialog)
    if key is None:
        return False

    if key in filter_peer_keys(dialog_filter, "exclude_peers"):
        return False

    # Telegram exposes pinned and included peers as explicit membership lists;
    # their presence takes precedence over the dynamic exclusion flags below.
    if key in filter_peer_keys(dialog_filter, "pinned_peers") | filter_peer_keys(
        dialog_filter, "include_peers"
    ):
        return True

    if bool(getattr(dialog_filter, "exclude_archived", False)) and dialog_is_archived(dialog):
        return False
    has_unread_mentions = bool(getattr(dialog, "unread_mentions_count", 0))
    has_unread_mark = bool(getattr(getattr(dialog, "dialog", None), "unread_mark", False))
    if (
        bool(getattr(dialog_filter, "exclude_read", False))
        and not dialog.unread_count
        and not has_unread_mentions
        and not has_unread_mark
    ):
        return False
    if (
        bool(getattr(dialog_filter, "exclude_muted", False))
        and dialog_is_muted(dialog)
        and not has_unread_mentions
    ):
        return False
    return dialog_matches_dynamic_categories(dialog, dialog_filter)


async def fetch_folder_window(
    folder_name: str,
    cursor: FetchCursor | Callable[[int, int], FetchCursor],
    fetch_started_at: datetime,
    *,
    limit_per_chat: int = 0,
    logger: logging.Logger | None = None,
) -> FolderFetch:
    """Collect inbound history in ``(cursor, fetch_started_at]`` without ACKing it.

    The caller owns cursor persistence.  Keeping collection separate lets it
    render the result before advancing state in the same transaction.
    """

    if limit_per_chat < 0:
        raise ValueError("limit_per_chat must be zero or positive.")
    upper_bound = utc_second(fetch_started_at)
    diagnostics = logging.getLogger("telegram_yeba_unread") if logger is None else logger

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.messages import GetDialogFiltersRequest
    except ModuleNotFoundError as error:
        configuration_error = RuntimeError(
            "Telethon is not installed. Run: python3 -m pip install -r "
            "skills/fetch-telegram-messages/requirements.txt"
        )
        record_failure(diagnostics, "configuration", "configuration")
        raise configuration_error from error

    try:
        api_id, api_hash = credentials()
        session, is_default_session = session_path()
        session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if is_default_session:
            session.parent.chmod(0o700)
    except Exception as error:
        record_failure(diagnostics, "configuration", "configuration")
        raise

    old_umask = os.umask(0o077)
    try:
        async with TelegramClient(str(session), api_id, api_hash) as client:
            diagnostics.info("event=connected")
            await authenticate_client(client, diagnostics)
            try:
                filters = dialog_filters(await client(GetDialogFiltersRequest()))
                diagnostics.info("event=folder_filters count=%d", len(filters))
                matching_filters = [item for item in filters if filter_title(item) == folder_name]
                if len(matching_filters) != 1 or not hasattr(matching_filters[0], "id"):
                    available = sorted(filter_title(item) for item in filters if filter_title(item))
                    if len(matching_filters) > 1:
                        raise LookupError(f'Multiple folders are named "{folder_name}"; rename one first.')
                    raise LookupError(
                        f'Folder "{folder_name}" was not found. Available folders: '
                        + (", ".join(available) if available else "none")
                    )
                selected_filter = matching_filters[0]
            except Exception as error:
                record_failure(diagnostics, "folder")
                raise

            try:
                account = await client.get_me()
                account_id = getattr(account, "id", None)
                if isinstance(account_id, bool) or not isinstance(account_id, int):
                    raise RuntimeError("Telegram did not return an authenticated account ID.")

                effective_cursor = (
                    cursor(account_id, selected_filter.id) if callable(cursor) else cursor
                )
                if effective_cursor.timestamp.tzinfo is None:
                    raise ValueError("Fetch cursor timestamp must include a timezone.")
                lower_bound = utc_second(effective_cursor.timestamp)
                if upper_bound < lower_bound:
                    raise ValueError("Fetch window end must not be before its cursor.")
                diagnostics.info(
                    "event=fetch_window start=%s end=%s",
                    lower_bound.isoformat(),
                    upper_bound.isoformat(),
                )
            except Exception as error:
                record_failure(diagnostics, "fetch")
                raise

            try:
                # Telethon's ``folder`` argument represents built-in folders, not a
                # custom DialogFilter ID.  Fetch the source/destination dialogs once
                # and evaluate the selected custom filter locally instead.
                dialogs = await client.get_dialogs(limit=None)
                diagnostics.info("event=folder_dialogs count=%d", len(dialogs))
                collected: list[FolderMessage] = []
                for dialog in dialogs:
                    if not dialog_matches_filter(dialog, selected_filter):
                        continue
                    chat_id = dialog.id
                    boundary_ids = effective_cursor.boundary_ids.get(chat_id, set())
                    messages_for_chat = 0
                    async for message in client.iter_messages(dialog.entity):
                        message_timestamp = utc_second(message.date)
                        if message_timestamp > upper_bound:
                            continue
                        if message_timestamp < lower_bound:
                            break
                        if message_timestamp == lower_bound and message.id in boundary_ids:
                            continue
                        if message.out:
                            continue
                        collected.append(
                            FolderMessage(
                                chat_id=chat_id,
                                chat_title=dialog_name(dialog),
                                unread_count=dialog.unread_count,
                                id=message.id,
                                date=message_timestamp.isoformat(),
                                sender_id=message.sender_id,
                                text=message_text(message),
                            )
                        )
                        messages_for_chat += 1
                        if limit_per_chat and messages_for_chat >= limit_per_chat:
                            break

                collected.sort(key=lambda item: (item.date, item.chat_id, item.id))
                diagnostics.info("event=fetch_complete messages=%d", len(collected))
                return FolderFetch(
                    account_id=account_id,
                    filter_id=selected_filter.id,
                    fetch_started_at=upper_bound,
                    messages=collected,
                )
            except Exception as error:
                record_failure(diagnostics, "fetch")
                raise
    except AuthenticationFailure:
        raise
    except Exception:
        if failure_metadata() is None:
            record_failure(diagnostics, "connect")
        raise
    finally:
        os.umask(old_umask)
        if session.exists():
            try:
                session.chmod(0o600)
            except OSError:
                pass


async def read_folder(folder_name: str, limit_per_chat: int) -> list[UnreadChat]:
    try:
        from telethon import TelegramClient
        from telethon.tl.functions.messages import GetDialogFiltersRequest
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Telethon is not installed. Run: python3 -m pip install -r "
            "skills/fetch-telegram-messages/requirements.txt"
        ) from error

    api_id, api_hash = credentials()
    session, is_default_session = session_path()
    session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if is_default_session:
        try:
            session.parent.chmod(0o700)
        except OSError:
            pass

    old_umask = os.umask(0o077)
    try:
        async with TelegramClient(str(session), api_id, api_hash) as client:
            await authenticate_client(client, logging.getLogger("telegram_yeba_unread"))
            filters = await client(GetDialogFiltersRequest())
            matching_filters = [
                item
                for item in filters
                if filter_title(item).casefold() == folder_name.casefold()
            ]
            if len(matching_filters) != 1 or not hasattr(matching_filters[0], "id"):
                available = sorted(filter_title(item) for item in filters if filter_title(item))
                if len(matching_filters) > 1:
                    raise LookupError(f'Multiple folders are named "{folder_name}"; rename one first.')
                raise LookupError(
                    f'Folder "{folder_name}" was not found. Available folders: '
                    + (", ".join(available) if available else "none")
                )
            selected_filter = matching_filters[0]

            # Asking Telegram for this folder's dialogs follows both explicit and
            # dynamic folder rules. Neither call acknowledges messages.
            dialogs = await client.get_dialogs(limit=None, folder=selected_filter.id)
            unread_chats: list[UnreadChat] = []
            for dialog in dialogs:
                if not dialog.unread_count:
                    continue
                read_to = getattr(dialog.dialog, "read_inbox_max_id", 0) or 0
                messages: list[UnreadMessage] = []
                async for message in client.iter_messages(dialog.entity, min_id=read_to):
                    if message.out or message.id <= read_to:
                        continue
                    messages.append(
                        UnreadMessage(
                            id=message.id,
                            date=message.date.astimezone(timezone.utc).isoformat(),
                            sender_id=message.sender_id,
                            text=message_text(message),
                        )
                    )
                    if limit_per_chat and len(messages) >= limit_per_chat:
                        break
                messages.reverse()
                unread_chats.append(
                    UnreadChat(
                        id=dialog.id,
                        title=dialog_name(dialog),
                        unread_count=dialog.unread_count,
                        messages=messages,
                    )
                )
            return unread_chats
    finally:
        os.umask(old_umask)
        if session.exists():
            try:
                session.chmod(0o600)
            except OSError:
                pass


def render_text(folder_name: str, chats: list[UnreadChat]) -> str:
    lines = [f'Folder: {folder_name}', f'Chats with unread messages: {len(chats)}']
    for chat in chats:
        lines.append(f'\n{chat.title} ({chat.unread_count} unread)')
        if not chat.messages:
            lines.append("  [No unread message body was returned]")
        for message in chat.messages:
            sender = f" sender={message.sender_id}" if message.sender_id else ""
            lines.append(f"  [{message.date}] id={message.id}{sender}: {message.text}")
    return "\n".join(lines)


def cursor_after_fetch(
    fetch: FolderFetch, previous_cursor: FetchCursor
) -> FetchCursor:
    """Build the next cursor from the fixed fetch boundary and its inbound IDs."""

    timestamp = utc_second(fetch.fetch_started_at)
    boundary_ids = (
        {chat_id: set(message_ids) for chat_id, message_ids in previous_cursor.boundary_ids.items()}
        if utc_second(previous_cursor.timestamp) == timestamp
        else {}
    )
    for message in fetch.messages:
        if utc_second(datetime.fromisoformat(message.date)) == timestamp:
            boundary_ids.setdefault(message.chat_id, set()).add(message.id)
    return FetchCursor(timestamp=timestamp, boundary_ids=boundary_ids)


def render_folder_fetch_text(
    folder_name: str,
    fetch: FolderFetch,
    *,
    state_advanced: bool,
    selected_date: CalendarDate | None = None,
) -> str:
    lines = [
        f"Folder: {folder_name}",
        (
            f"Messages for {selected_date.isoformat()}: {len(fetch.messages)}"
            if selected_date is not None
            else f"New inbound messages: {len(fetch.messages)}"
        ),
        "State update: "
        + (
            "will advance after successful output and state commit"
            if state_advanced
            else "will not advance (preview)"
        ),
    ]
    for message in fetch.messages:
        sender = f" sender={message.sender_id}" if message.sender_id else ""
        lines.append(
            f"[{message.date}] {message.chat_title} id={message.id}{sender}: {message.text}"
        )
    return "\n".join(lines)


def render_folder_fetch(
    folder_name: str,
    fetch: FolderFetch,
    output_format: str,
    *,
    state_advanced: bool,
    selected_date: CalendarDate | None = None,
) -> str:
    """Serialize all output before it is written, so failures retain the cursor."""

    if output_format == "json":
        payload: dict[str, Any] = {
            "folder": folder_name,
            "messages": [asdict(message) for message in fetch.messages],
            "preview": not state_advanced,
            # State is committed only after stdout is written and flushed.
            # Do not report a commit that has not happened yet.
            "state_will_advance_on_success": state_advanced,
        }
        if selected_date is not None:
            payload["date"] = selected_date.isoformat()
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
    if output_format == "text":
        return render_folder_fetch_text(
            folder_name,
            fetch,
            state_advanced=state_advanced,
            selected_date=selected_date,
        )
    raise ValueError(f"Unsupported output format: {output_format}")


async def run_fetch_transaction(
    folder_name: str,
    *,
    limit_per_chat: int,
    output_format: str,
    state_path: Path | None = None,
    fetch_started_at: datetime | None = None,
    stdout: TextIO | Any | None = None,
    fetcher: Callable[..., Awaitable[FolderFetch]] | None = None,
    logger: logging.Logger | None = None,
    selected_date: CalendarDate | None = None,
) -> bool:
    """Fetch, output, and persist one folder cursor under a single lock.

    ``True`` means the output was flushed and the cursor was atomically
    persisted. A limited invocation is intentionally a non-advancing preview.
    """

    if limit_per_chat < 0:
        raise ValueError("limit_per_chat must be zero or positive.")
    diagnostics = logging.getLogger("telegram_yeba_unread") if logger is None else logger
    FAILURE_METADATA.set(None)
    output = sys.stdout if stdout is None else stdout
    started_at = utc_second(fetch_started_at or datetime.now(timezone.utc))
    active_fetcher = fetch_folder_window if fetcher is None else fetcher

    if selected_date is not None:
        day_start, day_end = local_day_window(selected_date, now=started_at)
        date_cursor = FetchCursor(day_start, {})
        try:
            fetch = await active_fetcher(
                folder_name,
                lambda _account_id, _filter_id: date_cursor,
                day_end,
                limit_per_chat=limit_per_chat,
                logger=diagnostics,
            )
        except AuthenticationFailure:
            raise
        except Exception:
            if failure_metadata() is None:
                record_failure(diagnostics, "fetch")
            raise
        try:
            rendered = render_folder_fetch(
                folder_name,
                fetch,
                output_format,
                state_advanced=False,
                selected_date=selected_date,
            )
            output.write(rendered + "\n")
            output.flush()
        except Exception:
            record_failure(diagnostics, "output")
            raise
        diagnostics.info("event=output_flushed")
        diagnostics.info("event=preview_complete")
        return False

    try:
        target_state_path = (
            fetch_state_path() if state_path is None else validate_external_state_path(state_path)
        )
    except Exception as error:
        record_failure(diagnostics, "configuration", "configuration")
        raise
    advances_state = limit_per_chat == 0
    failure_logged = False

    try:
        with state_transaction(target_state_path):
            diagnostics.info("event=lock_acquired")
            try:
                state = load_fetch_state(target_state_path)
            except Exception:
                record_failure(diagnostics, "state")
                failure_logged = True
                raise

            def resolve_cursor(account_id: int, filter_id: int) -> FetchCursor:
                return cursor_for(state, account_id, filter_id, now=started_at)

            try:
                fetch = await active_fetcher(
                    folder_name,
                    resolve_cursor,
                    started_at,
                    limit_per_chat=limit_per_chat,
                    logger=diagnostics,
                )
            except AuthenticationFailure:
                raise
            except Exception as error:
                if failure_metadata() is None:
                    record_failure(diagnostics, "fetch")
                failure_logged = True
                raise
            try:
                rendered = render_folder_fetch(
                    folder_name, fetch, output_format, state_advanced=advances_state
                )
                output.write(rendered + "\n")
                output.flush()
            except Exception:
                record_failure(diagnostics, "output")
                failure_logged = True
                raise
            diagnostics.info("event=output_flushed")
            if not advances_state:
                diagnostics.info("event=preview_complete")
                return False

            try:
                set_cursor(
                    state,
                    fetch.account_id,
                    fetch.filter_id,
                    cursor_after_fetch(
                        fetch,
                        cursor_for(state, fetch.account_id, fetch.filter_id, now=started_at),
                    ),
                )
                write_fetch_state(target_state_path, state)
            except Exception:
                record_failure(diagnostics, "state")
                failure_logged = True
                raise
            diagnostics.info("event=cursor_committed")
            return True
    except AuthenticationFailure:
        raise
    except Exception:
        if not failure_logged:
            record_failure(diagnostics, "lock")
        raise


async def main() -> int:
    args = parse_args()
    logger = configure_logger(args.log_level)
    if args.limit_per_chat < 0:
        print("--limit-per-chat must be zero or positive.", file=sys.stderr)
        return 2
    try:
        await run_fetch_transaction(
            args.folder,
            limit_per_chat=args.limit_per_chat,
            output_format=args.format,
            logger=logger,
            selected_date=args.date,
        )
    except AuthenticationFailure:
        print("Error: Telegram authentication failed.", file=sys.stderr)
        return 2
    except (ValueError, LookupError, RuntimeError):
        print("Error: Telegram request failed.", file=sys.stderr)
        return 2
    except Exception:
        print("Telegram request failed.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

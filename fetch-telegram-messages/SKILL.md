---
name: fetch-telegram-messages
description: Use when checking new inbound Telegram messages in chats collected in the user's "yeba" chat folder through the user's own Telegram account.
---

# Fetch Telegram Messages

Fetch every new inbound message from the Telegram folder named `yeba`. This
userbot authenticates as the user's existing Telegram account, rather than
using the Bot API. “New” is measured from the last successful fetch, not from
Telegram's unread counter, so already-marked-read incoming messages are
included when their chats match the selected folder at fetch time.

## Safety

- Never request, print, commit, or place the API hash, session, or fetch state
  in the repository. Read credentials from `TELEGRAM_API_ID` and
  `TELEGRAM_API_HASH`. Credentials and session/state paths or contents must
  never appear in either `stdout` or `stderr`.
- The first run prompts for the account phone number, login code, and, if
  enabled, two-step-verification password. Later runs reuse a local session.
- Prompts are written only to `stderr`; never copy their values, credentials,
  session/state paths or contents, or raw Telegram errors into logs, either
  stream, or repository files.
- The reader does not call Telegram's read-acknowledgement API. It never sends,
  edits, deletes, forwards, or reacts to messages.
- Read only the requested folder. Do not use a different folder unless the user
  asks.

## Login and diagnostics

Use `--log-level DEBUG|INFO|WARNING|ERROR|CRITICAL` to control diagnostics.
When that option is omitted, `TELEGRAM_LOG_LEVEL` is used; otherwise the
default is `INFO`. An explicit `--log-level` takes precedence over
`TELEGRAM_LOG_LEVEL`. Diagnostics and login prompts go only to `stderr`, so
`stdout` remains the text or JSON message-data channel. The authorization flow
reuses an authorized session, otherwise requests a Telegram code and reports
only its delivery category: `app`, `sms`, `call`, `flash_call`, or `unknown`.

Failures use fixed lifecycle labels rather than exception details. Do not print
or log raw exception messages, tracebacks, or RPC responses. Diagnostics never
include chat identifiers, titles, or message content. Collected message data
is intentionally written only to `stdout` in the requested text or JSON
format. This documentation describes the implemented, offline-tested flow; it
does not claim that a real Telegram login has been verified in the current
environment.

## State and fetch semantics

The default fetch-state file is
`$XDG_STATE_HOME/telegram-yeba-unread/fetch-state.json`, or
`~/.local/state/telegram-yeba-unread/fetch-state.json` when
`XDG_STATE_HOME` is unset. It is outside the repository, permission-restricted,
and keyed by Telegram account ID plus folder ID; renaming a folder does not
reset its cursor.

Set `TELEGRAM_FETCH_STATE` to use a different absolute state-file path. It
must resolve outside the repository, including through symlinks. The same
external-only rule applies to `TELEGRAM_SESSION`; its default is
`$XDG_STATE_HOME/telegram-yeba-unread/userbot.session` (or
`~/.local/state/telegram-yeba-unread/userbot.session`).

These external session and state paths intentionally keep their legacy
`telegram-yeba-unread` directory name, so renaming the skill does not require
another login or reset the existing fetch cursor.

With no prior successful fetch for this account and folder, collection starts
at local yesterday `00:00` (not a rolling 24-hour interval). Later fetches use
the stored cursor and collect all incoming messages in the selected folder up
to the start of the current fetch. The cursor advances only after Telegram
fetching, output writing and flushing, and the atomic state-file replacement
all succeed; a failed or partial run leaves it unchanged.

`--limit-per-chat` defaults to `0`, meaning unlimited output and normal cursor
advancement. Any nonzero limit is a preview: output can be truncated and the
cursor deliberately does not advance. In JSON output,
`state_will_advance_on_success` is `true` only for an unlimited fetch whose
output and state commit can complete successfully; it is a pre-commit,
conditional status rather than a report that state has already advanced.
`preview` is the inverse.

Use `--date YYYY-MM-DD` to collect messages for one local calendar day. This
is always a non-advancing historical view: it does not resolve, lock, read, or
write the fetch-state file, even with `--limit-per-chat 0`. The selected day
includes local `00:00:00` through `23:59:59`; for today it ends at the fetch
start time. Future dates are rejected. Text output calls these "Messages for
YYYY-MM-DD" rather than new messages; JSON includes a `date` field.

## Run

Install the isolated dependency once, then run the installed personal skill:

```bash
python3 -m venv /private/tmp/telegram-yeba-venv
/private/tmp/telegram-yeba-venv/bin/pip install -r ~/.codex/skills/fetch-telegram-messages/requirements.txt
export TELEGRAM_API_ID='123456'
export TELEGRAM_API_HASH='your_api_hash'
/private/tmp/telegram-yeba-venv/bin/python ~/.codex/skills/fetch-telegram-messages/scripts/read_unread.py

```

Create API credentials for the user's account at
`https://my.telegram.org/apps`. Keep the API hash and all session/state files
outside the repository.

Use `--folder NAME` only when the user requests another folder,
`--limit-per-chat N` for a non-advancing preview, and `--format json` for
machine-readable output. Use `--date YYYY-MM-DD` for a selected day without
affecting the normal cursor. Use `--log-level LEVEL` for diagnostics and run
`--help` before using other options.

## Expected output

The command reports the selected folder and all collected incoming messages in
global chronological order. An empty message list means no messages arrived in
the selected folder since the applicable cursor. Text output says whether state
will advance after successful output and commit; JSON exposes the conditional
`state_will_advance_on_success` flag and `preview` status.

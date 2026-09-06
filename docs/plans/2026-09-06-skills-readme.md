
# Skills Repository README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development skill (recommended) or executing-plans skill to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a concise Russian-language `README.md` that explains the two agent skills in this repository and shows how to install them with the `skills` CLI.

**Architecture:** Keep the repository documentation in one root-level Markdown file. Lead with the repository-wide `npx skills add` flow, then document per-skill installation, Codex global installation, skill-specific prerequisites and limitations, and a short installation check. Treat the existing skill files and the approved design as the source of truth; do not change either skill.

**Tech Stack:** Markdown, `skills` CLI invoked through `npx`, shell-based documentation checks, `markdown-format`

---

## File Map

- Create: `README.md` — public overview, installation guide, skill catalog, setup notes, limitations, and verification.
- Reference only: `docs/specs/2026-09-06-skills-readme-design.md` — approved README requirements and source list.
- Reference only: `fetch-telegram-messages/SKILL.md` — canonical Telegram skill name, behavior, security rules, and runtime setup.
- Reference only: `fetch-telegram-messages/requirements.txt` — canonical Python dependency constraint.
- Reference only: `vkr-latex-check/SKILL.md` — canonical LaTeX skill name, evidence boundaries, and limitations.

The repository has already been initialized non-interactively for OpenSpec with `openspec init --tools none --language ru --no-animation .`. Implementation must not edit the generated `openspec/` files. This is a documentation-only change, so no application source files or test files are needed.

### Task 1: Write the repository README

**Files:**

- Create: `README.md`
- Read: `docs/specs/2026-09-06-skills-readme-design.md`
- Read: `fetch-telegram-messages/SKILL.md`
- Read: `fetch-telegram-messages/requirements.txt`
- Read: `vkr-latex-check/SKILL.md`

- [ ] **Step 1: Confirm the implementation baseline**

Run:

```bash
test ! -e README.md
git remote get-url origin
sed -n '2p' fetch-telegram-messages/SKILL.md
sed -n '2p' vkr-latex-check/SKILL.md

```

Expected: the first command exits successfully, the remote is `git@github.com:mezhendosina/skills-for-itmo-studying.git`, and the two metadata lines are exactly `name: fetch-telegram-messages` and `name: vkr-latex-check`. If `README.md` already exists, stop and inspect it instead of overwriting it.

- [ ] **Step 2: Create the complete README with `apply_patch`**

Create `README.md` with the following content. Preserve the exact repository and skill identifiers in every command.

````markdown

# Skills for ITMO studying

Коллекция скиллов для AI-агентов, которые помогают работать с учебными материалами и оформлением ВКР.

## Требования

- Node.js `22.20.0` или новее;
- доступная команда `npx`.

## Быстрый старт

Установите коллекцию через [`skills` CLI](https://www.skills.sh/docs/cli):

```bash
npx skills add mezhendosina/skills-for-itmo-studying

```

CLI предложит выбрать нужные скиллы и поддерживаемых агентов.

### Установить один скилл

```bash
npx skills add mezhendosina/skills-for-itmo-studying --skill vkr-latex-check
npx skills add mezhendosina/skills-for-itmo-studying --skill fetch-telegram-messages

```

### Установить глобально для Codex

```bash
npx skills add mezhendosina/skills-for-itmo-studying --agent codex --global

```

## Скиллы

| Скилл | Назначение | Что требуется |
| --- | --- | --- |
| [`vkr-latex-check`](vkr-latex-check/) | Проверка LaTeX-проекта ВКР и собранного PDF по требованиям факультета прикладной информатики | Исходники TEX; для полной визуальной проверки — связанный с ними PDF и доступные PDF-инструменты |
| [`fetch-telegram-messages`](fetch-telegram-messages/) | Получение новых входящих сообщений из папки Telegram `yeba` через пользовательский аккаунт | Python 3, Telethon, Telegram API ID и API hash, внешние файлы сессии и состояния |

## Настройка `fetch-telegram-messages`

После глобальной установки для Codex создайте отдельное Python-окружение и установите зависимость:

```bash
python3 -m venv /private/tmp/telegram-yeba-venv
/private/tmp/telegram-yeba-venv/bin/pip install -r ~/.codex/skills/fetch-telegram-messages/requirements.txt

```

Получите API ID и API hash на [my.telegram.org/apps](https://my.telegram.org/apps), затем передайте их только через переменные окружения:

```bash
export TELEGRAM_API_ID='your_api_id'
export TELEGRAM_API_HASH='your_api_hash'

```

Не сохраняйте реальные значения, session-файлы или состояние загрузки в репозитории. По умолчанию скилл хранит сессию и состояние вне репозитория; пути можно явно задать через `TELEGRAM_SESSION` и `TELEGRAM_FETCH_STATE`, и они также должны оставаться внешними.

Первый запуск интерактивно запросит номер телефона, код входа и, если включена, двухфакторную аутентификацию. Скилл не отправляет подтверждение прочтения, сообщения, реакции или другие изменения в Telegram. Реальный вход зависит от локальных учётных данных и не заявляется как заранее проверенный.

## Ограничения `vkr-latex-check`

Скилл проверяет работу по вложенному реестру требований и явно разделяет выводы статического анализа TEX и визуальные доказательства из PDF. Без корневого TEX-файла, полного состава проекта или доказанной связи PDF с исходниками часть критериев останется со статусом «не проверено».

Результат не гарантирует прохождение официального нормоконтроля и не подтверждает соответствие требованиям, которых нет во вложенном реестре.

## Проверка установки

Для глобальной установки в Codex выведите список установленных скиллов:

```bash
npx skills list --agent codex --global

```

В списке должны присутствовать выбранные `vkr-latex-check` и/или `fetch-telegram-messages`.

Подробнее о командах и параметрах — в [документации `skills` CLI](https://www.skills.sh/docs/cli).

````

Expected: `README.md` contains no popularity counters, secrets, claims of verified live Telegram login, or promises beyond the two skills' documented behavior.

- [ ] **Step 3: Compare the README against the approved requirements**

Read `README.md` from top to bottom and verify all of the following manually:

- the main installation command uses `mezhendosina/skills-for-itmo-studying` exactly;
- both `--skill` commands use the exact `name` values from the corresponding `SKILL.md` files;
- the Codex command contains both `--agent codex` and `--global`;
- the Node.js requirement is `22.20.0` or newer;
- Telegram setup mentions Python, Telethon installation, both credential variables, external session/state storage, first-run login, and no read acknowledgement;
- LaTeX limitations distinguish TEX evidence from PDF evidence and disclaim official norm-control guarantees;
- the only documented skills are `vkr-latex-check` and `fetch-telegram-messages`.

Expected: every item is present once in the appropriate section and no unsupported capability has been added.

### Task 2: Validate Markdown, links, and the final diff

**Files:**

- Validate: `README.md`
- Validate: `docs/plans/2026-09-06-skills-readme.md`

- [ ] **Step 1: Format the README with the repository Markdown formatter**

Use the `@markdown-format` skill and run:

```bash
/Users/mezhendosina/.agents/skills/markdown-format/fix_markdown_format.sh --files README.md

```

Expected: the command exits with status `0` and reports `README.md` as checked or formatted.

- [ ] **Step 2: Verify every relative target mentioned by the README exists**

Run:

```bash
test -d vkr-latex-check
test -f vkr-latex-check/SKILL.md
test -d fetch-telegram-messages
test -f fetch-telegram-messages/SKILL.md
test -f fetch-telegram-messages/requirements.txt

```

Expected: every command exits with status `0`.

- [ ] **Step 3: Check whitespace and final newline**

Run:

```bash
if LC_ALL=C grep -nE '[[:blank:]]+$' README.md; then exit 1; fi
tail -c 1 README.md | od -An -t u1

```

Expected: the regular-expression check prints nothing and exits with status `0`; `od` prints `10`, confirming a final LF byte. The regex check is intentional because `README.md` is untracked and therefore is not covered by a normal `git diff --check` yet.

- [ ] **Step 4: Review the complete documentation diff**

Run:

```bash
git diff --no-index -- /dev/null README.md
git status --short

```

Expected: `git diff --no-index` prints the complete new-file diff and exits with status `1` because differences exist; this exit code is expected. The README diff matches the approved outline and exact content above. Status shows `README.md` plus the already-created planning/OpenSpec artifacts, with no changes inside either skill directory and no unrelated files.

- [ ] **Step 5: Perform the final safety review without executing installation**

Inspect the rendered Markdown or source diff and confirm:

- shell blocks contain placeholders only, never real credentials;
- local external-state paths are examples and are never committed as files;
- all external links use HTTPS;
- installation commands are documentation only and were not run as part of validation;
- no commit, push, or pull request is created by this plan.

Expected: the documentation is ready for the repository owner to review and commit separately.

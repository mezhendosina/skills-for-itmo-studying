
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

CLI предложит выбрать нужные скиллы. Если найден только один поддерживаемый агент, CLI может выбрать его автоматически; иначе предложит выбрать агента интерактивно.

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
/private/tmp/telegram-yeba-venv/bin/pip install -r ~/.agents/skills/fetch-telegram-messages/requirements.txt

```

Получите API ID и API hash на [my.telegram.org/apps](https://my.telegram.org/apps), затем безопасно запросите их в текущей сессии zsh без отображения ввода и экспортируйте переменные окружения:

```zsh
read -r -s 'TELEGRAM_API_ID?Telegram API ID: '
printf '\n'
read -r -s 'TELEGRAM_API_HASH?Telegram API hash: '
printf '\n'
export TELEGRAM_API_ID TELEGRAM_API_HASH

```

Не сохраняйте реальные значения, session-файлы или состояние загрузки в репозитории. По умолчанию скилл хранит сессию и состояние вне репозитория; пути можно явно задать через `TELEGRAM_SESSION` и `TELEGRAM_FETCH_STATE`, и они также должны оставаться внешними.

Первый запуск интерактивно запросит номер телефона, код входа и, если включена, двухфакторную аутентификацию. Этот вход авторизует клиент и создаёт или обновляет локальный session-файл. Скилл не отправляет подтверждения прочтения, а также не отправляет, не редактирует, не удаляет и не пересылает сообщения и не ставит реакции. Реальный вход зависит от локальных учётных данных и не заявляется как заранее проверенный.

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

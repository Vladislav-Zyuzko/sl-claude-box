# sl-claude-box

Два контейнера: `claude-worker` (Claude Code + тулчейн Flutter, доступ по SSH) и `telegram-bot`
(SLNexus — Telegram-интерфейс к воркеру). Код sweet_limit живёт в `./workspace`, она смонтирована
в воркер и переживает пересборку.

## Запуск

```bash
cp .env.example .env      # заполнить токены
docker compose -f sl-docker-compose.yml up -d --build
```

Базовый образ `sl-claude-box:latest` собирается отдельно корневым `Dockerfile` — compose его НЕ
пересобирает:

```bash
docker build -t sl-claude-box:latest .
```

## Обновление

Бот (`tg-bot/bot.py`) смонтирован как volume, поэтому достаточно перезапуска:

```bash
docker compose -f sl-docker-compose.yml restart telegram-bot
```

Скрипты воркера (`setup-repo.sh`, `restore-repo.sh`, `nexus-state`, …) лежат внутри образа,
поэтому после их правки нужна пересборка:

```bash
docker compose -f sl-docker-compose.yml up -d --build claude-worker
```

### Claude Code

Версия задаётся аргументом сборки `CLAUDE_CODE_VERSION` (в `.env`): `latest` или точная версия
для пина и отката. Обычная пересборка берёт слой Claude из кеша, то есть версия сама не меняется.
Чтобы подтянуть свежую:

```bash
docker compose -f sl-docker-compose.yml build --build-arg CLAUDE_REFRESH=$(date +%s) claude-worker
docker compose -f sl-docker-compose.yml up -d claude-worker
```

Проверить, что стоит сейчас и что вышло в npm:

```bash
docker exec claude-worker claude --version
npm view @anthropic-ai/claude-code version
```

Автообновление внутри контейнера выключено (`DISABLE_AUTOUPDATER=1`): claude установлен от root в
`/usr/local`, а задачи идут под `claude-ssh`, поэтому обновить себя он всё равно не может.

## Бот

Команды: `/pr`, `/fix`, `/build`, `/model`, `/restore`, `/reset`, `/cancel`, `/clearphotos`, `/help`.
Сверху чата закреплена шапка состояния: ветка, число изменений, автосейв.

Модель: `/model` показывает меню из `NEXUS_MODELS` и ставит выбранную и главному агенту
(`--model`), и субагентам (`CLAUDE_CODE_SUBAGENT_MODEL`). Выбор хранится в воркере и переживает
перезапуск бота; агент с `model:` во frontmatter перебивает его, если не задан
`NEXUS_SUBAGENT_MODEL_FORCE=1`.

Рабочее дерево: сообщение в идущем диалоге продолжает работу на его ветке; новый диалог и старт
воркера сохраняют текущую работу в автосейв (`git stash` + `.git/nexus-state.json`) и начинают
с чистого `develop`. Вернуть сохранённое — `/restore`.

## Figma

Nexus верстает по ссылке на кадр: кидаешь в чат линк на фрейм, он читает его спеку —
точные размеры, hex-цвета, шрифты, скругления, отступы и auto-layout — и раздаёт работу
агентам. Сама Figma воркеру недоступна, поэтому работает это через оффлайн-слепок.

Слепок собирается на машине с Figma Desktop (Plugin API отдаёт всё, включая переменные,
на любом тарифе) и заливается на сервер:

1. Открыть в Figma панель плагина **Claude MCP Bridge** (репозиторий `sl-figma-plugin`).
2. В локальном Claude Code вызвать инструмент `dump_design` — он напишет слепок в папку.
3. `FIGMA_REMOTE=user@server:/opt/sl-claude-box/workspace/figma ./figma-snapshot.sh`

Слепок ложится в `workspace/figma`: каталог примонтирован в воркер, переживает рестарты
и сброс дерева `sweet_limit`, в git не попадает (`workspace/*` в `.gitignore`). Боту он
отдаётся через `--add-dir` плюс правило `Read(...)` в allowlist — так же, как каталог
присланных скринов.

Когда в задаче есть figma-ссылка, бот вытаскивает из неё `node-id`, проверяет, есть ли
такой кадр в слепке, и подмешивает в промпт пути к спеке и рендеру. Если кадра нет или
слепок ещё не залит — Nexus скажет об этом в ответе, вместо того чтобы верстать на глаз.
Слепок не синхронизируется с кодом автоматически: поменялся дизайн — пересобери его теми
же тремя шагами.

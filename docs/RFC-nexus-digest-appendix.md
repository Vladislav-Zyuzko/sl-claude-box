# Приложение к RFC: карта кода и факты окружения

Снято 25.09.2026 с боевого сервера и боевой базы. Всё ниже — проверено, а не предположено.

---

## 1. Расхождение прода и `develop`

Боевой `/opt/sl-claude-box` — **не git-репозиторий**, это набор файлов на сервере; рядом лежат
следы правки от 18.09 (`bot.py.bak-20260918-080740`, `sl-docker-compose.yml.bak-20260918-080740`).
Последний коммит в `develop` — `c78f698` от 10.09.2026 («add telegram command menu and /help»).

| Файл | Состояние |
|---|---|
| `tg-bot/bot.py` | прод: 1396 строк; в `develop` примерно на 613 строк меньше |
| `README.md` | в `develop` **пустой**, на сервере — 93 строки |
| `.env.example` | на сервере +20 строк (`CLAUDE_CODE_VERSION`, `NEXUS_MODEL(S)`, `NEXUS_SUBAGENT_MODEL_FORCE`, блок Figma) |
| `sl-docker-compose.yml` | на сервере +15 строк |
| `claude-worker-ssh/setup-repo.sh` | на сервере +97 строк |
| `claude-worker-ssh/worker-entrypoint.sh` | на сервере +17 строк |
| новый на сервере | `claude-worker-ssh/{nexus-git.sh,repo-status.sh,restore-repo.sh}`, `docker-compose.tg-bot.yml`, `figma-snapshot.sh` |

Вывод для реализации: базироваться на **боевом** состоянии (оно же в локальной копии у харнесса),
иначе правки лягут в устаревшую базу. Секретов в изменённых и новых файлах нет (проверено
регуляркой по токенам и ключам) — публикация в публичный репозиторий безопасна.

---

## 2. Карта `bot.py` (боевая версия, 1396 строк)

| Строки | Секция | Содержимое |
|---|---|---|
| 1–9 | docstring | SLNexus, «одна задача за раз» |
| 11–27 | импорты | `os, re, html, json, time, asyncio, logging, tempfile, typing`, `telegram`, `telegram.ext`, `asyncssh`. **HTTP-клиента нет** |
| 29–64 | настройки | чтение env, `PHOTOS_RULE`/`FIGMA_RULE` 58–59, `UPLOAD_*` 63–64 |
| 67–98 | SSH-обвязка | `_ssh()` 67–76, `_worker_run()` 79–83, `_bind_session()` 89–97 |
| 100–107 | лог/whisper | `WHISPER_MODEL` 101, `logging.basicConfig` 103–106, `logger` 107 |
| 110–111 | доступ | `is_allowed()` |
| 114–181 | модели | `DEFAULT_MODELS` 118–123, `_parse_models` 126–138, `MODELS` 143, `DEFAULT_MODEL` 144–148, `_load_model` 163–176, `_set_model` 179–181 |
| 184–281 | `stream_task()` | запуск задачи в воркере через SSH, `claude -p --output-format stream-json`; команда собирается 233–246, промпт в stdin 267–268 |
| 284–334 | фото | `_safe_name` 285, `_list_photos` 308, `_photos_prompt_note` 318 |
| 337–437 | Figma | regex 338–343, `_figma_nodes` 346, `FIGMA_HEAVY_SPEC` 370 |
| 440–464 | `_store_photos()` | заливка по SFTP |
| 467–505 | разбор stream-json | `_tool_step` 468, `summarize` 492 |
| 508–544 | `class Progress` | троттлинг правок 510, `start` 519, `add` 523, `final` 542 (там же разбивка по 4000) |
| 547–603 | закреплённая шапка | `_worker_status` 551, `_header_text` 562, `_update_header` 580 (толерантна к ошибкам, 602–603) |
| 606–723 | job-модель | `_running` 607, `_startup_checked` 624, `_wait_startup_check` 628, `_execute` 642–723 |
| 726–857 | сборка APK | `_execute_build`, `_drain_stderr` 749 |
| 860–880 | запуск | `_is_busy` 860, `_start_task` 865, `run_job` 869 |
| 883–908 | Whisper | `_whisper` 884, `transcribe_voice` 899 |
| 911–1313 | хендлеры | `cmd_start` 912, `handle_message` 940, медиа 946–1094, `cmd_pr` 1095, `cmd_fix` 1120, `cmd_cancel` 1130, `cmd_reset` 1141, `cmd_restore` 1220, `cmd_model` 1236, `cmd_build` 1280, `cmd_clearphotos` 1292 |
| 1320–1396 | запуск | `BOT_COMMANDS` 1322–1332, `_startup_notice` 1335–1353, `_background` 1356, `post_init` 1359–1368, `main()` 1371–1392, `run_polling` 1392 |

Ключевое для нас:

- **Адресаты.** Единственный источник — `ALLOWED_USER_IDS` (34–36, через запятую, пустой список =
  падение на старте). Персистентного хранилища подписчиков нет. Этот же список уже используется
  как список адресатов: 1341, 1348–1349, 1363–1368.
- **Планировщика нет.** `run_polling` 1392, `concurrent_updates` не задан (последовательная
  обработка апдейтов), `asyncio.sleep` только 1020 и 1347.
- **Образец долгоживущей фоновой таски** — 1360: `asyncio.create_task(_startup_notice(app.bot))`,
  регистрация в `_background` (1356) и `add_done_callback(_background.discard)` (1361–1362).
  Это ровно тот каркас, который нужен планировщику.
- **Слот задач `_running["task"]`** (607) делят `_start_task` (865), `cmd_build` (1289) и
  `cmd_restore` (1226); проверка — `_is_busy()` (860). Фоновая рассылка, которая не идёт через
  воркер, этот слот не трогает и никого не блокирует. Доводка текста через `claude -p` — трогает.
- **Отправка.** Обёрток нет, вызовы прямые. Образцы: `_update_header` 596
  (`send_message(..., parse_mode="HTML", disable_notification=True)`), `Progress.final` 542–544
  (разбивка по 4000). `html.escape` — 534, 563–576, 1203–1215, 1254, 1274.
- **Логирование.** `logging.basicConfig(..., level=logging.INFO)` 103–106; уровни библиотек нигде
  не понижаются — поэтому httpx и сыплет URL с токеном бота.

---

## 3. Точки врезки

**(а) Планировщик.** Каркас — `post_init` (1359–1368): там уже есть `app.bot` и создаётся
долгоживущая таска. Альтернатива `app.job_queue` **не годится**: в `requirements.txt`
`python-telegram-bot==22.6` без extra `[job-queue]`, apscheduler не установлен. Таймзоны в файле
не используются нигде — `zoneinfo` подключается с нуля.

**(б) Сбор данных.** Готовой инфраструктуры нет. Конфиг — рядом с env-блоком 40–64; функции —
новой секцией после 603 (там же, где шапка). Образцы стиля: `_worker_status` 551–559 (запрос +
разбор), `_execute_restore` 1181–1215 (сборка списка строк), `_update_header` 580–603 (доставка).

**(в) Рассылка.** Образец устойчивости — 1363–1368 (`try/except` на каждого адресата).

---

## 4. Образцы ответов SL Tracker API

Авторизация: `Authorization: Bearer <PAT>`. База: `https://tracker.72-56-41-79.sslip.io:8443/api`.

```jsonc
// GET /api/projects/sweet-limit/queues
{ "items": [ { "key": "INFRA", "name": "Инфраструктурные задачи", "description": "…",
               "projectSlug": "sweet-limit", "projectName": "…", "openIssueCount": 3,
               "role": "owner", "createdAt": "…", "updatedAt": "…" } ], "total": 2 }

// GET /api/queues/INFRA/statuses
{ "items": [ { "id": "…", "key": "open",        "name": "Открыт",       "category": "open" },
             { "id": "…", "key": "in_progress", "name": "В работе",      "category": "in_progress" },
             { "id": "…", "key": "review",      "name": "Ревью",         "category": "in_progress" },
             { "id": "…", "key": "testing",     "name": "Тестирование",  "category": "in_progress" },
             { "id": "…", "key": "closed",      "name": "Закрыт",        "category": "done" } ] }

// GET /api/queues/MOBILE/issues?status=in_progress,review,testing&limit=5
{ "items": [ { "key": "MOBILE-1",
               "title": "Реализовать изменение единиц измерения у …",
               "status": { "id": "…", "key": "in_progress", "name": "В работе", "category": "in_progress" },
               "priority": 50, "storyPoints": null,
               "assignee": { "id": "6dd8de68-…", "displayName": "Вячеслав Зюзько",
                             "avatarUrl": "https://avatars.yandex.net/…" } } ],
  "nextCursor": null, "total": 1, "role": "owner" }
// ВАЖНО: поля description в списке НЕТ. assignee — объект или null.

// GET /api/issues/MOBILE-1
{ "key": "MOBILE-1", "title": "…", "description": "…Markdown…",
  "status": { "id": "…", "key": "in_progress", "name": "В работе" },
  "priority": 50, "storyPoints": null,
  "author": { "id": "…", "displayName": "zyuzko2002", "avatarUrl": "…" },
  "assignee": { "id": "…", "displayName": "Вячеслав Зюзько", "avatarUrl": "…" },
  "queue": { "…": "…" }, "project": { "…": "…" }, "links": [], "role": "owner",
  "createdAt": "2026-09-24T06:31:20.295Z", "updatedAt": "2026-09-24T06:31:25.847Z" }

// GET /api/issues/INFRA-4/comments?limit=1
{ "items": [ { "id": "…", "body": "…", "author": { "…": "…" }, "mentions": [],
               "editedAt": null, "createdAt": "…", "permissions": { "…": "…" } } ],
  "nextCursor": null, "total": 1, "canComment": true }

// GET /api/me  (удобно для проверки токена и срока)
{ "id": "bf01c9b7-…", "displayName": "zyuzko2002", "email": "…", "isInstanceOwner": true,
  "canManageAccessList": true, "session": { "kind": "bearer", "expiresAt": "2027-09-24T06:22:17.741Z" } }
```

Проверено на проде: фильтр `status=in_progress,review,testing` возвращает задачи корректно
(`total` = 1 в обеих очередях), `review`/`testing` имеют `category = in_progress`, поэтому
фильтровать надо по `key`.

---

## 5. Факты окружения (проверено)

| Факт | Значение |
|---|---|
| `zoneinfo.ZoneInfo('Asia/Omsk')` в контейнере бота | работает; пакет `tzdata` (pip) отсутствует и не нужен — хватает системного |
| Доступность трекера из контейнера бота | `GET /api/health` → **200** по публичному адресу |
| Сети контейнера бота | только `sl-claude-box_default`; сети трекера (`sl-tracker_default`) нет |
| Монтирование бота | `./tg-bot:/app` — данные рядом с `bot.py` переживают рестарт и пересборку |
| Адресатов | 3 (в `ALLOWED_USER_IDS`), чаты приватные (`chat_id == user_id`) |
| Команды бота | `BOT_COMMANDS` 1322–1332 (+`/help`); меню ставится в 1363–1368 |
| Зависимости бота | `python-telegram-bot==22.6`, `asyncssh==2.15.0`, `faster-whisper==1.0.3`, `requests==2.32.3` (не используется) |
| Образ бота | `python:3.11-slim`, доустанавливается только `ffmpeg` |
| Утечка | логи `telegram-bot` содержат URL `getUpdates` с токеном бота (httpx INFO) |

---

## 6. Чем проверено

- Карта `bot.py` — построчным разбором боевого файла (1396 строк).
- Образцы API — реальными запросами на боевой трекер с PAT, 25.09.2026.
- `zoneinfo`, доступность трекера, сети, монтирования, список адресатов — командами
  `docker exec` / `docker inspect` на боевом сервере.
- Расхождение с `develop` — `git diff` боевых файлов против `origin/develop`.

---

## 7. Что `sl-tracker-mcp` отдаёт на самом деле (проверено 25.09.2026)

Сервер расширен под дайджест: было 6 инструментов, стало **8**. Ничего из старого не
переименовано, поэтому `sl-tracker-mcp` продолжает работать как раньше.

| Инструмент | Аргументы | Ответ (structuredContent) |
|---|---|---|
| `list_queues` | `project?` | `{items: [{project, key, name}], projects: {<slug>: [{key, name}]}}` |
| `list_issues` | `queue`, `status?`, `limit?` | `{queue, statuses, total, items: [{key, title, queue: {key}, status: {key, name, category}, priority, storyPoints, assignee: {id, displayName, avatarUrl} \| null}]}` |
| `list_comments` | `key`, `limit?` | `{key, total, items: [{id, body, author: {id, displayName, avatarUrl}, createdAt, editedAt}], canComment}` |
| `get_task` | `key`, `includeComments?` | карточка задачи: `description`, `status` **строкой**, `assignee` **строкой** или `null` |

Остальные четыре — `create_task`, `update_task_description`, `add_comment`,
`set_task_status` — без изменений.

### Готовый блок для `.env`

```ini
SL_TRACKER_MCP_URL=https://mcp.72-56-41-79.sslip.io:8443/mcp
SL_TRACKER_MCP_TOKEN=<MCP_CLIENT_TOKEN из /opt/sl-tracker/.env, 64 hex>
# единственное расхождение имён: у сервера инструмент называется get_task, а не get_issue
SL_TRACKER_TOOLS={"issue":"get_task"}
```

`queues`, `issues` и `comments` совпадают с именами по умолчанию в `tracker.py`, поэтому
переопределять их не нужно. `SL_TRACKER_MCP_TOKEN` — это **клиентский токен MCP-сервера**
(64 hex), а не PAT трекера (80 символов, `uuid.verifier`): с PAT сервер отвечает 401.

Блок стоит добавить и в `.env.example` репозитория (в коммите с дайджестом он не появился,
там только ссылки из compose).

### Как проверено

Из контейнера `telegram-bot` (та же compose-сеть, что у `nexus-digest`), по публичному
адресу, тем же способом разбора ответа, что в `digest-mcp/digest/tracker.py`:

```
tools/list: 8 -> add_comment, create_task, get_task, list_comments, list_issues,
                list_queues, set_task_status, update_task_description
list_queues(project=sweet-limit): 2 -> [('MOBILE', 'Flutter задачи'),
                                       ('INFRA', 'Инфраструктурные задачи')]
list_issues(INFRA, in_progress,review,testing): total 1, строк 1
    INFRA-4 | Проверка MCP-сервера | статус in_progress «В работе»
    | исполнитель None | приоритет 50 | описания в строке нет (добирается get_task)
list_comments(INFRA-4): total 1 | последний автор zyuzko2002 | текст: Мост работает…
```

То есть путь дайджеста «`list_queues` → `list_issues` → `get_task`/`list_comments`» на
боевом трекере работает целиком. Живая проверка того же пути одной командой —
`node --import tsx scripts/smoke-live.ts` в `sl-tracker-mcp` (печатает 8 инструментов,
очереди и активные задачи).


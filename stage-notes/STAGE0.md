# Этап 0 — де-риск воркера (ручной прогон)

Цель: доказать, что воркер способен **сам** сделать работу на реальном sweet_limit —
склонировать, внести правку, закоммитить, запушить в свою ветку и (по запросу) открыть PR.
Пока без Telegram-бота: запускаем руками через `docker exec`.

> Сам код sweet_limit нигде в sl-claude-box не коммитится — воркер тянет его в
> volume `./workspace` (склонирует в `/workspace/sweet_limit`). `workspace/` в `.gitignore`.

## 0. Подготовка (один раз)

1. Создай **fine-grained PAT** на github.com:
   - Repository access → только `Tsezia/sweet_limit`
   - Permissions: **Contents: Read and write**, **Pull requests: Read and write**
2. Впиши его в `.env`: `GITHUB_TOKEN=github_pat_...`

## 1. Сборка и запуск воркера

```bash
docker compose -f docker-compose.tg-bot.yml build claude-worker
docker compose -f docker-compose.tg-bot.yml up -d claude-worker
```

## 2. Подтянуть sweet_limit внутрь воркера

```bash
docker exec -it claude-worker bash -lc 'setup-repo.sh'
```

Ожидаем: клон в `/workspace/sweet_limit`, checkout `develop`, в конце — последний коммит.

## 3. Тест A — git/gh плумбинг (без Claude)

Проверяем, что авторизация реально пускает push и PR.

```bash
docker exec -it claude-worker bash -lc '
  cd /workspace/sweet_limit &&
  git checkout -b test/stage0-throwaway develop &&
  date > STAGE0_PROBE.txt &&
  git add STAGE0_PROBE.txt &&
  git commit -m "chore: stage0 probe" &&
  git push -u origin test/stage0-throwaway &&
  gh pr create --base develop --head test/stage0-throwaway \
     --title "stage0 probe" --body "throwaway, удалить"
'
```

Если PR создался — git+gh auth работают. **После проверки прибери за собой:**

```bash
docker exec -it claude-worker bash -lc '
  cd /workspace/sweet_limit &&
  gh pr close test/stage0-throwaway -d &&
  git checkout develop
'
```

## 4. Тест B — Claude headless правит репозиторий

```bash
docker exec -it claude-worker bash -lc '
  cd /workspace/sweet_limit &&
  claude -p "Создай файл HELLO_FROM_BOT.md с одной строкой про проект. Больше ничего не делай." \
    --permission-mode acceptEdits
'
```

Ожидаем: файл появился (`git status` покажет изменение). Это доказывает, что Claude
реально работает в чекауте sweet_limit headless. Изменение можно откатить: `git checkout -- .`

## 5. Тест C — полный цикл (Claude делает всё сам)

Объединяем: одна headless-сессия делает ветку → правку → коммит → пуш.
PR оставляем **на явную команду** (Этап 0 это только проверяет вручную из шага 3).

```bash
docker exec -it claude-worker bash -lc '
  cd /workspace/sweet_limit &&
  claude -p "Создай ветку feature/stage0-demo от develop, добавь строку в README, \
    закоммить и запушь ветку. PR не открывай." \
    --permission-mode acceptEdits
'
```

---

## ⚠️ Известный подвох для Этапа 2 (бот)

`docker exec` наследует переменные окружения контейнера, **а вот вход по SSH — нет**.
То есть `GITHUB_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN`, заданные в compose, в SSH-сессии
бота **не будут видны**. Поэтому когда дойдём до Этапа 2, путь бота через SSH либо
заменяем на `docker exec`, либо прокидываем env через файл, который шелл сорсит на входе.
Для Этапа 0 это не мешает — мы намеренно идём через `docker exec`.

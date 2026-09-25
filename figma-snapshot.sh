#!/usr/bin/env bash
# Заливает оффлайн-слепок Figma на сервер, в volume воркера.
#
# Слепок делается локально, на машине с Figma Desktop: в Claude Code с
# подключённым figma-bridge вызывается инструмент dump_design (см. README
# в sl-figma-plugin). Он пишет в папку:
#
#   <snapshot>/index.json          карта всех кадров файла
#   <snapshot>/tokens.json         стили и переменные
#   <snapshot>/frames/1-23.json    спека кадра (node-id как в ссылке)
#   <snapshot>/frames/1-23.png     рендер кадра
#
# Этот скрипт переносит её в workspace/figma на сервере. Оттуда её видит
# Nexus: каталог примонтирован в воркер и добавлен боту через --add-dir.
# В git не попадает — workspace/* в .gitignore.
#
# Использование:
#   FIGMA_REMOTE=user@server:/opt/sl-claude-box/workspace/figma ./figma-snapshot.sh
#   FIGMA_REMOTE=... FIGMA_SNAPSHOT_DIR=~/figma-dump ./figma-snapshot.sh
#
# Переменные можно положить в .env рядом со скриптом — он их подхватит.
set -euo pipefail

cd "$(dirname "$0")"

# .env читаем мягко: он же хранит секреты бота, нам нужны только две переменные.
# Файл на Windows бывает с CRLF — иначе значение уедет с \r на конце и сломает путь.
read_env() {
  grep -E "^$1=" .env 2>/dev/null \
    | tail -1 \
    | cut -d= -f2- \
    | tr -d '\r' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

if [ -f .env ]; then
  FIGMA_REMOTE="${FIGMA_REMOTE:-$(read_env FIGMA_REMOTE || true)}"
  FIGMA_SNAPSHOT_DIR="${FIGMA_SNAPSHOT_DIR:-$(read_env FIGMA_SNAPSHOT_DIR || true)}"
  FIGMA_SSH_KEY="${FIGMA_SSH_KEY:-$(read_env FIGMA_SSH_KEY || true)}"
fi

SNAPSHOT_DIR="${FIGMA_SNAPSHOT_DIR:-./figma-snapshot}"
REMOTE="${FIGMA_REMOTE:-}"

# Отдельный ключ без passphrase — чтобы заливку можно было гонять неинтерактивно.
# IdentitiesOnly: иначе ssh сперва предложит ключи по умолчанию и может упереться
# в запрос passphrase у основного ключа.
SSH_KEY="${FIGMA_SSH_KEY:-}"
SSH_KEY="${SSH_KEY/#\~/$HOME}"
SSH_OPTS=""
if [ -n "$SSH_KEY" ]; then
  if [ ! -f "$SSH_KEY" ]; then
    echo "Не найден ключ $SSH_KEY (FIGMA_SSH_KEY)" >&2
    exit 2
  fi
  SSH_OPTS="-i $SSH_KEY -o IdentitiesOnly=yes"
fi

if [ -z "$REMOTE" ]; then
  cat >&2 <<'USAGE'
Не задан FIGMA_REMOTE — куда заливать слепок.

  FIGMA_REMOTE=user@server:/путь/до/sl-claude-box/workspace/figma

Путь на сервере — это каталог figma внутри примонтированного workspace
(см. volumes в sl-docker-compose.yml: ./workspace:/workspace).
USAGE
  exit 2
fi

# Страховка от rsync --delete не туда: путь обязан указывать на каталог figma.
case "$REMOTE" in
  *:*/figma|*:*/figma/) ;;
  *)
    echo "FIGMA_REMOTE должен заканчиваться на /figma (сейчас: $REMOTE)" >&2
    echo "Иначе синхронизация с --delete затрёт чужой каталог." >&2
    exit 2
    ;;
esac

if [ ! -f "$SNAPSHOT_DIR/index.json" ]; then
  echo "В $SNAPSHOT_DIR нет index.json — слепок не собран." >&2
  echo "Собери его локально: Claude Code + figma-bridge, инструмент dump_design." >&2
  exit 2
fi

FRAMES=$(find "$SNAPSHOT_DIR/frames" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
SIZE=$(du -sh "$SNAPSHOT_DIR" 2>/dev/null | cut -f1)
DUMPED_AT=$(grep -o '"dumpedAt": *"[^"]*"' "$SNAPSHOT_DIR/index.json" | head -1 | cut -d'"' -f4)

echo ">> слепок: $FRAMES кадров, $SIZE, собран $DUMPED_AT"
echo ">> заливаю в $REMOTE"

REMOTE_HOST="${REMOTE%%:*}"
REMOTE_PATH="${REMOTE#*:}"

# Каталога может ещё не быть — rsync создаст только последний уровень, а scp и того не умеет.
# shellcheck disable=SC2086
ssh $SSH_OPTS "$REMOTE_HOST" "mkdir -p '$REMOTE_PATH'"

if command -v rsync >/dev/null 2>&1; then
  # --delete: кадры, удалённые или переименованные в Figma, не должны оставаться
  # в слепке — иначе Nexus будет верстать по несуществующему экрану.
  rsync -az --delete --info=stats1 -e "ssh $SSH_OPTS" "$SNAPSHOT_DIR/" "$REMOTE/"
else
  echo ">> rsync не найден, заливаю через scp (без удаления устаревших кадров)"
  # shellcheck disable=SC2086
  scp -q $SSH_OPTS -r "$SNAPSHOT_DIR/." "$REMOTE/"
fi

echo ">> готово. Nexus увидит слепок как /workspace/figma"

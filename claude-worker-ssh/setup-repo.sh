#!/usr/bin/env bash
# Подтягивает sweet_limit в /workspace и настраивает git/gh auth внутри воркера.
# Сам код sweet_limit живёт только в volume /workspace, в репозиторий sl-claude-box он не попадает.
#
# Политика рабочего дерева:
#   setup-repo.sh --session <id>  сообщение в идущем диалоге: дерево и ветку НЕ трогаем, только fetch.
#                                 Если дерево уже не этого диалога (воркер перезапускался,
#                                 был новый диалог) — печатаем NEXUS_SESSION_MISMATCH и exit 3.
#   setup-repo.sh                 новый диалог: autosave → чистый BASE_BRANCH.
#                                 Исключение: дерево только что восстановлено /restore без
#                                 сессии (active, session пуст) — продолжаем в нём.
#   setup-repo.sh --startup       старт воркера: autosave → чистый BASE_BRANCH.
# Автосейвы возвращает restore-repo.sh (команда /restore в боте).
set -euo pipefail

: "${GITHUB_TOKEN:?нужен GITHUB_TOKEN (fine-grained PAT с доступом к Tsezia/sweet_limit)}"

REPO_URL="${SWEET_LIMIT_REPO:-https://github.com/Tsezia/sweet_limit.git}"
WORKDIR="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
BASE_BRANCH="${BASE_BRANCH:-develop}"
GIT_NAME="${GIT_USER_NAME:-SLNexus Bot}"
GIT_EMAIL="${GIT_USER_EMAIL:-slnexus-bot@users.noreply.github.com}"
export SWEET_LIMIT_DIR="$WORKDIR"

STARTUP=0
SESSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --startup) STARTUP=1 ;;
    --session) SESSION="${2:-}"; shift ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

# shellcheck source=nexus-git.sh
. /usr/local/lib/nexus-git.sh

echo ">> git identity: ${GIT_NAME} <${GIT_EMAIL}>"
git config --global user.name  "${GIT_NAME}"
git config --global user.email "${GIT_EMAIL}"
git config --global --add safe.directory "${WORKDIR}"

# GITHUB_TOKEN уже в окружении — gh использует его автоматически.
# `gh auth login --with-token` в этом случае ругается и падает (set -e),
# поэтому логин пропускаем и только подключаем gh как git credential helper,
# чтобы git clone/push по HTTPS шли с токеном (без него в remote URL).
echo ">> gh auth: токен берётся из окружения GITHUB_TOKEN"
gh auth setup-git

if [ ! -d "${WORKDIR}/.git" ]; then
  echo ">> клонирую ${REPO_URL} -> ${WORKDIR}"
  git clone "${REPO_URL}" "${WORKDIR}"
  git -C "${WORKDIR}" checkout "${BASE_BRANCH}"
  nexus-state set-current - 0
  echo ">> готово. Текущий HEAD:"
  git -C "${WORKDIR}" log --oneline -1
  exit 0
fi

IFS=$'\t' read -r CUR_SESSION CUR_ACTIVE < <(nexus-state get-current)
[ "$CUR_SESSION" = "-" ] && CUR_SESSION=""

if [ "$STARTUP" = "1" ]; then
  MODE=reset
elif [ -n "$SESSION" ]; then
  if [ "$CUR_ACTIVE" = "1" ] && [ "$SESSION" = "$CUR_SESSION" ]; then
    MODE=continue
  else
    echo ">> дерево не принадлежит диалогу ${SESSION} (сейчас: '${CUR_SESSION}', active=${CUR_ACTIVE})"
    echo "NEXUS_SESSION_MISMATCH"
    exit 3
  fi
elif [ "$CUR_ACTIVE" = "1" ] && [ -z "$CUR_SESSION" ]; then
  MODE=continue   # восстановлено через /restore без сессии — первый ход нового диалога в нём
else
  MODE=reset
fi

if [ "$MODE" = "continue" ]; then
  echo ">> продолжаю диалог на $(git -C "${WORKDIR}" rev-parse --abbrev-ref HEAD) — дерево не трогаю"
  git -C "${WORKDIR}" fetch origin --prune || echo ">> WARN: fetch не удался, продолжаю без него"
else
  echo ">> новый старт — autosave и чистый ${BASE_BRANCH}"
  nexus_abort_ops
  nexus_autosave "$CUR_SESSION"
  # дерево уже сохранено: даже если fetch ниже упадёт, это дерево больше не «активное»
  nexus-state set-current "${CUR_SESSION:--}" 0
  git -C "${WORKDIR}" fetch origin --prune
  git -C "${WORKDIR}" checkout -f "${BASE_BRANCH}"
  git -C "${WORKDIR}" reset --hard "origin/${BASE_BRANCH}"
  # clean -fd сносит untracked (ignored-артефакты вроде build/ и .env.* не трогаем)
  git -C "${WORKDIR}" clean -fd

  # новый диалог начат поверх несохранённой работы — бот подсветит это в ответе
  PENDING="$(nexus-state pending)"
  if [ -n "$PENDING" ]; then
    IFS=$'\t' read -r _ P_BRANCH P_HEAD _ _ P_SAVED_AT <<<"$PENDING"
    [ "$P_BRANCH" = "-" ] && P_BRANCH="${P_HEAD:0:7}"
    printf 'NEXUS_PENDING_AUTOSAVE:%s\t%s\n' "$P_BRANCH" "$P_SAVED_AT"
  fi
fi

echo ">> готово. Текущий HEAD:"
git -C "${WORKDIR}" log --oneline -1

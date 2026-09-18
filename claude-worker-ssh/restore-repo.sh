#!/usr/bin/env bash
# Возвращает прошлую работу (команда /restore в боте).
#
#  restore-repo.sh --saved  бот сам ведёт диалог в этом чате — значит, просят именно
#                           автосейв: берём самый свежий невосстановленный, текущее дерево
#                           сначала автосейвится само, затем checkout ветки и `git stash apply <sha>`.
#  restore-repo.sh          бот диалога не помнит (перезапускался). Если дерево «активное» —
#                           это и есть прерванный диалог: git не трогаем, отдаём ветку и сессию
#                           (про более старый автосейв сообщаем RESTORE_NOTE). Иначе — как --saved.
#
# Маркеры для бота (stdout):
#   RESTORE_NONE                       — восстанавливать нечего
#   RESTORED_BRANCH:<ветка или sha>
#   RESTORED_SESSION:<session_id или пусто>
#   RESTORED_FROM:current|<saved_at>
#   RESTORED_STASH:none|applied|conflict:<sha>
#   RESTORE_NOTE:<текст>
set -euo pipefail

WORKDIR="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
BASE_BRANCH="${BASE_BRANCH:-develop}"
export SWEET_LIMIT_DIR="$WORKDIR"

# shellcheck source=nexus-git.sh
. /usr/local/lib/nexus-git.sh

g() { git -C "$WORKDIR" "$@"; }
undash() { [ "$1" = "-" ] && echo "" || echo "$1"; }

SAVED_ONLY=0
[ "${1:-}" = "--saved" ] && SAVED_ONLY=1

if [ ! -d "$WORKDIR/.git" ]; then
  echo "RESTORE_NONE"
  exit 0
fi

IFS=$'\t' read -r CUR_SESSION CUR_ACTIVE < <(nexus-state get-current)
CUR_SESSION="$(undash "$CUR_SESSION")"

PENDING="$(nexus-state pending)"

if [ "$CUR_ACTIVE" = "1" ] && [ "$SAVED_ONLY" = "0" ]; then
  echo "RESTORED_BRANCH:$(g rev-parse --abbrev-ref HEAD)"
  echo "RESTORED_SESSION:$CUR_SESSION"
  echo "RESTORED_FROM:current"
  echo "RESTORED_STASH:none"
  if [ -n "$PENDING" ]; then
    IFS=$'\t' read -r _ P_BRANCH _ _ _ P_SAVED_AT <<<"$PENDING"
    echo "RESTORE_NOTE:есть и более ранний автосейв (ветка $(undash "$P_BRANCH"), $P_SAVED_AT) — /restore ещё раз вернёт его"
  fi
  exit 0
fi

if [ -z "$PENDING" ]; then
  echo "RESTORE_NONE"
  exit 0
fi
IFS=$'\t' read -r ID BRANCH HEAD STASH SESSION SAVED_AT <<<"$PENDING"
BRANCH="$(undash "$BRANCH")"
STASH="$(undash "$STASH")"
SESSION="$(undash "$SESSION")"

# текущее дерево не теряем: сохраняем его так же, как при новом старте
nexus_abort_ops
nexus_autosave "$CUR_SESSION" >&2

# checkout сохранённой ветки. Если её указатель с тех пор ушёл в сторону
# (не содержит сохранённый HEAD) — не двигаем чужую ветку, а заводим рядом новую.
if [ -z "$BRANCH" ]; then
  g checkout -f -q --detach "$HEAD"
  TARGET="$HEAD"
elif g show-ref --verify -q "refs/heads/$BRANCH"; then
  if g merge-base --is-ancestor "$HEAD" "refs/heads/$BRANCH"; then
    g checkout -f -q "$BRANCH"
    TARGET="$BRANCH"
  else
    TARGET="$BRANCH-restored-${HEAD:0:7}"
    g checkout -f -q -B "$TARGET" "$HEAD"
    echo "RESTORE_NOTE:ветка $BRANCH с тех пор изменилась — работа восстановлена в $TARGET"
  fi
else
  g checkout -f -q -B "$BRANCH" "$HEAD"
  TARGET="$BRANCH"
fi

STASH_STATE="none"
if [ -n "$STASH" ]; then
  if g stash apply -q "$STASH" >&2; then
    nexus_drop_stash "$STASH"
    STASH_STATE="applied"
  else
    # не смогли наложить — откатываем полупримененное, stash остаётся целым
    g reset -q --hard
    g clean -fdq
    STASH_STATE="conflict:$STASH"
  fi
fi

if [ "$STASH_STATE" != "conflict:$STASH" ]; then
  nexus-state mark-restored "$ID"
fi
nexus-state set-current "${SESSION:--}" 1

echo "RESTORED_BRANCH:$TARGET"
echo "RESTORED_SESSION:$SESSION"
echo "RESTORED_FROM:$SAVED_AT"
echo "RESTORED_STASH:$STASH_STATE"

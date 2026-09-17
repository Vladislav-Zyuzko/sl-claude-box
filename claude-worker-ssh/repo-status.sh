#!/usr/bin/env bash
# Сводка состояния чекаута для бота (уведомление при старте).
#   STATUS_BRANCH:<текущая ветка>
#   STATUS_DIRTY:<число изменённых файлов>
#   STATUS_ACTIVE:0|1            — дерево принадлежит идущему диалогу
#   PENDING:<ветка>\t<saved_at>\t<stash sha или ->   — последний невосстановленный автосейв
set -euo pipefail

WORKDIR="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
export SWEET_LIMIT_DIR="$WORKDIR"

[ -d "$WORKDIR/.git" ] || exit 0

echo "STATUS_BRANCH:$(git -C "$WORKDIR" rev-parse --abbrev-ref HEAD)"
echo "STATUS_DIRTY:$(git -C "$WORKDIR" status --porcelain --untracked-files=all | wc -l)"

IFS=$'\t' read -r _ CUR_ACTIVE < <(nexus-state get-current)
echo "STATUS_ACTIVE:$CUR_ACTIVE"

PENDING="$(nexus-state pending)"
if [ -n "$PENDING" ]; then
  IFS=$'\t' read -r _ BRANCH HEAD STASH _ SAVED_AT <<<"$PENDING"
  [ "$BRANCH" = "-" ] && BRANCH="${HEAD:0:7}"
  printf 'PENDING:%s\t%s\t%s\n' "$BRANCH" "$SAVED_AT" "$STASH"
fi

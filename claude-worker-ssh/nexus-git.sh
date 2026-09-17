# Общие git-хелперы автосейва. Сорсится из setup-repo.sh и restore-repo.sh.
# Ожидает: WORKDIR, BASE_BRANCH; SWEET_LIMIT_DIR экспортирован (его читает nexus-state).

# Прервать недоделанные merge/rebase (например, down посреди задачи) —
# с незавершённым merge `git stash` не работает.
nexus_abort_ops() {
  git -C "$WORKDIR" merge --abort  2>/dev/null || true
  git -C "$WORKDIR" rebase --abort 2>/dev/null || true
}

# Удалить stash по его sha (stash@{n} сдвигаются, sha — нет).
nexus_drop_stash() {
  local ref
  ref="$(git -C "$WORKDIR" stash list --format='%gd %H' | awk -v s="$1" '$2 == s { print $1; exit }')"
  if [ -n "$ref" ]; then
    git -C "$WORKDIR" stash drop -q "$ref" || true
  fi
}

# Сохранить текущую работу перед сбросом дерева: незакоммиченное (включая untracked,
# но не ignored) — в stash, ветку/HEAD/сессию — в .git/nexus-state.json.
# $1 — session_id диалога, которому принадлежит дерево (может быть пустым).
# После вызова дерево чистое.
nexus_autosave() {
  local session="${1:-}" branch head dirty before after stash="" pruned sha
  branch="$(git -C "$WORKDIR" symbolic-ref --short -q HEAD || true)"
  head="$(git -C "$WORKDIR" rev-parse HEAD)"
  dirty="$(git -C "$WORKDIR" status --porcelain --untracked-files=all)"

  # чистый BASE_BRANCH без локальных коммитов — сохранять нечего
  if [ -z "$dirty" ] && [ "$branch" = "$BASE_BRANCH" ] \
     && git -C "$WORKDIR" merge-base --is-ancestor HEAD "origin/$BASE_BRANCH" 2>/dev/null; then
    echo ">> autosave: сохранять нечего (чистый $BASE_BRANCH)"
    return 0
  fi

  if [ -n "$dirty" ]; then
    before="$(git -C "$WORKDIR" rev-parse -q --verify refs/stash || true)"
    git -C "$WORKDIR" stash push -u -m "nexus-autosave ${branch:-detached} $(date -u '+%F %H:%M')"
    after="$(git -C "$WORKDIR" rev-parse -q --verify refs/stash || true)"
    [ "$after" != "$before" ] && stash="$after"
  fi

  pruned="$(nexus-state add-save "${branch:--}" "$head" "${stash:--}" "${session:--}")"
  for sha in $pruned; do
    nexus_drop_stash "$sha"
  done
  echo ">> autosave: ветка ${branch:-detached}@${head:0:7}${stash:+, правки в stash ${stash:0:7}}"
}

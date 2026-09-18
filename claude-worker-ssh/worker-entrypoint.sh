#!/usr/bin/env bash
# Entrypoint воркера.
# SSH-сессия НЕ наследует переменные окружения контейнера, поэтому на старте
# мы сбрасываем нужные env в /etc/profile.d/worker-env.sh. Любой вход через
# login-shell (`bash -lc '...'`, как делает бот) сорсит /etc/profile -> profile.d
# и получает токены. Секреты лежат только в файле (600, владелец claude-ssh),
# а не в аргументах команд (где их видно в `ps`).
set -euo pipefail

ENV_FILE=/etc/profile.d/worker-env.sh
SSH_USER="${SSH_RUNTIME_USER:-claude-ssh}"

: > "$ENV_FILE"
for var in \
  GITHUB_TOKEN \
  CLAUDE_CODE_OAUTH_TOKEN \
  DISABLE_AUTOUPDATER \
  SWEET_LIMIT_REPO \
  SWEET_LIMIT_DIR \
  BASE_BRANCH \
  GIT_USER_NAME \
  GIT_USER_EMAIL
do
  if [ -n "${!var:-}" ]; then
    # %q экранирует значение для безопасного reuse в шелле
    printf 'export %s=%q\n' "$var" "${!var}" >> "$ENV_FILE"
  fi
done

# Доступ только пользователю SSH-сессий
chown "$SSH_USER":"$SSH_USER" "$ENV_FILE" 2>/dev/null || true
chmod 600 "$ENV_FILE"

echo ">> worker-env.sh собран ($(wc -l < "$ENV_FILE") переменных)"

# Чекаут sweet_limit ведёт SSH-пользователь (бот заходит под ним), поэтому
# именно он должен владеть /workspace. Иначе root-owned клон из ручных тестов
# даёт "cd: Permission denied" и ломает git-операции под claude-ssh.
mkdir -p /workspace
chown -R "$SSH_USER":"$SSH_USER" /workspace 2>/dev/null || true
echo ">> /workspace передан пользователю $SSH_USER"

# Тулчейн-volume тоже должен принадлежать SSH-пользователю (он запускает сборки).
# chown без -R: на большом SDK рекурсия дорогая, а содержимое уже создаётся под ним.
mkdir -p /opt/toolchain
chown "$SSH_USER":"$SSH_USER" /opt/toolchain 2>/dev/null || true

# ~/.claude (транскрипты сессий для --resume) живёт в volume, чтобы /restore
# возвращал и диалог. Свежий named volume монтируется root-owned — отдаём пользователю.
mkdir -p "/home/$SSH_USER/.claude"
chown "$SSH_USER":"$SSH_USER" "/home/$SSH_USER/.claude" 2>/dev/null || true

# Рабочее дерево после (пере)запуска: прошлая работа уходит в автосейв, стартуем
# с чистого BASE_BRANCH. Бот подсветит автосейв, вернуть — /restore.
PROJECT="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
# git, убитый посреди операции (down во время задачи), оставляет lock и блокирует всё
rm -f "$PROJECT/.git/index.lock"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  # login-shell: подхватывает worker-env.sh с токенами и PATH из profile.d
  su -l "$SSH_USER" -c 'setup-repo.sh --startup' \
    || echo ">> WARN: setup-repo.sh --startup упал — дерево не сброшено, см. лог выше"
fi

service ssh start
exec tail -f /dev/null

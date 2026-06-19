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

service ssh start
exec tail -f /dev/null

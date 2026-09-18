# Берём за основу твой существующий образ
FROM sl-claude-box:latest

# Переключаемся на root (в оригинальном образе может быть другой пользователь)
USER root

# Устанавливаем SSH-сервер и GitHub CLI (gh нужен для `gh pr create`)
RUN apt-get update && apt-get install -y openssh-server curl gpg && \
    mkdir -p -m 755 /etc/apt/keyrings && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Скрипт «склонируй-или-обнови» sweet_limit + настройка git/gh auth
COPY setup-repo.sh /usr/local/bin/setup-repo.sh
RUN chmod +x /usr/local/bin/setup-repo.sh

# Автосейв/восстановление рабочего дерева (/restore в боте) + state в .git/nexus-state.json
COPY nexus-git.sh /usr/local/lib/nexus-git.sh
COPY nexus-state /usr/local/bin/nexus-state
COPY restore-repo.sh /usr/local/bin/restore-repo.sh
COPY repo-status.sh /usr/local/bin/repo-status.sh
RUN chmod 644 /usr/local/lib/nexus-git.sh && \
    chmod +x /usr/local/bin/nexus-state /usr/local/bin/restore-repo.sh /usr/local/bin/repo-status.sh

# Entrypoint: прокидывает env контейнера в SSH-сессии через /etc/profile.d
COPY worker-entrypoint.sh /usr/local/bin/worker-entrypoint.sh
RUN chmod +x /usr/local/bin/worker-entrypoint.sh

# --- Android/Flutter build toolchain ---
# JDK 17 + fvm ставим в образ (лёгкое, стабильное). Сам Android SDK + Flutter
# версии + кэши gradle/pub живут в persistent-volume /opt/toolchain (ставит
# setup-toolchain.sh при первом /build).
RUN apt-get update && apt-get install -y --no-install-recommends \
      openjdk-17-jdk-headless unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/*

# fvm: ставим официальным скриптом, затем кладём бинарь в системный /usr/local/bin.
# Под root скрипт инсталлит fvm в /root/.fvm_flutter/bin — это недоступно
# пользователю claude-ssh (домашняя папка root), отсюда было `fvm: command not found`.
# `fvm --version` в конце валидирует установку прямо на этапе сборки.
RUN set -eux; \
    curl -fsSL https://fvm.app/install.sh | bash; \
    fvmbin="$(find / -type f -path '*bin/fvm' 2>/dev/null | head -1)"; \
    install -m 0755 "$fvmbin" /usr/local/bin/fvm; \
    fvm --version

# Статический env тулчейна для login-сессий (бот заходит через bash -lc)
COPY toolchain-env.sh /etc/profile.d/10-toolchain.sh
RUN chmod 644 /etc/profile.d/10-toolchain.sh

# Скрипты: разовая установка тулчейна и детерминированная сборка APK
COPY setup-toolchain.sh /usr/local/bin/setup-toolchain.sh
COPY build-apk.sh /usr/local/bin/build-apk.sh
RUN chmod +x /usr/local/bin/setup-toolchain.sh /usr/local/bin/build-apk.sh

# Конфиг Dart MCP-сервера для Claude (подключается флагом --mcp-config)
COPY dart-mcp.json /usr/local/etc/dart-mcp.json

# Создаём пользователя для SSH
RUN useradd -m -s /bin/bash claude-ssh && \
    mkdir -p /home/claude-ssh/.ssh && \
    chmod 700 /home/claude-ssh/.ssh

# Копируем публичный ключ
COPY ssh-key.pub /home/claude-ssh/.ssh/authorized_keys

# Права
RUN chown -R claude-ssh:claude-ssh /home/claude-ssh/.ssh && \
    chmod 600 /home/claude-ssh/.ssh/authorized_keys

# Настройка SSH
RUN echo "PermitRootLogin no" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config

# --- Claude Code ---
# Базовый образ sl-claude-box ставит claude-code сам, но пересобирается редко, поэтому
# версия там замерзает. Ставим поверх свою — сборкой воркера, ею же и обновляем.
#   CLAUDE_CODE_VERSION=latest  — тянуть свежую (по умолчанию), или точная версия для пина
#   CLAUDE_REFRESH              — «ломатель» кеша: обычная пересборка берёт слой из кеша,
#                                 а с --build-arg CLAUDE_REFRESH=$(date +%s) идёт в npm за новой
# Слой держим ближе к концу файла: его инвалидация не тянет пересборку всего образа.
# EBADENGINE про node>=22 при Node 18 — косметика: npm ставит нативный бинарь,
# и claude при запуске Node не использует.
ARG CLAUDE_CODE_VERSION=latest
ARG CLAUDE_REFRESH=0
RUN echo "claude refresh=${CLAUDE_REFRESH}" && \
    npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && \
    claude --version

EXPOSE 22

# Создаём скрипт запуска (исправляет проблему с PID 1)
RUN mkdir -p /var/run/sshd

# Запускаем SSH и держим контейнер живым (env прокидывается в profile.d)
CMD ["/usr/local/bin/worker-entrypoint.sh"]

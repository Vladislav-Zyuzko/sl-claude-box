# Берём за основу твой существующий образ
FROM sl-claude-box:latest

# Переключаемся на root (в оригинальном образе может быть другой пользователь)
USER root

# Устанавливаем SSH-сервер
RUN apt-get update && apt-get install -y openssh-server && rm -rf /var/lib/apt/lists/*

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

EXPOSE 22

# Создаём скрипт запуска (исправляет проблему с PID 1)
RUN mkdir -p /var/run/sshd

# Запускаем SSH и держим контейнер живым
CMD service ssh start && tail -f /dev/null

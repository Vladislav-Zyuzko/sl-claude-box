FROM debian:bookworm-slim

# Устанавливаем базовые зависимости
RUN apt-get update && apt-get install -y \
    curl \
    git \
    nodejs \
    npm \
    python3 \
    python3-pip \
    openssh-client \
    build-essential \
    zsh \
    tmux \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Создаем пользователя
ARG USERNAME=claude
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME \
    && mkdir -p /home/$USERNAME/.ssh \
    && chown -R $USERNAME:$USERNAME /home/$USERNAME/.ssh

# Копируем SSH-ключи (если есть)
COPY .ssh/ /home/$USERNAME/.ssh/
RUN chown -R $USERNAME:$USERNAME /home/$USERNAME/.ssh && chmod 700 /home/$USERNAME/.ssh

# Устанавливаем рабочую папку
WORKDIR /workspace
RUN chown -R $USERNAME:$USERNAME /workspace

# Переключаемся на пользователя
USER $USERNAME

# Настройка zsh
RUN sh -c "$(curl -fsSL https://raw.github.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" || true

# Запускаем Claude
CMD ["claude"]

#!/usr/bin/env bash
# Подтягивает sweet_limit в /workspace и настраивает git/gh auth внутри воркера.
# Идемпотентен: если репо уже склонировано — fetch + обновление базовой ветки,
# иначе делает clone. Сам код sweet_limit живёт только в volume /workspace,
# в репозиторий sl-claude-box он не попадает.
set -euo pipefail

: "${GITHUB_TOKEN:?нужен GITHUB_TOKEN (fine-grained PAT с доступом к Tsezia/sweet_limit)}"

REPO_URL="${SWEET_LIMIT_REPO:-https://github.com/Tsezia/sweet_limit.git}"
WORKDIR="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
BASE_BRANCH="${BASE_BRANCH:-develop}"
GIT_NAME="${GIT_USER_NAME:-SLNexus Bot}"
GIT_EMAIL="${GIT_USER_EMAIL:-slnexus-bot@users.noreply.github.com}"

echo ">> git identity: ${GIT_NAME} <${GIT_EMAIL}>"
git config --global user.name  "${GIT_NAME}"
git config --global user.email "${GIT_EMAIL}"
git config --global --add safe.directory "${WORKDIR}"

echo ">> gh auth (через токен)"
echo "${GITHUB_TOKEN}" | gh auth login --with-token
# делает gh credential-helper'ом для github.com -> токен не светится в remote URL
gh auth setup-git

if [ -d "${WORKDIR}/.git" ]; then
  echo ">> репо уже есть, обновляю ветку ${BASE_BRANCH}"
  git -C "${WORKDIR}" fetch origin --prune
  git -C "${WORKDIR}" checkout "${BASE_BRANCH}"
  git -C "${WORKDIR}" pull --ff-only origin "${BASE_BRANCH}"
else
  echo ">> клонирую ${REPO_URL} -> ${WORKDIR}"
  git clone "${REPO_URL}" "${WORKDIR}"
  git -C "${WORKDIR}" checkout "${BASE_BRANCH}"
fi

echo ">> готово. Текущий HEAD:"
git -C "${WORKDIR}" log --oneline -1

#!/usr/bin/env bash
# Разовая (идемпотентная) установка Android SDK + Flutter в persistent-volume
# /opt/toolchain. Запускается ботом перед /build; после первого раза всё из кэша.
set -euo pipefail

SDK="${ANDROID_SDK_ROOT:-/opt/toolchain/android-sdk}"
PROJECT="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"

mkdir -p "$SDK" "${PUB_CACHE:-/opt/toolchain/pub-cache}" \
  "${GRADLE_USER_HOME:-/opt/toolchain/gradle}" "${FVM_CACHE_PATH:-/opt/toolchain/fvm}"

# 1) Android command-line tools
if [ ! -d "$SDK/cmdline-tools/latest" ]; then
  echo ">> ставлю Android cmdline-tools"
  tmp="$(mktemp -d)"
  wget -q "$CMDLINE_TOOLS_URL" -O "$tmp/cmdline.zip"
  mkdir -p "$SDK/cmdline-tools"
  unzip -q "$tmp/cmdline.zip" -d "$SDK/cmdline-tools"
  mv "$SDK/cmdline-tools/cmdline-tools" "$SDK/cmdline-tools/latest"
  rm -rf "$tmp"
fi

export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$PATH"

# 2) SDK-пакеты + лицензии (проект: compileSdk 36 — см. android/app/build.gradle)
echo ">> sdkmanager: лицензии и пакеты"
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager --install "platform-tools" "platforms;android-36" "build-tools;36.0.0" >/dev/null

# 3) Flutter через fvm — версия из конфигурации проекта (.fvmrc)
echo ">> fvm install (версия проекта) в $FVM_CACHE_PATH"
cd "$PROJECT"
fvm install

# делаем версию глобальной — чтобы работал и «обычный» flutter/dart, не только `fvm flutter`
VER="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' "$PROJECT/.fvmrc" 2>/dev/null | head -1 || true)"
if [ -n "$VER" ]; then
  fvm global "$VER" || true
fi

fvm flutter config --no-analytics >/dev/null 2>&1 || true
fvm flutter precache --android >/dev/null
echo ">> Flutter: $(fvm flutter --version | head -1)"

echo ">> тулчейн готов"

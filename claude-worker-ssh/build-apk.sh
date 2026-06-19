#!/usr/bin/env bash
# Детерминированная сборка APK текущего чекаута sweet_limit (debug, split-per-abi).
# Печатает строки ARTIFACT:<path> для каждого собранного APK — их парсит бот.
set -euo pipefail

PROJECT="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
cd "$PROJECT"

echo ">> ветка: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

echo ">> flutter pub get"
fvm flutter pub get

# Кодогенерация, если проект использует build_runner (freezed/riverpod/json)
if grep -q "build_runner" pubspec.yaml; then
  echo ">> build_runner"
  fvm dart run build_runner build --delete-conflicting-outputs
fi

# Проект коммитит локальный Windows-путь org.gradle.java.home — на Linux он невалиден.
# Убираем строку, чтобы gradle взял системный JDK, и явно ставим JAVA_HOME (openjdk-17).
GP="android/gradle.properties"
[ -f "$GP" ] && sed -i '/^org\.gradle\.java\.home/d' "$GP"
export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
echo ">> JAVA_HOME=$JAVA_HOME"

# У проекта нет Android-флейворов, но есть разные точки входа (main_dev/main_prod).
# По умолчанию собираем prod; переопределяется через BUILD_TARGET.
TARGET="${BUILD_TARGET:-lib/main_prod.dart}"
# release: меньше debug в 2-3 раза и влезает в лимит Telegram; подпись — debug-ключами
# (signingConfig signingConfigs.debug в build.gradle), keystore не нужен. Тумблер BUILD_MODE.
MODE="${BUILD_MODE:-release}"
OUT="$PROJECT/build/app/outputs/flutter-apk"

# чистим APK прошлых сборок, чтобы они не копились на сервере (остаётся только текущая)
rm -f "$OUT"/*.apk 2>/dev/null || true

echo ">> flutter build apk ($MODE, split-per-abi, target=$TARGET)"
fvm flutter build apk --"$MODE" --split-per-abi --target "$TARGET"

# Отдаём arm64-v8a (реальные устройства). Если нужен другой ABI — поправить тут.
for f in "$OUT"/app-arm64-v8a-"$MODE".apk; do
  if [ -f "$f" ]; then
    echo "ARTIFACT:$f"
  fi
done

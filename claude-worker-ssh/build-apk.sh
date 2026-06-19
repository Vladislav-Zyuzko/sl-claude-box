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

echo ">> flutter build apk (debug, split-per-abi)"
fvm flutter build apk --debug --split-per-abi

OUT="$PROJECT/build/app/outputs/flutter-apk"
# Отдаём arm64-v8a (реальные устройства). Если нужен другой ABI — поправить тут.
for f in "$OUT"/app-arm64-v8a-debug.apk; do
  if [ -f "$f" ]; then
    echo "ARTIFACT:$f"
  fi
done

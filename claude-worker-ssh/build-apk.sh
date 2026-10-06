#!/usr/bin/env bash
# Детерминированная сборка APK текущего чекаута sweet_limit.
# Сборку выбирает BUILD_FLAVOR=dev|prod (точка входа + свой .env), см. ниже.
# Печатает строки ARTIFACT:<path> для каждого собранного APK — их парсит бот.
set -euo pipefail

PROJECT="${SWEET_LIMIT_DIR:-/workspace/sweet_limit}"
cd "$PROJECT"

echo ">> ветка: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

# dev/prod — это НЕ Android-флейворы (их у проекта нет), а разные точки входа
# lib/main_<flavor>.dart. Каждая грузит свой .env.<flavor> через dotenv, и этот
# файл объявлен ассетом в pubspec.yaml. По умолчанию prod — так /build вёл себя
# до появления /build_dev и /build_prod.
FLAVOR="${BUILD_FLAVOR:-prod}"
case "$FLAVOR" in
  dev|prod) ;;
  *) echo "BUILD_FLAVOR: ожидается dev или prod, получено '$FLAVOR'" >&2; exit 2 ;;
esac
TARGET="${BUILD_TARGET:-lib/main_$FLAVOR.dart}"

# Предполётные проверки. Без них отсутствующий .env.<flavor> даёт невнятную ошибку
# сборки ассетов где-то в середине лога. Эти файлы лежат в .gitignore, то есть в
# свежий клон НЕ попадают: они живут в рабочем дереве воркера и переживают сброс
# задачи, потому что git clean -fd игнорируемые файлы не удаляет. Если дерево
# пересоздали с нуля — их надо положить руками.
[ -f "$TARGET" ] || { echo "Нет точки входа $TARGET" >&2; exit 2; }
if [ ! -f ".env.$FLAVOR" ]; then
  echo "Нет $PROJECT/.env.$FLAVOR — он объявлен ассетом в pubspec.yaml," >&2
  echo "без него flutter build упадёт на этапе ассетов. Файл в .gitignore и в" >&2
  echo "клон не попадает: положи его в рабочее дерево воркера вручную." >&2
  exit 2
fi

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

# release: меньше debug в 2-3 раза и влезает в лимит Telegram; подпись — debug-ключами
# (signingConfig signingConfigs.debug в build.gradle), keystore не нужен. Тумблер BUILD_MODE.
MODE="${BUILD_MODE:-release}"
# Собираем ТОЛЬКО arm64-v8a (реальные устройства) одним APK. Раньше был
# --split-per-abi: он делал 3 APK (arm64/armeabi-v7a/x86_64), а бот шлёт и удаляет
# лишь arm64 — два оставшихся ABI копились в build/. Один target-platform = один
# файл, его и отправляем/чистим, хвостов нет. Другой ABI — через BUILD_ABI.
ABI="${BUILD_ABI:-android-arm64}"
OUT="$PROJECT/build/app/outputs/flutter-apk"

# чистим ВСЕ apk прошлых сборок (в т.ч. лишние ABI от старых split-сборок), чтобы не копились
rm -f "$OUT"/*.apk 2>/dev/null || true

echo ">> flutter build apk ($FLAVOR, $MODE, $ABI, target=$TARGET)"
fvm flutter build apk --"$MODE" --target-platform "$ABI" --target "$TARGET"

# single-ABI сборка (без --split-per-abi) кладёт файл как app-$MODE.apk — имя
# одинаковое для dev и prod, потому что Android-флейворов нет. Переименовываем:
# иначе в Telegram обе сборки приходят как app-release.apk и их не различить.
APK="$OUT/app-$MODE.apk"
if [ -f "$APK" ]; then
  FINAL="$OUT/app-$FLAVOR-$MODE.apk"
  mv -f "$APK" "$FINAL"
  echo "ARTIFACT:$FINAL"
fi

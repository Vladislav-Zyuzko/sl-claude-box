# Статический env Android/Flutter-тулчейна. Кладётся в /etc/profile.d, поэтому
# подхватывается login-сессиями (бот заходит через `bash -lc`). SDK и кэши —
# в persistent-volume /opt/toolchain.
export ANDROID_SDK_ROOT=/opt/toolchain/android-sdk
export ANDROID_HOME=/opt/toolchain/android-sdk
export PUB_CACHE=/opt/toolchain/pub-cache
export GRADLE_USER_HOME=/opt/toolchain/gradle
# Кэш версий Flutter у fvm — тоже в volume, иначе Flutter качается заново при каждой пересборке
export FVM_CACHE_PATH=/opt/toolchain/fvm
# fvm default/bin даёт «обычный» flutter/dart на PATH (помимо `fvm flutter`)
export PATH="$PATH:/opt/toolchain/fvm/default/bin:/opt/toolchain/android-sdk/cmdline-tools/latest/bin:/opt/toolchain/android-sdk/platform-tools:/usr/local/bin"

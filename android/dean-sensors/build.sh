#!/usr/bin/env bash
# Builds dean-sensors.apk with the Android SDK build tools directly (no Gradle).
# Needs: JDK 17, Android SDK with platforms;android-36 and build-tools;36.1.0.
set -euo pipefail
cd "$(dirname "$0")"

SDK=${ANDROID_HOME:-${LOCALAPPDATA:-$HOME}/Android/Sdk}
BT="$SDK/build-tools/36.1.0"
JAR="$SDK/platforms/android-36/android.jar"
EXE=""; [ -f "$BT/aapt2.exe" ] && EXE=".exe"
BAT=""; [ -f "$BT/d8.bat" ] && BAT=".bat"

rm -rf build && mkdir -p build/classes build/dex

echo "compiling Java"
javac --release 8 -encoding UTF-8 -classpath "$JAR" -d build/classes src/com/dean/sensors/*.java

echo "converting to dex"
"$BT/d8$BAT" --release --min-api 26 --lib "$JAR" --output build/dex $(find build/classes -name '*.class')

echo "packaging"
"$BT/aapt2$EXE" link -o build/base.apk -I "$JAR" --manifest AndroidManifest.xml \
  --min-sdk-version 26 --target-sdk-version 35 --version-code 1 --version-name 1.0
python - <<'PY'
import zipfile
with zipfile.ZipFile("build/base.apk", "a") as z:
    z.write("build/dex/classes.dex", "classes.dex")
PY
"$BT/zipalign$EXE" -f -p 4 build/base.apk build/aligned.apk

[ -f debug.keystore ] || keytool -genkeypair -keystore debug.keystore -storepass android \
  -keypass android -alias dean -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=Dean Sensors" >/dev/null 2>&1
"$BT/apksigner$BAT" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android \
  --out dean-sensors.apk build/aligned.apk
echo "built $(pwd)/dean-sensors.apk"

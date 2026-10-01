#!/usr/bin/env bash
# Builds dean-finder.apk with the Android SDK build tools directly (no Gradle).
# Needs: JDK 17, Android SDK with platforms;android-36 and build-tools;36.1.0.
# The private ntfy channel name is read from topic.secret (not committed); create
# it with any long random string, e.g. dean-find-<24 random letters/digits>.
set -euo pipefail
cd "$(dirname "$0")"

SDK=${ANDROID_HOME:-${LOCALAPPDATA:-$HOME}/Android/Sdk}
BT="$SDK/build-tools/36.1.0"
JAR="$SDK/platforms/android-36/android.jar"
EXE=""; [ -f "$BT/aapt2.exe" ] && EXE=".exe"
BAT=""; [ -f "$BT/d8.bat" ] && BAT=".bat"
TOPIC=$(tr -d ' \r\n' < topic.secret)

rm -rf build && mkdir -p build/gen/com/dean/finder build/classes build/dex
cat > build/gen/com/dean/finder/Config.java <<EOF
package com.dean.finder;
final class Config { static final String TOPIC = "$TOPIC"; }
EOF

echo "compiling Java"
javac --release 8 -encoding UTF-8 -classpath "$JAR" \
  -d build/classes src/com/dean/finder/*.java build/gen/com/dean/finder/Config.java

echo "converting to dex"
"$BT/d8$BAT" --release --min-api 26 --lib "$JAR" --output build/dex $(find build/classes -name '*.class')

echo "packaging"
"$BT/aapt2$EXE" link -o build/base.apk -I "$JAR" --manifest AndroidManifest.xml \
  --min-sdk-version 26 --target-sdk-version 35 --version-code 1 --version-name 1.0
python - <<'EOF'
import zipfile
with zipfile.ZipFile("build/base.apk", "a") as z:
    z.write("build/dex/classes.dex", "classes.dex")
EOF
"$BT/zipalign$EXE" -f -p 4 build/base.apk build/aligned.apk

[ -f debug.keystore ] || keytool -genkeypair -keystore debug.keystore -storepass android \
  -keypass android -alias dean -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=Dean Finder" >/dev/null 2>&1
"$BT/apksigner$BAT" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android \
  --out dean-finder.apk build/aligned.apk
echo "built $(pwd)/dean-finder.apk"

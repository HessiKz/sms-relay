# Building the Android APK on your own machine

> **Never build the APK on the VPS.** It has no JDK, no Gradle and no
> Android SDK, and building there would eat the memory the production
> services need. Build locally, or use the provided CI workflow.

## Prerequisites (on your computer)

- JDK 17 (Temurin 17 or Android Studio's bundled JDK)
- Android SDK with platform 35 and build-tools 35
- Android Studio (Ladybug or newer) **or** the Android command line tools
- `gradle` (any recent 8.x) only to generate the Gradle wrapper once

Check what you have:

```bash
java -version          # expect 17.x
echo "$ANDROID_HOME"   # e.g. /home/you/Android/Sdk
ls "$ANDROID_HOME/platforms" | tail -3
```

## Build

The Gradle wrapper (8.11.1) **is included** in the project, so no
separate Gradle installation is needed:

```bash
cd android-app
./gradlew assembleDebug
```

Point the build at your SDK once (Android Studio does this for you):

```bash
export ANDROID_HOME="$HOME/Android/Sdk"      # or wherever your SDK is
printf 'sdk.dir=%s\n' "$ANDROID_HOME" > local.properties
```

If you prefer to use a system Gradle instead of the wrapper, use
**Gradle 8.9 or newer** (AGP 8.7 rejects anything older, and Gradle 9 is
too new for AGP 8.7).

Output:

```
android-app/app/build/outputs/apk/debug/app-debug.apk
```

Install it on the phone:

```bash
adb install -r android-app/app/build/outputs/apk/debug/app-debug.apk
```

or copy the APK to the phone and open it (see [apk-install.md](apk-install.md)).

## Build a release-signed APK (optional, for daily use)

Debug APKs are fine for personal use. For a smaller, non-debuggable build:

`assembleRelease` produces an **unsigned** APK
(`app-release-unsigned.apk`); sign it with apksigner:

```bash
keytool -genkeypair -v -keystore ~/relay.keystore -alias relay \
        -keyalg RSA -keysize 2048 -validity 10000
./gradlew assembleRelease
$ANDROID_HOME/build-tools/35.0.0/zipalign -f 4 \
    app/build/outputs/apk/release/app-release-unsigned.apk \
    app/build/outputs/apk/release/app-release-aligned.apk
$ANDROID_HOME/build-tools/35.0.0/apksigner sign \
    --ks ~/relay.keystore --ks-key-alias relay \
    --out app/build/outputs/apk/release/app-release.apk \
    app/build/outputs/apk/release/app-release-aligned.apk
```

Keep the keystore file offline; losing it means you cannot upgrade the
installed app without uninstalling first (which loses the local queue).

## Build from CI instead (no local machine needed)

`.github/workflows/android-build.yml` builds the debug APK on a GitHub
runner. Push the repository to a private GitHub repository, then run the
**android-build** workflow manually (*Run workflow*). The APK appears as a
build artifact. This keeps the VPS completely untouched.

## Resource notes

- A first Gradle sync downloads a few hundred MB into `~/.gradle` — do it
  on a workstation, not on the VPS.
- Keep `android-app/local.properties` out of git (it contains your SDK
  path). The repository's `.gitignore` already excludes it.
- If the build fails with an out-of-memory error, lower the Gradle heap in
  `android-app/gradle.properties` (`org.gradle.jvmargs=-Xmx2g`) — this is
  a workstation concern, not a server one.

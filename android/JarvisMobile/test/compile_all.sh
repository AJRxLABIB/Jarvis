#!/bin/sh
# Compiles EVERY app source against test/android_api (compile-only Android stand-ins): catches typos, missing imports
# and wrong types without the Android SDK. It cannot prove the Android API is used correctly - build_android.ps1 does.
set -e
cd "$(dirname "$0")/.."
OUT="$(mktemp -d)"
javac -nowarn -d "$OUT" $(find src -name '*.java') $(find test/android_api -name '*.java')
echo "ALL SOURCES COMPILE"

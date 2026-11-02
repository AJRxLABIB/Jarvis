#!/bin/sh
# Compiles the phone's sync code against small Android stand-ins and runs the JVM tests.
# Checks: SyncClient rules (run), AppSync/SyncJob/Store/Pc/PhoneActions/NotifyForward/LiveEvents (all sources: compile_all.sh) compile against the Android API shapes they use.
# MainActivity is NOT compiled here (too many Android classes). Needs a JDK (javac).  Usage: sh test/run.sh
set -e
cd "$(dirname "$0")/.."
OUT="$(mktemp -d)"
S=src/com/jarvis/mobile
javac -d "$OUT" $S/Json.java $S/SyncClient.java $S/AppSync.java $S/SyncJob.java $S/Store.java $S/Pc.java \
  $S/PhoneActions.java $S/LockAdmin.java $S/NotifyForward.java $S/LiveEvents.java \
  $(find test/android_api test/stubs/com -name '*.java') test/SyncClientTest.java
java -cp "$OUT" com.jarvis.mobile.SyncClientTest

#!/usr/bin/env bash
# Run trove-app's Maestro flows on the one booted simulator or emulator.
#   run_flows.sh <ios|android> <app file>
# Maestro echoes typed text, so its console stays in the private log; the
# junit report, screenshots and recordings land in $RUNNER_TEMP/flows-out/<platform>.
# Always exits 0: renders.py verdict decides pass or fail after sealing.
set -uo pipefail
platform="$1"; app="$2"
tools="$(cd "$(dirname "$0")" && pwd)"
src="$RUNNER_TEMP/trove-source"
out="$RUNNER_TEMP/flows-out/$platform"
mkdir -p "$out" "$RUNNER_TEMP/private"
if [ "$platform" = android ]; then
  app_id=com.adestudios.trove
  adb install -r "$app" > "$RUNNER_TEMP/private/install.log" 2>&1 || { echo "android: install failed"; exit 0; }
  # A freshly booted emulator's launcher often raises "isn't responding" over the app.
  adb shell settings put global hide_error_dialogs 1 > /dev/null 2>&1 || true
  # Never echo the last typed character of a password field.
  adb shell settings put system show_password 0 > /dev/null 2>&1 || true
else
  app_id=ade-studios.Munch-RKCK
  xcrun simctl install booted "$app" > "$RUNNER_TEMP/private/install.log" 2>&1 || { echo "ios: install failed"; exit 0; }
fi
# Same library every run: drop the test account's earlier imports of the test link.
python3 -I "$tools/remove_test_imports.py" || true
cd "$out"
run_maestro() {
  # Cap each attempt (macOS has no timeout(1)); a hung driver otherwise eats the whole job.
  # The iOS driver (an XCTest runner) can be slow to come up on a busy runner: give it three minutes.
  MAESTRO_DRIVER_STARTUP_TIMEOUT=180000 perl -e 'alarm shift; exec @ARGV' 1500 maestro test "$src/flows" \
    --format junit --output "$out/report.xml" --test-output-dir "$out/maestro" \
    -e APP_ID="$app_id" \
    -e TROVE_TEST_EMAIL="$TROVE_TEST_EMAIL" -e TROVE_TEST_PASSWORD="$TROVE_TEST_PASSWORD" \
    >> "$RUNNER_TEMP/private/maestro-$platform.log" 2>&1
}
# The runner, not the app, failed: no report at all, or Maestro lost the device (its driver died).
runner_failed() {
  [ ! -s "$out/report.xml" ] || grep -q "DeviceUnreachableException\|Device became unreachable" "$out/report.xml"
}
run_maestro
status=$?
if runner_failed; then
  echo "$platform: the device or its driver failed (maestro exited $status); retrying once on a fresh start"
  # Why: the log's last lines, minus any line holding the test credentials (GitHub masks them too).
  tail -n 25 "$RUNNER_TEMP/private/maestro-$platform.log" | grep -vF -- "$TROVE_TEST_PASSWORD" | grep -vF -- "$TROVE_TEST_EMAIL" \
    | sed "s/^/$platform: maestro: /" || true
  rm -f "$out/report.xml"
  if [ "$platform" = ios ]; then
    # A dead XCTest driver survives in a running simulator: restart it and reinstall the app.
    xcrun simctl shutdown booted > /dev/null 2>&1 || true
    xcrun simctl boot trove-flows > /dev/null 2>&1 || true
    xcrun simctl bootstatus trove-flows -b > /dev/null 2>&1 || true
    xcrun simctl install booted "$app" >> "$RUNNER_TEMP/private/install.log" 2>&1 || true
  else
    adb reconnect > /dev/null 2>&1 || true
    adb wait-for-device > /dev/null 2>&1 || true
  fi
  run_maestro
  status=$?
fi
echo "$platform: maestro exited $status"
# Flow names only (each flow gets a folder when it starts), so a hang shows where it stopped.
for d in "$out"/maestro/*/*/; do [ -d "$d" ] && echo "$platform: started  $(basename "$d")"; done
python3 -I "$tools/check_account_plan.py" "$platform" || true
# The recordings that never film the password being typed; only these become clear previews at publish.
python3 -I "$tools/previewable.py" "$src/flows" > "$out/previewable.txt" || true
# Screenshots and recordings from the flows are written to the working directory.
exit 0

#!/usr/bin/env bash
# Run trove-app's Maestro flows on the one booted simulator or emulator.
#   run_flows.sh <ios|android> <app file>
# Maestro echoes typed text, so its console stays in the private log; the
# junit report, screenshots and recordings land in $RUNNER_TEMP/flows-out/<platform>.
# Always exits 0: renders.py verdict decides pass or fail after sealing.
set -uo pipefail
platform="$1"; app="$2"
src="$RUNNER_TEMP/trove-source"
out="$RUNNER_TEMP/flows-out/$platform"
mkdir -p "$out" "$RUNNER_TEMP/private"
if [ "$platform" = android ]; then
  app_id=com.adestudios.trove
  adb install -r "$app" > "$RUNNER_TEMP/private/install.log" 2>&1 || { echo "android: install failed"; exit 0; }
else
  app_id=ade-studios.Munch-RKCK
  xcrun simctl install booted "$app" > "$RUNNER_TEMP/private/install.log" 2>&1 || { echo "ios: install failed"; exit 0; }
fi
cd "$out"
maestro test "$src/flows" \
  --format junit --output "$out/report.xml" --test-output-dir "$out/maestro" \
  -e APP_ID="$app_id" \
  -e TROVE_TEST_EMAIL="$TROVE_TEST_EMAIL" -e TROVE_TEST_PASSWORD="$TROVE_TEST_PASSWORD" \
  > "$RUNNER_TEMP/private/maestro-$platform.log" 2>&1
echo "$platform: maestro exited $?"
# Screenshots and recordings from the flows are written to the working directory.
exit 0

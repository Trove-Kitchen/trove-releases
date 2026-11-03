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
# On request (device-tests input long_plan): a long meal plan, to check sign-in with one.
python3 -I "$tools/seed_long_plan.py" || true
cd "$out"
# One Maestro run per flow, in file order (01-, 02-, ...), each capped at 10 minutes: Maestro's iOS driver
# sometimes hangs (often while stopping a recording), and a hang should cost one flow, not the whole job.
run_flow() {  # <flow file> <attempt>
  local name; name="$(basename "$1" .yaml)"
  # The iOS driver (an XCTest runner) can be slow to come up on a busy runner: give it three minutes.
  MAESTRO_DRIVER_STARTUP_TIMEOUT=180000 perl -e 'alarm shift; exec @ARGV' 600 maestro test "$1" \
    --format junit --output "$out/report-$name.xml" --test-output-dir "$out/maestro" \
    -e APP_ID="$app_id" \
    -e TROVE_TEST_EMAIL="$TROVE_TEST_EMAIL" -e TROVE_TEST_PASSWORD="$TROVE_TEST_PASSWORD" \
    >> "$RUNNER_TEMP/private/maestro-$platform.log" 2>&1
}
# The runner, not the app, failed: no report, or Maestro lost the device (its driver died).
runner_failed() {  # <report>
  [ ! -s "$1" ] || grep -q "DeviceUnreachableException\|Device became unreachable" "$1"
}
fresh_device() {
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
}
# With a long plan (diagnostics): sample the iPhone app's threads 40, 70 and 100 s into sign-in, so a
# stalled main thread shows where it is (trove-app #462). Simulator apps are host processes, so macOS
# `sample` reads them. The samples go to the private renders as XML (renders.py commits .xml), never
# to this public log.
sample_app() {
  mkdir -p "$out/diagnostics"
  local n
  for n in 40 70 100; do
    sleep 30
    # Simulator apps run as host processes from the device's Bundle/Application folder; the only
    # other one there is Maestro's XCTest runner.
    local pid; pid="$(pgrep -fl 'CoreSimulator/Devices/.*/Bundle/Application/.*\.app/' 2>/dev/null \
      | grep -v -i 'runner\|maestro\|xctest' | awk '{print $1; exit}')"
    [ -n "$pid" ] || echo "$platform: no app process to sample at ${n}s"
    [ -n "$pid" ] || continue
    sample "$pid" 3 -file "$RUNNER_TEMP/private/sample-$n.txt" > /dev/null 2>&1 || continue
    { printf '<sample at="%ss"><![CDATA[\n' "$n"; sed 's/]]>/]] >/g' "$RUNNER_TEMP/private/sample-$n.txt"; printf ']]></sample>\n'; } \
      > "$out/diagnostics/sample-$n.xml"
    echo "$platform: sampled the app at ${n}s"
  done
}
status=0
for flow in "$src"/flows/[0-9]*.yaml; do
  name="$(basename "$flow" .yaml)"
  if [ "$platform" = ios ] && [ "${SEED_PLAN_ROWS:-0}" -gt 0 ] && [ "$name" = 01-sign-in ]; then
    (sleep 10; sample_app) &
  fi
  run_flow "$flow"; status=$?
  if runner_failed "$out/report-$name.xml"; then
    echo "$platform: $name: the device or its driver failed (maestro exited $status); retrying on a fresh start"
    # Why: the log's last lines, minus any line holding the test credentials (GitHub masks them too).
    tail -n 12 "$RUNNER_TEMP/private/maestro-$platform.log" | grep -vF -- "$TROVE_TEST_PASSWORD" | grep -vF -- "$TROVE_TEST_EMAIL" \
      | grep -v '^\s*$' | sed "s/^/$platform: maestro: /" || true
    rm -f "$out/report-$name.xml"
    fresh_device
    run_flow "$flow"; status=$?
  fi
  # A flow that really failed stops the run, as Maestro's continueOnFailure: false did.
  if [ "$status" -ne 0 ]; then break; fi
done
echo "$platform: maestro exited $status"
# Flow names only (each flow gets a folder when it starts), so a hang shows where it stopped.
for d in "$out"/maestro/*/*/; do [ -d "$d" ] && echo "$platform: started  $(basename "$d")"; done
python3 -I "$tools/check_account_plan.py" "$platform" || true
# The recordings that never film the password being typed; only these become clear previews at publish.
python3 -I "$tools/previewable.py" "$src/flows" > "$out/previewable.txt" || true
# Screenshots and recordings from the flows are written to the working directory.
exit 0

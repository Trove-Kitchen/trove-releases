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
echo "$platform: setup before the first flow took $SECONDS s"
# One Maestro run per flow, in file order (01-, 02-, ...), each capped at $flow_limit seconds: Maestro's iOS driver
# sometimes hangs (often while stopping a recording), and a hang should cost one attempt, not the whole job.
# A healthy flow takes 1-5 minutes, driver startup included.
flow_limit=480
# Runner failures (below) are retried on a fresh device: up to 2 retries for one flow, 4 for the whole run.
# Flow failures (a report with a failed step) are never retried.
flow_retries=2; run_retries=4
run_flow() {  # <flow file>
  local name; name="$(basename "$1" .yaml)"
  # The timeout runs Maestro in its own process group and stops the whole group, so no xcodebuild, XCTest
  # runner or recorder outlives a hung attempt and holds the driver for the next one. Exit 142 means timed out.
  # The iOS driver (an XCTest runner) can be slow to come up on a busy runner: give it three minutes.
  MAESTRO_DRIVER_STARTUP_TIMEOUT=180000 perl -e '
    my $limit = shift; my $pid = fork // die "fork: $!";
    if (!$pid) { setpgrp(0, 0); exec @ARGV or exit 127 }
    $SIG{ALRM} = sub {
      kill "TERM", -$pid;
      for (1 .. 15) { last if waitpid($pid, 1) > 0; sleep 1 }  # 1 = WNOHANG
      kill "KILL", -$pid; waitpid $pid, 0; exit 142 };
    alarm $limit; waitpid $pid, 0; alarm 0;
    exit($? & 127 ? 128 + ($? & 127) : $? >> 8)' "$flow_limit" maestro test "$1" \
    --format junit --output "$out/report-$name.xml" --test-output-dir "$out/maestro" \
    -e APP_ID="$app_id" \
    -e TROVE_TEST_EMAIL="$TROVE_TEST_EMAIL" -e TROVE_TEST_PASSWORD="$TROVE_TEST_PASSWORD" \
    >> "$RUNNER_TEMP/private/maestro-$platform.log" 2>&1
}
# The runner, not the app, failed: Maestro timed out (142), wrote no report, or lost the device (its driver died).
runner_failed() {  # <report> <maestro status>
  [ "$2" -eq 142 ] || [ ! -s "$1" ] || grep -q "DeviceUnreachableException\|Device became unreachable" "$1"
}
# Why a runner failure happened, without leaking anything from the app or the account: only the exception
# class names this attempt wrote to Maestro's private log (its messages can quote screen text, so they stay there).
: >> "$RUNNER_TEMP/private/maestro-$platform.log"
log_lines() { wc -l < "$RUNNER_TEMP/private/maestro-$platform.log" | tr -d ' '; }
runner_reason() {  # <maestro status> <log line the attempt started after>
  local classes
  classes="$(tail -n +"$(($2 + 1))" "$RUNNER_TEMP/private/maestro-$platform.log" \
    | grep -oE '([a-z][a-z0-9_]*\.)+[A-Z][A-Za-z0-9_$]*(Exception|Error|Timeout)' | sort -u | head -n 5 | paste -sd, - | sed 's/,/, /g')"
  if [ "$1" -eq 142 ]; then printf 'timed out after %s s' "$flow_limit"; else printf 'maestro exited %s' "$1"; fi
  [ -n "$classes" ] && printf '; %s' "$classes"
  return 0
}
fresh_device() {
  if [ "$platform" = ios ]; then
    # Anything left from the failed attempt: Maestro, its xcodebuild-run XCTest driver, a video recorder.
    pkill -f 'maestro\.cli' > /dev/null 2>&1 || true
    pkill -f 'xcodebuild.*maestro' > /dev/null 2>&1 || true
    pkill -f 'simctl io .*recordVideo' > /dev/null 2>&1 || true
    # A dead XCTest driver survives in a running simulator: restart it, drop the driver so Maestro
    # installs a fresh one, and reinstall the app.
    xcrun simctl shutdown trove-flows > /dev/null 2>&1 || true
    xcrun simctl boot trove-flows > /dev/null 2>&1 || true
    xcrun simctl bootstatus trove-flows -b > /dev/null 2>&1 || true
    xcrun simctl uninstall booted dev.mobile.maestro-driver-iosUITests.xctrunner > /dev/null 2>&1 || true
    xcrun simctl install booted "$app" >> "$RUNNER_TEMP/private/install.log" 2>&1 || true
    local i; for i in $(seq 1 30); do
      xcrun simctl get_app_container booted "$app_id" > /dev/null 2>&1 && return 0; sleep 2
    done
    echo "$platform: the app is not installed on the restarted simulator"
  else
    adb reconnect > /dev/null 2>&1 || true
    adb wait-for-device > /dev/null 2>&1 || true
  fi
}
status=0; retried=0
for flow in "$src"/flows/[0-9]*.yaml; do
  name="$(basename "$flow" .yaml)"
  attempt=1
  while :; do
    began=$SECONDS; logged=$(log_lines)
    run_flow "$flow"; status=$?
    took=$((SECONDS - began))
    if ! runner_failed "$out/report-$name.xml" "$status"; then
      if [ "$status" -eq 0 ]; then echo "$platform: $name: passed (attempt $attempt, $took s)"
      else echo "$platform: $name: flow failure (attempt $attempt, $took s, maestro exited $status): a step failed; see the report"; fi
      break
    fi
    # A timed-out attempt may leave a partial report; it is no verdict on the app.
    rm -f "$out/report-$name.xml"
    if [ "$attempt" -gt "$flow_retries" ] || [ "$retried" -ge "$run_retries" ]; then
      echo "$platform: $name: runner failure (attempt $attempt, $took s, $(runner_reason "$status" "$logged")); no retries left, so this flow has no result"
      break
    fi
    echo "$platform: $name: runner failure (attempt $attempt, $took s, $(runner_reason "$status" "$logged")); retrying on a fresh device"
    fresh_device
    attempt=$((attempt + 1)); retried=$((retried + 1))
  done
  # A flow that really failed, or a runner that kept failing, stops the run, as Maestro's continueOnFailure: false did.
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

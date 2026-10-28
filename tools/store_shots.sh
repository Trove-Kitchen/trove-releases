#!/usr/bin/env bash
# Capture and frame trove-app's store screenshots on the one booted simulator or
# emulator. Each frame starts from a fresh install with the DEBUG store-screenshot
# account (trove-app: Munch/StoreScreenshots.swift, android .../StoreShots.kt),
# which seeds TheMealDB recipes on the device and reaches no backend.
#   store_shots.sh capture <ios|android> <app file>
#   store_shots.sh frame <ios|android> <Inter fonts dir>
# Output: $RUNNER_TEMP/flows-out/<platform>/{captures,store,proof}/*.png and
# store-shots.xml (one junit case per capture, and one for the framed set),
# which renders.py seals. Prints frame names and verdicts only. Always exits 0.
set -uo pipefail
cmd="$1"; platform="$2"
src="$RUNNER_TEMP/trove-source"
out="$RUNNER_TEMP/flows-out/$platform"
shots="$src/docs/app-store/screenshots"
mkdir -p "$out/captures" "$RUNNER_TEMP/private"
log="$RUNNER_TEMP/private/store-shots-$platform.log"

# frame name in the app and its capture file (frames.json)
FRAMES=(
  "import 01-save-from-link.png"
  "recipe 02-recipe-source.png"
  "cooking 03-cooking-timer.png"
  "mealPlan 04-meal-plan-week.png"
  "grocery 05-grocery-list.png"
  "discover 06-discover.png"
  "guides 07-guides.png"
  "library 08-library.png"
)
WAIT=14  # seconds for the seed, Discover's catalog and the photos to land

capture_ios() {
  local app="$1" bundle=ade-studios.Munch-RKCK
  for entry in "${FRAMES[@]}"; do
    set -- $entry; local frame="$1" file="$2"
    xcrun simctl terminate booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl uninstall booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl install booted "$app" >> "$log" 2>&1 || { echo "ios: install failed"; return; }
    local tab=recipes
    case "$frame" in mealPlan|grocery) tab="$frame" ;; library) tab=profile ;; esac
    # The unsigned build has no iCloud entitlement, so CloudKit stays off.
    SIMCTL_CHILD_MUNCH_DISABLE_CLOUDKIT=1 SIMCTL_CHILD_MUNCH_STORE_SHOTS="$frame" SIMCTL_CHILD_MUNCH_START_TAB="$tab" \
      xcrun simctl launch booted "$bundle" MUNCH_SKIP_ONBOARDING >> "$log" 2>&1 || echo "ios: $frame: launch failed"
    sleep "$WAIT"
    xcrun simctl io booted screenshot "$out/captures/$file" >> "$log" 2>&1 || echo "ios: $frame: screenshot failed"
    echo "ios: captured $frame"
  done
  xcrun simctl spawn booted log show --last 20m --style compact --predicate 'process == "Munch"' > "$RUNNER_TEMP/private/app.log" 2>/dev/null || true
}

capture_android() {
  local apk="$1" pkg=com.adestudios.trove
  adb install -r "$apk" >> "$log" 2>&1 || { echo "android: install failed"; return; }
  adb shell settings put global hide_error_dialogs 1 >> "$log" 2>&1 || true
  # Gesture navigation (no three-button bar) and a clean status bar: 9:41, full battery.
  adb shell cmd overlay enable com.android.internal.systemui.navbar.gestural >> "$log" 2>&1 || true
  adb shell settings put global sysui_demo_allowed 1 >> "$log" 2>&1
  demo() { adb shell am broadcast -a com.android.systemui.demo -e command "$@" >> "$log" 2>&1; }
  demo enter
  demo clock -e hhmm 0941
  demo battery -e level 100 -e plugged false -e powersave false
  demo network -e wifi show -e level 4 -e fully true
  demo network -e mobile show -e datatype none -e level 4 -e fully true
  demo notifications -e visible false
  # The Pixel 7 image's launcher can stall under software rendering and leave an
  # "isn't responding" dialog over every frame. The frames start the app directly,
  # so the launcher isn't needed: turn it off.
  adb shell pm disable-user --user 0 com.google.android.apps.nexuslauncher >> "$log" 2>&1 || true
  for entry in "${FRAMES[@]}"; do
    set -- $entry; local frame="$1" file="$2"
    adb shell pm clear "$pkg" >> "$log" 2>&1 || true
    adb shell pm grant "$pkg" android.permission.POST_NOTIFICATIONS >> "$log" 2>&1 || true
    adb shell am start -W -n "$pkg/.MainActivity" --es trove.storeShots "$frame" >> "$log" 2>&1 || echo "android: $frame: launch failed"
    sleep "$WAIT"
    adb shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS >> "$log" 2>&1 || true
    sleep 1
    adb exec-out screencap -p > "$out/captures/$file" 2>> "$log" || echo "android: $frame: screenshot failed"
    echo "android: captured $frame"
  done
  adb logcat -d > "$RUNNER_TEMP/private/logcat.txt" 2>/dev/null || true
}

frame_set() {
  local fonts="$1" status=0
  (cd "$shots" && node build.mjs frames.json --target "$platform" --captures "$out/captures" --out "$out/store" --fonts "$fonts") >> "$log" 2>&1 || status=$?
  (cd "$shots" && node build.mjs frames.json --target "$platform" --captures "$out/captures" --out "$out/proof" --fonts "$fonts" --proof) >> "$log" 2>&1 || true
  # The build's own complaints (frame names and layout notes, no source) are worth seeing here.
  grep -E '^(not built|layout warnings)|^  (ios|android)[/:]' "$log" | sed "s/^/$platform: /" | tail -n 20 || true
  # junit: one case per capture (present and not blank), one for the framed set.
  python3 -I - "$out" "$platform" "$status" <<'PY'
import os, sys
from xml.sax.saxutils import quoteattr
out, platform, status = sys.argv[1], sys.argv[2], int(sys.argv[3])
cases = []
for name in ["01-save-from-link", "02-recipe-source", "03-cooking-timer", "04-meal-plan-week", "05-grocery-list", "06-discover",
             "07-guides", "08-library"]:
    path = os.path.join(out, "captures", name + ".png")
    ok = os.path.isfile(path) and os.path.getsize(path) > 30_000
    cases.append((f"capture {name}", ok))
cases.append(("framed set (build.mjs)", status == 0))
with open(os.path.join(out, "store-shots.xml"), "w") as f:
    f.write(f'<testsuite name="store screenshots {platform}" tests="{len(cases)}">\n')
    for name, ok in cases:
        f.write(f'  <testcase name={quoteattr(name)}>' + ('' if ok else '<failure/>') + '</testcase>\n')
    f.write('</testsuite>\n')
PY
}

case "$cmd" in
  capture) if [ "$platform" = ios ]; then capture_ios "$3"; else capture_android "$3"; fi ;;
  frame) frame_set "$3" ;;
  *) echo "usage: store_shots.sh capture|frame ios|android <file>"; exit 0 ;;
esac
exit 0

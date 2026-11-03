#!/usr/bin/env bash
# Capture and frame trove-app's store screenshots on the one booted simulator or
# emulator. Each frame starts from a fresh install with the DEBUG store-screenshot
# account (trove-app: Munch/StoreScreenshots.swift, android .../StoreShots.kt),
# which seeds TheMealDB recipes on the device and reaches no backend.
#   store_shots.sh capture <ios|android> <app file> [ipad]   (ipad: an iPad simulator is booted)
#   store_shots.sh frame <ios|android> <Inter fonts dir>
#   store_shots.sh preview ios <app file>          (records flows/preview/, Maestro on PATH)
#   store_shots.sh compose ios <Inter fonts dir>   (captions and joins them, ffmpeg on PATH)
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
  "link 09-paste-link.png"
)
WAIT=18  # seconds for the seed, Discover's catalog and the photos to land

# Languages to capture on iPhone (STORE_LANGS, space separated, default en). English goes
# to captures/ and store/, any other language to captures/<lang>/ and store/<lang>/, framed
# with frames.json's localized set (build.mjs --lang). Android has no translations yet,
# so it stays English.
LANGS="${STORE_LANGS:-en}"
[[ "$LANGS" =~ ^[A-Za-z]{2}(-[A-Za-z]{2})?( [A-Za-z]{2}(-[A-Za-z]{2})?)*$ ]] || { echo "STORE_LANGS: language codes like de or pt-BR, space separated"; exit 0; }
[ "$platform" = android ] && LANGS=en
locale_for() {
  case "$1" in
    en) echo en_US ;; es) echo es_MX ;; *-*) echo "${1%-*}_${1#*-}" ;; *) echo "${1}_$(echo "$1" | tr a-z A-Z)" ;;
  esac
}

capture_ios() {
  local app="$1" device="${2:-}" bundle=ade-studios.Munch-RKCK lang dir root="$out/captures"
  [ "$device" = ipad ] && root="$out/captures-ipad"
  for lang in $LANGS; do
  dir="$root"; [ "$lang" = en ] || dir="$root/$lang"
  mkdir -p "$dir"
  for entry in "${FRAMES[@]}"; do
    set -- $entry; local frame="$1" file="$2"
    xcrun simctl terminate booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl uninstall booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl install booted "$app" >> "$log" 2>&1 || { echo "ios: install failed"; return; }
    local tab=recipes
    case "$frame" in mealPlan|grocery) tab="$frame" ;; library) tab=profile ;; esac
    # The unsigned build has no iCloud entitlement, so CloudKit stays off.
    SIMCTL_CHILD_MUNCH_DISABLE_CLOUDKIT=1 SIMCTL_CHILD_MUNCH_STORE_SHOTS="$frame" SIMCTL_CHILD_MUNCH_START_TAB="$tab" \
      xcrun simctl launch booted "$bundle" MUNCH_SKIP_ONBOARDING -AppleLanguages "($lang)" -AppleLocale "$(locale_for "$lang")" \
      >> "$log" 2>&1 || echo "ios: $lang $frame: launch failed"
    sleep "$WAIT"
    xcrun simctl io booted screenshot "$dir/$file" >> "$log" 2>&1 || echo "ios: $lang $frame: screenshot failed"
    echo "ios: captured ${device:+$device }$lang $frame"
  done
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
  # SystemUI can restart while the emulator is busy and drop demo mode, so this
  # runs again before every screenshot.
  statusbar() {
    demo enter
    demo clock -e hhmm 0941
    demo battery -e level 100 -e plugged false -e powersave false
    demo network -e wifi show -e level 4 -e fully true
    demo network -e mobile show -e datatype none -e level 4 -e fully true
    demo notifications -e visible false
  }
  statusbar
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
    statusbar
    sleep 2
    adb exec-out screencap -p > "$out/captures/$file" 2>> "$log" || echo "android: $frame: screenshot failed"
    echo "android: captured $frame"
  done
  adb logcat -d > "$RUNNER_TEMP/private/logcat.txt" 2>/dev/null || true
}

frame_set() {
  local fonts="$1" status=0 lang targets=("$platform") t
  [ "$platform" = ios ] && targets=(ios ios-69 ipad)
  for lang in $LANGS; do
    local langflag=() sub=""
    [ "$lang" = en ] || { langflag=(--lang "$lang"); sub="/$lang"; }
    for t in "${targets[@]}"; do
      # The 6.9-inch and iPad sets need a trove-app with those targets; older commits skip them.
      [ "$t" != "$platform" ] && ! grep -q "\"$t\"" "$shots/frames.json" && continue
      local caps="$out/captures"; [ "$t" = ipad ] && caps="$out/captures-ipad"
      [ "$t" = ipad ] && [ ! -d "$caps" ] && continue
      caps="$caps$sub"
      local o="$out/store/$t$sub"; [ "$t" = "$platform" ] && o="$out/store$sub"
      (cd "$shots" && node build.mjs frames.json --target "$t" ${langflag[@]+"${langflag[@]}"} --captures "$caps" --out "$o" --fonts "$fonts") >> "$log" 2>&1 || status=$?
      (cd "$shots" && node build.mjs frames.json --target "$t" ${langflag[@]+"${langflag[@]}"} --captures "$caps" --out "${o/store/proof}" --fonts "$fonts" --proof) >> "$log" 2>&1 || true
    done
  done
  # The App Store header and search images (iOS 27+), per language, from the iPhone captures.
  if [ "$platform" = ios ] && grep -q -- "--lang" "$shots/covers.mjs"; then
    for lang in $LANGS; do
      local caps="$out/captures" langflag=() o="$out/store/covers"
      [ "$lang" = en ] || { caps="$caps/$lang"; langflag=(--lang "$lang"); o="$o/$lang"; }
      for a in header search universal; do
        (cd "$shots" && node covers.mjs frames.json --only "$a" ${langflag[@]+"${langflag[@]}"} --captures "$caps" --out "$o" --fonts "$fonts") >> "$log" 2>&1 || status=$?
      done
    done
  fi
  # The build's own complaints (frame names and layout notes, no source) are worth seeing here.
  grep -E '^(not built|layout warnings)|^  [A-Za-z0-9/_.-]+: ' "$log" | sed "s/^/$platform: /" | tail -n 20 || true
  # junit: one case per capture (present and not blank), one for the framed set.
  python3 -I - "$out" "$platform" "$status" $LANGS <<'PY'
import os, sys
from xml.sax.saxutils import quoteattr
out, platform, status, langs = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4:]
cases = []
roots = [("captures", "")] + ([("captures-ipad", "ipad ")] if os.path.isdir(os.path.join(out, "captures-ipad")) else [])
for root, label in roots:
 for lang in langs:
  for name in ["01-save-from-link", "02-recipe-source", "03-cooking-timer", "04-meal-plan-week", "05-grocery-list", "06-discover",
               "07-guides", "08-library"] + (["09-paste-link"] if platform == "ios" else []):
    path = os.path.join(out, root, *([] if lang == "en" else [lang]), name + ".png")
    ok = os.path.isfile(path) and os.path.getsize(path) > 30_000
    cases.append((f"capture {label}{name}" if lang == "en" else f"capture {label}{lang} {name}", ok))
cases.append(("framed sets (build.mjs)", status == 0))
with open(os.path.join(out, "store-shots.xml"), "w") as f:
    f.write(f'<testsuite name="store screenshots {platform}" tests="{len(cases)}">\n')
    for name, ok in cases:
        f.write(f'  <testcase name={quoteattr(name)}>' + ('' if ok else '<failure/>') + '</testcase>\n')
    f.write('</testsuite>\n')
PY
}

# App previews: trove-app's flows/preview/ drive the store-shot account (Review Recipe
# open, as frame "import") in each language; Maestro records each beat as its own clip.
preview_ios() {
  local app="$1" bundle=ade-studios.Munch-RKCK lang
  for lang in $LANGS; do
    local dir="$out/preview-raw/$lang" labels
    mkdir -p "$dir"
    [ -f "$src/flows/preview/preview.yaml" ] || { echo "ios: this trove-app has no flows/preview"; return; }
    labels="$(python3 -I "$src/scripts/localization/preview_labels.py" "$lang")" || { echo "ios: $lang: no preview labels"; continue; }
    xcrun simctl terminate booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl uninstall booted "$bundle" >> "$log" 2>&1 || true
    xcrun simctl install booted "$app" >> "$log" 2>&1 || { echo "ios: install failed"; return; }
    SIMCTL_CHILD_MUNCH_DISABLE_CLOUDKIT=1 SIMCTL_CHILD_MUNCH_STORE_SHOTS=import SIMCTL_CHILD_MUNCH_START_TAB=recipes \
      xcrun simctl launch booted "$bundle" MUNCH_SKIP_ONBOARDING -AppleLanguages "($lang)" -AppleLocale "$(locale_for "$lang")" \
      >> "$log" 2>&1 || { echo "ios: $lang preview: launch failed"; continue; }
    sleep "$WAIT"
    eval "set -- $labels"
    MAESTRO_DRIVER_STARTUP_TIMEOUT=180000 perl -e 'alarm shift; exec @ARGV' 600 maestro test "$src/flows/preview/preview.yaml" \
      -e APP_ID="$bundle" "$@" --format junit --output "$dir/report.xml" --test-output-dir "$dir/maestro" \
      >> "$RUNNER_TEMP/private/maestro-preview.log" 2>&1 && echo "ios: recorded $lang preview" \
      || echo "ios: $lang preview: a beat failed ($(grep -oE 'name="Preview [0-9]' "$dir/report.xml" 2>/dev/null | tail -1 | grep -oE '[0-9]$' | sed 's/^/after beat /'))"
    cp "$RUNNER_TEMP/private/maestro-preview.log" "$dir/maestro.log" 2>/dev/null || true
  done
}

# Each clip scaled to Apple's 886x1920 portrait at 30 fps, captioned with its beat's line
# from captions.json (previewBeats), then joined: preview/<lang>/app-preview.mp4.
compose_ios() {
  local fonts="$1" lang
  for lang in $LANGS; do
    local dir="$out/preview-raw/$lang" dest="$out/preview/$lang" n=0 list
    [ -d "$dir/maestro" ] || continue
    mkdir -p "$dest"; list="$dest/.clips.txt"; : > "$list"
    while IFS= read -r beat; do
      n=$((n + 1))
      local clip; clip="$(find "$dir/maestro" -name "preview-$n-*.mp4" | head -1)"
      [ -n "$clip" ] || { echo "ios: $lang preview: no clip $n"; continue 2; }
      python3 -I - "$src/scripts/localization/store/captions.json" "$lang" "$beat" > "$dest/.caption-$n.txt" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
print(c["captions"][sys.argv[2]][c["frames"].index(sys.argv[3])], end="")
PY
      cp "$GITHUB_WORKSPACE/tools/caption_png.mjs" "$shots/.caption.mjs"
      (cd "$shots" && node .caption.mjs "$dest/.caption-$n.txt" "$fonts" "$dest/.caption-$n.png") >> "$log" 2>&1 \
        || { echo "ios: $lang preview: caption $n didn't render"; continue 2; }
      # Apple takes 15 to 30 seconds: a beat longer than 9 s (Maestro's own pauses) plays faster.
      local secs speed
      secs="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$clip")"
      speed="$(python3 -I -c 'import sys; print(f"{min(1.0, 9.0 / max(float(sys.argv[1]), 0.1)):.4f}")' "${secs:-9}")"
      ffmpeg -nostdin -y -loglevel error -i "$clip" -i "$dest/.caption-$n.png" -an -r 30 \
        -filter_complex "[0:v]setpts=${speed}*PTS,scale=886:-2,crop=886:1920[v];[v][1:v]overlay=(W-w)/2:H*0.78" \
        -c:v libx264 -pix_fmt yuv420p -profile:v high -preset slow -crf 18 "$dest/.clip-$n.mp4" >> "$log" 2>&1 \
        || { echo "ios: $lang preview: clip $n didn't encode ($(grep -oE "No such (filter|file or directory)|Invalid argument|Unknown encoder|not found|Error [a-z ]+" "$log" | tail -1))"; continue 2; }
      echo "file '.clip-$n.mp4'" >> "$list"
    done < <(python3 -I -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["previewBeats"]))' "$src/scripts/localization/store/captions.json")
    # The joined preview, then sped up as a whole when it runs past 29 s: Maestro's raw clips
    # can report a shorter length than they play, so the per-beat cap above can miss.
    (cd "$dest" && ffmpeg -nostdin -y -loglevel error -f concat -safe 0 -i .clips.txt -c copy .joined.mp4) >> "$log" 2>&1
    local total; total="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$dest/.joined.mp4" 2>/dev/null)"
    if python3 -I -c 'import sys; sys.exit(0 if float(sys.argv[1] or 0) > 29 else 1)' "${total:-0}"; then
      local fit; fit="$(python3 -I -c 'import sys; print(f"{28.5 / float(sys.argv[1]):.4f}")' "$total")"
      (cd "$dest" && ffmpeg -nostdin -y -loglevel error -i .joined.mp4 -an -r 30 -vf "setpts=${fit}*PTS" \
        -c:v libx264 -pix_fmt yuv420p -profile:v high -preset slow -crf 18 .fitted.mp4 && mv .fitted.mp4 .joined.mp4) >> "$log" 2>&1
    fi
    (cd "$dest" && mv .joined.mp4 app-preview.mp4) >> "$log" 2>&1 \
      && echo "ios: $lang preview: $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$dest/app-preview.mp4" | cut -d. -f1)s" \
      || echo "ios: $lang preview: join failed"
    rm -f "$dest"/.clip-*.mp4 "$dest"/.caption-*.txt "$dest"/.caption-*.png "$list" "$shots/.caption.mjs"
  done
  # junit: one case per language, a preview of 15 to 30 seconds.
  python3 -I - "$out" $LANGS <<'PY'
import os, subprocess, sys
from xml.sax.saxutils import quoteattr
out, langs = sys.argv[1], sys.argv[2:]
cases = []
for lang in langs:
    path = os.path.join(out, "preview", lang, "app-preview.mp4")
    ok = False
    if os.path.isfile(path):
        d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
        ok = 15 <= float(d or 0) <= 30
    cases.append((f"app preview {lang}", ok))
with open(os.path.join(out, "previews.xml"), "w") as f:
    f.write(f'<testsuite name="app previews" tests="{len(cases)}">\n')
    for name, ok in cases:
        f.write(f'  <testcase name={quoteattr(name)}>' + ('' if ok else '<failure/>') + '</testcase>\n')
    f.write('</testsuite>\n')
PY
  # The raw beats (Maestro's report, screenshots and recordings) stay in the sealed set, so a
  # failed beat can be read in trove-renders.
}

case "$cmd" in
  capture) if [ "$platform" = ios ]; then capture_ios "$3" "${4:-}"; else capture_android "$3"; fi ;;
  frame) frame_set "$3" ;;
  preview) preview_ios "$3" ;;
  compose) compose_ios "$3" ;;
  *) echo "usage: store_shots.sh capture|frame ios|android <file>"; exit 0 ;;
esac
exit 0

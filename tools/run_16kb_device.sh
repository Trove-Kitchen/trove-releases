#!/usr/bin/env bash
# Run trove-app's SpeechModelDeviceTest on the one booted emulator, which should
# have 16 KB memory pages, and print a one-line verdict:
#   16 KB device: page size 16384; vosk loaded: yes; transcribed: yes
# The Gradle log stays in $RUNNER_TEMP/private. Only test names, verdicts and the
# first line of a failure (an exception message, never source) are printed.
set -uo pipefail
src="$RUNNER_TEMP/trove-source/android"
private="$RUNNER_TEMP/private"
mkdir -p "$private"
page="$(adb shell getconf PAGE_SIZE 2>/dev/null | tr -d '\r\n')"
echo "emulator page size: ${page:-unknown}"
adb logcat -c 2>/dev/null || true
cd "$src"
gradle --no-daemon --console=plain :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.class=com.adestudios.trove.SpeechModelDeviceTest \
  > "$private/connected.log" 2>&1
status=$?
echo "gradle connectedDebugAndroidTest exited $status"
adb logcat -d > "$private/logcat.txt" 2>/dev/null || true
if [ $status -ne 0 ]; then
  # Why it failed, without source: Gradle's run summary and the crash lines
  # (signal, native library, exception names) from logcat.
  grep -E "Instrumentation run failed|Process crashed|Test run failed|Tests on .* failed|INSTALL_FAILED" \
    "$private/connected.log" | sed -E "s#$RUNNER_TEMP/trove-source/##g" | sort -u | head -10 | sed 's/^/  gradle: /'
  grep -E "SpeechModelDeviceTest|Fatal signal|FATAL EXCEPTION|Abort message|UnsatisfiedLinkError|dlopen|libvosk|libjnidispatch|com\.sun\.jna|org\.vosk|LinkageError|OutOfMemory|Process: com\.adestudios|backtrace:|  #[0-9]{2} pc" \
    "$private/logcat.txt" | head -60 | sed 's/^/  logcat: /'
fi
PAGE="$page" python3 -I - app/build/outputs/androidTest-results/connected <<'PY'
import glob, os, sys
import xml.etree.ElementTree as ET
results = {}
for path in glob.glob(os.path.join(sys.argv[1], "**", "TEST-*.xml"), recursive=True):
    for case in ET.parse(path).iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if case.find("skipped") is not None:
            results[case.get("name")] = ("skipped", "")
        elif bad is not None:
            first = (bad.get("message") or bad.text or "").strip().splitlines()
            results[case.get("name")] = ("failed", first[0][:240] if first else "")
        else:
            results[case.get("name")] = ("passed", "")
for name, (verdict, why) in sorted(results.items()):
    print(f"  {name}: {verdict}" + (f" ({why})" if why else ""))
ok = lambda n: "yes" if results.get(n, ("missing",))[0] == "passed" else "no"
page = os.environ.get("PAGE") or "unknown"
print(f"16 KB device: page size {page}; vosk loaded: {ok('voskLoadsModelAndRecognizer')}; "
      f"transcribed: {ok('transcribesSpokenClip')}")
expected = {"pageSizeIs16Kb", "voskLoadsModelAndRecognizer", "transcribesSpokenClip"}
passed = expected <= {n for n, (v, _) in results.items() if v == "passed"}
print("16 KB device check: " + ("passed" if passed and page == "16384" else "failed"))
sys.exit(0 if passed and page == "16384" else 1)
PY
verdict=$?
if [ $status -ne 0 ] && [ $verdict -eq 0 ]; then verdict=1; fi
exit $verdict

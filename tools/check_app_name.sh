#!/bin/bash
# Check the name a built Trove app shows in system prompts.
#
#   check_app_name.sh <path to Munch.app> <errors.md to append to>
#
# The system sign-in prompt ("“Trove” Wants to Use “google.com” to Sign In")
# shows CFBundleName. The app target generates its Info.plist, so CFBundleName
# there is the product name, Munch; the app's InfoPlist.xcstrings overrides it
# with a localized "Trove" in each language. This prints the bundle name and
# display name from Info.plist and from each <lang>.lproj/InfoPlist.strings, and
# fails if any language (or the app as a whole, with no languages) resolves to
# a name other than Trove.
#
# Public-safe: it prints language codes and those two names, nothing else
# derived from trove-app source.
set -u
app="$1"
out="$2"
want="Trove"

read_key() { plutil -extract "$1" raw -o - "$2" 2>/dev/null || true; }

[ -f "$app/Info.plist" ] || { echo "app name: no built Info.plist to check"; echo "- The app name check found no built Info.plist." >> "$out"; exit 1; }

name="$(read_key CFBundleName "$app/Info.plist")"
display="$(read_key CFBundleDisplayName "$app/Info.plist")"
echo "Info.plist: CFBundleName=${name:-(none)} CFBundleDisplayName=${display:-(none)}"

bad=()
checked=0
for lproj in "$app"/*.lproj; do
  [ -d "$lproj" ] || continue
  lang="$(basename "$lproj" .lproj)"
  [ "$lang" = Base ] && continue
  strings="$lproj/InfoPlist.strings"
  lname=""; ldisplay=""
  if [ -f "$strings" ]; then
    lname="$(read_key CFBundleName "$strings")"
    ldisplay="$(read_key CFBundleDisplayName "$strings")"
  fi
  echo "$lang: InfoPlist CFBundleName=${lname:-(none)} CFBundleDisplayName=${ldisplay:-(none)}"
  checked=$((checked + 1))
  [ "${lname:-$name}" = "$want" ] || bad+=("$lang shows ${lname:-$name}")
  [ "${ldisplay:-$display}" = "$want" ] || bad+=("$lang home screen shows ${ldisplay:-$display}")
done

# en is the development language: it must have its own override, or the
# app falls back to Info.plist's CFBundleName for English and any language
# without an .lproj.
if [ ! -f "$app/en.lproj/InfoPlist.strings" ] && [ "$name" != "$want" ]; then
  bad+=("English and unlisted languages show ${name:-(none)}: no en.lproj/InfoPlist.strings")
fi
[ "$checked" -gt 0 ] || { [ "$name" = "$want" ] || bad+=("CFBundleName is ${name:-(none)}"); }

if [ ${#bad[@]} -gt 0 ]; then
  echo "app name: failed (${#bad[@]} problems)"
  {
    echo "- The sign-in prompt would not say $want. It shows CFBundleName, which the generated Info.plist sets to the product name; localize CFBundleName and CFBundleDisplayName in Munch/InfoPlist.xcstrings for every language:"
    for b in "${bad[@]}"; do echo "  - $b"; done
  } >> "$out"
  exit 1
fi
echo "app name: $want in Info.plist or every language's InfoPlist.strings"

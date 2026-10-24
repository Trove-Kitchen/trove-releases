#!/usr/bin/env bash
# Refuse to test against anything but trove-staging and the staging resolver.
set -euo pipefail
for n in SUPABASE_URL SUPABASE_ANON_KEY RESOLVER_BASE_URL TROVE_TEST_EMAIL TROVE_TEST_PASSWORD; do
  [ -n "${!n:-}" ] || { echo "Missing $n in trove-device-tests"; exit 1; }
done
case "$SUPABASE_URL" in
  https://kcmldknrfijrhacnqwjx.supabase.co|https://kcmldknrfijrhacnqwjx.supabase.co/) ;;
  *) echo "SUPABASE_URL is not trove-staging; refusing to run tests."; exit 1 ;;
esac
case "$RESOLVER_BASE_URL" in
  https://trove-video-resolver-staging.up.railway.app|https://trove-video-resolver-staging.up.railway.app/) ;;
  *) echo "RESOLVER_BASE_URL is not the staging resolver; refusing to run tests."; exit 1 ;;
esac
echo "Backend: trove-staging and the staging resolver."

# trove-releases

The release pipeline for **Trove**. This repository holds workflows and credentials. **It holds no app source.**

Source lives in two private repositories, Trove-Kitchen/trove-app and Trove-Kitchen/trove-commerce. Each run clones them with a short-lived token from the **`trove-kitchen-ci`** GitHub App (contents: read on those two repositories only), minted by `actions/create-github-app-token` from `TROVE_CI_APP_ID` and `TROVE_CI_APP_PRIVATE_KEY`.

## Why this repo is public

GitHub bills macOS runners at **10× the Linux rate** on private repositories — about **$0.062 a minute**, and a Trove build runs 20–40 minutes, so roughly **$1.24–$2.48 per build**. Public repositories get macOS minutes free.

Being public costs nothing here because there is no source and no readable secret: GitHub encrypts secrets, and a pull request from a fork never receives them.

## Workflows

| Workflow | Runner | Environment | What it proves |
| --- | --- | --- | --- |
| **Trove iOS** (`ios-testflight.yml`) | macOS | `trove-ios` | Archive, sign, validate with Apple; upload to TestFlight when `dry_run` is off |
| **Trove iOS compile** (`ios-compile.yml`) | macOS | none | The app compiles for the simulator and the MunchMatching tests pass, with no signing |
| **Trove Android unit tests** (`android-unit.yml`) | Linux | none | `:app:testDebugUnitTest` passes |
| **Trove web tests** (`web-tests.yml`) | Linux | none | The web site's design tokens are current and pass contrast, its unit tests pass, it builds on fixture data, and its Playwright e2e tests pass in Chromium |
| **Trove video resolver container** (`resolver-container.yml`) | Linux | none | The resolver's production image builds, starts offline, and passes its mocked API and extraction tests with no network |
| **Trove checks on a schedule** (`check-schedule.yml`) | Linux | none | Every 15 minutes, dispatches the web tests and the video resolver container for trove-app heads that changed their paths and haven't been checked |
| **Trove device tests** (`device-tests.yml`) | Linux + macOS | `trove-device-tests` | trove-app's Maestro flows pass on an Android emulator and an iPhone simulator against trove-staging; recordings go to trove-renders |
| **Trove web flows** (`web-flows.yml`) | Linux | `trove-device-tests` | trove-app's Playwright flows pass in Chromium (phone and desktop) against trove-staging; videos go to trove-renders |
| **Trove Android** (`android-play.yml`) | Linux | `trove-android` for the release job | `bundleRelease` builds and meets Play's 16 KB page rule; signs and sends to the Play **internal** track when `dry_run` is off |
| **Trove Android 16 KB device check** (`android-16kb-device.yml`) | Linux | none | On an Android 15 emulator with 16 KB pages, the Vosk speech model loads its native libraries and transcribes a spoken clip (trove-app's `SpeechModelDeviceTest`) |

The check workflows (iOS compile, Android unit tests, web tests, video resolver container) take a trove-app `ref` and, optionally, a trove-app `pr_number`. They are built for agents that have no Mac: dispatch one at a branch and read the result. A pull request that changes a check workflow, `fetch-source` or `tools/report.py` runs that check once against trove-app `main`.

### Checks on a schedule

trove-app runs no Actions of its own. `check-schedule.yml` runs every 15 minutes (and by hand) and dispatches `web-tests.yml` and `resolver-container.yml` for trove-app heads, with `tools/check_schedule.py`:

- **Which heads.** main, and every branch with a commit in the last 3 days. A branch counts when `main...head` changes the check's paths. For main, it's the range since the newest commit that check already ran on.
- **Which paths.** Web tests: `web/`, `scripts/design-tokens/`, `docs/design/tokens.json`. Resolver: `services/video-resolver/`.
- **How work is found.** `trove-kitchen-ci` can read trove-app's contents but not its pull requests, so heads come from branches and comparisons.
- **When a head is skipped.** A commit runs once per check: a run named with that SHA in the last 5 days skips it. At most 3 checks are dispatched per run.
- **Pull request numbers.** When `TROVE_PR_COMMENT_TOKEN` can read trove-app's pull requests, each pick's open pull request is found and passed as `pr_number`, so the details are posted there.
- **Dry runs.** A pull request that changes the schedule runs its unit tests (`tools/test_check_schedule.py`) and a dry run that dispatches nothing.

The two scheduled checks take an optional `sha` as well as `ref`. Their run names and logs show only the SHA and the pull request number, never a trove-app branch name; a `ref` is read from the event and resolved to its commit before anything prints it.

### Nothing from trove-app is printed here

This repository is public and trove-app is private. Workflows keep build and test logs on the runner and print pass/fail and counts. The check workflows post the details (path:line and the compiler's or test's message, never source) only as a comment on the trove-app pull request (`tools/report.py`). No bundle, APK or IPA is uploaded as an artifact. Private source, logs and products are deleted at the end of every job.

### Trove Android

| Input | Meaning |
| --- | --- |
| `dry_run` | **Default `true`.** Builds an unsigned bundle and runs the 16 KB check. Sends nothing to Play. |
| `version_code` | `versionCode` for this build. It must be higher than any on Play. |
| `version_name` | `versionName`. Defaults to `1.0.0`. |
| `ref` | trove-app ref. Defaults to `main`. |
| `backend` | `production` or `staging` Supabase. |
| `validate_with_play` | On a dry run, also run the release job: sign, upload into a Play edit, ask Play to validate it, then delete the edit. Needs the `trove-android` approval. |

The `check` job needs no secrets or approval. It builds `:app:bundleRelease` and `:app:assembleRelease` unsigned, then runs two checks: `tools/check_16kb.py` confirms every arm64-v8a and x86_64 `.so` has 16 KB-aligned ELF LOAD segments, and `zipalign -c -P 16` checks the APK's zip offsets. zipalign alone isn't enough, because it doesn't look inside the libraries.

The `release` job runs in `trove-android`. While the upload-key secrets are empty, it skips signing and upload with a notice. Otherwise it signs with the upload key (Play App Signing re-signs for devices) and uses one Play edit, either to validate an internal release or to commit it. It never touches any track except **internal**.

## Running it

### Trove iOS

**Actions → Trove iOS → Run workflow.**

| Input | Meaning |
| --- | --- |
| `dry_run` | **Default `true`.** Builds, signs, and asks Apple to validate. Publishes nothing. |
| `build_number` | `CFBundleVersion` for this build. Must not already exist in App Store Connect. |
| `backend` | `production` or `staging`. |
| `ref` | Which trove-app ref to build. Defaults to `main`. |

A dry run proves the whole chain — the source token, Apple credentials, automatic signing, and Apple's own upload validation — **without anything reaching TestFlight or a tester**.

## How it signs

There is no certificate or provisioning profile stored anywhere. `xcodebuild -allowProvisioningUpdates` with an App Store Connect API key makes Xcode mint them per build.

Android can't work this way: Play has no equivalent, so the upload keystore lives in `trove-android`, and Play App Signing holds the app signing key.

## Credentials

`trove-ios` environment:

- `APP_STORE_CONNECT_API_KEY_ID` — key `3HHV69YS9L`, named `trove-releases` in App Store Connect
- `APP_STORE_CONNECT_ISSUER_ID`
- `APP_STORE_CONNECT_API_KEY_P8`

Optional, and only needed before a real upload — without them the app builds and signs but reaches no backend:

- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `RESOLVER_BASE_URL`

The key is dedicated to this pipeline, so it can be revoked without disturbing the `GitHub TestFlight` key used by the workflow in trove-app.

Repository secrets (every workflow):

- `TROVE_CI_APP_ID`, `TROVE_CI_APP_PRIVATE_KEY` — the `trove-kitchen-ci` app
- `TROVE_PR_COMMENT_TOKEN` — optional. With it, the check workflows post their details on the trove-app pull request; without it, results stay pass/fail in the log.

`trove-device-tests` environment (no reviewer; staging only):

- `TROVE_TEST_EMAIL`, `TROVE_TEST_PASSWORD` — the staging test account
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `RESOLVER_BASE_URL` — **staging** values. `tools/require_staging.sh` refuses to run if they point anywhere else.
- `TROVE_RENDERS_AGE_KEY` — the age private key, used only by the publish jobs to decrypt reports before committing them to trove-renders. Without it, only verdicts are committed.
- `TROVE_RENDERS_APP_ID`, `TROVE_RENDERS_APP_PRIVATE_KEY` — the `trove-kitchen-renders` app (here or as repository secrets)

Repository variable `TROVE_RENDERS_AGE_RECIPIENT` — the age public key. Test jobs encrypt everything except flow names and verdicts to it before it leaves the runner. Recordings are never committed: they're encrypted assets of a trove-renders release (`device-tests-<date>-<sha>`, `web-flows-<date>-<sha>`), and the newest 10 of each are kept.

`trove-android` environment (Samuel's review):

- `TROVE_ANDROID_UPLOAD_KEYSTORE_B64` (the upload keystore, base64), `TROVE_ANDROID_UPLOAD_STORE_PASSWORD`, `TROVE_ANDROID_UPLOAD_KEY_ALIAS`, `TROVE_ANDROID_UPLOAD_KEY_PASSWORD`
- `TROVE_PLAY_SERVICE_ACCOUNT_JSON` — a service account with release access to `com.adestudios.trove`
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `STAGING_SUPABASE_URL`, `STAGING_SUPABASE_ANON_KEY` for `backend: staging`

## Guard rails

- `dry_run` defaults to `true` on both stores; uploading is a deliberate choice.
- Android only ever goes to the Play **internal** track. Promoting a build is done in Play Console.
- Apple credentials are checked with `altool --list-apps` **before** a 30-minute build.
- `altool` can print a failure and still exit `0`, so both the validate and upload steps read the log text rather than trusting the exit code.

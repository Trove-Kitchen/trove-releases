# trove-releases

The release pipeline for **Trove**. This repository holds workflows and credentials. **It holds no app source.**

Source lives in two private repositories and is cloned at build time through **read-only deploy keys**:

| Repository | Secret |
| --- | --- |
| `samuelalake/Munch` | `TROVE_MUNCH_READ_KEY` |
| `samuelalake/munch-commerce` | `TROVE_COMMERCE_READ_KEY` |

## Why this repo is public

GitHub bills macOS runners at **10× the Linux rate** on private repositories — about **$0.062 a minute**, and a Trove build runs 20–40 minutes, so roughly **$1.24–$2.48 per build**. Public repositories get macOS minutes free.

Being public costs nothing here because there is no source and no readable secret: GitHub encrypts secrets, and a pull request from a fork never receives them.

## Running it

**Actions → Trove iOS → Run workflow.**

| Input | Meaning |
| --- | --- |
| `dry_run` | **Default `true`.** Builds, signs, and asks Apple to validate. Publishes nothing. |
| `build_number` | `CFBundleVersion` for this build. Must not already exist in App Store Connect. |
| `ref` | Which `Munch` ref to build. Defaults to `main`. |

A dry run proves the whole chain — deploy keys, Apple credentials, automatic signing, and Apple's own upload validation — **without anything reaching TestFlight or a tester**.

## How it signs

There is no certificate or provisioning profile stored anywhere. `xcodebuild -allowProvisioningUpdates` with an App Store Connect API key makes Xcode mint them per build.

Android will not work this way when it is added: Play has no equivalent, an upload keystore is unavoidable, and it will need the encrypted build→release handoff that this pipeline does not.

## Credentials

`trove-ios` environment:

- `APP_STORE_CONNECT_API_KEY_ID` — key `3HHV69YS9L`, named `trove-releases` in App Store Connect
- `APP_STORE_CONNECT_ISSUER_ID`
- `APP_STORE_CONNECT_API_KEY_P8`

Optional, and only needed before a real upload — without them the app builds and signs but reaches no backend:

- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `RESOLVER_BASE_URL`

The key is dedicated to this pipeline, so it can be revoked without disturbing the `GitHub TestFlight` key used by the existing workflow in `Munch`.

## Guard rails

- `dry_run` defaults to `true`; uploading is a deliberate choice.
- Apple credentials are checked with `altool --list-apps` **before** a 30-minute build.
- `altool` can print a failure and still exit `0`, so both the validate and upload steps read the log text rather than trusting the exit code.

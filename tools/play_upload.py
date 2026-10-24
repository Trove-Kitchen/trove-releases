"""Send a signed Trove bundle to the Google Play internal track, or only validate it.

  play_upload.py <bundle.aab> <validate|commit> <release name>

Uses one Play edit: upload the bundle, put its version code on the internal
track as a completed release, then either
  validate  -> ask Play to validate the edit, then delete it (nothing is published,
               and the version code stays unused), or
  commit    -> commit the edit (internal testers get the build).

The service account JSON comes from TROVE_PLAY_SERVICE_ACCOUNT_JSON and needs
release access to com.adestudios.trove. Prints version codes and Play's status
codes only.
"""

import json
import os
import sys

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

PACKAGE = "com.adestudios.trove"
TRACK = "internal"
API = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}"
UPLOAD = f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{PACKAGE}"


def check(resp, what: str) -> dict:
    if resp.status_code >= 300:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        sys.exit(f"Play refused {what} ({resp.status_code}): {message[:500]}")
    return resp.json() if resp.content else {}


def main(bundle: str, mode: str, name: str) -> None:
    if mode not in ("validate", "commit"):
        sys.exit(__doc__)
    info = json.loads(os.environ["TROVE_PLAY_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/androidpublisher"])
    play = AuthorizedSession(creds)

    edit = check(play.post(f"{API}/edits", json={}, timeout=60), "opening an edit")["id"]
    try:
        with open(bundle, "rb") as f:
            uploaded = check(play.post(
                f"{UPLOAD}/edits/{edit}/bundles?uploadType=media", data=f,
                headers={"Content-Type": "application/octet-stream"}, timeout=900), "the bundle")
        code = str(uploaded["versionCode"])
        print(f"Play accepted the bundle as version code {code}.")
        check(play.put(f"{API}/edits/{edit}/tracks/{TRACK}", json={
            "track": TRACK,
            "releases": [{"name": name, "versionCodes": [code], "status": "completed"}],
        }, timeout=60), f"the {TRACK} track")
        if mode == "validate":
            check(play.post(f"{API}/edits/{edit}:validate", timeout=120), "validation")
            print(f"Play validated an {TRACK} release of version code {code}. Nothing was published.")
        else:
            check(play.post(f"{API}/edits/{edit}:commit", timeout=120), "the commit")
            edit = None
            print(f"Version code {code} is on the {TRACK} track.")
    finally:
        if edit:
            play.delete(f"{API}/edits/{edit}", timeout=60)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])

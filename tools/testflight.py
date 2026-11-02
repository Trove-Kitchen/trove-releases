"""TestFlight details for one uploaded build, through the App Store Connect API.

    testflight.py <bundle id> <build number> <what to test file> <groups, comma separated>

Waits (up to 45 minutes) for Apple to finish processing the build, answers export
compliance when it's still open (Trove uses only Apple's standard encryption: HTTPS),
sets "What to Test" (whatsNew, en-US) and adds the build to each named beta group. With no groups,
it lists the app's groups. Prints group names and verdicts only; the key never leaves
the environment.
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
import jwt  # PyJWT, with cryptography for ES256

API = "https://api.appstoreconnect.apple.com"


def token() -> str:
    now = int(time.time())
    return jwt.encode({"iss": os.environ["ISSUER_ID"], "iat": now, "exp": now + 1100, "aud": "appstoreconnect-v1"},
                      os.environ["KEY_P8"], algorithm="ES256", headers={"kid": os.environ["KEY_ID"], "typ": "JWT"})


def call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("errors", [{}])[0]
        raise SystemExit(f"App Store Connect {method} {path.split('/')[2]}: {e.code} {detail.get('code', '')} {detail.get('title', '')}")


def main() -> None:
    bundle, number, notes_file, groups_arg = sys.argv[1:5]
    apps = call("GET", "/v1/apps", query={"filter[bundleId]": bundle})["data"]
    if not apps:
        raise SystemExit("No app with that bundle id")
    app = apps[0]["id"]

    groups = call("GET", f"/v1/apps/{app}/betaGroups", query={"limit": "50"})["data"]
    for g in groups:
        a = g["attributes"]
        testers = call("GET", f"/v1/betaGroups/{g['id']}/betaTesters", query={"limit": "200", "fields[betaTesters]": "inviteType"})["data"]
        kind = "internal" if a.get("isInternalGroup") else "external"
        auto = ", gets every build" if a.get("hasAccessToAllBuilds") else ""
        link = ", public link on" if a.get("publicLinkEnabled") else ""
        print(f"Beta group: {a['name']} ({kind}, {len(testers)} testers{auto}{link})")
    wanted = [n.strip() for n in groups_arg.split(",") if n.strip()]

    build = None
    for _ in range(90):
        found = call("GET", "/v1/builds", query={"filter[app]": app, "filter[version]": number, "limit": "1"})["data"]
        if found and found[0]["attributes"]["processingState"] in ("VALID", "FAILED", "INVALID"):
            build = found[0]
            break
        print(f"Build {number}: {found[0]['attributes']['processingState'] if found else 'not visible yet'}; waiting")
        time.sleep(30)
    if not build:
        raise SystemExit(f"Build {number} didn't finish processing in 45 minutes")
    state = build["attributes"]["processingState"]
    if state != "VALID":
        raise SystemExit(f"Build {number} is {state}")
    print(f"Build {number}: processed")

    if build["attributes"].get("usesNonExemptEncryption") is None:
        call("PATCH", f"/v1/builds/{build['id']}", {"data": {"type": "builds", "id": build["id"], "attributes": {"usesNonExemptEncryption": False}}})
        print("Export compliance: answered (standard encryption only)")

    notes = open(notes_file, encoding="utf-8").read().strip()
    if notes:
        locs = call("GET", f"/v1/builds/{build['id']}/betaBuildLocalizations")["data"]
        en = next((l for l in locs if l["attributes"]["locale"] == "en-US"), None)
        if en:
            call("PATCH", f"/v1/betaBuildLocalizations/{en['id']}", {"data": {"type": "betaBuildLocalizations", "id": en["id"], "attributes": {"whatsNew": notes}}})
        else:
            call("POST", "/v1/betaBuildLocalizations", {"data": {"type": "betaBuildLocalizations", "attributes": {"locale": "en-US", "whatsNew": notes},
                                                                 "relationships": {"build": {"data": {"type": "builds", "id": build["id"]}}}}})
        print("What to Test: set")

    by_name = {g["attributes"]["name"].lower(): g for g in groups}
    missing = [n for n in wanted if n.lower() not in by_name]
    for name in wanted:
        g = by_name.get(name.lower())
        if not g:
            continue
        call("POST", f"/v1/betaGroups/{g['id']}/relationships/builds", {"data": [{"type": "builds", "id": build["id"]}]})
        print(f"Added to {g['attributes']['name']}")
    if missing:
        raise SystemExit("No beta group named: " + ", ".join(missing))


if __name__ == "__main__":
    main()

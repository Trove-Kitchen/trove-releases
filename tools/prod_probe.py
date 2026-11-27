#!/usr/bin/env python3
"""Probe the PRODUCTION video resolver without an account.

For each link in trove-app's scripts/import-check/links.json, POST /preview with
the server's service token: metadata only (title, caption), no download, no user,
nothing stored. That is the step that breaks when a site walls the server or the
proxy runs out. Then GET /admin/proxy, when the resolver has it.

stdout (this public log) shows platform and pass/fail only. The report with the
links and error codes is written to --report for the trove-app issue.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

BASE = "https://trove-video-resolver-production.up.railway.app"
HEADER = "X-Trove-Service-Token"


def call(method: str, path: str, token: str, body: dict | None = None, timeout: int = 90) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={HEADER: token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except ValueError:
            return err.code, {}
    except Exception as err:  # timeouts, connection errors
        return 0, {"error": type(err).__name__}


def platform(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for name in ("instagram", "tiktok", "youtube", "youtu.be"):
        if name in host:
            return "youtube" if name == "youtu.be" else name
    return host


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("links")
    ap.add_argument("--report", required=True)
    args = ap.parse_args()
    token = os.environ["RESOLVER_SERVICE_TOKEN"]
    links = json.load(open(args.links))["links"]

    rows, passed = [], {}
    for link in links:
        url = link["url"]
        status, body = call("POST", "/preview", token, {"url": url})
        ok = status == 200 and bool(body.get("title") or body.get("caption"))
        p = platform(url)
        passed.setdefault(p, [0, 0])
        passed[p][1] += 1
        passed[p][0] += ok
        rows.append((p, link.get("id", ""), url, status, "ok" if ok else body.get("error", "?"),
                     bool(body.get("proxy_failing"))))
        print(f"{p}: {'pass' if ok else 'FAIL'}")

    pstatus, proxy = call("GET", "/admin/proxy", token, timeout=20)
    proxy_line = (f"configured={proxy.get('configured')} failing={proxy.get('failing')} "
                  f"last_failure_kind={proxy.get('last_failure_kind')}") if pstatus == 200 else \
                 f"not available (HTTP {pstatus}; needs the resolver from trove-app #556)"
    print("proxy: " + ("FAILING" if pstatus == 200 and proxy.get("failing") else "ok or unknown"))

    total_ok = sum(v[0] for v in passed.values())
    total = sum(v[1] for v in passed.values())
    lines = [f"Production resolver, `/preview` only (no account, no download, nothing stored): "
             f"**{total_ok}/{total}** links resolved.", "",
             "By platform: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(passed.items())), "",
             f"Proxy: {proxy_line}", "",
             "| Platform | Link | HTTP | Result | proxy_failing |", "|---|---|---|---|---|"]
    lines += [f"| {p} | [{i}]({u}) | {s} | {r} | {'yes' if f else ''} |" for p, i, u, s, r, f in rows]
    open(args.report, "w").write("\n".join(lines) + "\n")
    proxy_bad = pstatus == 200 and bool(proxy.get("failing"))
    return 0 if total_ok == total and not proxy_bad else 1


if __name__ == "__main__":
    sys.exit(main())

"""Turn a private build log into a count for this public log and a comment for the private Munch PR.

This repository is public and Munch is private, so nothing derived from Munch
source is printed here: the workflow log gets pass/fail and counts, and the
details (path:line and the compiler's or test's message, never source lines)
go only to the Munch pull request.

  report.py xcode  <build.log> <out.md>        compiler errors from xcodebuild/swift
  report.py junit  <results dir> <out.md>      failing tests from JUnit XML
  report.py post   <title> <out.md> <pr> <sha> comment on samuelalake/Munch#<pr>
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCE_ROOT = os.path.join(os.environ.get("RUNNER_TEMP", ""), "trove-source") + "/"
ERROR = re.compile(r"^(?P<path>/[^:\s]+):(?P<line>\d+):(?:(?P<col>\d+):)? error: (?P<msg>.+)$")
MAX_ITEMS = 50


def xcode(log: str, out: str) -> int:
    seen: list[str] = []
    for raw in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
        m = ERROR.match(raw.strip())
        if not m:
            continue
        path = m["path"].removeprefix(SOURCE_ROOT)
        item = f"- `{path}:{m['line']}` {m['msg'][:300]}"
        if item not in seen:
            seen.append(item)
    Path(out).write_text("\n".join(seen[:MAX_ITEMS]) + ("\n" if seen else ""), encoding="utf-8")
    print(f"{len(seen)} compiler error(s)")
    return len(seen)


def junit(results: str, out: str) -> int:
    total = failed = 0
    items: list[str] = []
    for f in sorted(Path(results).rglob("TEST-*.xml")):
        for case in ET.parse(f).getroot().iter("testcase"):
            total += 1
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is None:
                continue
            failed += 1
            first = (problem.get("message") or "").splitlines()[:1]
            items.append(f"- `{case.get('classname')}.{case.get('name')}` {first[0][:300] if first else ''}")
    Path(out).write_text("\n".join(items[:MAX_ITEMS]) + ("\n" if items else ""), encoding="utf-8")
    print(f"{total} test(s), {failed} failed")
    return failed


def post(title: str, out: str, pr: str, sha: str) -> None:
    token = os.environ.get("TROVE_MUNCH_PR_COMMENT_TOKEN", "")
    if not pr or not token:
        print("No pull request or comment token; details stay on the runner.")
        return
    if not pr.isdigit() or not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit("pull request must be a number and sha a 40-character commit")
    details = Path(out).read_text(encoding="utf-8").strip()
    run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    verdict = "failed" if details else "passed"
    body = f"**{title} {verdict}** at `{sha[:12]}` ([run]({run}))\n\n{details}\n\n---\n_Posted by trove-releases_"
    req = urllib.request.Request(
        f"https://api.github.com/repos/samuelalake/Munch/issues/{pr}/comments",
        data=json.dumps({"body": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Posted the result on the Munch pull request ({resp.status}).")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "xcode":
        xcode(*args)
    elif cmd == "junit":
        junit(*args)
    elif cmd == "post":
        post(*args)
    else:
        sys.exit(__doc__)

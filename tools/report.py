"""Turn a private build log into a count for this public log and a comment for the private trove-app PR.

This repository is public and trove-app is private, so nothing derived from trove-app
source is printed here: the workflow log gets pass/fail and counts, and the
details (path:line and the compiler's or test's message, never source lines)
go only to the trove-app pull request.

  report.py xcode  <build.log> <out.md>        compiler errors from xcodebuild/swift
  report.py junit  <results dir> <out.md>      failing tests from JUnit XML
  report.py screens <screens.json> <out.md>    a table of a device run's screenshots (renders.py)
  report.py post   <title> <out.md> <pr> <sha> [verdict]
                                               comment on Trove-Kitchen/trove-app#<pr>; the verdict
                                               is "failed" when out.md has any text, unless given
"""

import json
import os
import re
import sys
import urllib.parse
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


def screens(listing: str, out: str) -> int:
    """One row per screenshot name, a column per platform, images from the private trove-renders.

    Only people who can open trove-renders see the images; the paths name flows and screenshots,
    which the public log already prints.
    """
    data = json.loads(Path(listing).read_text(encoding="utf-8"))
    commit = str(data.get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        Path(out).write_text("", encoding="utf-8")
        print("No trove-renders commit; no screenshots to post.")
        return 0
    platforms = ["android", "ios", "web"]
    by_name: dict[str, dict[str, str]] = {}
    for shot in data.get("shots", []):
        path, platform = str(shot.get("path", "")), str(shot.get("platform", ""))
        if platform not in platforms or not re.fullmatch(r"[A-Za-z0-9 ._/()-]+\.png", path):
            continue
        name = Path(path).stem
        by_name.setdefault(name, {})[platform] = path
    used = [p for p in platforms if any(p in row for row in by_name.values())]
    if not by_name:
        Path(out).write_text("", encoding="utf-8")
        print("No screenshots in this run.")
        return 0
    base = f"https://github.com/Trove-Kitchen/trove-renders/blob/{commit}/"
    labels = {"android": "Android", "ios": "iPhone", "web": "Web"}
    lines = ["| Screenshot | " + " | ".join(labels[p] for p in used) + " |",
             "|---|" + "---|" * len(used)]
    for name in sorted(by_name):
        cells = []
        for p in used:
            path = by_name[name].get(p)
            url = base + urllib.parse.quote(path) + "?raw=true" if path else ""
            cells.append(f'<img src="{url}" width="200">' if path else "–")
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(by_name)} screenshot(s)")
    return len(by_name)


LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish (Latin America)", "es-ES": "Spanish (Spain)",
              "pt-BR": "Portuguese (Brazil)", "pt-PT": "Portuguese (Portugal)"}


def gallery(listing: str, out: str) -> int:
    """Store screenshots by language: the iPhone 6.9-inch frames in a row, the iPad 13-inch frames,
    the header and search images, and a link to the app preview. Images from the private trove-renders."""
    data = json.loads(Path(listing).read_text(encoding="utf-8"))
    commit = str(data.get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        Path(out).write_text("", encoding="utf-8")
        return 0
    base = f"https://github.com/Trove-Kitchen/trove-renders/blob/{commit}/"
    url = lambda path: base + urllib.parse.quote(path) + "?raw=true"
    langs: dict[str, dict[str, list[str]]] = {}
    for shot in data.get("shots", []):
        path = str(shot.get("path", ""))
        if not re.fullmatch(r"[A-Za-z0-9 ._/()-]+\.(png|mp4)", path):
            continue
        m = re.search(r"/ios/(store|preview)/(?:(ios-69|ipad|covers)/)?(?:([a-z]{2}(?:-[A-Z]{2})?)/)?[^/]+$", path)
        if not m:
            continue
        kind, target, lang = m.group(1), m.group(2) or ("preview" if m.group(1) == "preview" else "ios"), m.group(3) or "en"
        if kind == "preview":
            lang = m.group(3) or Path(path).parent.name
        langs.setdefault(lang, {}).setdefault(target, []).append(path)
    if not langs:
        Path(out).write_text("", encoding="utf-8")
        return 0
    order = [l for l in LANG_NAMES if l in langs] + sorted(l for l in langs if l not in LANG_NAMES)
    lines = []
    for lang in order:
        t = langs[lang]
        lines.append(f"### {LANG_NAMES.get(lang, lang)} (`{lang}`)")
        for target, label, width in (("ios-69", "iPhone 6.9″ (1320×2868)", 96), ("ipad", "iPad 13″ (2064×2752)", 120), ("covers", "Header and search", 220)):
            paths = sorted(t.get(target, []), key=lambda p: Path(p).name)
            paths = [p for p in paths if "sheet" not in Path(p).name]
            if paths:
                lines.append(f"**{label}**\n\n" + " ".join(f'<a href="{url(p)}"><img src="{url(p)}" width="{width}"></a>' for p in paths))
        for p in t.get("preview", []):
            lines.append(f"**App preview:** [{Path(p).name}]({url(p)})")
        lines.append("")
    Path(out).write_text("\n\n".join(lines).strip() + "\n", encoding="utf-8")
    print(f"{len(order)} language(s)")
    return len(order)


def post(title: str, out: str, pr: str, sha: str, verdict: str = "") -> None:
    token = os.environ.get("TROVE_PR_COMMENT_TOKEN", "")
    if not pr or not token:
        print("No pull request or comment token; details stay on the runner.")
        return
    if not pr.isdigit() or not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit("pull request must be a number and sha a 40-character commit")
    details = Path(out).read_text(encoding="utf-8").strip()
    run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    # A report that always has content (the import check's table) passes its own verdict.
    verdict = verdict or ("failed" if details else "passed")
    body = f"**{title} {verdict}** at `{sha[:12]}` ([run]({run}))\n\n{details}\n\n---\n_Posted by trove-releases_"
    req = urllib.request.Request(
        f"https://api.github.com/repos/Trove-Kitchen/trove-app/issues/{pr}/comments",
        data=json.dumps({"body": body}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Posted the result on the trove-app pull request ({resp.status}).")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "xcode":
        xcode(*args)
    elif cmd == "junit":
        junit(*args)
    elif cmd == "screens":
        screens(*args)
    elif cmd == "gallery":
        gallery(*args)
    elif cmd == "post":
        post(*args)
    else:
        sys.exit(__doc__)

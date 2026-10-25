"""The Trove flow board: one self-contained folder, board/ in trove-renders (index.html plus media/, every path
relative and below it), so a session can publish it as-is to its stable claude.ai artifact.

Follows the review-pages pattern (samuelalake/agent-skills review-pages, the version pinned in trove-app
docs/engineering/artifacts.md), with platforms as a list: each flow names its own.

Inputs, all data, never code:
- trove-app's docs/evidence/flows.json: the flows (id, title, goal) and, per platform, the recorded test covering
  each one. Every field is checked against a fixed shape here, and the page escapes everything it shows.
- trove-renders' device-tests/ and web-flows/: runs.json (newest first) and each run folder's results, written by
  renders.py. Phones: <platform>.json verdicts and the Maestro screenshots. Web: the Playwright artifact folders,
  <test>-phone/ and <test>-desktop/, whose screenshot is test-finished-1.png or test-failed-1.png.

A run folder is named after the trove-app commit, so a later run of the same commit replaces its platform's files:
only the newest run per (folder, platform) is read. End screens become small JPEGs under board/media/<folder>/, and a
phone flow's clear preview (renders.py: <platform>/previews/NN-*.mp4, matched by its screenshots' NN- number) is
copied beside them.
Run: python3 -I tools/board.py <trove-renders> <flows.json>
"""
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "board_template.html"
HISTORY = 15
WIDTH = 360
PLATFORMS = ("ios", "android", "web", "desktop")
SHORT = {"ios": "iOS", "android": "Android", "web": "Web", "desktop": "Desktop"}
# Cards sit in labelled groups; inside a group a card is titled by what differs within it.
GROUPS = [{"id": "mobile", "title": "Mobile"}, {"id": "web", "title": "Web"}]
GROUP = {"ios": "mobile", "android": "mobile", "web": "web", "desktop": "web"}
CARD = {"web": "Phone width", "desktop": "Desktop"}
ASPECT = {"ios": "393 / 852", "android": "412 / 915", "web": "390 / 844", "desktop": "1280 / 900"}
KINDS = {"device-tests": ("ios", "android"), "web-flows": ("web",)}
FOLDER = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{12}")
RUN_URL = re.compile(r"https://github\.com/Trove-Kitchen/trove-releases/actions/runs/([1-9][0-9]{0,19})")
FLOW_ID = re.compile(r"[1-9][0-9]{0,2}")
TEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,.'-]{0,119}")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
MAX_INPUT, MAX_LINE, MAX_TEXT = 256 * 1024, 160, 600


class BoardError(Exception):
    pass


def text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


def read_json(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_INPUT:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None


def checked_flows(data):
    """trove-app's flows.json as (platforms, flows), each a fixed shape, or BoardError."""
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list) or not data["flows"]:
        raise BoardError("flows.json has no flows")
    if not isinstance(data.get("platforms", []), list):
        raise BoardError("flows.json has malformed platforms")
    platforms = []
    for p in data.get("platforms", []):
        if not (isinstance(p, dict) and p.get("id") in PLATFORMS and text(p.get("title"), 40)):
            raise BoardError("flows.json has a malformed platform")
        platforms.append({"id": p["id"], "title": p["title"], "short": SHORT[p["id"]], "aspect": ASPECT[p["id"]],
                          "wide": p["id"] == "desktop", "group": GROUP[p["id"]], "card": CARD.get(p["id"], p["title"])})
    known = {p["id"] for p in platforms}
    flows, ids = [], set()
    for f in data["flows"]:
        if not isinstance(f, dict):
            raise BoardError("flows.json has a malformed flow")
        tests = f.get("tests")
        if not (isinstance(f.get("id"), str) and FLOW_ID.fullmatch(f["id"]) and f["id"] not in ids and
                text(f.get("title"), MAX_LINE) and text(f.get("goal"), MAX_TEXT) and isinstance(tests, dict) and tests and
                all(p in known and isinstance(t, str) and TEST.fullmatch(t) for p, t in tests.items())):
            raise BoardError("flows.json has a malformed flow")
        ids.add(f["id"])
        flows.append({"id": f["id"], "title": f["title"], "goal": f["goal"],
                      "tests": {p: tests[p] for p in PLATFORMS if p in tests}})
    return platforms, flows


def runs(renders):
    """Every run as {kind, folder, run, platforms}, oldest first, newest run per (folder, platform) only."""
    seen, out = set(), []
    entries = []
    for kind in KINDS:
        data = read_json(renders / kind / "runs.json")
        for e in data if isinstance(data, list) else []:
            if not (isinstance(e, dict) and isinstance(e.get("folder"), str) and FOLDER.fullmatch(e["folder"]) and
                    isinstance(e.get("verdicts"), dict)):
                continue
            # renders.py records each platform's own run in "runs" when a folder holds several runs' results.
            per = e.get("runs") if isinstance(e.get("runs"), dict) else {}
            for p in KINDS[kind]:
                url = per.get(p, e.get("run"))
                m = RUN_URL.fullmatch(url) if p in e["verdicts"] and isinstance(url, str) else None
                if m:
                    entries.append((int(m.group(1)), kind, e["folder"], [p]))
    grouped = {}
    for run, kind, folder, plats in entries:
        grouped.setdefault((run, kind, folder), []).extend(plats)
    entries = [(run, kind, folder, plats) for (run, kind, folder), plats in grouped.items()]
    for run, kind, folder, plats in sorted(entries, reverse=True):  # newest first, to keep only the newest per pair
        mine = [p for p in plats if (folder, p) not in seen]
        seen.update((folder, p) for p in mine)
        if mine:
            out.append({"kind": kind, "folder": folder, "run": str(run), "platforms": mine})
    return list(reversed(out[:HISTORY]))


def script_json(data):
    """JSON that can sit inside <script> whatever the text holds (no </script>, <!--, or line separators)."""
    return (json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def date_of(folder):
    y, m, d = folder[:10].split("-")
    return f"{int(d)} {MONTHS[int(m) - 1]} {y}"


def newest_png(paths):
    files = sorted(p for p in paths if p.is_file() and not p.is_symlink() and p.suffix == ".png")
    return files[-1] if files else None


def preview_for(folder_dir, platform, flow_dir):
    """The flow's clear preview (renders.py), found by the number its screenshots start with ("01-..."), or None."""
    shots = sorted(p.name for p in (flow_dir / "takeScreenshot").glob("*.png")) if flow_dir else []
    m = re.match(r"([0-9]{2})-", shots[0]) if shots else None
    found = sorted((folder_dir / platform / "previews").glob(f"{m.group(1)}-*.mp4")) if m else []
    return found[0] if found and found[0].is_file() and not found[0].is_symlink() else None


def phone_results(folder_dir, platform):
    """{maestro flow name: (verdict, end screen, preview)} for one phone platform of one run."""
    data = read_json(folder_dir / f"{platform}.json")
    if not isinstance(data, dict) or not isinstance(data.get("flows"), list):
        return {}
    out = {}
    for row in data["flows"]:
        if not (isinstance(row, dict) and isinstance(row.get("flow"), str) and TEST.fullmatch(row["flow"])):
            continue
        verdict = "pass" if row.get("result") == "passed" else "fail"
        dirs = sorted((folder_dir / platform / "maestro").glob(f"*/{row['flow']}"))
        shot = None
        if dirs:
            sub = "takeScreenshot" if verdict == "pass" else "screenshots"
            shot = newest_png((dirs[-1] / sub).glob("*.png")) or newest_png(dirs[-1].glob("*/*.png"))
        out[row["flow"]] = (verdict, shot, preview_for(folder_dir, platform, dirs[-1] if dirs else None))
    return out


def web_result(folder_dir, test, project):
    d = folder_dir / "web" / "artifacts" / f"{test}-{project}"
    if not d.is_dir():
        return None
    failed = newest_png(d.glob("test-failed-*.png"))
    clip = folder_dir / "web" / "previews" / f"{test}-{project}.mp4"
    clip = clip if clip.is_file() and not clip.is_symlink() else None
    return ("fail", failed, clip) if failed else ("pass", newest_png(d.glob("test-finished-*.png")), clip)


def thumbnail(src, dest):
    """A small JPEG of an end screen, or None if Pillow is missing (the card then says no screen was kept)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    Image.MAX_IMAGE_PIXELS = 40_000_000  # a phone screenshot is ~3 million; anything far larger is refused
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((WIDTH * 2, WIDTH * 4))
            im.save(dest, "JPEG", quality=72, optimize=True)
    except Exception:  # a truncated or oversized screenshot: the card shows no end screen, the board still builds
        dest.unlink(missing_ok=True)
        return None
    return dest


def build(renders, flows_json, now=None):
    platforms, flows = checked_flows(read_json(flows_json))
    out = renders / "board"
    if out.exists():
        shutil.rmtree(out)
    (out / "media").mkdir(parents=True)
    history = runs(renders)
    results, cache = {}, {}
    shared = {}
    for f in flows:
        for p, test in f["tests"].items():
            shared[(p, test)] = shared.get((p, test), 0) + 1
    for r in history:
        folder_dir = renders / r["kind"] / r["folder"]
        per = results.setdefault(r["folder"] + "#" + r["run"], {})
        for f in flows:
            for p, test in f["tests"].items():
                if r["kind"] == "web-flows" and p in ("web", "desktop") and "web" in r["platforms"]:
                    got = web_result(folder_dir, test, "phone" if p == "web" else "desktop")
                elif r["kind"] == "device-tests" and p in r["platforms"]:
                    key = (r["folder"], p)
                    if key not in cache:
                        cache[key] = phone_results(folder_dir, p)
                    got = cache[key].get(test)
                else:
                    continue
                if not got:
                    continue
                verdict, shot, clip = got
                thumb = preview = None
                stem = Path("media") / r["folder"] / f"{p}-{re.sub(r'[^a-z0-9]+', '-', test.lower()).strip('-')}"
                if shot:
                    rel = stem.with_suffix(".jpg")
                    if (out / rel).exists() or thumbnail(shot, out / rel):
                        thumb = rel.as_posix()
                if clip:
                    rel = stem.with_suffix(".mp4")
                    if not (out / rel).exists():
                        (out / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(clip, out / rel)
                    preview = rel.as_posix()
                row = {"verdict": verdict, "thumb": thumb, "run": r["run"], "run_date": date_of(r["folder"])}
                if preview:
                    row["preview"] = preview
                if shared[(p, test)] > 1:
                    row["note"] = "One test covers this flow and others here; the screen is where that test ends."
                per[f"{p}/{f['id']}"] = row
    order = [r["folder"] + "#" + r["run"] for r in history]
    latest = {}
    for key in order:  # oldest first, so newer runs overwrite
        latest.update(results.get(key, {}))
    board_flows = []
    for f in flows:
        plats = list(f["tests"])
        vs = [latest.get(f"{p}/{f['id']}", {}).get("verdict", "none") for p in plats]
        state = ("none" if all(v == "none" for v in vs) else "pass" if all(v == "pass" for v in vs)
                 else "partial" if "pass" in vs else "fail")
        last = None
        for key, r in zip(reversed(order), reversed(history)):
            passed = [p for p in plats if results.get(key, {}).get(f"{p}/{f['id']}", {}).get("verdict") == "pass"]
            if passed:
                names = ", ".join(next(x["title"] for x in platforms if x["id"] == p) for p in passed)
                last = f"{date_of(r['folder'])} · Run {r['run']} ({names})"
                break
        board_flows.append({"id": f["id"], "title": f["title"], "parent": None, "scenarios": [f["id"]],
                            "platforms": plats, "state": state, "means": f["goal"], "last_pass": last,
                            "components": [], "next": [], "changed": [], "issue": None})
    data = {"platforms": platforms, "groups": GROUPS, "flows": board_flows,
            "results": dict({"latest": latest}, **{k: v for k, v in results.items()}),
            "runs": [{"folder": k, "date": date_of(r["folder"]), "run": r["run"], "archived": False}
                     for k, r in zip(order, history)]}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    when = f"{now.day} {MONTHS[now.month - 1]} {now.year}, {now:%H:%M} UTC"
    values = {"DATA": script_json(data), "BANNER": "null", "RUN_WHEN": when}
    # One pass, so text inside the data that looks like a placeholder is left alone.
    page = re.sub(r"\{\{([A-Z_]+)\}\}", lambda m: values.get(m.group(1), m.group(0)), TEMPLATE.read_text())
    (out / "index.html").write_text(page)
    passing = sum(f["state"] == "pass" for f in board_flows)
    return f"board: {len(board_flows)} flows, {passing} passing on every platform, {len(history)} runs"


if __name__ == "__main__":
    try:
        print(build(Path(sys.argv[1]), Path(sys.argv[2])))
    except BoardError as error:
        print(f"board: {error}")
        sys.exit(1)

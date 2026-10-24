"""Seal flow-test output on a test runner, and publish it to Trove-Kitchen/trove-renders.

This repository is public and trove-app is private. Test runners hold app
code, so everything they hand on is either a verdict (flow name, passed or
failed) or encrypted with age to the renders public key. The publish job runs
no trove-app code: it decrypts with the renders private key, commits reports
and screenshots to the private trove-renders, and attaches the still-encrypted
archives (which hold the recordings) to a trove-renders release. Recordings
never enter trove-renders' git history.

  renders.py seal <platform> <output dir>       -> $RUNNER_TEMP/sealed/<platform>.{age,json}
  renders.py verdict <platform>                 exits 1 if a flow failed or none ran
  renders.py publish <kind> <sha> <sealed dir>  kind: device-tests | web-flows

Environment:
  TROVE_RENDERS_AGE_RECIPIENT  age public key (seal, publish)
  TROVE_RENDERS_AGE_KEY        age private key (publish; without it only verdicts are committed)
  TROVE_RENDERS_TOKEN          trove-kitchen-renders installation token (publish)
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = "Trove-Kitchen/trove-renders"
KEEP = 10
RECIPIENT = re.compile(r"age1[0-9a-z]{58}")
PLATFORM = re.compile(r"(ios|android|web)")
SHA = re.compile(r"[0-9a-f]{40}")
# Committed in the clear: screenshots and junit only. Maestro's commands-*.json
# record typed text (the test password), so they and every other file stay
# inside the encrypted archive.
COMMITTED = (".png", ".jpg", ".xml")
RECORDING = (".mp4", ".webm", ".mov")


def temp() -> Path:
    return Path(os.environ.get("RUNNER_TEMP", "/tmp"))


def recipient() -> str:
    value = os.environ.get("TROVE_RENDERS_AGE_RECIPIENT", "").strip()
    if not RECIPIENT.fullmatch(value):
        sys.exit("Set the repository variable TROVE_RENDERS_AGE_RECIPIENT to an age public key.")
    return value


def flows_from_junit(out: Path) -> list[dict]:
    """Every <testcase> under out: its name and whether it passed. Names come from our own flow files."""
    flows = []
    for report in sorted(out.rglob("*.xml")):
        try:
            root = ET.parse(report).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            failed = case.find("failure") is not None or case.find("error") is not None
            skipped = case.find("skipped") is not None
            name = re.sub(r"[^A-Za-z0-9 ._:/()-]", "", case.get("name") or "?")[:120]
            flows.append({"flow": name, "result": "skipped" if skipped else "failed" if failed else "passed"})
    return flows


def seal(platform: str, out_dir: str) -> None:
    if not PLATFORM.fullmatch(platform):
        sys.exit("platform must be ios, android or web")
    out, sealed = Path(out_dir), temp() / "sealed"
    sealed.mkdir(parents=True, exist_ok=True)
    flows = flows_from_junit(out) if out.is_dir() else []
    (sealed / f"{platform}.json").write_text(json.dumps({"platform": platform, "flows": flows}, indent=2) + "\n")
    for f in flows:
        print(f"{platform}: {f['result']:<8} {f['flow']}")
    if not out.is_dir():
        print(f"{platform}: no output to seal")
        return
    tar = subprocess.Popen(["tar", "-C", str(out.parent), "-cf", "-", out.name], stdout=subprocess.PIPE)
    subprocess.run(["age", "-r", recipient(), "-o", str(sealed / f"{platform}.age")], stdin=tar.stdout, check=True)
    if tar.wait():
        sys.exit("tar failed")
    print(f"{platform}: sealed {(sealed / f'{platform}.age').stat().st_size} bytes")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("sealed=true\n")


def verdict(platform: str) -> None:
    summary = temp() / "sealed" / f"{platform}.json"
    flows = json.loads(summary.read_text())["flows"] if summary.is_file() else []
    failed = [f["flow"] for f in flows if f["result"] != "passed"]
    if not flows:
        sys.exit(f"{platform}: no flow ran")
    if failed:
        sys.exit(f"{platform}: {len(failed)} of {len(flows)} flows failed or were skipped")
    print(f"{platform}: all {len(flows)} flows passed")


def api(method: str, url: str, token: str, payload=None, data: bytes | None = None, content_type="application/json"):
    body = json.dumps(payload).encode() if payload is not None else data
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": content_type, "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, None


def safe_extract(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                continue
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep):
                continue
            if member.isfile() and member.size > 200 * 1024 * 1024:
                continue
            tar.extract(member, dest, filter="data")


def decrypt(sealed: Path, plain_dir: Path) -> bool:
    key = os.environ.pop("TROVE_RENDERS_AGE_KEY", "").strip()
    if not key:
        return False
    private = temp() / "renders-private"
    private.mkdir(mode=0o700, exist_ok=True)
    key_file = private / "key.txt"
    key_file.write_text(key + "\n")
    key_file.chmod(0o600)
    try:
        derived = subprocess.run(["age-keygen", "-y", str(key_file)], capture_output=True, text=True).stdout.strip()
        if derived != recipient():
            sys.exit("TROVE_RENDERS_AGE_KEY does not match TROVE_RENDERS_AGE_RECIPIENT")
        for archive in sorted(sealed.glob("*.age")):
            tar_path = private / (archive.stem + ".tar")
            subprocess.run(["age", "-d", "-i", str(key_file), "-o", str(tar_path), str(archive)], check=True)
            plain_dir.mkdir(parents=True, exist_ok=True)
            safe_extract(tar_path, plain_dir)  # the archive's top directory is <platform>/
            tar_path.unlink()
    finally:
        key_file.unlink(missing_ok=True)
    return True


def readme(kind: str, sha: str, run_url: str, summaries: list[dict], files: dict[str, list[str]], tag: str) -> str:
    lines = [f"# {kind} of trove-app `{sha[:12]}`", "",
             f"Run: {run_url} · Backend: trove-staging", ""]
    for s in summaries:
        p = s["platform"]
        passed = sum(f["result"] == "passed" for f in s["flows"])
        lines += [f"## {p}: {passed} of {len(s['flows'])} passed", "", "| Flow | Result |", "| --- | --- |"]
        lines += [f"| {f['flow']} | {f['result']} |" for f in s["flows"]]
        shots = [f for f in files.get(p, []) if f.endswith(".png")]
        if shots:
            lines += [""] + [f"![{Path(f).stem}]({f})" for f in shots[:40]]
        lines.append("")
    lines += ["## Recordings", "",
              f"Encrypted assets of the release `{tag}` (one `.age` per platform). Decrypt with the renders key:",
              "`age -d -i key.txt android.age | tar -x`", ""]
    return "\n".join(lines)


def carry_over(old_dir: Path, tree: Path, entry: dict, runs: list, summaries: list[dict], files: dict[str, list[str]],
               kind: str, sha: str, run_url: str, tag: str) -> dict:
    """This run's runs.json entry. A run on one platform keeps the other platforms' results for the same commit
    (same folder): their files are copied into tree, and their verdicts and run links carry over."""
    folder = old_dir.name
    mine = {s["platform"] for s in summaries}
    previous = next((r for r in runs if isinstance(r, dict) and r.get("folder") == folder), {})
    merged = dict(entry, verdicts=dict(entry["verdicts"]), runs={p: run_url for p in mine})
    for p, v in (previous.get("verdicts") or {}).items():
        if p in mine or not PLATFORM.fullmatch(str(p)) or not (old_dir / f"{p}.json").is_file():
            continue
        merged["verdicts"][p] = v
        merged["runs"][p] = (previous.get("runs") or {}).get(p, previous.get("run", ""))
        if (old_dir / p).is_dir():
            shutil.copytree(old_dir / p, tree / p, dirs_exist_ok=True)
        shutil.copyfile(old_dir / f"{p}.json", tree / f"{p}.json")
    kept = [json.loads((tree / f"{p}.json").read_text()) for p in sorted(set(merged["verdicts"]) - mine)]
    if kept:
        all_files = dict(files, **{k["platform"]: sorted(f.relative_to(tree).as_posix()
                                                        for f in (tree / k["platform"]).rglob("*") if f.is_file())
                                   for k in kept if (tree / k["platform"]).is_dir()})
        (tree / "README.md").write_text(readme(kind, sha, run_url, summaries + kept, all_files, tag))
    return merged


def git(args: list[str], cwd: Path, auth: str, check: bool = True) -> int:
    """One git command. Its output and the auth header never reach the log; failures name the subcommand only."""
    result = subprocess.run(["git", "-c", f"http.extraheader={auth}", "-c", "core.hooksPath=/dev/null", *args],
                            cwd=cwd, capture_output=True, text=True)
    if check and result.returncode:
        sys.exit(f"git {args[0]} failed in trove-renders ({result.returncode}).")
    return result.returncode


def publish(kind: str, sha: str, sealed_dir: str) -> None:
    if kind not in ("device-tests", "web-flows") or not SHA.fullmatch(sha):
        sys.exit(__doc__)
    token = os.environ.pop("TROVE_RENDERS_TOKEN", "")
    if not token:
        sys.exit("No trove-kitchen-renders token; nothing was published.")
    sealed = Path(sealed_dir)
    summaries = [json.loads(p.read_text()) for p in sorted(sealed.glob("*.json"))]
    summaries = [s for s in summaries if PLATFORM.fullmatch(str(s.get("platform")))]
    if not summaries:
        sys.exit("Nothing was sealed; nothing to publish.")
    folder = f"{datetime.datetime.now(datetime.UTC):%Y-%m-%d}-{sha[:12]}"
    tag = f"{kind}-{folder}"
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"

    tree = temp() / "renders-tree" / folder
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)
    plain = temp() / "renders-private" / "plain"
    files: dict[str, list[str]] = {}
    if decrypt(sealed, plain):
        for s in summaries:
            p = s["platform"]
            src = plain / p
            for f in sorted(src.rglob("*")) if src.is_dir() else []:
                if (f.is_file() and f.suffix.lower() in COMMITTED and not f.name.startswith("commands-")
                        and f.stat().st_size < 20 * 1024 * 1024):
                    rel = Path(p) / f.relative_to(src)
                    (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, tree / rel)
                    files.setdefault(p, []).append(rel.as_posix())
        shutil.rmtree(plain, ignore_errors=True)
    else:
        print("::notice::TROVE_RENDERS_AGE_KEY is not set: committing verdicts only; the encrypted archives go to the release.")
    for s in summaries:
        (tree / f"{s['platform']}.json").write_text(json.dumps(s, indent=2) + "\n")
    (tree / "README.md").write_text(readme(kind, sha, run_url, summaries, files, tag))

    auth = "AUTHORIZATION: basic " + __import__("base64").b64encode(f"x-access-token:{token}".encode()).decode()
    print(f"::add-mask::{auth}")
    entry = {"folder": folder, "source_sha": sha, "run": run_url,
             "verdicts": {s["platform"]: f"{sum(f['result'] == 'passed' for f in s['flows'])}/{len(s['flows'])}"
                          for s in summaries}}
    work = temp() / "renders-git"
    for attempt in range(4):
        # Another publish may push first (device-tests and web-flows share main); start again from its commit.
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        git(["remote", "add", "origin", f"https://github.com/{REPO}.git"], work, auth)
        git(["fetch", "-q", "--depth", "1", "origin", "main"], work, auth)
        git(["checkout", "-q", "-B", "main", "FETCH_HEAD"], work, auth)
        base = work / kind
        base.mkdir(exist_ok=True)
        index = base / "runs.json"
        runs = json.loads(index.read_text()) if index.is_file() else []
        merged = carry_over(base / folder, tree, entry, runs, summaries, files, kind, sha, run_url, tag)
        runs = [r for r in runs if isinstance(r, dict) and r.get("folder") != folder]
        runs.insert(0, merged)
        dropped = [r["folder"] for r in runs[KEEP:] if isinstance(r.get("folder"), str)]
        for old in dropped:
            shutil.rmtree(base / old, ignore_errors=True)
        runs = runs[:KEEP]
        shutil.rmtree(base / folder, ignore_errors=True)
        shutil.copytree(tree, base / folder)
        index.write_text(json.dumps(runs, indent=2) + "\n")
        git(["add", "-A"], work, auth)
        git(["-c", "user.name=trove-kitchen-renders", "-c", "user.email=renders@trove-releases.invalid",
             "commit", "-q", "-m", f"{kind} of trove-app {sha[:12]}"], work, auth)
        if git(["push", "-q", "origin", "HEAD:refs/heads/main"], work, auth, check=False) == 0:
            break
        print(f"trove-renders main moved; retrying ({attempt + 1}).")
    else:
        sys.exit("Could not push to trove-renders after 4 tries.")
    print(f"Committed {kind}/{folder} ({sum(len(v) for v in files.values())} files).")

    # Recordings: the sealed archives, still encrypted, as release assets.
    gh = "https://api.github.com"
    status, release = api("POST", f"{gh}/repos/{REPO}/releases", token, {
        "tag_name": tag, "name": tag, "target_commitish": "main",
        "body": f"Encrypted recordings of {kind}/{folder}/. Written by trove-releases, never by hand."})
    if status == 422:
        status, release = api("GET", f"{gh}/repos/{REPO}/releases/tags/{tag}", token)
    if status not in (200, 201) or not release:
        sys.exit(f"The trove-renders release could not be created ({status}).")
    status, assets = api("GET", f"{gh}/repos/{REPO}/releases/{release['id']}/assets", token)
    existing = {a["name"]: a["id"] for a in (assets or [])}
    failed = 0
    for archive in sorted(sealed.glob("*.age")):
        if archive.name in existing:
            status, _ = api("DELETE", f"{gh}/repos/{REPO}/releases/assets/{existing[archive.name]}", token)
            if status != 204:
                print(f"Could not replace {archive.name} ({status})."); failed += 1; continue
        status, _ = api("POST", f"https://uploads.github.com/repos/{REPO}/releases/{release['id']}/assets?name={archive.name}",
                        token, data=archive.read_bytes(), content_type="application/octet-stream")
        print(f"Uploaded {archive.name} ({status}).")
        failed += status != 201
    # Releases follow runs.json: a release whose folder was dropped above goes too.
    for old in dropped:
        status, rel = api("GET", f"{gh}/repos/{REPO}/releases/tags/{kind}-{old}", token)
        if status == 200 and rel:
            api("DELETE", f"{gh}/repos/{REPO}/releases/{rel['id']}", token)
            api("DELETE", f"{gh}/repos/{REPO}/git/refs/tags/{kind}-{old}", token)
    if failed:
        sys.exit(f"{failed} recording archive(s) did not upload.")
    print(f"https://github.com/{REPO}/tree/main/{kind}/{folder}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or [""]
    {"seal": seal, "verdict": verdict, "publish": publish}.get(cmd, lambda *a: sys.exit(__doc__))(*args)

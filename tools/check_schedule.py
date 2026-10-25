"""Trove checks on a schedule (check-schedule.yml): every 15 minutes, dispatch web-tests.yml and
resolver-container.yml for trove-app heads that changed their paths and have not been checked yet.

This adds no new way to build or test anything. It picks commits and dispatches the existing check
workflows, which clone and test them exactly as a manual dispatch would.

trove-kitchen-ci can read trove-app's contents and metadata, not its pull requests, so work is found
from branch heads:
- a branch other than main whose head commit is under STALE old, compared with main
  (main...head); it counts when a changed file is under the workflow's paths;
- main itself: the commits of the last STALE back to the newest one already checked by that
  workflow, compared as one range from the parent of the oldest; main's head runs when the range
  touches the paths.
A head (a commit) runs once per workflow: a run of that workflow whose name carries its SHA, created
in the last WINDOW, means it is skipped. At most MAX_DISPATCHES are sent per run and at most
MAX_COMPARED comparisons are read.

The job is split so that no step holds two credentials:
- runs (GITHUB_TOKEN alone): this repository's runs of the two workflows, for the SHAs in their names.
- pick (the trove-kitchen-ci token alone): trove-app's branches and comparisons.
- prs (a trove-kitchen-ci token with pull requests: read alone): the open pull request of each
  picked branch, so the check can post its details there. When it can't be read, there's none.
- dispatch (GITHUB_TOKEN alone, actions: write): one POST per pick, to one of the two workflows'
  dispatches endpoint, ref main, with exactly a SHA and maybe a pull request number.

This repository is public and trove-app private, so no branch name, path or message read from
trove-app is printed: every line is fixed apart from counts, workflow names, short SHAs and pull
request numbers. Between steps, picks are carried in files under $RUNNER_TEMP/trove-check-schedule,
which the workflow deletes.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

CONTROL_REPO = "Trove-Kitchen/trove-releases"
APP_OWNER, APP_NAME = "Trove-Kitchen", "trove-app"
APP_REPO = f"{APP_OWNER}/{APP_NAME}"
API = "https://api.github.com"

# Each check: its run name's lead (the workflow's run-name, exactly, up to the SHA) and the trove-app
# paths whose change makes a head worth it (the old trove-app workflows' path filters).
CHECKS = {
    "web-tests.yml": {
        "title": "Trove web tests at ",
        "prefixes": ("web/", "scripts/design-tokens/"),
        "files": ("docs/design/tokens.json",),
    },
    "resolver-container.yml": {
        "title": "Trove video resolver container at ",
        "prefixes": ("services/video-resolver/",),
        "files": (),
    },
}
STALE = datetime.timedelta(days=3)
# Longer than STALE, so a head that was checked is never checked again while it's fresh.
WINDOW = datetime.timedelta(days=5)
MAX_DISPATCHES = 3
MAX_COMPARED = 20
# compare returns at most 300 files; a range that reaches it counts as touching everything.
COMPARE_FILE_LIMIT = 300
PAGES = 30
SHA = re.compile(r"[0-9a-f]{40}")
NUMBER = re.compile(r"[1-9][0-9]{0,6}")
STATE = "trove-check-schedule"
RUNS_FILE, PICKS_FILE = "runs.json", "picks.json"
DRY_RUN_EVENTS = ("pull_request",)


class ScheduleError(ValueError):
    pass


def check_run():
    """The schedule or a manual dispatch from main; a pull request runs as a dry run. Returns dry."""
    repo, ref, event = (os.environ.get(n) for n in ("GITHUB_REPOSITORY", "GITHUB_REF", "GITHUB_EVENT_NAME"))
    if repo != CONTROL_REPO:
        raise ScheduleError("The schedule runs only in Trove-Kitchen/trove-releases")
    if event in DRY_RUN_EVENTS:
        return True
    if ref != "refs/heads/main" or event not in ("schedule", "workflow_dispatch"):
        raise ScheduleError("The schedule runs only by its schedule or a manual dispatch, from main")
    return False


def state(fresh=False):
    temp = os.environ.get("RUNNER_TEMP")
    if not temp or not Path(temp).is_dir():
        raise ScheduleError("Runner temporary directory is unavailable")
    path = Path(temp) / STATE
    if fresh:
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(mode=0o700)
    return path


def read_state(name):
    try:
        return json.loads((state() / name).read_text())
    except (OSError, ValueError):
        return None


def write_state(name, value):
    (state() / name).write_text(json.dumps(value))


def request(url, token, data=None, method="GET", opener=None):
    """One GitHub API call: (status, body). Bodies are never printed."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as response:
            return response.status, response.read(16 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError):
        return 0, b""


def control_get(path, token):
    """A read of this repository's runs of the two checks, nothing else."""
    if not any(path.startswith(f"actions/workflows/{w}/runs?") for w in CHECKS):
        raise ScheduleError("Only the checks' runs are read from this repository")
    return request(f"{API}/repos/{CONTROL_REPO}/{path}", token)


def app_get(path, token):
    """A read of trove-app's commits and comparisons, nothing else."""
    if not re.fullmatch(r"(commits\?sha=[0-9a-f]{40}&.*|compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}(\?.*)?)", path):
        raise ScheduleError("Only trove-app's commits and comparisons are read")
    return request(f"{API}/repos/{APP_REPO}/{path}", token)


BRANCHES_QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    refs(refPrefix: "refs/heads/", first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name target { ... on Commit { oid committedDate } } }
    }
  }
}"""


def app_graphql(variables, token):
    """The one GraphQL query this script makes: trove-app's branch heads and their commit dates."""
    return request(f"{API}/graphql", token, {"query": BRANCHES_QUERY, "variables": variables}, "POST")


def pr_get(branch, token):
    """The open pull requests whose head is this trove-app branch, with the pull-request read token."""
    query = urllib.parse.urlencode({"state": "open", "head": f"{APP_OWNER}:{branch}", "per_page": "10"})
    return request(f"{API}/repos/{APP_REPO}/pulls?{query}", token)


def listing(get, path, token, key=None):
    """Every item of a paginated list (key: the list's field in an object page), up to PAGES pages."""
    items = []
    for page in range(1, PAGES + 1):
        status, body = get(f"{path}&per_page=100&page={page}", token)
        if status != 200:
            raise ScheduleError("A GitHub list could not be read")
        try:
            batch = json.loads(body)
        except ValueError:
            raise ScheduleError("A GitHub list could not be read") from None
        batch = batch.get(key) if key and isinstance(batch, dict) else batch
        if not isinstance(batch, list):
            raise ScheduleError("A GitHub list could not be read")
        items += batch
        if len(batch) < 100:
            return items
    raise ScheduleError("A GitHub list is too long")


def parse_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def title_pattern(workflow):
    lead = CHECKS[workflow]["title"]
    return re.compile(re.escape(lead) + r"([0-9a-f]{40})(?: \(trove-app #[1-9][0-9]{0,6}\))?")


def recent_runs(token, now, get=control_get):
    """{workflow: SHAs}: the commits each check has a run for, created in the last WINDOW, read from
    the run name. A name not in the form counts for nothing (a manual run of a branch name)."""
    since = (now - WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    tested = {}
    for workflow in CHECKS:
        runs = listing(get, f"actions/workflows/{workflow}/runs?created=" + urllib.parse.quote(">=" + since),
                       token, "workflow_runs")
        pattern, shas = title_pattern(workflow), set()
        for run in runs:
            title = run.get("display_title") if isinstance(run, dict) else None
            found = pattern.fullmatch(title) if isinstance(title, str) else None
            if found:
                shas.add(found.group(1))
        tested[workflow] = shas
    return tested


def branches(token, graphql=app_graphql):
    """(default branch, [(name, sha, committed)]) for every trove-app branch whose head is a commit."""
    heads, after, default = [], None, None
    for _ in range(PAGES):
        status, body = graphql({"owner": APP_OWNER, "name": APP_NAME, "after": after}, token)
        try:
            repo = json.loads(body)["data"]["repository"] if status == 200 else None
            refs = repo["refs"]
            default = (repo.get("defaultBranchRef") or {}).get("name")
            nodes, page = refs["nodes"], refs["pageInfo"]
        except (ValueError, KeyError, TypeError):
            raise ScheduleError("trove-app's branches could not be read") from None
        for node in nodes:
            target = (node or {}).get("target") or {}
            name, sha, when = (node or {}).get("name"), target.get("oid"), parse_time(target.get("committedDate"))
            if isinstance(name, str) and isinstance(sha, str) and SHA.fullmatch(sha) and when:
                heads.append((name, sha, when))
        if not page.get("hasNextPage"):
            return default, heads
        after = page.get("endCursor")
    raise ScheduleError("trove-app has too many branches")


def touches(workflow, files):
    """A changed file (or a renamed file's old path) is under the check's paths."""
    check = CHECKS[workflow]
    for entry in files:
        if not isinstance(entry, dict):
            continue
        for path in (entry.get("filename"), entry.get("previous_filename")):
            if isinstance(path, str) and (path.startswith(check["prefixes"]) or path in check["files"]):
                return True
    return False


class Comparer:
    """compare/base...head reads, cached and capped at MAX_COMPARED. None when unreadable or over the cap."""

    def __init__(self, token, get):
        self.token, self.get, self.cache, self.count = token, get, {}, 0

    def files(self, base, head):
        key = (base, head)
        if key not in self.cache:
            if self.count >= MAX_COMPARED:
                return None
            self.count += 1
            status, body = self.get(f"compare/{base}...{head}", self.token)
            try:
                data = json.loads(body) if status == 200 else None
                files = data.get("files") if isinstance(data, dict) else None
            except ValueError:
                files = None
            if isinstance(files, list) and len(files) >= COMPARE_FILE_LIMIT:
                files = [{"filename": p} for c in CHECKS.values() for p in c["prefixes"] + c["files"]]
            self.cache[key] = files if isinstance(files, list) else None
        return self.cache[key]


def main_commits(token, head, now, get):
    """main's commits of the last STALE, newest first: [(sha, parent sha or None)]."""
    since = (now - STALE).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = listing(get, f"commits?sha={head}&since=" + urllib.parse.quote(since), token)
    out = []
    for commit in commits[:100]:
        sha = commit.get("sha") if isinstance(commit, dict) else None
        parents = commit.get("parents") if isinstance(commit, dict) else None
        parent = parents[0].get("sha") if isinstance(parents, list) and parents and isinstance(parents[0], dict) else None
        if isinstance(sha, str) and SHA.fullmatch(sha):
            out.append((sha, parent if isinstance(parent, str) and SHA.fullmatch(parent) else None))
    return out


def main_range(commits, tested):
    """(base, head) of main's commits not yet checked: from the parent of the oldest unchecked one to
    the head, or None when the head is checked or there's no parent to compare from."""
    pending = []
    for sha, parent in commits:
        if sha in tested:
            break
        pending.append((sha, parent))
    if not pending or pending[-1][1] is None:
        return None
    return pending[-1][1], pending[0][0]


def pick(token, tested, now, get=app_get, graphql=app_graphql):
    """([{workflow, sha, branch}], fresh, compared): what to dispatch, newest heads first, main first
    of all; fresh counts the branches committed to in the last STALE. Nothing read here is printed."""
    default, heads = branches(token, graphql)
    main = next((h for h in heads if h[0] == default), None)
    if main is None:
        raise ScheduleError("trove-app's main branch could not be read")
    compare, picks = Comparer(token, get), []

    def add(workflow, sha, branch):
        if len(picks) < MAX_DISPATCHES and sha not in tested.get(workflow, set()) and \
                not any(p["workflow"] == workflow and p["sha"] == sha for p in picks):
            picks.append({"workflow": workflow, "sha": sha, "branch": branch})

    fresh = [h for h in heads if h[0] != default and now - h[2] <= STALE]
    fresh.sort(key=lambda h: h[2], reverse=True)
    if now - main[2] <= STALE:
        commits = main_commits(token, main[1], now, get)
        for workflow in CHECKS:
            span = main_range(commits, tested.get(workflow, set()))
            files = compare.files(*span) if span else None
            if files is not None and touches(workflow, files):
                add(workflow, main[1], None)
    for name, sha, _ in fresh:
        if len(picks) >= MAX_DISPATCHES:
            break
        if all(sha in tested.get(w, set()) for w in CHECKS) or sha == main[1]:
            continue
        files = compare.files(main[1], sha)
        if files is None:
            continue
        for workflow in CHECKS:
            if touches(workflow, files):
                add(workflow, sha, name)
    return picks, len(fresh), compare.count


def pr_number(branch, sha, token, get=pr_get):
    """The open trove-app pull request whose head is this branch at this SHA, or None."""
    status, body = get(branch, token)
    try:
        pulls = json.loads(body) if status == 200 else []
    except ValueError:
        return None
    for pull in pulls if isinstance(pulls, list) else []:
        number = pull.get("number") if isinstance(pull, dict) else None
        head = pull.get("head") if isinstance(pull, dict) else None
        if isinstance(number, int) and not isinstance(number, bool) and NUMBER.fullmatch(str(number)) and \
                isinstance(head, dict) and head.get("sha") == sha:
            return number
    return None


def dispatch_payload(sha, number=None):
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ScheduleError("The pick is not an exact commit")
    inputs = {"sha": sha}
    if number is not None:
        if not isinstance(number, int) or isinstance(number, bool) or not NUMBER.fullmatch(str(number)):
            raise ScheduleError("The pick's pull request is not a number")
        inputs["pr_number"] = str(number)
    return {"ref": "main", "inputs": inputs}


def control_dispatch(workflow, token, payload, opener=None):
    """The one write this script makes: a check's workflow dispatch, on main, with exactly a SHA and maybe a
    pull request number. Anything else is refused before it is sent. Returns the status."""
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if workflow not in CHECKS:
        raise ScheduleError("The schedule dispatches nothing but the checks")
    if set(payload) != {"ref", "inputs"} or payload["ref"] != "main" or not isinstance(inputs, dict) or \
            set(inputs) - {"pr_number"} != {"sha"} or not isinstance(inputs["sha"], str) or \
            not SHA.fullmatch(inputs["sha"]) or \
            ("pr_number" in inputs and not (isinstance(inputs["pr_number"], str) and NUMBER.fullmatch(inputs["pr_number"]))):
        raise ScheduleError("The dispatch is not exactly main with a SHA and maybe a pull request number")
    status, _ = request(f"{API}/repos/{CONTROL_REPO}/actions/workflows/{workflow}/dispatches", token,
                        payload, "POST", opener)
    return status


def label(entry):
    number = entry.get("pr")
    return f"{entry['workflow']} at {entry['sha'][:12]}" + (f" (trove-app #{int(number)})" if number else "")


def runs_step(now=None, get=control_get):
    check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ScheduleError("GitHub token unavailable")
    state(fresh=True)
    tested = recent_runs(token, now or datetime.datetime.now(datetime.timezone.utc), get)
    write_state(RUNS_FILE, {w: sorted(s) for w, s in tested.items()})
    for workflow, shas in tested.items():
        print(f"Trove check schedule: {len(shas)} commits have a {workflow} run in the last {WINDOW.days} days")


def pick_step(now=None, get=app_get, graphql=app_graphql):
    check_run()
    # Held in memory alone from here: never in a child's environment, a file, an argument or a printed line.
    token = os.environ.pop("TROVE_CI_TOKEN", "")
    if not token:
        raise ScheduleError("trove-kitchen-ci token unavailable")
    runs = read_state(RUNS_FILE)
    if not isinstance(runs, dict):
        raise ScheduleError("The recent runs were not recorded")
    tested = {w: {s for s in runs.get(w, []) if isinstance(s, str)} for w in CHECKS}
    picks, fresh, compared = pick(token, tested, now or datetime.datetime.now(datetime.timezone.utc), get, graphql)
    write_state(PICKS_FILE, picks)
    print(f"Trove check schedule: {fresh} branches with commits in the last {STALE.days} days, "
          f"{compared} compared, {len(picks)} checks to dispatch")


def prs_step(get=pr_get):
    check_run()
    token = os.environ.pop("TROVE_PR_READ_TOKEN", "")
    picks = read_state(PICKS_FILE)
    if not isinstance(picks, list) or not picks:
        return
    found = 0
    for entry in picks:
        branch = entry.get("branch") if isinstance(entry, dict) else None
        number = pr_number(branch, entry.get("sha"), token, get) if token and isinstance(branch, str) else None
        entry["pr"] = number
        found += number is not None
    write_state(PICKS_FILE, picks)
    print(f"Trove check schedule: {found} of {len(picks)} picks have an open pull request"
          + ("" if token else " (no pull-request read token)"))


def dispatch_step(post=control_dispatch):
    dry = check_run()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ScheduleError("GitHub token unavailable")
    picks = read_state(PICKS_FILE)
    if not picks:
        print("Trove check schedule: nothing to dispatch")
        return
    if not isinstance(picks, list):
        raise ScheduleError("The picks are malformed; nothing was dispatched")
    failed = 0
    for entry in picks:
        if not isinstance(entry, dict) or entry.get("workflow") not in CHECKS:
            raise ScheduleError("The picks are malformed")
        payload = dispatch_payload(entry.get("sha"), entry.get("pr"))
        if dry:
            print(f"Trove check schedule: would dispatch {label(entry)} (dry run)")
            continue
        status = post(entry["workflow"], token, payload)
        if status in (200, 204):
            print(f"Trove check schedule: dispatched {label(entry)}")
        else:
            failed += 1
            print(f"Trove check schedule: could not dispatch {label(entry)} ({int(status)})")
    if failed:
        raise ScheduleError(f"{failed} dispatches failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("runs", "pick", "prs", "dispatch"))
    args = parser.parse_args()
    {"runs": runs_step, "pick": pick_step, "prs": prs_step, "dispatch": dispatch_step}[args.step]()


if __name__ == "__main__":
    try:
        main()
    except ScheduleError as error:
        raise SystemExit("Trove check schedule stopped: " + error.args[0]) from None
    except Exception:
        raise SystemExit("Trove check schedule stopped; check the tokens and the API") from None

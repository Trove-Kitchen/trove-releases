"""Tests for check_schedule.py, with no network: python3 -I -m unittest discover -s tools -p 'test_*.py'"""

import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_schedule as cs  # noqa: E402

NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
A, B, C, D, E, P = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40)


def iso(delta):
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def graphql_of(heads, default="main"):
    """A GraphQL stand-in returning heads [(name, sha, age)] in one page."""
    def graphql(variables, token):
        nodes = [{"name": n, "target": {"oid": s, "committedDate": iso(age)}} for n, s, age in heads]
        return 200, json.dumps({"data": {"repository": {
            "defaultBranchRef": {"name": default},
            "refs": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}).encode()
    return graphql


def app_of(compares, commits=()):
    """An app_get stand-in: compares {(base, head): [paths]}, main commits [(sha, parent)]."""
    calls = []

    def get(path, token):
        calls.append(path)
        if path.startswith("compare/"):
            base, head = path[len("compare/"):].split("...")
            if (base, head) not in compares:
                return 404, b""
            return 200, json.dumps({"files": [{"filename": p} for p in compares[(base, head)]]}).encode()
        if path.startswith("commits?"):
            return 200, json.dumps([{"sha": s, "parents": [{"sha": p}] if p else []} for s, p in commits]).encode()
        raise AssertionError(path)
    get.calls = calls
    return get


class Touches(unittest.TestCase):
    def test_paths(self):
        self.assertTrue(cs.touches("web-tests.yml", [{"filename": "web/src/app/page.tsx"}]))
        self.assertTrue(cs.touches("web-tests.yml", [{"filename": "scripts/design-tokens/generate.mjs"}]))
        self.assertTrue(cs.touches("web-tests.yml", [{"filename": "docs/design/tokens.json"}]))
        self.assertFalse(cs.touches("web-tests.yml", [{"filename": "webby/x"}, {"filename": "android/a.kt"}]))
        self.assertTrue(cs.touches("resolver-container.yml", [{"filename": "services/video-resolver/app/main.py"}]))
        self.assertFalse(cs.touches("resolver-container.yml", [{"filename": "web/x"}]))

    def test_renamed_from(self):
        self.assertTrue(cs.touches("resolver-container.yml",
                                   [{"filename": "old/x.py", "previous_filename": "services/video-resolver/x.py"}]))


class RecentRuns(unittest.TestCase):
    def test_reads_shas_from_run_names_only(self):
        runs = {
            "web-tests.yml": [
                {"display_title": f"Trove web tests at {A}"},
                {"display_title": f"Trove web tests at {B} (trove-app #12)"},
                {"display_title": "Trove web tests"},
                {"display_title": f"Trove web tests at {C} extra"},
            ],
            "resolver-container.yml": [{"display_title": f"Trove video resolver container at {D}"}],
        }

        def get(path, token):
            workflow = path.split("/")[2]
            self.assertIn("created=%3E%3D", path)
            return 200, json.dumps({"workflow_runs": runs[workflow]}).encode()
        tested = cs.recent_runs("t", NOW, get)
        self.assertEqual(tested, {"web-tests.yml": {A, B}, "resolver-container.yml": {D}})

    def test_control_get_refuses_other_paths(self):
        with self.assertRaises(cs.ScheduleError):
            cs.control_get("actions/workflows/android-play.yml/runs?x", "t")

    def test_app_get_refuses_other_paths(self):
        for path in ("contents/web/package.json", "pulls?state=open", f"compare/main...{A}"):
            with self.assertRaises(cs.ScheduleError):
                cs.app_get(path, "t")


class MainRange(unittest.TestCase):
    def test_from_parent_of_oldest_unchecked(self):
        commits = [(A, B), (B, C), (C, D)]
        self.assertEqual(cs.main_range(commits, {C}), (C, A))
        self.assertEqual(cs.main_range(commits, set()), (D, A))

    def test_head_checked(self):
        self.assertIsNone(cs.main_range([(A, B)], {A}))

    def test_no_parent(self):
        self.assertIsNone(cs.main_range([(A, None)], set()))


class Pick(unittest.TestCase):
    hour, day = datetime.timedelta(hours=1), datetime.timedelta(days=1)

    def test_branch_heads_by_paths(self):
        heads = [("main", A, 10 * self.day), ("feature-web", B, self.hour), ("feature-resolver", C, 2 * self.hour),
                 ("feature-ios", D, 3 * self.hour), ("old-web", E, 4 * self.day)]
        get = app_of({(A, B): ["web/a.ts"], (A, C): ["services/video-resolver/app/x.py"], (A, D): ["Munch/x.swift"]})
        picks, fresh, compared = cs.pick("t", {}, NOW, get, graphql_of(heads))
        self.assertEqual([(p["workflow"], p["sha"], p["branch"]) for p in picks],
                         [("web-tests.yml", B, "feature-web"), ("resolver-container.yml", C, "feature-resolver")])
        self.assertEqual((fresh, compared), (3, 3))
        self.assertNotIn(f"compare/{A}...{E}", get.calls)

    def test_skips_checked_heads(self):
        heads = [("main", A, 10 * self.day), ("both", B, self.hour)]
        get = app_of({(A, B): ["web/a.ts", "services/video-resolver/x"]})
        picks, _, _ = cs.pick("t", {"web-tests.yml": {B}}, NOW, get, graphql_of(heads))
        self.assertEqual([p["workflow"] for p in picks], ["resolver-container.yml"])
        picks, _, compared = cs.pick("t", {"web-tests.yml": {B}, "resolver-container.yml": {B}}, NOW, get,
                                     graphql_of(heads))
        self.assertEqual((picks, compared), ([], 0))

    def test_caps_dispatches(self):
        heads = [("main", A, 10 * self.day)] + [(f"b{i}", s, self.hour * (i + 1)) for i, s in enumerate((B, C, D, E))]
        get = app_of({(A, s): ["web/x", "services/video-resolver/y"] for s in (B, C, D, E)})
        picks, _, _ = cs.pick("t", {}, NOW, get, graphql_of(heads))
        self.assertEqual(len(picks), cs.MAX_DISPATCHES)
        self.assertEqual([p["sha"] for p in picks], [B, B, C])

    def test_main_range(self):
        heads = [("main", A, self.hour)]
        get = app_of({(C, A): ["web/x"]}, commits=[(A, B), (B, C)])
        picks, _, _ = cs.pick("t", {"resolver-container.yml": {A}}, NOW, get, graphql_of(heads))
        self.assertEqual([(p["workflow"], p["sha"], p["branch"]) for p in picks], [("web-tests.yml", A, None)])

    def test_main_since_last_checked(self):
        heads = [("main", A, self.hour)]
        get = app_of({(B, A): ["Munch/x.swift"]}, commits=[(A, B), (B, C)])
        picks, _, _ = cs.pick("t", {"web-tests.yml": {B}}, NOW, get, graphql_of(heads))
        self.assertEqual(picks, [])

    def test_unreadable_compare_is_passed_over(self):
        heads = [("main", A, 10 * self.day), ("x", B, self.hour), ("y", C, 2 * self.hour)]
        get = app_of({(A, C): ["web/x"]})
        picks, _, _ = cs.pick("t", {}, NOW, get, graphql_of(heads))
        self.assertEqual([p["sha"] for p in picks], [C])

    def test_large_compare_touches_everything(self):
        heads = [("main", A, 10 * self.day), ("big", B, self.hour)]
        get = app_of({(A, B): [f"other/{i}" for i in range(cs.COMPARE_FILE_LIMIT)]})
        picks, _, _ = cs.pick("t", {}, NOW, get, graphql_of(heads))
        self.assertEqual(len(picks), 2)

    def test_missing_main(self):
        with self.assertRaises(cs.ScheduleError):
            cs.pick("t", {}, NOW, app_of({}), graphql_of([("dev", A, self.hour)]))


class PullRequests(unittest.TestCase):
    def test_matches_head_sha(self):
        def get(branch, token):
            return 200, json.dumps([{"number": 7, "head": {"sha": B}}, {"number": 8, "head": {"sha": A}}]).encode()
        self.assertEqual(cs.pr_number("x", A, "t", get), 8)
        self.assertIsNone(cs.pr_number("x", C, "t", get))

    def test_unreadable_is_none(self):
        self.assertIsNone(cs.pr_number("x", A, "t", lambda b, t: (403, b"")))


class Dispatch(unittest.TestCase):
    def test_payload(self):
        self.assertEqual(cs.dispatch_payload(A), {"ref": "main", "inputs": {"sha": A}})
        self.assertEqual(cs.dispatch_payload(A, 5), {"ref": "main", "inputs": {"sha": A, "pr_number": "5"}})
        for bad in (("main",), (A[:12],), (A, "5"), (A, True), (A, 0)):
            with self.assertRaises(cs.ScheduleError):
                cs.dispatch_payload(*bad)

    def test_control_dispatch_refuses_before_sending(self):
        opener = mock.Mock(side_effect=AssertionError("sent"))
        bad = [
            ("android-play.yml", {"ref": "main", "inputs": {"sha": A}}),
            ("web-tests.yml", {"ref": "dev", "inputs": {"sha": A}}),
            ("web-tests.yml", {"ref": "main", "inputs": {"sha": A, "ref": "feature"}}),
            ("web-tests.yml", {"ref": "main", "inputs": {"ref": "feature"}}),
            ("web-tests.yml", {"ref": "main", "inputs": {"sha": "main"}}),
            ("web-tests.yml", {"ref": "main", "inputs": {"sha": A, "pr_number": "x"}}),
        ]
        for workflow, payload in bad:
            with self.assertRaises(cs.ScheduleError):
                cs.control_dispatch(workflow, "t", payload, opener)
        opener.assert_not_called()

    def test_control_dispatch_sends(self):
        sent = []

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b""

        def opener(req, timeout):
            sent.append((req.full_url, req.get_method(), json.loads(req.data)))
            return Response()
        payload = {"ref": "main", "inputs": {"sha": A, "pr_number": "3"}}
        self.assertEqual(cs.control_dispatch("web-tests.yml", "t", payload, opener), 204)
        self.assertEqual(sent, [(f"{cs.API}/repos/{cs.CONTROL_REPO}/actions/workflows/web-tests.yml/dispatches",
                                 "POST", payload)])


class Steps(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "RUNNER_TEMP": self.temp.name, "GITHUB_REPOSITORY": cs.CONTROL_REPO, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "schedule", "GH_TOKEN": "gh", "TROVE_CI_TOKEN": "ci",
            "TROVE_PR_READ_TOKEN": "pr"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_quietly(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args, **kwargs)
        return out.getvalue()

    def test_refuses_other_runs(self):
        for name, value in (("GITHUB_REPOSITORY", "someone/fork"), ("GITHUB_REF", "refs/heads/dev"),
                            ("GITHUB_EVENT_NAME", "push")):
            with mock.patch.dict(os.environ, {name: value}):
                with self.assertRaises(cs.ScheduleError):
                    cs.check_run()
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/1/merge"}):
            self.assertTrue(cs.check_run())

    def test_end_to_end_prints_no_branch_name(self):
        def control(path, token):
            return 200, json.dumps({"workflow_runs": []}).encode()
        heads = [("main", A, datetime.timedelta(days=9)), ("secret-feature-name", B, datetime.timedelta(hours=1))]
        sent = []
        out = self.run_quietly(cs.runs_step, NOW, control)
        out += self.run_quietly(cs.pick_step, NOW, app_of({(A, B): ["web/x"]}), graphql_of(heads))
        self.assertNotIn("TROVE_CI_TOKEN", os.environ)
        out += self.run_quietly(cs.prs_step, lambda b, t: (200, json.dumps([{"number": 4, "head": {"sha": B}}]).encode()))
        out += self.run_quietly(cs.dispatch_step, lambda w, t, p: sent.append((w, p)) or 204)
        self.assertEqual(sent, [("web-tests.yml", {"ref": "main", "inputs": {"sha": B, "pr_number": "4"}})])
        self.assertIn(f"dispatched web-tests.yml at {B[:12]} (trove-app #4)", out)
        self.assertNotIn("secret-feature-name", out)
        self.assertNotIn(B, out)

    def test_dry_run_sends_nothing(self):
        Path(self.temp.name, cs.STATE).mkdir()
        cs.write_state(cs.PICKS_FILE, [{"workflow": "web-tests.yml", "sha": A, "branch": "x", "pr": None}])
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            out = self.run_quietly(cs.dispatch_step, mock.Mock(side_effect=AssertionError("sent")))
        self.assertIn("would dispatch web-tests.yml", out)

    def test_failed_dispatch_stops(self):
        Path(self.temp.name, cs.STATE).mkdir()
        cs.write_state(cs.PICKS_FILE, [{"workflow": "web-tests.yml", "sha": A, "branch": None, "pr": None}])
        with self.assertRaises(cs.ScheduleError):
            self.run_quietly(cs.dispatch_step, lambda w, t, p: 422)


if __name__ == "__main__":
    unittest.main()

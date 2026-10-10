"""Tests for the helper the action runs: run `python3 scripts/test_yoriwake_action.py`.

The decision-record and observation fixtures under `fixtures/` were written by real runs of the
plugin on its demo sample, so a change in what the plugin writes shows up here as a failing read.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yoriwake_action as ya  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A run that does not select says so, so a `yoriwake.select` in the build's gradle.properties
# or environment cannot make it select.
OFF = ["-Pyoriwake.select=false", "-Pyoriwake.observe=false"]

LIST = "/tmp/state/trusted.tsv"
TRUSTED = f"-Pyoriwake.trustedMaps={LIST}"


def choose(event, payload=None, **overrides):
    facts = dict(
        observe=False,
        isolated_capture=False,
        default_branch="",
        map_restored=True,
        history_ready=True,
        job_total=1,
        key="",
        tasks="test",
        gradle_args="",
        trusted_list=LIST,
        trusted_listed=True,
    )
    facts.update(overrides)
    return ya.choose_flags(event, payload, **facts)


def pull_request(base="main", labels=()):
    return {
        "pull_request": {
            "base": {"ref": base},
            "labels": [{"name": name} for name in labels],
        }
    }


def push(ref="refs/heads/main", default="main"):
    return {"ref": ref, "repository": {"default_branch": default}}


class ChooseFlags(unittest.TestCase):
    def test_a_push_to_the_default_branch_records_and_saves(self):
        decision = choose("push", push())
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", OFF, True))
        self.assertEqual(decision.warnings, [])

    def test_a_recording_run_asks_for_isolated_capture_when_the_input_is_set(self):
        decision = choose("push", push(), isolated_capture=True)
        self.assertEqual(decision.flags, [*OFF, "-Pyoriwake.isolatedCapture"])
        self.assertTrue(decision.save)

    def test_a_schedule_records_and_saves(self):
        decision = choose("schedule", {"schedule": "0 3 * * *"}, isolated_capture=True)
        self.assertEqual((decision.kind, decision.flags, decision.save),
                         ("record", [*OFF, "-Pyoriwake.isolatedCapture"], True))

    def test_the_default_branch_input_wins_over_the_payload(self):
        decision = choose("push", push(ref="refs/heads/trunk", default="main"),
                          default_branch="trunk")
        self.assertTrue(decision.save)

    def test_a_push_to_another_branch_records_saves_nothing_and_warns(self):
        decision = choose("push", push(ref="refs/heads/feature/x"))
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", OFF, False))
        self.assertIn("refs/heads/feature/x", decision.warnings[0])

    def test_a_push_whose_default_branch_is_unknown_saves_nothing(self):
        decision = choose("push", {"ref": "refs/heads/main"})
        self.assertFalse(decision.save)
        self.assertTrue(decision.warnings)

    def test_a_pull_request_selects_against_its_base(self):
        decision = choose("pull_request", pull_request(base="release/1.x"))
        self.assertEqual(decision.kind, "select")
        self.assertEqual(decision.flags,
                         ["-Pyoriwake.select", "-Pyoriwake.observe=false",
                          "-Pyoriwake.base=origin/release/1.x", TRUSTED])
        self.assertFalse(decision.save)
        self.assertEqual(decision.warnings, [])

    def test_a_pull_request_that_narrows_always_passes_the_trusted_list(self):
        for observe, labels in ((False, ()), (True, ()), (False, ["yoriwake:full-run"])):
            decision = choose("pull_request", pull_request(labels=labels), observe=observe)
            self.assertIn(TRUSTED, decision.flags, (observe, labels))

    def test_a_pull_request_without_a_trusted_list_file_records_and_warns(self):
        decision = choose("pull_request", pull_request(), trusted_list="", trusted_listed=False)
        self.assertEqual((decision.kind, decision.flags), ("record", OFF))
        self.assertIn("trusted-map list", decision.warnings[0])

    def test_a_pull_request_whose_trusted_list_names_no_map_records_and_says_why(self):
        # A plugin that ignores the list would otherwise narrow from whatever was restored.
        for observe in (False, True):
            decision = choose("pull_request", pull_request(), observe=observe,
                              trusted_listed=False)
            self.assertEqual((decision.kind, decision.flags), ("record", OFF), observe)
            self.assertIn("vouched", decision.warnings[0])

    def test_a_pull_request_whose_trusted_list_path_holds_whitespace_records(self):
        # A self-hosted runner's temp directory can hold a space; the flags are word-split.
        decision = choose("pull_request", pull_request(), trusted_list="/runner one/trusted.tsv")
        self.assertEqual((decision.kind, decision.flags), ("record", OFF))
        self.assertIn("whitespace", decision.warnings[0])

    def test_a_recording_run_passes_no_trusted_list(self):
        for event, payload in (("push", push()), ("schedule", {}), ("pull_request_target", push())):
            self.assertFalse(any("trustedMaps" in flag for flag in choose(event, payload).flags),
                             event)

    def test_a_pull_request_observes_instead_when_asked_and_never_both(self):
        decision = choose("pull_request", pull_request(), observe=True)
        self.assertEqual(decision.kind, "observe")
        self.assertIn("-Pyoriwake.observe", decision.flags)
        self.assertNotIn("-Pyoriwake.select", decision.flags)
        self.assertIn("-Pyoriwake.select=false", decision.flags)
        self.assertIn("-Pyoriwake.base=origin/main", decision.flags)

    def test_the_full_run_label_adds_the_full_run_flag(self):
        decision = choose("pull_request", pull_request(labels=["bug", "yoriwake:full-run"]))
        self.assertEqual(decision.flags[-1], "-Pyoriwake.fullRun")
        self.assertIn("-Pyoriwake.select", decision.flags)

    def test_another_label_adds_nothing(self):
        decision = choose("pull_request", pull_request(labels=["yoriwake:full"]))
        self.assertNotIn("-Pyoriwake.fullRun", decision.flags)

    def test_a_pull_request_never_saves_and_never_isolates(self):
        decision = choose("pull_request", pull_request(), isolated_capture=True)
        self.assertFalse(decision.save)
        self.assertNotIn("-Pyoriwake.isolatedCapture", decision.flags)

    def test_a_pull_request_without_a_restored_map_records_and_warns(self):
        decision = choose("pull_request", pull_request(labels=["yoriwake:full-run"]),
                          map_restored=False)
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", OFF, False))
        self.assertIn("no map", decision.warnings[0])

    def test_a_pull_request_whose_history_is_not_ready_records_and_warns(self):
        for ready in (False, None):
            decision = choose("pull_request", pull_request(), observe=True, history_ready=ready)
            self.assertEqual((decision.kind, decision.flags), ("record", OFF))
            self.assertIn("history", decision.warnings[0])

    def test_a_pull_request_without_a_base_records(self):
        for payload in ({}, {"pull_request": {"base": {}}}, pull_request(base="bad..name"),
                        pull_request(base="-x"), None):
            decision = choose("pull_request", payload)
            self.assertEqual((decision.kind, decision.flags), ("record", OFF), payload)
            self.assertTrue(decision.warnings)

    def test_a_matrix_job_without_a_key_records_and_names_the_input(self):
        decision = choose("pull_request", pull_request(), job_total=3)
        self.assertEqual((decision.kind, decision.flags), ("record", OFF))
        self.assertIn("`key`", decision.warnings[0])
        self.assertEqual(choose("pull_request", pull_request(), job_total=3, key="jdk17").kind,
                         "select")

    def test_a_matrix_job_without_a_key_saves_no_map_and_names_the_input(self):
        for event, payload in (("push", push()), ("schedule", {})):
            decision = choose(event, payload, job_total=2, isolated_capture=True)
            self.assertEqual((decision.kind, decision.flags, decision.save),
                             ("record", OFF, False), event)
            self.assertIn("`key`", decision.warnings[0])
            self.assertTrue(choose(event, payload, job_total=2, key="jdk17").save, event)

    def test_flags_the_action_owns_in_the_inputs_make_the_run_record(self):
        for tasks, gradle_args in (
            ("test", "-Pyoriwake.select"),
            ("test", "--info -Pyoriwake.base=origin/x"),
            ("test", "-P yoriwake.observe"),
            ("test", "--project-prop=yoriwake.fullRun"),
            ("test -Pyoriwake.complement", ""),
            ("test", "-Dorg.gradle.project.yoriwake.select=true"),
            ("test", "-Pyoriwake.trustedMaps=/tmp/mine.tsv"),
            ("test", "-P yoriwake.trustedMaps"),
        ):
            decision = choose("pull_request", pull_request(), tasks=tasks, gradle_args=gradle_args)
            self.assertEqual((decision.kind, decision.flags), ("record", OFF), gradle_args)
            self.assertIn("owns", decision.warnings[0])

    def test_other_flags_in_the_inputs_change_nothing(self):
        decision = choose("pull_request", pull_request(),
                          gradle_args="-Pyoriwake.isolatedCapture -Pyoriwake.alwaysRun=a.B --info")
        self.assertEqual(decision.kind, "select")

    def test_every_other_event_records_saves_nothing_and_names_the_event(self):
        for event in ("pull_request_target", "issue_comment", "workflow_dispatch",
                      "merge_group", "workflow_run"):
            decision = choose(event, push(), isolated_capture=True)
            self.assertEqual((decision.kind, decision.flags, decision.save),
                             ("record", OFF, False), event)
            self.assertIn(event, decision.warnings[0])

    def test_the_default_branch_s_run_ignores_preconditions_of_selection(self):
        decision = choose("push", push(), map_restored=False, history_ready=None)
        self.assertEqual((decision.kind, decision.save), ("record", True))


def git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


class Repos:
    """A remote holding a default branch, a pull request's merge ref and an unrelated branch.

    The default branch is longer than the first deepening step, so a clone deepened by it stays
    shallow.
    """

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin"
        work = root / "author"
        work.mkdir()
        git(work, "init", "-q", "-b", "main")
        git(work, "config", "user.email", "t@example.com")
        git(work, "config", "user.name", "t")
        git(work, "config", "commit.gpgsign", "false")
        self.main = [self.commit(work, f"main {n}") for n in range(60)]
        git(work, "checkout", "-q", "-b", "feature", self.main[55])
        feature = [self.commit(work, f"feature {n}") for n in range(2)]
        git(work, "checkout", "-q", "main")
        git(work, "merge", "-q", "--no-ff", "-m", "merge", "feature")
        self.merge = git(work, "rev-parse", "HEAD")
        git(work, "reset", "-q", "--hard", self.main[-1])
        git(work, "checkout", "-q", "-b", "other", self.main[1])
        self.other = self.commit(work, "other")
        git(work, "checkout", "-q", "main")
        git(root, "clone", "-q", "--bare", str(work), str(self.origin))
        git(self.origin, "update-ref", "refs/pull/1/merge", self.merge)
        git(self.origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
        self.feature_tip = feature[-1]

    @staticmethod
    def commit(work, message):
        name = message.replace(" ", "-") + ".txt"
        (work / name).write_text(message + "\n")
        git(work, "add", name)
        git(work, "commit", "-q", "-m", message)
        return git(work, "rev-parse", "HEAD")

    def pull_request_checkout(self, depth=1):
        """What actions/checkout leaves for a pull request: the merge ref only, detached."""
        ws = Path(tempfile.mkdtemp(dir=self.root))
        git(ws, "init", "-q")
        git(ws, "remote", "add", "origin", self.origin.as_uri())
        fetch = ["fetch", "-q", "--no-tags"]
        if depth:
            fetch.append(f"--depth={depth}")
        git(ws, *fetch, "origin", "+refs/pull/1/merge:refs/remotes/pull/1/merge")
        git(ws, "checkout", "-q", "--detach", "refs/remotes/pull/1/merge")
        return ws


def write_map(ws: Path, stamp: str, task="test-0123abcd"):
    task_dir = ws / ".gradle" / "yoriwake" / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "capture-commit").write_text(stamp + "\n")
    return ws / ".gradle" / "yoriwake"


class Deepen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.repos = Repos(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_depth_one_pull_request_checkout_reaches_the_merge_base_in_the_second_step(self):
        ws = self.repos.pull_request_checkout()
        maps = write_map(ws, self.repos.main[-3])
        history = ya.deepen("main", maps, ws)
        self.assertTrue(history.ready, history.notes)
        self.assertEqual(history.steps, 2)
        self.assertEqual(git(ws, "rev-parse", "refs/remotes/origin/main"), self.repos.main[-1])
        self.assertEqual(git(ws, "rev-parse", "--is-shallow-repository"), "true")

    def test_a_capture_commit_on_no_fetched_branch_unshallows_then_reports_not_ready(self):
        ws = self.repos.pull_request_checkout()
        maps = write_map(ws, self.repos.other)
        history = ya.deepen("main", maps, ws)
        self.assertFalse(history.ready)
        self.assertEqual(history.steps, 4)  # 1, 50, 500, then all
        self.assertEqual(git(ws, "rev-parse", "--is-shallow-repository"), "false")
        self.assertTrue(any(self.repos.other[:12] in note for note in history.notes),
                        history.notes)

    def test_every_map_s_capture_commit_must_be_on_the_base(self):
        ws = self.repos.pull_request_checkout()
        write_map(ws, self.repos.main[-3], task="test-0123abcd")
        maps = write_map(ws, self.repos.feature_tip, task="integrationTest-89abcdef")
        self.assertFalse(ya.deepen("main", maps, ws).ready)

    def test_a_capture_stamp_that_is_not_a_commit_is_not_ready(self):
        ws = self.repos.pull_request_checkout()
        maps = write_map(ws, "not-a-hash")
        self.assertFalse(ya.deepen("main", maps, ws).ready)

    def test_a_map_without_a_capture_stamp_needs_only_the_merge_base(self):
        ws = self.repos.pull_request_checkout()
        maps = ws / ".gradle" / "yoriwake"
        (maps / "test-0123abcd").mkdir(parents=True)
        history = ya.deepen("main", maps, ws)
        self.assertTrue(history.ready, history.notes)

    def test_a_full_clone_takes_one_step(self):
        ws = self.repos.pull_request_checkout(depth=0)
        maps = write_map(ws, self.repos.main[-3])
        history = ya.deepen("main", maps, ws)
        self.assertTrue(history.ready, history.notes)
        self.assertEqual(history.steps, 1)

    def test_a_base_the_remote_does_not_have_is_not_ready(self):
        ws = self.repos.pull_request_checkout()
        maps = write_map(ws, self.repos.main[-3])
        history = ya.deepen("nope", maps, ws)
        self.assertFalse(history.ready)
        self.assertTrue(history.notes)

    def test_a_base_that_is_not_a_branch_name_is_never_fetched(self):
        ws = self.repos.pull_request_checkout()
        maps = write_map(ws, self.repos.main[-3])
        for base in ("", "-upload-pack=x", "a..b", "main:refs/heads/x"):
            self.assertFalse(ya.deepen(base, maps, ws).ready, base)
        self.assertEqual(git(ws, "for-each-ref", "refs/remotes/origin"), "")

    def test_a_directory_that_is_not_a_repository_is_not_ready(self):
        plain = Path(tempfile.mkdtemp(dir=self.tmp))
        self.assertFalse(ya.deepen("main", plain / "maps", plain).ready)


class DeepenALongHistory(unittest.TestCase):
    def test_a_capture_commit_beyond_the_last_depth_is_reached_by_unshallowing(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = tmp / "origin"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        stream = "".join(
            f"commit refs/heads/main\nmark :{n}\ncommitter t <t@example.com> {1700000000 + n} +0000\n"
            f"data 2\n{n % 10}\n" + (f"from :{n - 1}\n" if n > 1 else "") + "\n"
            for n in range(1, 601))
        subprocess.run(["git", "fast-import", "--quiet"], cwd=origin, input=stream, text=True,
                       check=True)
        stamp = git(origin, "rev-parse", "main~550")
        ws = tmp / "ws"
        git(tmp, "clone", "-q", "--depth=1", origin.as_uri(), str(ws))
        maps = write_map(ws, stamp)
        history = ya.deepen("main", maps, ws)
        self.assertTrue(history.ready, history.notes)
        self.assertEqual(history.steps, 4)
        self.assertEqual(git(ws, "rev-parse", "--is-shallow-repository"), "false")


def place(map_dir: Path, task: str, files: dict, age_seconds=0):
    """Writes files into a task's map directory: text, a fixture file, or a fixture run's files."""
    task_dir = map_dir / task
    task_dir.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        if isinstance(source, Path) and source.is_dir():
            for each in source.iterdir():
                shutil.copyfile(each, task_dir / each.name)
            continue
        target = task_dir / name
        if isinstance(source, Path):
            shutil.copyfile(source, target)
        else:
            target.write_text(source, encoding="utf-8")
    if age_seconds:
        then = time.time() - age_seconds
        for each in task_dir.iterdir():
            os.utime(each, (then, then))
    return task_dir


def row_of(text, label="test"):
    return next(line for line in text.splitlines() if line.startswith(f"| `{label}`"))


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.maps = self.tmp / "maps"
        self.stage = self.tmp / "stage"
        self.started = time.time() - 5

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def summarise(self, kind="select", flags="-Pyoriwake.select", warnings=()):
        return ya.summarise(self.maps, self.started, "c0ffee" * 6 + "abcd", kind, flags,
                            list(warnings), self.stage)

    def test_a_narrowed_task_shows_tests_run_and_skipped(self):
        place(self.maps, "test-7f007aca", {"schema-version": "7\n", "run": FIXTURES / "narrowed"})
        text, staged = self.summarise()
        row = row_of(text)
        self.assertIn("narrowed", row)
        self.assertIn("| 3 | 8 |", row)
        self.assertIn("| 7 |", row)
        self.assertIn("c0ffee", text)
        self.assertEqual(staged, [])

    def test_two_parts_are_merged(self):
        part = next((FIXTURES / "narrowed").iterdir()).read_text().splitlines()
        head = [line for line in part if line.startswith("#")]
        body = [line for line in part if not line.startswith("#")]
        place(self.maps, "test-7f007aca", {
            "decisions.tsv.100-1.part": "\n".join(head + body[:5]) + "\n",
            "decisions.tsv.200-1.part": "\n".join(head + body[5:]) + "\n",
            "decisions.tsv": "\n".join(head + body[5:]) + "\n",
        })
        self.assertIn("| 3 | 8 |", row_of(self.summarise()[0]))

    def test_a_recording_run_over_several_test_jvms_counts_every_test_once(self):
        place(self.maps, "test-7f007aca", {"schema-version": "7\n", "run": FIXTURES / "record"})
        self.assertGreater(len(list((self.maps / "test-7f007aca").glob("*.part"))), 1)
        row = row_of(self.summarise(kind="record", flags="")[0])
        self.assertIn("recorded", row)
        self.assertIn("| 11 | 0 |", row)

    def test_a_test_any_writer_ran_counts_as_run(self):
        head = "# test\tverdict\treason\n#!outcome\tnarrowed\n"
        place(self.maps, "test-7f007aca", {
            "decisions.tsv.1-1.part": head + "[t:a]\tincluded\tREACHES_CHANGE\n",
            "decisions.tsv.2-1.part": head + "[t:a]\texcluded\tSKIPPED\n[t:b]\texcluded\tSKIPPED\n",
        })
        self.assertIn("| 1 | 1 |", row_of(self.summarise()[0]))

    def test_containers_are_not_counted_as_tests(self):
        head = "# test\tverdict\treason\n#!outcome\tnarrowed\n"
        place(self.maps, "test-7f007aca", {"decisions.tsv.1-1.part": head + "".join(
            f"{test}\t{verdict}\tR\n" for test, verdict in (
                ("[engine:e]", "included"),
                ("[engine:e]/[class:A]", "included"),
                ("[engine:e]/[class:A]/[method:a()]", "included"),
                ("[engine:e]/[class:A]/[method:a2()]", "excluded"),
                ("[engine:e]/[class:B]", "excluded"),
                ("[engine:e]/[class:B]/[method:b()]", "excluded"),
            ))})
        self.assertIn("| 1 | 2 |", row_of(self.summarise()[0]))

    def test_files_older_than_the_run_read_did_not_run(self):
        place(self.maps, "test-7f007aca", {
            "schema-version": "7\n",
            "run": FIXTURES / "observed",
            "decisions.tsv": next((FIXTURES / "observed").glob("*.part")),
        }, age_seconds=3600)
        text, staged = self.summarise()
        self.assertIn("did not run", row_of(text))
        self.assertNotIn("Observed", text)
        self.assertEqual(staged, [])

    def test_a_declined_run_names_the_decline(self):
        place(self.maps, "test-7f007aca", {"schema-version": "7\n", "run": FIXTURES / "declined"})
        row = row_of(self.summarise(flags="-Pyoriwake.select -Pyoriwake.fullRun")[0])
        self.assertIn("full-run-requested", row)
        self.assertIn("| 11 | 0 |", row)

    def test_an_observed_run_shows_the_observation_s_counts_and_stages_it(self):
        place(self.maps, "app-test-89abcdef", {"schema-version": "7\n", "run": FIXTURES / "observed"})
        text, staged = self.summarise(kind="observe", flags="-Pyoriwake.observe")
        row = row_of(text, "app-test")
        self.assertIn("observed: would have narrowed", row)
        self.assertIn("| 11 | 0 |", row)
        self.assertIn("2 failed, 2 of them kept; 0 would-be misses; 8 tests would have been "
                      "skipped, 0.007 s of recorded test time", text)
        self.assertEqual(len(staged), 1)
        self.assertEqual(json.loads(staged[0].read_text())["wouldBeSkipped"], 8)
        self.assertEqual(staged[0].parent.name, "app-test")

    def test_an_observation_with_a_would_be_miss_counts_it(self):
        place(self.maps, "test-7f007aca", {"observation.json": FIXTURES / "observation-miss.json"})
        text, _ = self.summarise(kind="observe")
        self.assertIn("2 failed, 1 of them kept; 1 would-be miss; 2 tests would have been "
                      "skipped, 0.004 s of recorded test time", text)

    def test_the_summary_shows_counts_never_percentages(self):
        place(self.maps, "a-test-11111111", {"run": FIXTURES / "narrowed"})
        place(self.maps, "b-test-22222222", {"run": FIXTURES / "declined"})
        place(self.maps, "c-test-33333333", {"run": FIXTURES / "observed"})
        place(self.maps, "d-test-44444444", {"observation.json": FIXTURES / "observation-miss.json"})
        text, _ = self.summarise(warnings=["a warning"])
        self.assertNotIn("%", text)
        self.assertIn("a warning", text)

    def test_a_missing_or_unparseable_file_is_a_line_never_an_exception(self):
        place(self.maps, "test-7f007aca", {
            "decisions.tsv.1-1.part": "\x00\x01 not a record",
            "observation.json": "{ not json",
            "schema-version": "seven\n",
        })
        place(self.maps, "other-0badf00d", {"observation.json": "[]"})
        (self.maps / "binary-0badf00d").mkdir()
        (self.maps / "binary-0badf00d" / "decisions.tsv.1-1.part").write_bytes(b"#!outcome\t\xff\n")
        text, staged = self.summarise(kind="observe")
        self.assertIn("could not be read", text)
        self.assertIn("`binary`", text)
        self.assertEqual(staged, [])

    def test_no_map_directory_says_so(self):
        text, staged = self.summarise(kind="record", flags="")
        self.assertIn("No map directory", text)
        self.assertEqual(staged, [])

    def test_text_from_the_files_cannot_break_the_table(self):
        head = "# test\tverdict\treason\n#!outcome\tfull-run\n#!full-run-kind\ta|b`c\n"
        place(self.maps, "test-7f007aca", {"decisions.tsv.1-1.part": head})
        row = row_of(self.summarise()[0])
        self.assertEqual(row.count("|"), 7, row)


class CommandLine(unittest.TestCase):
    """The subcommands as the action calls them: facts in the environment, answers in files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "output"
        self.out.write_text("")
        self.state = self.tmp / "state"
        self.state.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, command, env, cwd=None):
        full = {
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(self.out),
            "YORIWAKE_STATE": str(self.state),
            **env,
        }
        return subprocess.run(
            [sys.executable, str(Path(ya.__file__)), command],
            env=full, cwd=cwd or self.tmp, capture_output=True, text=True,
        )

    def outputs(self):
        return dict(line.split("=", 1) for line in self.out.read_text().splitlines() if line)

    def payload(self, data, name="event.json"):
        path = self.tmp / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return str(path)

    def test_flags_reads_the_event_from_the_environment(self):
        (self.state / "trusted.tsv").write_text(f"test-7f007aca\t{'ab' * 32}\n")
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": self.payload(pull_request(labels=["yoriwake:full-run"])),
            "YORIWAKE_MAP_RESTORED": "true",
            "YORIWAKE_HISTORY_READY": "true",
            "YORIWAKE_JOB_TOTAL": "1",
            "YORIWAKE_TASKS": "test",
            "YORIWAKE_TRUSTED_LIST": str(self.state / "trusted.tsv"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {
            "kind": "select",
            "flags": "-Pyoriwake.select -Pyoriwake.observe=false -Pyoriwake.base=origin/main "
                     f"-Pyoriwake.trustedMaps={self.state / 'trusted.tsv'} -Pyoriwake.fullRun",
            "save": "false",
        })

    def test_an_overriding_event_is_read_before_the_runner_s(self):
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_EVENT_PATH": self.payload({}),
            "YORIWAKE_EVENT_NAME": "push",
            "YORIWAKE_EVENT_PATH": self.payload(push(), "push.json"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["save"], "true")

    def test_an_overriding_event_is_ignored_unless_the_run_was_dispatched(self):
        for real in ("pull_request_target", "issue_comment", "workflow_run", "pull_request", ""):
            self.out.write_text("")
            result = self.run_helper("flags", {
                "GITHUB_EVENT_NAME": real,
                "GITHUB_EVENT_PATH": self.payload(pull_request()),
                "YORIWAKE_EVENT_NAME": "push",
                "YORIWAKE_EVENT_PATH": self.payload(push(), "push.json"),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.outputs()["save"], "false", real)

    def test_flags_records_when_the_trusted_list_names_no_map(self):
        for text in ("", "garbled line\n"):
            self.out.write_text("")
            (self.state / "trusted.tsv").write_text(text)
            result = self.run_helper("flags", {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": self.payload(pull_request()),
                "YORIWAKE_MAP_RESTORED": "true",
                "YORIWAKE_HISTORY_READY": "true",
                "YORIWAKE_TRUSTED_LIST": str(self.state / "trusted.tsv"),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.outputs()["kind"], "record", text)
            self.assertEqual(self.outputs()["flags"], " ".join(OFF), text)

    def test_an_unreadable_payload_records_and_warns(self):
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": self.payload("{ not json"),
            "YORIWAKE_MAP_RESTORED": "true",
            "YORIWAKE_HISTORY_READY": "true",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["kind"], "record")
        self.assertEqual(self.outputs()["flags"], " ".join(OFF))
        self.assertIn("::warning", result.stdout)
        self.assertTrue((self.state / "warnings").read_text().strip())

    def test_only_the_word_true_counts_as_established(self):
        for restored, ready in (("True ", "true"), ("true", ""), ("1", "true"), ("", "")):
            self.out.write_text("")
            self.run_helper("flags", {
                "GITHUB_EVENT_NAME": "pull_request",
                "GITHUB_EVENT_PATH": self.payload(pull_request()),
                "YORIWAKE_MAP_RESTORED": restored,
                "YORIWAKE_HISTORY_READY": ready,
            })
            self.assertEqual(self.outputs()["kind"], "record", (restored, ready))

    def test_deepen_reports_false_when_it_cannot_tell(self):
        result = self.run_helper("deepen", {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": self.payload(pull_request()),
            "YORIWAKE_MAP_DIR": str(self.tmp / "maps"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {"history-ready": "false"})

    def test_summary_writes_the_step_summary_and_lists_staged_observations(self):
        maps = self.tmp / "maps"
        place(maps, "test-7f007aca", {"run": FIXTURES / "observed"})
        (self.state / "warnings").write_text("the restore step failed\n")
        summary = self.tmp / "summary.md"
        result = self.run_helper("summary", {
            "GITHUB_STEP_SUMMARY": str(summary),
            "YORIWAKE_MAP_DIR": str(maps),
            "YORIWAKE_STARTED": str(int(time.time()) - 60),
            "YORIWAKE_KIND": "observe",
            "YORIWAKE_FLAGS": "-Pyoriwake.observe",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        text = summary.read_text()
        self.assertIn("the restore step failed", text)
        self.assertIn("would-be miss", text)
        self.assertEqual(self.outputs()["observations"], "true")
        self.assertTrue((self.state / "observation" / "test" / "observation.json").is_file())

    def test_summary_without_a_start_time_reads_nothing_as_this_run_s(self):
        maps = self.tmp / "maps"
        place(maps, "test-7f007aca", {"run": FIXTURES / "narrowed"})
        summary = self.tmp / "summary.md"
        result = self.run_helper("summary", {
            "GITHUB_STEP_SUMMARY": str(summary),
            "YORIWAKE_MAP_DIR": str(maps),
            "YORIWAKE_STARTED": "",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("did not run", summary.read_text())


REPO_ID = "4242"
DEFAULT_SHA = "d" * 40
KEY = "yoriwake-Linux-ci-test--map7-" + "c" * 40
NAME = "yoriwake-maps-" + hashlib.sha256(KEY.encode()).hexdigest()[:32]
DIGEST = "ab" * 32


def zipped(text, member="trusted.tsv"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


class FakeGitHub:
    """The REST endpoints `trusted` calls, with every request it received recorded.

    An artifact's zip answers with a redirect to a storage URL on the same server, as GitHub
    redirects to pre-signed storage.
    """

    def __init__(self):
        self.artifacts = []
        self.runs = {}
        self.compare = {}  # head_sha -> status, compared against DEFAULT_SHA
        self.zips = {}
        self.status = {}  # path prefix -> HTTP status to answer instead
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.requests.append((self.path, self.headers.get("Authorization")))
                for prefix, code in fake.status.items():
                    if self.path.startswith(prefix):
                        return self.answer(code, {"message": "no"})
                path = self.path.split("?")[0]
                parts = path.strip("/").split("/")
                if path == "/repos/o/r/actions/artifacts":
                    return self.answer(200, {"total_count": len(fake.artifacts),
                                             "artifacts": fake.artifacts})
                if parts[:4] == ["repos", "o", "r", "actions"] and parts[4] == "runs":
                    run = fake.runs.get(parts[5])
                    return self.answer(200 if run else 404, run or {})
                if path.startswith("/repos/o/r/git/ref/heads/"):
                    return self.answer(200, {"object": {"sha": DEFAULT_SHA, "type": "commit"}})
                if parts[:4] == ["repos", "o", "r", "compare"]:
                    base, _, head = parts[4].partition("...")
                    status = fake.compare.get(base) if head == DEFAULT_SHA else None
                    return self.answer(200 if status else 404, {"status": status})
                if parts[:4] == ["repos", "o", "r", "actions"] and parts[-1] == "zip":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{fake.port}/storage/{parts[5]}")
                    self.end_headers()
                    return None
                if parts[0] == "storage" and parts[1] in fake.zips:
                    body = fake.zips[parts[1]]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                return self.answer(404, {})

            def answer(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def upload(self, artifact_id, *, event="push", branch="main", repo_id=REPO_ID, sha=None,
               created="2026-10-01T00:00:00Z", text=f"test-7f007aca\t{DIGEST}\n", status=None,
               name=NAME):
        """An artifact of a run of the given event, branch and repository, newest added last."""
        sha = sha or f"{artifact_id:040x}"
        run_id = str(9000 + artifact_id)
        self.artifacts.insert(0, {
            "id": artifact_id, "name": name, "expired": False, "created_at": created,
            "workflow_run": {"id": int(run_id), "repository_id": int(REPO_ID),
                             "head_repository_id": int(repo_id), "head_branch": branch,
                             "head_sha": sha},
        })
        self.runs[run_id] = {"id": int(run_id), "event": event, "head_branch": branch,
                             "head_sha": sha}
        self.compare[sha] = status or "ahead"
        self.zips[str(artifact_id)] = zipped(text) if isinstance(text, str) else text

    def paths(self, fragment):
        return [path for path, _ in self.requests if fragment in path]


class Trusted(unittest.TestCase):
    """The trusted-map list a pull request passes, from GitHub's own record of who uploaded it."""

    def setUp(self):
        self.github = FakeGitHub()
        self.addCleanup(self.github.close)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.list = self.tmp / "trusted.tsv"
        self.list.write_text("left over\tfrom before\n")
        self.out = self.tmp / "output"
        self.out.write_text("")

    def trusted(self, event="pull_request", payload=None, matched_key=KEY, **env):
        events = self.tmp / "event.json"
        events.write_text(json.dumps(payload if payload is not None else {
            "pull_request": {"base": {"ref": "main"}},
            "repository": {"default_branch": "main", "id": int(REPO_ID)},
        }))
        full = {
            "PATH": os.environ["PATH"],
            "GITHUB_OUTPUT": str(self.out),
            "GITHUB_EVENT_NAME": event,
            "GITHUB_EVENT_PATH": str(events),
            "GITHUB_API_URL": f"http://127.0.0.1:{self.github.port}",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_REPOSITORY_ID": REPO_ID,
            "GITHUB_RUN_ID": "1",
            "GITHUB_REF_NAME": "refs/pull/7/merge",
            "YORIWAKE_STATE": str(self.tmp),
            "YORIWAKE_TRUSTED_LIST": str(self.list),
            "YORIWAKE_MATCHED_KEY": matched_key,
            "YORIWAKE_TOKEN": "secret-token",
            **env,
        }
        result = subprocess.run([sys.executable, str(Path(ya.__file__)), "trusted"], env=full,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def listed(self):
        return self.list.read_text()

    def test_a_default_branch_push_s_artifact_is_the_list(self):
        self.github.upload(1)
        self.trusted()
        self.assertEqual(self.listed(), f"test-7f007aca\t{DIGEST}\n")

    def test_a_pull_request_run_s_artifact_of_the_same_name_is_skipped(self):
        self.github.upload(1, event="pull_request")
        out = self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertIn("::warning", out)
        self.assertEqual(self.github.paths("/zip"), [])

    def test_a_fork_s_branch_named_like_the_default_is_filtered_without_asking_for_its_run(self):
        self.github.upload(1, repo_id="777")
        self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertEqual(self.github.paths("/runs/"), [])

    def test_a_tag_named_like_the_default_branch_at_a_commit_off_it_is_skipped(self):
        self.github.upload(1, status="diverged")
        self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertEqual(len(self.github.paths("/compare/")), 1)

    def test_a_push_at_a_commit_the_default_branch_does_not_contain_is_skipped(self):
        self.github.upload(1, status="behind")
        self.trusted()
        self.assertEqual(self.listed(), "")

    def test_a_commit_the_default_branch_points_at_is_contained(self):
        self.github.upload(1, status="identical")
        self.trusted()
        self.assertEqual(self.listed(), f"test-7f007aca\t{DIGEST}\n")

    def test_the_default_branch_is_compared_by_its_commit_never_by_a_name_a_tag_could_share(self):
        self.github.upload(1)
        self.trusted()
        self.assertEqual(self.github.paths("/compare/"),
                         [f"/repos/o/r/compare/{1:040x}...{DEFAULT_SHA}?per_page=1"])

    def test_an_artifact_whose_run_reports_another_commit_is_skipped_before_any_compare(self):
        self.github.upload(1)
        self.github.runs[str(9000 + 1)]["head_sha"] = "e" * 40
        self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertEqual(self.github.paths("/compare/"), [])
        self.assertEqual(self.github.paths("/zip"), [])

    def test_a_failing_compare_skips_that_candidate(self):
        self.github.upload(1)
        self.github.status["/repos/o/r/compare/"] = 500
        self.trusted()
        self.assertEqual(self.listed(), "")

    def test_a_dispatched_run_s_artifact_is_skipped_on_a_pull_request(self):
        self.github.upload(1, event="workflow_dispatch")
        self.trusted()
        self.assertEqual(self.listed(), "")

    def test_the_newest_trusted_candidate_wins_over_newer_untrusted_ones(self):
        self.github.upload(1, created="2026-10-01T00:00:00Z", text=f"older\t{DIGEST}\n")
        self.github.upload(2, created="2026-10-02T00:00:00Z", text=f"newest\t{'cd' * 32}\n")
        self.github.upload(3, event="pull_request", created="2026-10-03T00:00:00Z")
        self.github.upload(4, created="2026-10-04T00:00:00Z", status="diverged")
        self.trusted()
        self.assertEqual(self.listed(), f"newest\t{'cd' * 32}\n")

    def test_many_uploads_under_the_trusted_name_cost_a_bounded_number_of_requests(self):
        self.github.upload(1, created="2026-09-01T00:00:00Z")
        for n in range(2, 102):
            self.github.upload(n, event="pull_request", created=f"2026-10-01T00:00:{n % 60:02d}Z")
        self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertEqual(len(self.github.paths("/actions/artifacts?")), 1)
        self.assertLessEqual(len(self.github.paths("/runs/")) + len(self.github.paths("/compare/")),
                             10)
        self.assertEqual(self.github.paths("/zip"), [])

    def test_a_token_without_actions_read_leaves_the_list_empty_and_names_the_permission(self):
        self.github.upload(1)
        self.github.status["/repos/o/r/actions/artifacts"] = 403
        out = self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertIn("actions: read", out)

    def test_no_artifact_leaves_the_list_empty_and_warns(self):
        out = self.trusted()
        self.assertEqual(self.listed(), "")
        self.assertIn("::warning", out)

    def test_no_matched_key_leaves_the_list_empty_without_a_request(self):
        self.github.upload(1)
        self.trusted(matched_key="")
        self.assertEqual(self.listed(), "")
        self.assertEqual(self.github.requests, [])

    def test_an_artifact_named_for_another_key_is_not_this_entry_s(self):
        self.github.upload(1)
        self.trusted(matched_key=KEY + "-other")
        self.assertEqual(self.listed(), "")
        self.assertEqual(self.github.paths("/zip"), [])

    def test_a_malformed_zip_leaves_the_list_empty(self):
        self.github.upload(1, text=b"not a zip at all")
        self.trusted()
        self.assertEqual(self.listed(), "")

    def test_malformed_lines_in_the_artifact_are_dropped(self):
        self.github.upload(1, text=f"good\t{DIGEST}\nbad\tnot-hex\n\tno-name\nextra\t{DIGEST}\tx\n")
        self.trusted()
        self.assertEqual(self.listed(), f"good\t{DIGEST}\n")

    def test_the_token_goes_to_the_api_and_never_to_the_storage_redirect(self):
        self.github.upload(1)
        self.trusted()
        sent = dict(self.github.requests)
        self.assertEqual(sent["/repos/o/r/actions/artifacts/1/zip"], "Bearer secret-token")
        self.assertIsNone(sent["/storage/1"])
        self.assertEqual(self.listed(), f"test-7f007aca\t{DIGEST}\n")

    def test_an_event_other_than_a_pull_request_writes_an_empty_list_and_asks_nothing(self):
        self.github.upload(1)
        self.trusted(event="push", payload={"ref": "refs/heads/main"})
        self.assertEqual(self.listed(), "")
        self.assertEqual(self.github.requests, [])

    def test_the_default_branch_input_names_the_trusted_branch(self):
        self.github.upload(1, branch="trunk")
        self.github.upload(2, branch="main", created="2026-10-02T00:00:00Z")
        self.trusted(YORIWAKE_DEFAULT_BRANCH="trunk")
        self.assertEqual(self.listed(), f"test-7f007aca\t{DIGEST}\n")
        self.assertEqual(self.github.paths("/git/ref/"), ["/repos/o/r/git/ref/heads/trunk"])
        self.assertEqual(self.github.paths("/zip"), ["/repos/o/r/actions/artifacts/1/zip"])

    def test_a_dispatched_test_trusts_only_its_own_run_s_upload(self):
        self.github.upload(1, event="workflow_dispatch", created="2026-10-01T00:00:00Z",
                           text=f"own\t{DIGEST}\n")
        self.github.upload(2, event="workflow_dispatch", created="2026-10-02T00:00:00Z")
        self.github.upload(3, event="push", created="2026-10-03T00:00:00Z")
        self.trusted(event="workflow_dispatch", GITHUB_RUN_ID="9001", GITHUB_REF_NAME="main",
                     YORIWAKE_EVENT_NAME="pull_request")
        self.assertEqual(self.listed(), f"own\t{DIGEST}\n")


class Digests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.maps = self.tmp / "maps"
        self.out = self.tmp / "output"
        self.out.write_text("")

    def digests(self, key=KEY):
        result = subprocess.run(
            [sys.executable, str(Path(ya.__file__)), "digests"],
            env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(self.out),
                 "YORIWAKE_STATE": str(self.tmp / "state"), "YORIWAKE_MAP_DIR": str(self.maps),
                 "YORIWAKE_CACHE_KEY": key},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in self.out.read_text().splitlines() if line)

    def test_every_map_s_digest_is_staged_under_the_name_the_key_gives(self):
        place(self.maps, "test-7f007aca", {"map-digest": f"sha256 {DIGEST}\n"})
        place(self.maps, "integrationTest-0badf00d", {"map-digest": f"sha256 {'cd' * 32}\n"})
        place(self.maps, "unfinished-11111111", {"coverage.tsv": ""})
        place(self.maps, "garbled-22222222", {"map-digest": "md5 abc\n"})
        outputs = self.digests()
        self.assertEqual(outputs["artifact-name"], NAME)
        self.assertEqual(outputs["count"], "2")
        self.assertEqual((Path(outputs["path"]) / "trusted.tsv").read_text(),
                         f"integrationTest-0badf00d\t{'cd' * 32}\ntest-7f007aca\t{DIGEST}\n")

    def test_no_digest_stages_nothing(self):
        place(self.maps, "test-7f007aca", {"coverage.tsv": ""})
        self.assertEqual(self.digests()["count"], "0")

    def test_what_digests_stages_is_what_trusted_reads(self):
        place(self.maps, "test-7f007aca", {"map-digest": f"sha256 {DIGEST}\n"})
        staged = (Path(self.digests()["path"]) / "trusted.tsv").read_text()
        self.assertEqual(ya.parse_trusted_list(staged), [f"test-7f007aca\t{DIGEST}"])


def action_step(step_id):
    """One step of action.yml, as its lines, from `- id: <step_id>` to the next step."""
    lines = (Path(__file__).resolve().parent.parent / "action.yml").read_text().splitlines()
    start = lines.index(f"    - id: {step_id}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("    - ")),
               len(lines))
    return lines[start:end]


class UploadConditions(unittest.TestCase):
    """The digests are uploaded exactly when the save wrote an entry: never on an exact-key hit,
    whose save writes nothing, and also when the tests failed."""

    def condition(self, step_id):
        return next(line for line in action_step(step_id) if line.strip().startswith("if:"))

    def test_the_digests_follow_a_save_that_wrote(self):
        condition = self.condition("digests")
        self.assertIn("!cancelled()", condition)
        self.assertIn("steps.save.outcome == 'success'", condition)
        self.assertIn("steps.restore.outputs.cache-hit != 'true'", condition)
        self.assertNotIn("success()", condition)

    def test_the_save_follows_the_head_check(self):
        self.assertIn("steps.save-check.outputs.save == 'true'", self.condition("save"))

    def test_the_upload_never_overwrites(self):
        upload = action_step("upload-digests")
        self.assertIn("steps.digests.outputs.count", self.condition("upload-digests"))
        self.assertFalse(any("overwrite" in line for line in upload))
        self.assertTrue(any("continue-on-error: true" in line for line in upload))

    def test_the_upload_keeps_the_digests_for_the_retention_days_input(self):
        self.assertIn("        retention-days: ${{ inputs.retention-days }}",
                      action_step("upload-digests"))
        lines = (Path(__file__).resolve().parent.parent / "action.yml").read_text().splitlines()
        at = lines.index("  retention-days:")
        default = next(line for line in lines[at:] if line.strip().startswith("default:"))
        self.assertEqual(default.strip(), "default: '90'")


class CachedPaths(unittest.TestCase):
    """What the cache holds. `selection.tsv` leaves tests out of a complement run and no digest
    covers it, so it never comes from a cache; restore and save name the same paths, or a saved
    entry is never restored."""

    @staticmethod
    def paths(lines):
        start = next(i for i, line in enumerate(lines) if line.strip() == "path: |") + 1
        return [line.strip() for line in lines[start:start + 3]]

    def test_restore_and_save_leave_out_raw_records_and_the_selection_record(self):
        restore = self.paths(action_step("restore"))
        self.assertEqual(restore, [
            "${{ steps.setup.outputs.map-dir }}",
            "!${{ steps.setup.outputs.map-dir }}/*/raw",
            "!${{ steps.setup.outputs.map-dir }}/*/selection.tsv",
        ])
        self.assertEqual(self.paths(action_step("save")), restore)

    def test_a_restored_selection_record_is_removed_before_the_run(self):
        # The path list does not filter what a restore extracts, so an entry can still carry one.
        step = action_step("clear")
        at = next(i for i, line in enumerate(step) if line.strip().startswith("rm -f"))
        end = next(i for i in range(at, len(step)) if not step[i].endswith("\\"))
        self.assertIn('"$MAP_DIR"/*/selection.tsv', " ".join(step[at:end + 1]))

    def test_the_test_workflow_plants_entries_under_the_action_s_paths(self):
        lines = (Path(__file__).resolve().parent.parent / ".github" / "workflows"
                 / "test.yml").read_text().splitlines()
        saves = [i for i, line in enumerate(lines) if "actions/cache/save@" in line]
        self.assertTrue(saves)
        for at in saves:
            self.assertEqual(self.paths(lines[at:]), [
                "fixture/.gradle/yoriwake",
                "!fixture/.gradle/yoriwake/*/raw",
                "!fixture/.gradle/yoriwake/*/selection.tsv",
            ])


def gradle_step_script():
    """The `run` block of action.yml's gradle step, as bash receives it."""
    lines = (Path(__file__).resolve().parent.parent / "action.yml").read_text().splitlines()
    start = lines.index("    - id: gradle")
    body_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |") + 1
    body = []
    for line in lines[body_at:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body) + "\n"


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class GradleStepDropsOwnedFlags(unittest.TestCase):
    """Only the gradle step's bash keeps the user's yoriwake flags off the real Gradle command,
    and it must drop exactly what `owned_flags` names, or a run that records could select."""

    CHOSEN = "-Pyoriwake.select -Pyoriwake.observe=false -Pyoriwake.base=origin/main"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        gradlew = self.tmp / "gradlew"
        gradlew.write_text('#!/bin/sh\nfor arg in "$@"; do echo "$arg"; done > argv\n')
        gradlew.chmod(0o755)
        self.script = self.tmp / "gradle-step.sh"
        self.script.write_text(gradle_step_script())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def gradle_argv(self, tasks, gradle_args, outcome="success"):
        result = subprocess.run(
            ["bash", "-e", str(self.script)],
            env={"PATH": os.environ["PATH"], "STATE": "", "TASKS": tasks,
                 "GRADLE_ARGS": gradle_args, "FLAGS_OUTCOME": outcome, "FLAGS": self.CHOSEN},
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.tmp / "argv").read_text().splitlines()

    def test_owned_flags_never_reach_gradle_and_the_run_records(self):
        for tasks, gradle_args in (
            ("test", "-Pyoriwake.select"),
            ("test", "--info -Pyoriwake.base=origin/x"),
            ("test", "-P yoriwake.observe"),
            ("test", "--project-prop yoriwake.complement=true"),
            ("test", "--project-prop=yoriwake.fullRun"),
            ("test -Pyoriwake.complement", ""),
            ("test", "-Dorg.gradle.project.yoriwake.select=true"),
            ("test", "-Pyoriwake.trustedMaps=/tmp/mine.tsv"),
            ("test", "--project-prop yoriwake.trustedMaps=/tmp/mine.tsv"),
        ):
            owned = ya.owned_flags(tasks, gradle_args)
            self.assertTrue(owned, gradle_args)
            kept = [arg for arg in f"{tasks} {gradle_args}".split()
                    if arg not in owned and arg not in ("-P", "--project-prop")]
            self.assertEqual(self.gradle_argv(tasks, gradle_args), [*kept, *OFF], gradle_args)

    def test_flags_that_could_not_be_chosen_turn_selection_off(self):
        for outcome in ("failure", "skipped", ""):
            self.assertEqual(self.gradle_argv("test", "--info", outcome),
                             ["test", "--info", *OFF], outcome)

    def test_near_misses_reach_gradle_with_the_chosen_flags(self):
        for gradle_args in ("-Pyoriwake.selectX", "-Pyoriwake.isolatedCapture",
                            "-Pyoriwake.alwaysRun=a.B", "-P yoriwake.baseline=1"):
            self.assertEqual(ya.owned_flags("test", gradle_args), [], gradle_args)
            self.assertEqual(self.gradle_argv("test", gradle_args),
                             ["test", *gradle_args.split(), *self.CHOSEN.split()], gradle_args)


if __name__ == "__main__":
    unittest.main()

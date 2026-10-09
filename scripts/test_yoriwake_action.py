"""Tests for the helper the action runs: run `python3 scripts/test_yoriwake_action.py`.

The decision-record and observation fixtures under `fixtures/` were written by real runs of the
plugin on its demo sample, so a change in what the plugin writes shows up here as a failing read.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yoriwake_action as ya  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


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
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", [], True))
        self.assertEqual(decision.warnings, [])

    def test_a_recording_run_asks_for_isolated_capture_when_the_input_is_set(self):
        decision = choose("push", push(), isolated_capture=True)
        self.assertEqual(decision.flags, ["-Pyoriwake.isolatedCapture"])
        self.assertTrue(decision.save)

    def test_a_schedule_records_and_saves(self):
        decision = choose("schedule", {"schedule": "0 3 * * *"}, isolated_capture=True)
        self.assertEqual((decision.kind, decision.flags, decision.save),
                         ("record", ["-Pyoriwake.isolatedCapture"], True))

    def test_the_default_branch_input_wins_over_the_payload(self):
        decision = choose("push", push(ref="refs/heads/trunk", default="main"),
                          default_branch="trunk")
        self.assertTrue(decision.save)

    def test_a_push_to_another_branch_records_saves_nothing_and_warns(self):
        decision = choose("push", push(ref="refs/heads/feature/x"))
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", [], False))
        self.assertIn("refs/heads/feature/x", decision.warnings[0])

    def test_a_push_whose_default_branch_is_unknown_saves_nothing(self):
        decision = choose("push", {"ref": "refs/heads/main"})
        self.assertFalse(decision.save)
        self.assertTrue(decision.warnings)

    def test_a_pull_request_selects_against_its_base(self):
        decision = choose("pull_request", pull_request(base="release/1.x"))
        self.assertEqual(decision.kind, "select")
        self.assertEqual(decision.flags,
                         ["-Pyoriwake.select", "-Pyoriwake.base=origin/release/1.x"])
        self.assertFalse(decision.save)
        self.assertEqual(decision.warnings, [])

    def test_a_pull_request_observes_instead_when_asked_and_never_both(self):
        decision = choose("pull_request", pull_request(), observe=True)
        self.assertEqual(decision.kind, "observe")
        self.assertIn("-Pyoriwake.observe", decision.flags)
        self.assertNotIn("-Pyoriwake.select", decision.flags)
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
        self.assertEqual((decision.kind, decision.flags, decision.save), ("record", [], False))
        self.assertIn("no map", decision.warnings[0])

    def test_a_pull_request_whose_history_is_not_ready_records_and_warns(self):
        for ready in (False, None):
            decision = choose("pull_request", pull_request(), observe=True, history_ready=ready)
            self.assertEqual((decision.kind, decision.flags), ("record", []))
            self.assertIn("history", decision.warnings[0])

    def test_a_pull_request_without_a_base_records(self):
        for payload in ({}, {"pull_request": {"base": {}}}, pull_request(base="bad..name"),
                        pull_request(base="-x"), None):
            decision = choose("pull_request", payload)
            self.assertEqual((decision.kind, decision.flags), ("record", []), payload)
            self.assertTrue(decision.warnings)

    def test_a_matrix_job_without_a_key_records_and_names_the_input(self):
        decision = choose("pull_request", pull_request(), job_total=3)
        self.assertEqual((decision.kind, decision.flags), ("record", []))
        self.assertIn("`key`", decision.warnings[0])
        self.assertEqual(choose("pull_request", pull_request(), job_total=3, key="jdk17").kind,
                         "select")

    def test_flags_the_action_owns_in_the_inputs_make_the_run_record(self):
        for tasks, gradle_args in (
            ("test", "-Pyoriwake.select"),
            ("test", "--info -Pyoriwake.base=origin/x"),
            ("test", "-P yoriwake.observe"),
            ("test", "--project-prop=yoriwake.fullRun"),
            ("test -Pyoriwake.complement", ""),
            ("test", "-Dorg.gradle.project.yoriwake.select=true"),
        ):
            decision = choose("pull_request", pull_request(), tasks=tasks, gradle_args=gradle_args)
            self.assertEqual((decision.kind, decision.flags), ("record", []), gradle_args)
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
                             ("record", [], False), event)
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

    def payload(self, data):
        path = self.tmp / "event.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return str(path)

    def test_flags_reads_the_event_from_the_environment(self):
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": self.payload(pull_request(labels=["yoriwake:full-run"])),
            "YORIWAKE_MAP_RESTORED": "true",
            "YORIWAKE_HISTORY_READY": "true",
            "YORIWAKE_JOB_TOTAL": "1",
            "YORIWAKE_TASKS": "test",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs(), {
            "kind": "select",
            "flags": "-Pyoriwake.select -Pyoriwake.base=origin/main -Pyoriwake.fullRun",
            "save": "false",
        })

    def test_an_overriding_event_is_read_before_the_runner_s(self):
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_EVENT_PATH": self.payload({}),
            "YORIWAKE_EVENT_NAME": "push",
            "YORIWAKE_EVENT_PATH": self.payload(push()),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["save"], "true")

    def test_an_unreadable_payload_records_and_warns(self):
        result = self.run_helper("flags", {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": self.payload("{ not json"),
            "YORIWAKE_MAP_RESTORED": "true",
            "YORIWAKE_HISTORY_READY": "true",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.outputs()["kind"], "record")
        self.assertEqual(self.outputs()["flags"], "")
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


if __name__ == "__main__":
    unittest.main()

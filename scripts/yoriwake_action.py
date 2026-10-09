"""The decisions the yoriwake action makes outside Gradle.

Three subcommands, each run by one step of `action.yml` and each reading its facts from the
environment:

- `deepen`: fetch the pull request's base and deepen a shallow clone until selection has a base
  the restored maps are related to. Answers `history-ready`.
- `flags`: choose the run's Gradle flags from the event. Answers `kind`, `flags` and `save`.
- `summary`: write the job summary from the files this run's test tasks wrote, and stage each
  observation report for upload.

Every path that cannot establish what selection needs ends in a run that selects nothing: only
the exact word `true` counts as an established map or history, and anything this script cannot
read becomes a warning, never a selection flag.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

FULL_RUN_LABEL = "yoriwake:full-run"

# The flags this action sets. One named in the action's inputs would change what runs without
# the action knowing, so such a run records instead.
OWNED_FLAG = re.compile(r"yoriwake\.(select|observe|complement|fullRun|base)(=|$)")

# Deepening steps for a shallow clone, after the base's own tip: commits from each tip, then all.
DEPTHS = (1, 50, 500, None)

STAMP = re.compile(r"[0-9a-fA-F]{7,40}")
MAP_DIR_SUFFIX = re.compile(r"-[0-9a-f]{8}$")


class Decision(NamedTuple):
    kind: str
    flags: list
    save: bool
    warnings: list


class History(NamedTuple):
    ready: bool
    steps: int
    notes: list


def is_branch_name(name) -> bool:
    """A name git accepts as a branch and that cannot be read as an option or a refspec."""
    if not isinstance(name, str) or not name or name.startswith(("-", "/", ".")):
        return False
    if name.endswith(("/", ".", ".lock")) or ".." in name or "//" in name or "@{" in name:
        return False
    if name == "@" or "/." in name:
        return False
    return not any(ch in name for ch in " ~^:?*[\\") and all(31 < ord(ch) != 127 for ch in name)


def pull_request_base(payload):
    try:
        base = payload["pull_request"]["base"]["ref"]
    except (KeyError, TypeError):
        return None
    return base if is_branch_name(base) else None


def labels(payload):
    try:
        return {label["name"] for label in payload["pull_request"]["labels"]}
    except (KeyError, TypeError):
        return set()


def owned_flags(tasks: str, gradle_args: str) -> list:
    return [token for token in f"{tasks} {gradle_args}".split() if OWNED_FLAG.search(token)]


def choose_flags(event, payload, *, observe, isolated_capture, default_branch, map_restored,
                 history_ready, job_total, key, tasks, gradle_args) -> Decision:
    """The run kind, its Gradle flags, and whether the map may be saved, for one event."""
    capture = ["-Pyoriwake.isolatedCapture"] if isolated_capture else []

    if event == "schedule":
        return Decision("record", capture, True, [])

    if event == "push":
        ref = payload.get("ref") if isinstance(payload, dict) else None
        default = default_branch
        if not default and isinstance(payload, dict):
            default = (payload.get("repository") or {}).get("default_branch") or ""
        if default and ref == f"refs/heads/{default}":
            return Decision("record", capture, True, [])
        if not default:
            why = "the default branch is unknown (set the `default-branch` input)"
        else:
            why = f"{ref} is not the default branch {default}"
        return Decision("record", [], False, [
            f"a push to {ref}: {why}, so this run records and saves no map"])

    if event != "pull_request":
        return Decision("record", [], False, [
            f"the event {event or '(none)'} neither selects nor saves a map: this run records. "
            "The action selects on pull_request and saves on a push to the default branch "
            "or a schedule"])

    def record(why):
        return Decision("record", [], False, [f"{why}, so this run records every test"])

    named = owned_flags(tasks, gradle_args)
    if named:
        return record(f"the inputs name {' '.join(named)}; the action owns those flags")
    if job_total > 1 and not key:
        return record(f"this job is one of {job_total} in a matrix and the `key` input is empty, "
                      "so its legs would share one map")
    base = pull_request_base(payload)
    if base is None:
        return record("the pull request's base branch could not be read from the event")
    if not map_restored:
        return record("no map was restored (a cache miss, or the restore failed)")
    if history_ready is not True:
        return record(f"the history is not ready: no merge base with origin/{base}, or a "
                      "restored map's capture commit is not on it")

    flags = ["-Pyoriwake.observe" if observe else "-Pyoriwake.select",
             f"-Pyoriwake.base=origin/{base}"]
    if FULL_RUN_LABEL in labels(payload):
        flags.append("-Pyoriwake.fullRun")
    return Decision("observe" if observe else "select", flags, False, [])


def _git(cwd, *args):
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                timeout=1800)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return result.returncode, (result.stdout if result.returncode == 0 else result.stderr).strip()


def capture_stamps(map_dir: Path):
    """Each task map's capture commit, as written; a map without one has nothing to relate."""
    stamps = {}
    if not map_dir.is_dir():
        return stamps
    for stamp_file in sorted(map_dir.glob("*/capture-commit")):
        try:
            stamps[stamp_file.parent.name] = stamp_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            stamps[stamp_file.parent.name] = ""
    return stamps


def deepen(base, map_dir, cwd) -> History:
    """Fetch `base` into origin/<base> and deepen until selection has a base the maps relate to.

    Ready when HEAD and origin/<base> have a merge base and every restored map's capture commit
    is an ancestor of origin/<base>. A shallow clone deepens both tips in steps, then
    unshallows; a full clone fetches the base once.
    """
    notes = []
    if not is_branch_name(base):
        return History(False, 0, [f"the base {base!r} is not a branch name"])
    code, shallow = _git(cwd, "rev-parse", "--is-shallow-repository")
    if code != 0:
        return History(False, 0, [f"not a git repository: {shallow}"])
    code, head = _git(cwd, "rev-parse", "HEAD")
    if code != 0:
        return History(False, 0, [f"HEAD does not resolve: {head}"])
    remote = f"refs/remotes/origin/{base}"
    refspec = f"+refs/heads/{base}:{remote}"
    stamps = capture_stamps(Path(map_dir))

    def ready():
        code, _ = _git(cwd, "merge-base", "HEAD", remote)
        if code != 0:
            return f"no merge base between HEAD and origin/{base}"
        for task, stamp in stamps.items():
            if not STAMP.fullmatch(stamp):
                return f"the map {task} has a capture stamp that is not a commit"
            code, _ = _git(cwd, "merge-base", "--is-ancestor", stamp, remote)
            if code != 0:
                return f"the map {task} was captured at {stamp[:12]}, which is not on origin/{base}"
        return None

    depths = DEPTHS if shallow == "true" else ("full",)
    step = 0
    for step, depth in enumerate(depths, start=1):
        if depth is None and _git(cwd, "rev-parse", "--is-shallow-repository")[1] != "true":
            notes.append(f"step {step}: the clone is already complete")
            break
        if depth == "full":
            options = []
        elif depth is None:
            options = ["--unshallow"]
        else:
            options = [f"--depth={depth}"]
        code, out = _git(cwd, "fetch", "--no-tags", "--quiet", *options, "origin", refspec)
        if code != 0:
            notes.append(f"fetching {base} from origin failed: {out}")
            return History(False, step, notes)
        if depth not in ("full", 1):
            # HEAD may be a merge commit no branch holds, so its own history is fetched by id.
            code, out = _git(cwd, "fetch", "--no-tags", "--quiet", *options, "origin", head)
            if code != 0:
                notes.append(f"deepening HEAD failed: {out}")
        missing = ready()
        if missing is None:
            notes.append(f"history ready after step {step}")
            return History(True, step, notes)
        notes.append(f"step {step}: {missing}")
    return History(False, step, notes)


# --- summary -------------------------------------------------------------------------------------


def cell(text) -> str:
    """Text from a file, safe inside a markdown table cell."""
    return re.sub(r"[^A-Za-z0-9 ._:;,=/()+-]", "?", str(text)).strip()


def task_label(dir_name: str) -> str:
    return MAP_DIR_SUFFIX.sub("", dir_name)


class Record(NamedTuple):
    notes: dict
    run: set
    skipped: set


def read_decisions(paths) -> Record | None:
    """Merge decision records: a test any writer included ran; one every writer excluded did not."""
    notes, verdicts, readable = {}, {}, False
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if line.startswith("#!"):
                    key, _, value = line[2:].partition("\t")
                    notes.setdefault(key, value)
                    readable = readable or key == "record-version" or key == "outcome"
                elif line and not line.startswith("#"):
                    fields = line.split("\t")
                    if len(fields) != 3 or fields[1] not in ("included", "excluded"):
                        return None
                    verdicts.setdefault(fields[0], set()).add(fields[1])
    if not readable:
        return None
    # A record lists containers (an engine, a class) beside their tests; count only the leaves.
    containers = {test[:at + 1] for test in verdicts
                  for at in range(len(test)) if test.startswith("]/[", at)}
    leaves = {test: seen for test, seen in verdicts.items() if test not in containers}
    run = {test for test, seen in leaves.items() if "included" in seen}
    return Record(notes, run, set(leaves) - run)


def describe_run(notes: dict):
    """The run column and the forced-or-declined column for one task."""
    outcome = notes.get("outcome", "")
    why = notes.get("refusal-kind") or notes.get("full-run-kind") or ""
    declines = notes.get("declines", "")
    if "observed-outcome" in notes:
        observed = notes["observed-outcome"].split("\t")
        kind = "observed: would have " + (
            "narrowed" if observed[0] == "narrowed" else "run everything")
        why = next((token for token in reversed(observed[1:]) if token), "")
    elif outcome == "selection-not-requested":
        kind = "recorded"
    elif outcome == "narrowed":
        kind = "narrowed"
    elif outcome == "full-run":
        kind = "ran everything"
    elif outcome == "not-decided":
        kind = "not decided"
    else:
        kind = outcome or "unknown"
    reasons = [why] if why else []
    reasons += [d for d in declines.split(",") if d and d not in reasons]
    return kind, ", ".join(reasons)


def seconds(nanos) -> str:
    value = nanos / 1e9
    return f"{value:.3f}" if value < 1 else f"{value:.1f}"


def describe_observation(data: dict) -> str:
    outcome = data.get("observedOutcome")
    if outcome == "narrowed":
        head = "selection would have narrowed"
    else:
        why = data.get("refusalKind") or data.get("fullRunKind") or "no reason given"
        head = f"selection would have run everything ({cell(why)})"
    misses = len(data.get("wouldBeMisses") or [])
    parts = [
        head,
        f"{int(data.get('failures', 0))} failed, {int(data.get('failuresKept', 0))} of them kept",
        f"{misses} would-be miss" + ("" if misses == 1 else "es"),
        f"{int(data.get('wouldBeSkipped', 0))} tests would have been skipped, "
        f"{seconds(int(data.get('recordedInstrumentedTestNanos', 0)))} s of recorded test time "
        "(instrumented, summed over forks; not a wall-clock saving)",
    ]
    if data.get("complete") is False:
        parts.append(f"{int(data.get('testsWithoutVerdict', 0))} tests ran with no verdict")
    if data.get("note"):
        parts.append(cell(data["note"]))
    return "; ".join(parts)


def fresh(path: Path, since: float | None) -> bool:
    try:
        return since is not None and path.is_file() and path.stat().st_mtime >= since
    except OSError:
        return False


def summarise(map_dir, since, commit, kind, flags, warnings, stage_dir):
    """The job summary's markdown, and the observation reports staged for upload."""
    map_dir, stage_dir = Path(map_dir), Path(stage_dir)
    lines = ["### yoriwake", ""]
    asked = f"**{kind}**" if kind else "**record** (no flags were chosen)"
    lines.append(f"Commit `{cell(commit)}`. Run kind the action asked for: {asked}"
                 + (f", with `{cell(flags)}`." if flags else "."))
    lines.append("")
    staged, observations, unreadable = [], [], []
    task_dirs = sorted(p for p in map_dir.iterdir() if p.is_dir()) if map_dir.is_dir() else []
    if not task_dirs:
        lines.append(f"No map directory under `{cell(map_dir)}`: no test task applied the plugin, "
                     "or none ran.")
    else:
        lines += ["| Task | Map version | Run | Forced or declined | Tests run | Tests skipped |",
                  "|---|---|---|---|---|---|"]
    for task_dir in task_dirs:
        label = task_label(task_dir.name)
        version = ""
        try:
            version = (task_dir / "schema-version").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            pass
        if version and not version.isdigit():
            unreadable.append(f"`{cell(label)}`: schema-version could not be read")
            version = "?"
        parts = [p for p in sorted(task_dir.glob("decisions.tsv.*.part")) if fresh(p, since)]
        if not parts and fresh(task_dir / "decisions.tsv", since):
            parts = [task_dir / "decisions.tsv"]
        observation = task_dir / "observation.json"
        row = [f"`{cell(label)}`", cell(version)]
        if parts:
            try:
                record = read_decisions(parts)
            except (OSError, UnicodeDecodeError):
                record = None
            if record is None:
                unreadable.append(f"`{cell(label)}`: the decision record could not be read")
                row += ["could not be read", "", "", ""]
            else:
                run, why = describe_run(record.notes)
                row += [cell(run), cell(why), str(len(record.run)), str(len(record.skipped))]
        else:
            row += ["did not run", "", "", ""]
        lines.append("| " + " | ".join(row) + " |")
        if fresh(observation, since):
            try:
                data = json.loads(observation.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                observations.append(f"- `{cell(label)}`: {describe_observation(data)}.")
                target = stage_dir / cell(label).replace(" ", "_") / "observation.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(observation, target)
                staged.append(target)
            except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
                unreadable.append(f"`{cell(label)}`: observation.json could not be read "
                                  f"({cell(error)})")
    if observations:
        lines += ["", "Observed:", ""] + observations
    if unreadable:
        lines += ["", "Unreadable:", ""] + [f"- {item}" for item in unreadable]
    if warnings:
        lines += ["", "Warnings:", ""] + [f"- {cell(w)}" for w in warnings]
    return "\n".join(lines) + "\n", staged


# --- the steps -----------------------------------------------------------------------------------


def state_dir() -> Path | None:
    path = os.environ.get("YORIWAKE_STATE")
    return Path(path) if path else None


def warn(message: str):
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title=yoriwake::{escaped}", flush=True)
    state = state_dir()
    if state is not None:
        try:
            with open(state / "warnings", "a", encoding="utf-8") as handle:
                handle.write(message.replace("\n", " ") + "\n")
        except OSError:
            pass


def set_outputs(**values):
    text = "".join(f"{name.replace('_', '-')}={value}\n" for name, value in values.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)


def event():
    """The event's name and payload. A test of the action may supply both instead of the runner's."""
    name = os.environ.get("YORIWAKE_EVENT_NAME") or os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("YORIWAKE_EVENT_PATH") or os.environ.get("GITHUB_EVENT_PATH", "")
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as error:
        warn(f"the event payload could not be read ({error})")
        payload = None
    return name, payload


def established(name: str) -> bool:
    return os.environ.get(name, "") == "true"


def run_flags():
    name, payload = event()
    try:
        job_total = int(os.environ.get("YORIWAKE_JOB_TOTAL") or "1")
    except ValueError:
        job_total = 1
    decision = choose_flags(
        name, payload,
        observe=established("YORIWAKE_OBSERVE"),
        isolated_capture=established("YORIWAKE_ISOLATED_CAPTURE"),
        default_branch=os.environ.get("YORIWAKE_DEFAULT_BRANCH", "").strip(),
        map_restored=established("YORIWAKE_MAP_RESTORED"),
        history_ready=True if established("YORIWAKE_HISTORY_READY") else None,
        job_total=job_total,
        key=os.environ.get("YORIWAKE_KEY", "").strip(),
        tasks=os.environ.get("YORIWAKE_TASKS", ""),
        gradle_args=os.environ.get("YORIWAKE_GRADLE_ARGS", ""),
    )
    for message in decision.warnings:
        warn(message)
    print(f"yoriwake: {decision.kind} {' '.join(decision.flags)}".rstrip())
    set_outputs(kind=decision.kind, flags=" ".join(decision.flags),
                save="true" if decision.save else "false")


def run_deepen():
    name, payload = event()
    if name != "pull_request":
        return
    ready = False
    try:
        base = pull_request_base(payload)
        history = deepen(base, Path(os.environ.get("YORIWAKE_MAP_DIR", ".gradle/yoriwake")),
                         os.getcwd())
        for note in history.notes:
            print(f"yoriwake: {note}")
        ready = history.ready
    finally:
        set_outputs(history_ready="true" if ready else "false")


def run_summary():
    started = os.environ.get("YORIWAKE_STARTED", "")
    since = float(started) if started.replace(".", "", 1).isdigit() else None
    warnings = []
    state = state_dir()
    if state is not None and (state / "warnings").is_file():
        warnings = [line for line in (state / "warnings").read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    code, commit = _git(os.getcwd(), "rev-parse", "HEAD")
    if code != 0:
        commit = os.environ.get("GITHUB_SHA", "unknown")
    stage = (state or Path(".")) / "observation"
    text, staged = summarise(
        Path(os.environ.get("YORIWAKE_MAP_DIR", ".gradle/yoriwake")), since, commit,
        os.environ.get("YORIWAKE_KIND", ""), os.environ.get("YORIWAKE_FLAGS", ""), warnings, stage)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    set_outputs(observations="true" if staged else "false", observation_dir=str(stage))


COMMANDS = {"flags": run_flags, "deepen": run_deepen, "summary": run_summary}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: {Path(argv[0]).name} {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    COMMANDS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

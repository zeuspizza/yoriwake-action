"""The decisions the yoriwake action makes outside Gradle.

Five subcommands, each run by one step of `action.yml` and each reading its facts from the
environment:

- `deepen`: fetch the pull request's base and deepen a shallow clone until selection has a base
  the restored maps are related to. Answers `history-ready`.
- `trusted`: on a pull request, write the trusted-map list from the digests a run of the default
  branch uploaded for the restored cache entry; an empty list on anything it cannot establish.
- `flags`: choose the run's Gradle flags from the event. Answers `kind`, `flags` and `save`.
- `summary`: write the job summary from the files this run's test tasks wrote, and stage each
  observation report for upload.
- `digests`: on a run that saved the map, stage every map's digest for upload as the artifact a
  pull request's `trusted` looks up. Answers `artifact-name`, `path` and `count`.

Every path that cannot establish what selection needs ends in a run that selects nothing: only
the exact word `true` counts as an established map or history, and anything this script cannot
read becomes a warning, never a selection flag.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

FULL_RUN_LABEL = "yoriwake:full-run"

# The flags this action sets. One named in the action's inputs would change what runs without
# the action knowing, so such a run records instead. The gradle step in action.yml drops the
# same flags from the Gradle command in bash; change both together.
OWNED_FLAG = re.compile(r"yoriwake\.(select|observe|complement|fullRun|base|trustedMaps)(=|$)")

# Every run states both mode flags on the command line, which outranks gradle.properties and the
# environment: a `yoriwake.select` there would otherwise make a recording run select.
OFF = ["-Pyoriwake.select=false", "-Pyoriwake.observe=false"]

# Deepening steps for a shallow clone, after the base's own tip: commits from each tip, then all.
DEPTHS = (1, 50, 500, None)

# The trusted-map list: the digests a run of the default branch uploaded, as an artifact named
# from the cache key it saved (keys hold characters artifact names reject).
ARTIFACT_PREFIX = "yoriwake-maps-"
LIST_MEMBER = "trusted.tsv"
LIST_LINE = re.compile(r"[^\t\r\n]+\t[0-9a-f]{64}")
DIGEST_LINE = re.compile(r"sha256 ([0-9a-f]{64})")
SHA = re.compile(r"[0-9a-f]{40}")
# Events whose runs ran the default branch's own workflow and code.
TRUSTED_EVENTS = ("push", "schedule")
# Candidates looked at beyond the listing: anyone can upload under the name, so the requests a
# lookup makes are bounded whatever was uploaded.
LOOKUP_CAP = 5
MAX_LIST_BYTES = 1 << 20

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
                 history_ready, job_total, key, tasks, gradle_args, trusted_list) -> Decision:
    """The run kind, its Gradle flags, and whether the map may be saved, for one event."""
    capture = OFF + (["-Pyoriwake.isolatedCapture"] if isolated_capture else [])

    def record(why):
        return Decision("record", OFF, False, [f"{why}, so this run records every test"])

    # Before the saving events too: a map one leg saved would be restored by the others.
    if job_total > 1 and not key:
        return record(f"this job is one of {job_total} in a matrix and the `key` input is empty, "
                      "so its legs would share one map")

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
        return Decision("record", OFF, False, [
            f"a push to {ref}: {why}, so this run records and saves no map"])

    if event != "pull_request":
        return Decision("record", OFF, False, [
            f"the event {event or '(none)'} neither selects nor saves a map: this run records. "
            "The action selects on pull_request and saves on a push to the default branch "
            "or a schedule"])

    named = owned_flags(tasks, gradle_args)
    if named:
        return record(f"the inputs name {' '.join(named)}; the action owns those flags")
    base = pull_request_base(payload)
    if base is None:
        return record("the pull request's base branch could not be read from the event")
    if not map_restored:
        return record("no map was restored (a cache miss, or the restore failed)")
    if history_ready is not True:
        return record(f"the history is not ready: no merge base with origin/{base}, or a "
                      "restored map's capture commit is not on it")
    if not trusted_list:
        return record("the trusted-map list has no file")

    if observe:
        flags = ["-Pyoriwake.observe", "-Pyoriwake.select=false"]
    else:
        flags = ["-Pyoriwake.select", "-Pyoriwake.observe=false"]
    flags.append(f"-Pyoriwake.base=origin/{base}")
    # Always passed, empty or not: the plugin then narrows only from a map the list names.
    flags.append(f"-Pyoriwake.trustedMaps={trusted_list}")
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


# --- the trusted-map list ------------------------------------------------------------------------


def artifact_name(key: str) -> str:
    return ARTIFACT_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def parse_trusted_list(text: str) -> list:
    """The well-formed `<map directory>\t<sha256>` lines; any other line names no map."""
    return [line for line in text.splitlines() if LIST_LINE.fullmatch(line)]


def collect_digests(map_dir: Path) -> list:
    """Each task map's digest, as the plugin wrote it to `map-digest`, as list lines."""
    lines = []
    if not map_dir.is_dir():
        return lines
    for digest_file in sorted(map_dir.glob("*/map-digest")):
        try:
            found = DIGEST_LINE.fullmatch(digest_file.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeDecodeError):
            continue
        line = f"{digest_file.parent.name}\t{found.group(1)}" if found else ""
        if LIST_LINE.fullmatch(line):
            lines.append(line)
    return lines


def write_list(path: Path, lines: list):
    """Replaced whole, so a reader sees the old list or the new one, never part of one."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    os.replace(temporary, path)


class GitHub:
    """The few REST calls the lookup makes, with stdlib urllib."""

    def __init__(self, api_url: str, repository: str, token: str):
        self.api = api_url.rstrip("/")
        self.repo = repository
        self.token = token

    def request(self, path: str):
        request = urllib.request.Request(f"{self.api}/repos/{self.repo}/{path}", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "yoriwake-action",
        })
        if self.token:
            # Not copied onto a redirect, so the pre-signed storage URL an artifact's zip
            # redirects to never receives the token.
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(request, timeout=30)

    def json(self, path: str):
        with self.request(path) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} did not answer an object")
        return data

    def download(self, path: str) -> bytes:
        with self.request(path) as response:
            body = response.read(MAX_LIST_BYTES * 4 + 1)
        if len(body) > MAX_LIST_BYTES * 4:
            raise ValueError("the artifact is larger than a list of digests can be")
        return body


def find_trusted(github: GitHub, *, name: str, branch: str, repo_id: str, events,
                 run_id: str | None = None):
    """The newest artifact named `name` that a run of `events` on `branch` of this repository
    uploaded at a commit `branch` contains, or None, and why each candidate was passed over.

    Candidates are filtered on the listing's own fields before any further request, and at
    most LOOKUP_CAP of the rest are looked at.
    """
    notes = []
    listing = github.json(f"actions/artifacts?name={urllib.parse.quote(name)}&per_page=100")
    candidates = []
    for artifact in listing.get("artifacts") or []:
        run = artifact.get("workflow_run") or {}
        if (artifact.get("name") == name and artifact.get("expired") is False
                and run.get("head_branch") == branch
                and str(run.get("head_repository_id")) == repo_id
                and SHA.fullmatch(str(run.get("head_sha")))):
            candidates.append(artifact)
    candidates.sort(key=lambda a: (str(a.get("created_at")), int(a.get("id") or 0)), reverse=True)
    branch_sha = None
    for artifact in candidates[:LOOKUP_CAP]:
        uploaded = artifact["workflow_run"]
        run = github.json(f"actions/runs/{int(uploaded['id'])}")
        if run.get("event") not in events or (run_id is not None and str(run.get("id")) != run_id):
            notes.append(f"artifact {artifact.get('id')}: uploaded by a {run.get('event')} run")
            continue
        if run.get("head_sha") != uploaded["head_sha"]:
            notes.append(f"artifact {artifact.get('id')}: its run reports another commit")
            continue
        if branch_sha is None:
            # By the branch's commit, never its name: a tag can share the name.
            ref = github.json(f"git/ref/heads/{urllib.parse.quote(branch, safe='/')}")
            branch_sha = str((ref.get("object") or {}).get("sha"))
            if not SHA.fullmatch(branch_sha):
                raise ValueError(f"the branch {branch} did not resolve to a commit")
        try:
            status = github.json(
                f"compare/{uploaded['head_sha']}...{branch_sha}?per_page=1").get("status")
        except (urllib.error.URLError, OSError, ValueError) as error:
            notes.append(f"artifact {artifact.get('id')}: its commit could not be compared "
                         f"({error})")
            continue
        if status not in ("identical", "ahead"):
            notes.append(f"artifact {artifact.get('id')}: {branch} does not contain "
                         f"{uploaded['head_sha'][:12]} ({status})")
            continue
        return artifact, notes
    if len(candidates) > LOOKUP_CAP:
        notes.append(f"stopped after {LOOKUP_CAP} of {len(candidates)} candidates")
    return None, notes


def trusted_lines(github: GitHub, artifact) -> list:
    body = github.download(f"actions/artifacts/{int(artifact['id'])}/zip")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        member = archive.getinfo(LIST_MEMBER)
        if member.file_size > MAX_LIST_BYTES:
            raise ValueError("the list is larger than a list of digests can be")
        return parse_trusted_list(archive.read(member).decode("utf-8"))


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
    """The event's name and payload. A dispatched test of the action may supply both instead.

    Only on a dispatched run: elsewhere an earlier step can set the variables through
    GITHUB_ENV, and a pull request's code posing as a push would save the map.
    """
    name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if name == "workflow_dispatch":
        name = os.environ.get("YORIWAKE_EVENT_NAME") or name
        path = os.environ.get("YORIWAKE_EVENT_PATH") or path
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
        trusted_list=os.environ.get("YORIWAKE_TRUSTED_LIST", ""),
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


def run_trusted():
    """Writes the empty list before anything else, so every failure leaves a list naming no map:
    the plugin then runs every test of a selecting run (`map-unverified`)."""
    target = Path(os.environ["YORIWAKE_TRUSTED_LIST"])
    write_list(target, [])
    name, payload = event()
    if name != "pull_request":
        return
    key = os.environ.get("YORIWAKE_MATCHED_KEY", "")
    if not key:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        branch = os.environ.get("YORIWAKE_DEFAULT_BRANCH", "").strip()
        if not branch and isinstance(payload, dict):
            branch = (payload.get("repository") or {}).get("default_branch") or ""
        events, run_id = TRUSTED_EVENTS, None
    else:
        # A dispatched test of the action plays the default branch with the branch it was
        # dispatched from, and trusts only what its own run uploaded.
        branch = os.environ.get("GITHUB_REF_NAME", "")
        events, run_id = ("workflow_dispatch",), os.environ.get("GITHUB_RUN_ID", "")
    repo_id = os.environ.get("GITHUB_REPOSITORY_ID", "")
    unverified = "a selecting run runs every test (map-unverified)"
    if not is_branch_name(branch) or not repo_id:
        warn(f"the default branch or the repository id is unknown, so {unverified}")
        return
    github = GitHub(os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                    os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("YORIWAKE_TOKEN", ""))
    wanted = artifact_name(key)
    try:
        artifact, notes = find_trusted(github, name=wanted, branch=branch, repo_id=repo_id,
                                       events=events, run_id=run_id)
        for note in notes:
            print(f"yoriwake: {note}")
        if artifact is None:
            warn(f"no run of {branch} uploaded the digests of the restored cache entry "
                 f"({wanted}), so {unverified}")
            return
        lines = trusted_lines(github, artifact)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            warn(f"the token cannot read this repository's artifacts (HTTP {error.code}); give the "
                 f"job `permissions: actions: read`. Until then {unverified}")
        else:
            warn(f"looking up the default branch's digests failed (HTTP {error.code}), so "
                 f"{unverified}")
        return
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError,
            zipfile.BadZipFile) as error:
        warn(f"looking up the default branch's digests failed ({error}), so {unverified}")
        return
    write_list(target, lines)
    print(f"yoriwake: trusted-map list from artifact {artifact.get('id')}: {len(lines)} maps")


def run_digests():
    key = os.environ.get("YORIWAKE_CACHE_KEY", "")
    lines = collect_digests(Path(os.environ.get("YORIWAKE_MAP_DIR", ".gradle/yoriwake")))
    stage = (state_dir() or Path(".")) / "digests"
    stage.mkdir(parents=True, exist_ok=True)
    write_list(stage / LIST_MEMBER, lines)
    set_outputs(artifact_name=artifact_name(key), path=str(stage), count=str(len(lines)))


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


COMMANDS = {"flags": run_flags, "deepen": run_deepen, "trusted": run_trusted,
            "summary": run_summary, "digests": run_digests}


def main(argv):
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: {Path(argv[0]).name} {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    COMMANDS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

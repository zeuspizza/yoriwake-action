# yoriwake-action

Turns on [yoriwake](https://github.com/zeuspizza/yoriwake) test selection in GitHub Actions with
one step. It restores the coverage map from the cache, fetches the history selection needs, picks
yoriwake's flags from the event, runs Gradle, and writes a job summary of what ran and what was
skipped. Pushes to the default branch run every test and save the map; pull requests restore it
and select.

Nothing it does can skip a test on its own: it only passes flags, and the plugin decides. Every
step of its own that fails, or cannot tell, leaves a run that records every test. A pull request
narrows only from a map a run of the default branch recorded.

## Usage

The build must apply the plugin (`io.github.zeuspizza.yoriwake`), and the runner needs Python 3.8
or later (every GitHub-hosted runner has it). It is tested on Linux and macOS runners.

```yaml
on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened, labeled]

permissions:
  contents: read
  actions: read # to read the map digests the default branch's runs uploaded

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-java@de7274f081f381c8f8158605e0321c36c376e2e6 # v6.0.1
        with:
          distribution: temurin
          java-version: '21'
      - uses: zeuspizza/yoriwake-action@<commit SHA of a release> # vX.Y.Z
```

The default checkout is enough: on a pull request the action fetches the base branch and deepens a
shallow clone as far as it needs.

Use the action release named after the plugin release your build applies. The cache key carries
the map format of that plugin release, so an upgrade of both starts from a cache miss rather than
from a map the plugin would refuse.

## What it does, by event

| Event | Flags | Saves the map |
|---|---|---|
| `push` to the default branch, `schedule` | selection off: the run records every test (`-Pyoriwake.isolatedCapture` with `isolated-capture: true`) | yes, when HEAD is the commit the run was started for |
| `pull_request` | `-Pyoriwake.select -Pyoriwake.base=origin/<base> -Pyoriwake.trustedMaps=<list>`, or `-Pyoriwake.observe` in place of `select` with `observe: true`; `-Pyoriwake.fullRun` added when the pull request carries the `yoriwake:full-run` label | never |
| any other (`pull_request_target`, `issue_comment`, `workflow_dispatch`, `merge_group`, pushes to other branches) | selection off: the run records every test | never, and a warning names the event |

The action always states every mode flag on the command line (`-Pyoriwake.select=false
-Pyoriwake.observe=false` when it records, the other one `=false` when it selects or observes), so
a `yoriwake.select` in `gradle.properties` or the environment never turns its recording run into a
selecting one.

A pull request selects only when a map was restored and the history is ready: the merge base with
the base branch resolves, and every restored map's capture commit is on the base branch. Otherwise
it records every test.

Events that run with the default branch's permissions but can check out pull request code
(`pull_request_target`, `issue_comment`, `workflow_run`) never save, so a pull request cannot put
its own map into the cache the default branch's runs read.

The label applies to the run it is present for. It must be on the pull request before the run
starts, so subscribe to `labeled` as above; a re-run reuses the old event, so push again after
adding it.

## Which maps a pull request trusts

A pull request's run can save a cache entry that its later runs restore before the default
branch's, through a workflow or build edit it pushes and then reverts, which the final diff no
longer shows. A crafted map can make a run skip almost every test. So:

- A push to the default branch or a schedule that saves the map also uploads each map's digest
  (the `map-digest` file the plugin writes) as an artifact named after the cache key,
  `yoriwake-maps-<hash of the key>`. An exact-key hit saves nothing and uploads nothing.
- A pull request looks up the artifact for the entry it restored and keeps it only when GitHub's
  record of the run that uploaded it says: a `push` or `schedule` event, on the default branch
  (`default-branch` when set), in this repository, at a commit the default branch contains. It
  looks at the five newest candidates at most.
- It always passes that list as `-Pyoriwake.trustedMaps`, empty when no artifact qualified. The
  plugin narrows only from a map whose digest the list names, and otherwise runs every test as
  `map-unverified` (not listed) or `map-untrusted` (listed with another digest). The job summary
  names which.

This needs `actions: read` on the job's token, as in the example. A fork's pull request gets what
its workflow grants, read-only.

What it cannot cover:

- **Code in a cache entry a pull request wrote.** A restore extracts whatever the entry holds,
  wherever the runner can write, not only the map: an init script, a jar, the action's own
  scripts. That code runs outside any diff and can change the map after the check. Only that pull
  request's runs restore such an entry.
- **Persistent self-hosted runners** that run pull requests: code an earlier job left behind runs
  in later ones, the default branch's included. Run pull requests on ephemeral runners.
- **A plugin release older than the action's.** A build applying a plugin without
  `-Pyoriwake.trustedMaps` ignores the list and selects from any map restored.

A pull request whose own entry is restored runs every test on each push until that entry expires,
after seven days unused, or is deleted: `gh cache list --ref refs/pull/<number>/merge`, then
`gh cache delete <key>`. The plugin reference's
[Who can write the map you restore](https://github.com/zeuspizza/yoriwake/blob/main/docs/reference.md#who-can-write-the-map-you-restore)
has the threat model.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `tasks` | `test` | The Gradle tasks to run, separated by spaces. |
| `gradle-args` | | More arguments for Gradle, separated by spaces, without quoting. Naming yoriwake's `select`, `observe`, `complement`, `fullRun` or `base` here makes the run record every test: the action sets those. |
| `observe` | `false` | On pull requests, run every test and report what selection would have left out, instead of selecting. Each task's `observation.json` is uploaded as an artifact named after the commit and job. |
| `isolated-capture` | `false` | Record the map with a fresh test JVM per test class on the runs that save it. Slower to record, narrower to select. |
| `key` | | Added to the cache key. Required in a matrix (`strategy.job-total` above 1): without it a matrix job records every test and saves no map, since its legs would share one map. |
| `working-directory` | `.` | The directory holding `gradlew`. |
| `map-dir` | `<working-directory>/.gradle/yoriwake` | The map directory to cache. Set it when the build uses `--project-cache-dir`. |
| `default-branch` | the repository's | The branch whose pushes record and save the map, and whose uploaded digests pull requests trust. |
| `github-token` | `github.token` | Reads the uploaded digests on pull requests. Needs `actions: read`. |

The cache key is made of the runner OS, the workflow, the job, `key`, the map format and the
commit; a restore falls back to the newest entry with the same prefix. Each job keeps its own maps.
The undecoded `raw/` records are never cached.

## Outputs

| Output | Meaning |
|---|---|
| `run-kind` | `select`, `observe` or `record`: what the action asked Gradle for. |
| `flags` | The yoriwake flags it passed. |
| `map-restored` | `true` when a map was restored from the cache. |
| `history-ready` | On a pull request with a restored map, `true` when selection has its base; otherwise empty. |

## The job summary

Per test task: the map's format version, what kind of run it was (recorded, narrowed, ran
everything, or observed), what forced a full run or declined selection, and how many tests ran and
were skipped. Observing runs add the observation's counts: failures, how many of them selection
would have kept, would-be misses, and the tests and recorded test time selection would have
skipped. It shows counts only, never percentages or speedups, and it reads only the files this run
wrote: a task with none reads "did not run".

## When it runs everything

The action never fails a job for a step of its own; only Gradle does, as without it. Each of these
leaves a run that records every test, and prints a warning that the job summary repeats:

| Warning | Cause |
|---|---|
| no Python 3.8 or later on this runner | The runner has no `python3` or `python` of that version. Nothing but Gradle runs. |
| no map was restored (a cache miss, or the restore failed) | The first run of a job, a new `key`, a new map format, an evicted entry, or a cache-service failure. Any directory left at `map-dir` is deleted before Gradle. The next push to the default branch saves a map. |
| the history is not ready | No merge base with `origin/<base>`, even after deepening to the full history, or a restored map was captured at a commit that is not on the base branch (the base was rebased or force-pushed). Deepening goes 50 commits, then 500, then the whole history; `fetch-depth: 0` on checkout skips it. |
| the pull request's base branch could not be read from the event | The event payload has no base branch, or not one git accepts. |
| the event payload could not be read | The runner's event file is missing or not JSON. |
| this job is one of N in a matrix and the `key` input is empty | Set `key` to something that tells the legs apart, such as `${{ matrix.java }}`. |
| the inputs name `<flag>`; the action owns those flags | `tasks` or `gradle-args` sets one of yoriwake's run flags. |
| left out of the Gradle command: `<flag>` | The same, as Gradle is started: those arguments are removed. |
| the flags could not be chosen | The step choosing them failed. |
| the event `<name>` neither selects nor saves a map | The workflow runs on an event other than `push`, `schedule` or `pull_request`. |
| a push to `<ref>`: ... is not the default branch | A push to another branch. Set `default-branch` if the repository's default is not the branch that should save. |
| HEAD is ..., not the commit this run was started for | A step before the action checked out another commit, so the map is not saved under this commit. |
| the trusted-map list has no file | The action's first step could not create it. |

Each of these leaves a pull request selecting with an empty trusted-map list, so the plugin runs
every test as `map-unverified`:

| Warning | Cause |
|---|---|
| the token cannot read this repository's artifacts | The job's token lacks `actions: read`. |
| no run of `<branch>` uploaded the digests of the restored cache entry | The restored entry is not one a push to the default branch or a schedule saved (a pull request's own, or one an older action release saved), its artifact expired, or the upload failed. The next push to the default branch uploads them. |
| looking up the default branch's digests failed | An API error or rate limit, or an artifact that could not be read. |
| the default branch or the repository id is unknown | The event has no default branch and `default-branch` is empty. |

The plugin can still run everything on a run the action asked to select, for its own reasons (no
usable map, a change it cannot see through, a requested full run); the summary's "Forced or
declined" column names them, and `./gradlew yoriwakeExplainTest` explains them.

## What it never does

- It never passes a selection flag on an event other than `pull_request`, without a restored map
  and a ready history, or without the trusted-map list.
- It never saves the map from a pull request, or from an event that can run pull request code with
  the default branch's permissions.
- It never decides which tests run.
- It never fails the job for a step of its own.

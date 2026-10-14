# Releasing

The action is released once for each [yoriwake](https://github.com/zeuspizza/yoriwake) release, at
the same version: action `v0.2.0` goes with plugin `0.2.0`. Its cache key carries the map format of
that plugin release, so a user who upgrades both starts from a cache miss rather than from a map
the plugin refuses.

Releases are immutable (Settings > General > Releases > "Enable release immutability"): once
published, a release's tag cannot be moved or deleted and its assets cannot change, so
`uses: zeuspizza/yoriwake-action@v0.2.0` always runs the code that was released. A mistake is
fixed by a new release, never by moving a tag. There is no moving major tag such as `v0`.

A fix to the action alone is a patch release of the action, `v0.2.1` after `v0.2.0`, and goes with
the same plugin release: its minor version names the plugin release, its patch is the action's own.

## Checklist

After the plugin's `v<version>` is tagged and published to the Gradle Plugin Portal, on a branch of
`main`, merged by a pull request:

1. `fixture/settings.gradle.kts` defaults to the plugin's `<version>`, so the test workflow runs the
   released plugin when dispatched without a `plugin-ref`.
2. The map format agrees with the plugin's at its tag:

   ```bash
   python3 .github/check_map_format.py v<version>
   ```

   It reads `AgentContract.MAP_SCHEMA_VERSION` from the plugin's source at that tag, without
   building it (add `--plugin-checkout <dir>` to read a local clone instead of GitHub), and fails
   when `MAP_FORMAT` in `action.yml` differs. On a failure, set `MAP_FORMAT` to the plugin's
   number and say in the release notes that the first run after upgrading records every test.
3. Every example and link naming an action version names `v<version>`.
4. The test workflow, dispatched once on the branch with no `plugin-ref`, passes on every OS:
   `gh workflow run test.yml --ref <branch>`.

Then, on the merged commit of `main`:

5. Draft a release on GitHub (Releases > Draft a new release): tag `v<version>` created on that
   commit, title `v<version>`, notes "For yoriwake `<version>`." plus any upgrade note from step 2,
   and "Publish this Action to the GitHub Marketplace" ticked. Publish it; from then on it cannot
   change.
6. Check that the release reads as immutable on its page, and that a workflow using
   `zeuspizza/yoriwake-action@v<version>` resolves it.

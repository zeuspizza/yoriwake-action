# Contributing

## Where a change belongs

Which tests run, the coverage map and the agent are
[yoriwake](https://github.com/zeuspizza/yoriwake)'s: change them there. The cache key, how each
event is handled, which restored maps a pull request trusts and the job summary are this
repository's.

## Tests

Run locally, as the `helper` job of `.github/workflows/test.yml` does:

```bash
python3 scripts/test_yoriwake_action.py
python3 .github/test_check_map_format.py
```

On a pull request, `test.yml` also runs the Action on the real event against the demo build in
`fixture/`. What it proves, and the maintainer's check from a fork, are in the README's
[Its own tests](README.md#its-own-tests).

## A pull request

Say what changes for a workflow using the Action: its inputs, its outputs, the cache key or
`MAP_FORMAT`, the job summary or its warnings. Update the README to match, and keep
[What it never does](README.md#what-it-never-does) true.

Releases follow [RELEASING.md](RELEASING.md).

## AI-assisted contributions

As in yoriwake:
[welcome, and labelled](https://github.com/zeuspizza/yoriwake/blob/main/CONTRIBUTING.md#ai-assisted-contributions).

## Reporting a bug

A bug in the Action goes through this repository's
[bug report form](https://github.com/zeuspizza/yoriwake-action/issues/new?template=bug.yml); one in
the plugin through [yoriwake's](https://github.com/zeuspizza/yoriwake/issues/new?template=bug.yml).
A vulnerability goes through the [security policy](SECURITY.md).

"""The steps of the action's own test workflow that are not the action itself.

    python3 .github/selftest.py payload push <branch> <file>
    python3 .github/selftest.py payload pull_request <base> <file> [label ...]
    python3 .github/selftest.py reset
    python3 .github/selftest.py mutate
    python3 .github/selftest.py check --outcome failure --kind select --ran selected ...

`payload` writes an event payload for the action to read through YORIWAKE_EVENT_PATH, so one
dispatched job can play every event. `reset` puts the fixture back between two runs of the action
in one job: the source unchanged, and no test results or map, so what the next run reads comes
from the cache. `check` asserts over what the fixture's tests did, from the test results and the
plugin's decision records: log lines say what the build believed, these files say what it did.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

FIXTURE = "fixture"
RESULTS = f"{FIXTURE}/build/test-results/test/*.xml"
MAPS = f"{FIXTURE}/.gradle/yoriwake"

# Alpha.twice doubles, so tripling breaks exactly the two tests that assert on it.
ALPHA = f"{FIXTURE}/src/main/java/dev/demo/Alpha.java"
MUTATION = ("return n * 2;", "return n * 3;")
BROKEN = {"dev.demo.AlphaTest.doubles", "dev.demo.AlphaTest.doublesAgain"}
UNREACHABLE = ("dev.demo.BetaTest.", "dev.demo.GammaTest.")
EVERY = {
    *(f"dev.demo.{cls}.{m}" for cls in ("AlphaTest", "BetaTest", "GammaTest")
      for m in ("doubles", "doublesAgain", "hasALabel")),
    "dev.demo.DeltaShapeTest.exposesExactlyTheMethodsItsContractPromises",
    "dev.demo.EpsilonRegistryTest.everyRegisteredTypeExposesTheMethodsTheRegistryPromises",
}


def payload(event, base, path, labels):
    if event == "push":
        data = {"ref": f"refs/heads/{base}", "repository": {"default_branch": base}}
    else:
        data = {
            "action": "synchronize",
            "pull_request": {
                "base": {"ref": base},
                "labels": [{"name": label} for label in labels],
            },
        }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    print(f"{event}: {json.dumps(data)}")


def reset():
    subprocess.run(["git", "checkout", "--", f"{FIXTURE}/src"], check=True)
    for path in (f"{FIXTURE}/build/test-results", MAPS):
        shutil.rmtree(path, ignore_errors=True)


def mutate():
    with open(ALPHA, encoding="utf-8") as handle:
        before = handle.read()
    if MUTATION[0] not in before:
        raise SystemExit(f"refusing: {ALPHA} does not contain {MUTATION[0]!r}")
    with open(ALPHA, "w", encoding="utf-8") as handle:
        handle.write(before.replace(*MUTATION, 1))


def results():
    ran, failed = set(), set()
    for path in glob.glob(RESULTS):
        for case in ET.parse(path).getroot().iter("testcase"):
            if case.find("skipped") is not None:
                continue
            name = f"{case.get('classname')}.{(case.get('name') or '').removesuffix('()')}"
            ran.add(name)
            if case.find("failure") is not None or case.find("error") is not None:
                failed.add(name)
    return ran, failed


def decision_notes():
    """Every note of every decision record part, as {key: {values}}."""
    notes = {}
    for path in glob.glob(f"{MAPS}/*/decisions.tsv.*.part"):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("#!"):
                    key, _, value = line[2:].rstrip("\n").partition("\t")
                    notes.setdefault(key, set()).add(value)
    return notes


def check(args):
    problems = []

    def expect(name, actual, wanted):
        if wanted is not None and actual != wanted:
            problems.append(f"{name} is {actual!r}, expected {wanted!r}")

    expect("the action step's outcome", os.environ.get("ACTION_OUTCOME"), args.outcome)
    expect("run-kind", os.environ.get("RUN_KIND"), args.kind)
    expect("map-restored", os.environ.get("MAP_RESTORED"), args.map_restored)
    expect("history-ready", os.environ.get("HISTORY_READY"), args.history_ready)

    ran, failed = results()
    if not ran:
        problems.append("no test ran at all")
    if args.ran == "all":
        missing = sorted(EVERY - ran)
        if missing:
            problems.append(f"every test should have run; these did not: {missing}")
    elif args.ran == "selected":
        leaked = sorted(t for t in ran if t.startswith(UNREACHABLE))
        if leaked:
            problems.append(f"tests the change cannot reach ran: {leaked}")
        notes = decision_notes()
        if "narrowed" not in notes.get("outcome", set()):
            problems.append(f"the decision record does not say narrowed: {notes.get('outcome')}")
    for test in sorted(BROKEN):
        if not args.broken:
            break
        if test not in ran:
            problems.append(f"{test} was skipped, but the change breaks it")
        elif test not in failed:
            problems.append(f"{test} ran but did not fail, so the mutation did not take")
    if not args.broken and failed:
        problems.append(f"tests failed on an unchanged tree: {sorted(failed)}")

    if args.decline:
        notes = decision_notes()
        named = notes.get("refusal-kind", set()) | {
            token for value in notes.get("declines", set()) for token in value.split(",")}
        if args.decline not in named:
            problems.append(f"no decision record names {args.decline}: {notes}")

    if args.observed:
        found = glob.glob(f"{MAPS}/*/observation.json")
        if not found:
            problems.append("no observation.json was written")
        for path in found:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            print(f"{path}: {json.dumps({k: v for k, v in data.items() if k != 'caveats'})}")
            if args.broken and data.get("failuresKept") != len(BROKEN):
                problems.append(f"{path}: selection should have kept both broken tests")
            if not data.get("wouldBeSkipped"):
                problems.append(f"{path}: selection would have skipped nothing")

    print(f"ran {len(ran)}, failed {sorted(failed)}")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("payload")
    write.add_argument("event")
    write.add_argument("base")
    write.add_argument("file")
    write.add_argument("labels", nargs="*")
    commands.add_parser("reset")
    commands.add_parser("mutate")
    assertion = commands.add_parser("check")
    assertion.add_argument("--outcome")
    assertion.add_argument("--kind")
    assertion.add_argument("--map-restored")
    assertion.add_argument("--history-ready")
    assertion.add_argument("--ran", choices=("all", "selected"), required=True)
    assertion.add_argument("--broken", action="store_true")
    assertion.add_argument("--decline")
    assertion.add_argument("--observed", action="store_true")
    args = parser.parse_args()
    if args.command == "payload":
        payload(args.event, args.base, args.file, args.labels)
        return 0
    if args.command == "reset":
        reset()
        return 0
    if args.command == "mutate":
        mutate()
        return 0
    return check(args)


if __name__ == "__main__":
    sys.exit(main())

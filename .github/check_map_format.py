"""Checks, before a release, that the action's map format is the plugin release's.

    python3 .github/check_map_format.py <plugin ref> [--plugin-checkout <dir>]

The cache key carries `MAP_FORMAT` from `action.yml`, and the plugin refuses a map of any format
but its own `AgentContract.MAP_SCHEMA_VERSION`. A release whose two numbers differ keys new maps
under a format the plugin does not write, so an upgrade restores maps it then refuses instead of
starting from a cache miss.

The plugin's number is read from its source at `<plugin ref>` (the release tag), never built:
from `<dir>` with `git show` when given, otherwise from GitHub. Exits 0 when the numbers agree,
1 when they differ, 2 when either cannot be read.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN_REPOSITORY = "zeuspizza/yoriwake"
CONTRACT = "agent/src/main/java/io/github/zeuspizza/yoriwake/agent/contract/AgentContract.java"
ACTION = Path(__file__).resolve().parent.parent / "action.yml"


class Unreadable(Exception):
    pass


def only_number(pattern: str, text: str, where: str) -> int:
    found = re.findall(pattern, text, re.MULTILINE)
    if len(found) != 1:
        raise Unreadable(f"{where}: expected one match of {pattern!r}, found {len(found)}")
    return int(found[0])


def action_format(action_yml: Path) -> int:
    try:
        text = action_yml.read_text(encoding="utf-8")
    except OSError as problem:
        raise Unreadable(f"{action_yml}: {problem}") from problem
    return only_number(r"^\s*MAP_FORMAT: '(\d+)'\s*$", text, str(action_yml))


def contract_source(ref: str, checkout: Path | None) -> str:
    if checkout is not None:
        result = subprocess.run(["git", "-C", str(checkout), "show", f"{ref}:{CONTRACT}"],
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise Unreadable(f"git show {ref}:{CONTRACT} in {checkout}: {result.stderr.strip()}")
        return result.stdout
    url = (f"https://raw.githubusercontent.com/{PLUGIN_REPOSITORY}/"
           f"{urllib.parse.quote(ref, safe='')}/{CONTRACT}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as problem:
        raise Unreadable(f"{url}: {problem}") from problem


def plugin_format(ref: str, checkout: Path | None) -> int:
    return only_number(r"^\s*public static final int MAP_SCHEMA_VERSION = (\d+);",
                       contract_source(ref, checkout), f"{CONTRACT} at {ref}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ref", help="the plugin's release tag, or a commit")
    parser.add_argument("--plugin-checkout", type=Path,
                        help="a clone of the plugin to read the ref from, instead of GitHub")
    parser.add_argument("--action", type=Path, default=ACTION, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        ours = action_format(args.action)
        theirs = plugin_format(args.ref, args.plugin_checkout)
    except Unreadable as problem:
        print(f"cannot check the map format: {problem}", file=sys.stderr)
        return 2
    if ours != theirs:
        print(f"action.yml has MAP_FORMAT {ours}, the plugin at {args.ref} writes maps of format "
              f"{theirs}: set MAP_FORMAT to '{theirs}' before releasing", file=sys.stderr)
        return 1
    print(f"map format {ours}: action.yml agrees with the plugin at {args.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

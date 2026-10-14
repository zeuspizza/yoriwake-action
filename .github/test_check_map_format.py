"""Tests for the release check of the map format: run `python3 .github/test_check_map_format.py`."""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_map_format as check  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class CheckMapFormatTest(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.plugin = self.dir / "plugin"
        self.plugin.mkdir()
        git(self.plugin, "init", "-q")
        self.action = self.dir / "action.yml"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.dir)], check=False)

    def release(self, contract, tag="v1.0.0"):
        source = self.plugin / check.CONTRACT
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(contract, encoding="utf-8")
        git(self.plugin, "add", "-A")
        git(self.plugin, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
            "commit", "-q", "-m", tag)
        git(self.plugin, "-c", "tag.gpgsign=false", "tag", tag)

    def run_check(self, map_format, ref="v1.0.0", action=None):
        self.action.write_text(action or f"    env:\n        MAP_FORMAT: '{map_format}'\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check.main([ref, "--plugin-checkout", str(self.plugin), "--action", str(self.action)])
        return code, out.getvalue() + err.getvalue()

    def test_agreeing_formats_pass(self):
        self.release("    public static final int MAP_SCHEMA_VERSION = 7;\n")
        self.assertEqual((0, "map format 7: action.yml agrees with the plugin at v1.0.0\n"),
                         self.run_check(7))

    def test_disagreeing_formats_fail(self):
        self.release("    public static final int MAP_SCHEMA_VERSION = 8;\n")
        code, output = self.run_check(7)
        self.assertEqual(1, code)
        self.assertIn("set MAP_FORMAT to '8'", output)

    def test_the_tag_is_read_not_the_newest_commit(self):
        self.release("    public static final int MAP_SCHEMA_VERSION = 7;\n", "v1.0.0")
        self.release("    public static final int MAP_SCHEMA_VERSION = 8;\n", "v2.0.0")
        self.assertEqual(0, self.run_check(7, "v1.0.0")[0])
        self.assertEqual(1, self.run_check(7, "v2.0.0")[0])

    def test_an_unknown_ref_cannot_be_checked(self):
        self.release("    public static final int MAP_SCHEMA_VERSION = 7;\n")
        self.assertEqual(2, self.run_check(7, "v9.9.9")[0])

    def test_a_contract_without_the_constant_cannot_be_checked(self):
        self.release("    public static final int RAW_SCHEMA_VERSION = 7;\n")
        self.assertEqual(2, self.run_check(7)[0])

    def test_an_action_without_one_map_format_cannot_be_checked(self):
        self.release("    public static final int MAP_SCHEMA_VERSION = 7;\n")
        self.assertEqual(2, self.run_check(7, action="MAP_FORMAT: '7'\nMAP_FORMAT: '7'\n")[0])
        self.assertEqual(2, self.run_check(7, action="name: yoriwake\n")[0])

    def test_the_real_action_yml_has_one_map_format(self):
        self.assertGreater(check.action_format(check.ACTION), 0)


if __name__ == "__main__":
    unittest.main()

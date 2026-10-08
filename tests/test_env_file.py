import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from stock_guide_agent.env_file import EnvFileError, load_environment_file


class EnvironmentFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / ".env"

    def write(self, content, encoding="utf-8"):
        self.path.write_text(content, encoding=encoding)

    def test_loads_values_without_interpolation_or_shell_execution(self):
        self.write("A=hello\nREF=${A}\nCOMMAND=$(touch SHOULD_NOT_EXIST)\nEMPTY=\n")
        environment = {}
        load_environment_file(self.path, environment)
        self.assertEqual(environment["REF"], "${A}")
        self.assertEqual(environment["COMMAND"], "$(touch SHOULD_NOT_EXIST)")
        self.assertEqual(environment["EMPTY"], "")
        self.assertFalse((Path.cwd() / "SHOULD_NOT_EXIST").exists())

    def test_existing_values_take_precedence_including_blank(self):
        self.write("KEEP=file\nBLANK=file\nNEW=value\n")
        environment = {"KEEP": "process", "BLANK": ""}
        result = load_environment_file(self.path, environment)
        self.assertIs(result, environment)
        self.assertEqual(environment, {"KEEP": "process", "BLANK": "", "NEW": "value"})

    def test_bom_and_unicode_are_supported(self):
        self.write("UNICODE=안녕하세요\n", encoding="utf-8-sig")
        environment = {}
        load_environment_file(self.path, environment)
        self.assertEqual(environment["UNICODE"], "안녕하세요")

    def test_comments_and_blank_lines_are_allowed(self):
        environment = {}
        load_environment_file(Path(".env.example"), environment)
        self.assertEqual(environment["AGENT_EXECUTION_ENV"], "mock")
        self.assertEqual(environment["TELEGRAM_BOT_TOKEN"], "")

    def test_missing_file_and_directory_are_rejected(self):
        with self.assertRaises(EnvFileError):
            load_environment_file(self.path)
        with self.assertRaises(EnvFileError):
            load_environment_file(Path(self.tmp.name))

    def test_invalid_lines_and_missing_assignments_are_atomic_and_silent(self):
        self.write("GOOD=keep\nBROKEN 'unterminated\nLATER=secret\n")
        environment = {"ORIGINAL": "value"}
        error_output = io.StringIO()
        with redirect_stderr(error_output), self.assertRaises(EnvFileError) as caught:
            load_environment_file(self.path, environment)
        self.assertEqual(environment, {"ORIGINAL": "value"})
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(error_output.getvalue(), "")

        self.write("GOOD=keep\nMISSING_ASSIGNMENT\n")
        with self.assertRaises(EnvFileError):
            load_environment_file(self.path, environment)
        self.assertEqual(environment, {"ORIGINAL": "value"})

        self.write("GOOD=keep\nBAD=value\x00secret\n")
        with self.assertRaises(EnvFileError) as caught:
            load_environment_file(self.path, environment)
        self.assertEqual(environment, {"ORIGINAL": "value"})
        self.assertNotIn("secret", str(caught.exception))

    def test_symlink_is_rejected(self):
        target = Path(self.tmp.name) / "target.env"
        target.write_text("TOKEN=secret", encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        with self.assertRaises(EnvFileError):
            load_environment_file(self.path, {})


if __name__ == "__main__":
    unittest.main()

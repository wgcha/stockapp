import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from stock_guide_agent.cli import main
from stock_guide_agent.connectivity import ConnectivityReport
from stock_guide_agent.data_lock import RuntimeDataLock


class Stage4CliTests(unittest.TestCase):
    def test_env_data_dir_is_loaded_before_maintenance(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("# local\nAGENT_DATA_DIR=example-data\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["agent", "--env-file", str(env), "--jobs-status"]), patch("stock_guide_agent.cli.job_status", return_value=[]) as status, redirect_stdout(io.StringIO()):
                main()
            status.assert_called_once_with("example-data")

    def test_explicit_data_dir_wins_over_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("AGENT_DATA_DIR=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["agent", "--env-file", str(env), "--jobs-status", "--data-dir", "explicit"]), patch("stock_guide_agent.cli.job_status", return_value=[]) as status, redirect_stdout(io.StringIO()):
                main()
            status.assert_called_once_with("explicit")

    def test_connection_cli_does_not_construct_runtime(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.argv", ["agent", "--check-telegram"]), patch("stock_guide_agent.cli.build_runtime_from_env") as build, redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 1)
        build.assert_not_called()
        self.assertIn('"ready": false', output.getvalue())

    def test_market_check_cannot_issue_token_while_local_bot_holds_lock(self):
        with tempfile.TemporaryDirectory() as folder, RuntimeDataLock(folder), patch("sys.argv", ["agent", "--check-market", "--data-dir", folder]), patch("stock_guide_agent.cli.run_market_connectivity") as check, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main()
            check.assert_not_called()

    def test_poll_and_close_failures_are_masked_and_release_lock(self):
        for source in ("poll", "close"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as folder:
                runtime = Mock()
                runtime.poll_once.return_value = Mock(events=[], failures=0)
                if source == "poll":
                    runtime.poll_once.side_effect = RuntimeError("SECRET_PAYLOAD")
                else:
                    runtime.close.side_effect = RuntimeError("SECRET_PAYLOAD")
                with patch("sys.argv", ["agent", "--run-bot", "--once", "--data-dir", folder]), patch("stock_guide_agent.cli.build_runtime_from_env", return_value=runtime), redirect_stderr(io.StringIO()) as output, redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main()
                self.assertEqual(error.exception.code, 1)
                self.assertNotIn("SECRET_PAYLOAD", output.getvalue())
                runtime.close.assert_called_once()
                with RuntimeDataLock(folder):
                    pass

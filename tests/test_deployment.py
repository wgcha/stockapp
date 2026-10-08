import re
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigurationTests(unittest.TestCase):
    def test_package_has_build_backend_and_console_entrypoint(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)

        self.assertEqual(project["build-system"]["build-backend"], "setuptools.build_meta")
        self.assertEqual(
            project["project"]["scripts"]["stock-guide-agent"],
            "stock_guide_agent.cli:main",
        )
        self.assertEqual(project["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])

    def test_example_environment_is_mock_only_and_contains_no_secrets(self) -> None:
        text = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("AGENT_EXECUTION_ENV=mock", text)
        self.assertIn("AGENT_LIVE_TRADING_ENABLED=false", text)
        self.assertIn("TOSSINVEST_LIVE_ORDERS=false", text)
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TOSSINVEST_CLIENT_ID",
            "TOSSINVEST_CLIENT_SECRET",
        ):
            self.assertRegex(text, rf"(?m)^{re.escape(name)}=$")

    def test_container_is_non_root_read_only_and_persists_only_data(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('VOLUME ["/data"]', dockerfile)
        self.assertIn('ENTRYPOINT ["stock-guide-agent"]', dockerfile)
        self.assertIn('CMD ["--run-bot"]', dockerfile)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:\n      - ALL", compose)
        self.assertIn("stock-guide-data:/data", compose)
        self.assertIn(".env", ignore.splitlines())
        self.assertIn("*.sqlite3", ignore.splitlines())


if __name__ == "__main__":
    unittest.main()

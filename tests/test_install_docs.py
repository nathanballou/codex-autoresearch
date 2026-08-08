from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from autoresearch_bank import bank_capacity, load_bank
from autoresearch_core import DOCS_DIR


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "docs" / "INSTALL.md"
JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
BASH_BLOCK = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)

# Install methods that clone first, so their copy source is fixed by the clone URL.
CLONING_INSTALLS = ("Manual Repository Install", "Manual User Install", "Development Symlink")

# Every host section that publishes a bank, and the capacity that bank must declare.
# The Claude Code pool was measured at 16; the Prime Agent ceiling is a declared policy.
EXPECTED_CAPACITY = {"claude-subagents": 16, "prime-agent-rlm": 8}


class InstallDocsTest(unittest.TestCase):
    """Guard the documented install methods against schema and host drift."""

    def setUp(self) -> None:
        self.install_text = INSTALL.read_text(encoding="utf-8")

    def validate_bank(self, block: str) -> dict[str, object]:
        """
        Run one documented bank through the real loader.
        Args:
        block: JSON text lifted from a fenced block in INSTALL.md.
        Return: The validated bank document.
        """
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / DOCS_DIR).mkdir()
            (repo / DOCS_DIR / "compute.json").write_text(block, encoding="utf-8")
            return load_bank(repo)

    def test_documented_banks_validate_and_declare_their_ceiling(self) -> None:
        found: dict[str, int] = {}
        for block in JSON_BLOCK.findall(self.install_text):
            if '"bank"' not in block:
                continue
            bank = self.validate_bank(block)
            for entry in bank["bank"]:
                if entry["id"] in EXPECTED_CAPACITY:
                    found[entry["id"]] = bank_capacity(bank)
        self.assertEqual(EXPECTED_CAPACITY, found)

    def test_every_supported_host_has_an_install_section(self) -> None:
        headings = re.findall(r"(?m)^## (.+)$", self.install_text)
        for host in ("Claude Code", "Prime Agent"):
            self.assertIn(host, headings)

    def section(self, heading: str) -> str:
        """
        Lift one install method out of INSTALL.md.
        Args:
        heading: Exact level-two heading naming the method.
        Return: That section's body, up to the next heading.
        """
        self.assertIn(f"\n## {heading}\n", self.install_text)
        return self.install_text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]

    def run_documented_install(self, block: str, home: Path) -> Path:
        """
        Execute one documented install sequence against a local clone.
        Args:
        block: The shell block exactly as published.
        home: Disposable directory standing in for the reader's HOME and cwd.
        Return: The directory the sequence claims to install into.

        The clone is redirected at this checkout so the test needs no network, but its
        destination name still comes from the published URL. That is the whole point:
        the copy source has to match the directory the documented clone actually makes.
        """
        url = re.search(r"git clone (\S+)", block).group(1)
        clone = url.rsplit("/", 1)[-1].removesuffix(".git")
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), clone],
            cwd=home,
            check=True,
        )
        environment = {**os.environ, "HOME": str(home)}
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block.replace(f"git clone {url}", ":")],
            cwd=home,
            env=environment,
            check=True,
        )
        destination = block.strip().splitlines()[-1].split()[-1].strip('"')
        return Path(destination.replace("~", str(home), 1))

    def test_cloning_installs_land_the_skill_where_they_claim(self) -> None:
        for heading in CLONING_INSTALLS:
            with self.subTest(install=heading):
                block = BASH_BLOCK.search(self.section(heading)).group(1)
                with tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary)
                    destination = self.run_documented_install(block, home)
                    if not destination.is_absolute():
                        destination = home / destination
                    self.assertEqual("autoresearch", destination.name)
                    self.assertTrue((destination / "SKILL.md").is_file(), destination)

    def test_prime_agent_section_wires_both_improvement_loops(self) -> None:
        section = self.install_text.split("\n## Prime Agent\n", 1)[1].split("\n## ", 1)[0]
        for call in ("goal.create(", "refine.run(", "--autonomous-gate"):
            self.assertIn(call, section)


if __name__ == "__main__":
    unittest.main()

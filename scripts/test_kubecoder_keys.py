"""kubecoder-keys.sh: ~/.ssh/known_hosts carries exactly the repo file's homelab host CA lines.

Each test runs a copy of the script in a scratch repo layout, with HOME pointed at a scratch
directory and none of the script's key variables set, so it writes no key and never touches the
real ~/.ssh.

Run: python3 -m unittest discover -s scripts
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "kubecoder-keys.sh"

OLD = "@cert-authority * ssh-ed25519 AAAAold homelab-ssh-host-ca"
NEW = "@cert-authority * ssh-ed25519 AAAAnew homelab-ssh-host-ca"
UNRELATED = [
    "github.com ssh-ed25519 AAAAgithub",
    "@cert-authority *.example.org ssh-ed25519 AAAAother other-ca",
    "# @cert-authority * ssh-ed25519 AAAAcommented homelab-ssh-host-ca",
    "pve.home ssh-ed25519 AAAAplain homelab-ssh-host-ca",
    "",
]


def lines(*items: str) -> str:
    return "".join(f"{item}\n" for item in items)


class EnsureHostCa(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "scripts").mkdir()
        self.script = root / "scripts" / SCRIPT.name
        shutil.copy2(SCRIPT, self.script)
        self.repo_file = root / "ansible/files/known_hosts.d/homelab"
        self.repo_file.parent.mkdir(parents=True)
        self.home = root / "home"
        self.home.mkdir()
        self.known_hosts = self.home / ".ssh" / "known_hosts"

    def run_script(self, repo: list[str], known_hosts: str | None = None) -> str:
        self.repo_file.write_text(lines("# Homelab SSH host CA.", *repo))
        if known_hosts is not None:
            self.known_hosts.parent.mkdir(exist_ok=True)
            self.known_hosts.write_text(known_hosts)
        result = subprocess.run(
            ["bash", str(self.script)],
            env={"HOME": str(self.home), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stderr

    def test_adds_the_repo_line_when_known_hosts_is_missing(self):
        self.run_script([NEW])
        self.assertEqual(self.known_hosts.read_text(), lines(NEW))

    def test_keeps_both_lines_while_the_repo_file_carries_both(self):
        self.run_script([OLD, NEW], lines(*UNRELATED, OLD))
        self.assertEqual(self.known_hosts.read_text(), lines(*UNRELATED, OLD, NEW))

    def test_removes_a_homelab_line_the_repo_file_no_longer_carries(self):
        stderr = self.run_script([NEW], lines(UNRELATED[0], OLD, UNRELATED[1], NEW, OLD))
        self.assertEqual(self.known_hosts.read_text(), lines(UNRELATED[0], UNRELATED[1], NEW))
        self.assertIn("removed a retired homelab host CA", stderr)

    def test_leaves_every_other_line_alone(self):
        self.run_script([NEW], lines(*UNRELATED))
        self.assertEqual(self.known_hosts.read_text(), lines(*UNRELATED, NEW))

    def test_an_empty_repo_file_retires_every_homelab_line(self):
        self.run_script([], lines(UNRELATED[0], OLD, NEW))
        self.assertEqual(self.known_hosts.read_text(), lines(UNRELATED[0]))

    def test_a_last_line_without_a_newline_is_not_joined(self):
        self.run_script([NEW], UNRELATED[0])
        self.assertEqual(self.known_hosts.read_text(), lines(UNRELATED[0], NEW))

    def test_a_converged_file_is_not_rewritten(self):
        self.run_script([NEW], lines(UNRELATED[0], NEW))
        inode = self.known_hosts.stat().st_ino
        self.run_script([NEW])
        self.assertEqual(self.known_hosts.stat().st_ino, inode)
        self.assertEqual(self.known_hosts.read_text(), lines(UNRELATED[0], NEW))
        self.assertEqual(list(self.known_hosts.parent.glob(".known_hosts.*")), [])


if __name__ == "__main__":
    unittest.main()

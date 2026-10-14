"""kubecoder-keys.sh: ~/.ssh/known_hosts carries exactly the repo file's homelab host CA lines, and
~/.ssh/config a Match block for each key whose variable is set.

Each test runs a copy of the script in a scratch repo layout, with HOME pointed at a scratch
directory and only the key variables the test sets, so it never touches the real ~/.ssh.

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


class KeysScript(unittest.TestCase):
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

    def run_script(
        self,
        repo: list[str],
        known_hosts: str | None = None,
        keys: dict[str, str] | None = None,
        returncode: int = 0,
    ) -> str:
        self.repo_file.write_text(lines("# Homelab SSH host CA.", *repo))
        if known_hosts is not None:
            self.known_hosts.parent.mkdir(exist_ok=True)
            self.known_hosts.write_text(known_hosts)
        result = subprocess.run(
            ["bash", str(self.script)],
            env={"HOME": str(self.home), "PATH": os.environ["PATH"], **(keys or {})},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, returncode, result.stderr)
        return result.stderr


class EnsureHostCa(KeysScript):
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


BOTH = {"SSH_KEY_PVE": "pve-key", "SSH_KEY_ANSIBLE": "ansible-key"}
BEGIN = "# BEGIN kubecoder-keys.sh"
OPERATOR = ["Host github.com", "    User git", ""]


class EnsureSshConfig(KeysScript):
    def setUp(self):
        super().setUp()
        self.config = self.home / ".ssh" / "config"

    def write_config(self, text: str):
        self.config.parent.mkdir(exist_ok=True)
        self.config.write_text(text)

    def resolve(self, target: str) -> dict[str, list[str]]:
        """The options ssh would use for target, from the scratch config alone."""
        result = subprocess.run(
            ["ssh", "-G", "-F", str(self.config), target], capture_output=True, text=True, check=True
        )
        options: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition(" ")
            options.setdefault(key, []).append(value)
        return options

    def test_no_key_variable_writes_no_config(self):
        self.run_script([NEW])
        self.assertFalse(self.config.exists())

    def test_no_key_variable_leaves_an_operator_config_alone(self):
        self.write_config(lines(*OPERATOR))
        inode = self.config.stat().st_ino
        self.run_script([NEW])
        self.assertEqual(self.config.stat().st_ino, inode)
        self.assertEqual(self.config.read_text(), lines(*OPERATOR))

    def test_writes_a_block_for_each_key_at_the_top_at_mode_0600(self):
        stderr = self.run_script([NEW], keys=BOTH)
        text = self.config.read_text()
        self.assertTrue(text.startswith(BEGIN), text)
        self.assertIn("Match user root host pve,", text)
        self.assertIn("IdentityFile ~/.ssh/id_ed25519_pve", text)
        self.assertIn("Match user ansible\n", text)
        self.assertIn("IdentityFile ~/.ssh/id_ed25519_ansible", text)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertIn("wrote the ssh config block", stderr)

    def test_only_a_key_whose_variable_is_set_gets_a_block(self):
        self.run_script([NEW], keys={"SSH_KEY_ANSIBLE": "ansible-key"})
        text = self.config.read_text()
        self.assertIn("id_ed25519_ansible", text)
        self.assertNotIn("id_ed25519_pve", text)

    def test_keeps_the_operator_lines_below_the_block(self):
        self.write_config(lines(*OPERATOR))
        self.run_script([NEW], keys=BOTH)
        self.assertTrue(self.config.read_text().endswith("# END kubecoder-keys.sh\n" + lines(*OPERATOR)))

    def test_drops_the_block_of_a_variable_no_longer_set(self):
        self.write_config(lines(*OPERATOR))
        self.run_script([NEW], keys=BOTH)
        self.run_script([NEW], keys={"SSH_KEY_ANSIBLE": "ansible-key"})
        text = self.config.read_text()
        self.assertNotIn("id_ed25519_pve", text)
        self.assertIn("id_ed25519_ansible", text)
        stderr = self.run_script([NEW])
        self.assertEqual(self.config.read_text(), lines(*OPERATOR))
        self.assertIn("removed the ssh config block", stderr)

    def test_a_converged_config_is_not_rewritten(self):
        self.write_config(lines(*OPERATOR))
        self.run_script([NEW], keys=BOTH)
        inode = self.config.stat().st_ino
        before = self.config.read_text()
        stderr = self.run_script([NEW], keys=BOTH)
        self.assertEqual(self.config.stat().st_ino, inode)
        self.assertEqual(self.config.read_text(), before)
        self.assertIn("ssh config block already current", stderr)
        self.assertEqual(list(self.config.parent.glob(".config.*")), [])

    def test_an_unclosed_block_fails_and_leaves_the_file(self):
        self.write_config(lines(BEGIN, *OPERATOR))
        stderr = self.run_script([NEW], keys=BOTH, returncode=1)
        self.assertEqual(self.config.read_text(), lines(BEGIN, *OPERATOR))
        self.assertIn("never closes", stderr)
        self.assertEqual(list(self.config.parent.glob(".config.*")), [])

    @unittest.skipUnless(shutil.which("ssh"), "needs the OpenSSH client")
    def test_root_offers_the_pve_key_on_the_pve_nodes_only(self):
        self.run_script([NEW], keys=BOTH)
        for target in ("root@pve", "root@pve1", "root@pve2.home", "root@10.1.0.21"):
            options = self.resolve(target)
            self.assertIn("~/.ssh/id_ed25519_pve", options["identityfile"], target)
            self.assertEqual(options["identitiesonly"], ["yes"], target)
        options = self.resolve("root@srvk8s1")
        self.assertNotIn("~/.ssh/id_ed25519_pve", options["identityfile"])
        self.assertEqual(options["identitiesonly"], ["no"])

    @unittest.skipUnless(shutil.which("ssh"), "needs the OpenSSH client")
    def test_ansible_offers_the_ansible_key_on_every_host(self):
        self.run_script([NEW], keys=BOTH)
        for target in ("ansible@pve", "ansible@srvk8s1", "ansible@10.1.0.30"):
            options = self.resolve(target)
            self.assertEqual(options["identityfile"], ["~/.ssh/id_ed25519_ansible"], target)
            self.assertEqual(options["identitiesonly"], ["yes"], target)

    @unittest.skipUnless(shutil.which("ssh"), "needs the OpenSSH client")
    def test_a_global_operator_line_stays_global_below_the_block(self):
        self.write_config(lines("ForwardAgent yes"))
        self.run_script([NEW], keys=BOTH)
        self.assertEqual(self.resolve("root@srvk8s1")["forwardagent"], ["yes"])


if __name__ == "__main__":
    unittest.main()

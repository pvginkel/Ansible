"""jenkins-agent-launch.sh: the agent secret reaches the agent as `-secret @file`, never on argv.

Each test runs the script with stub `yq`, `stat`, `chown` and `docker` first on PATH. The yq stub
prints the secret, the docker stub records its argv, and RUNTIME_DIRECTORY, which systemd's
RuntimeDirectory= sets, is a scratch directory.

Run: python3 -m unittest discover -s support/iac-agent/tests
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "jenkins-agent-launch.sh"
SECRET = "0123456789abcdef" * 4

STUBS = {
    "yq": 'printf \'%s\\n\' "$STUB_SECRET"\n',
    "stat": "echo 999\n",
    "chown": 'printf \'%s\\n\' "$@" >"$STUB_LOG/chown"\n',
    "docker": 'printf \'%s\\n\' "$@" >"$STUB_LOG/docker"\n',
}


class Launch(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        stubs = root / "stubs"
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text("#!/bin/sh\n" + body)
            (stubs / name).chmod(0o755)
        self.log = root / "log"
        self.log.mkdir()
        self.runtime = root / "run"
        self.runtime.mkdir(mode=0o700)
        secrets = root / "secrets.yaml"
        secrets.write_text("env: []\n")
        self.env = {
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "SECRETS_FILE": str(secrets),
            "RUNTIME_DIRECTORY": str(self.runtime),
            "STUB_LOG": str(self.log),
        }

    def launch(self, secret: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={**self.env, "STUB_SECRET": secret},
            capture_output=True,
            text=True,
        )

    def test_secret_reaches_the_agent_through_a_file(self):
        result = self.launch(SECRET)

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = (self.log / "docker").read_text().splitlines()
        self.assertFalse([arg for arg in argv if SECRET in arg], argv)
        self.assertEqual(argv[-2:], ["-secret", "@/run/secrets/jenkins-agent"])
        secret_file = self.runtime / "agent-secret"
        self.assertIn(f"{secret_file}:/run/secrets/jenkins-agent:ro", argv)
        self.assertEqual(secret_file.read_text(), SECRET + "\n")
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        self.assertEqual(
            (self.log / "chown").read_text().splitlines(), ["1000:1000", str(secret_file)]
        )
        self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_rejected_secret_is_not_echoed(self):
        rejected = "kv/iac/jenkins#agent_secret"
        result = self.launch(rejected)

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be a literal 64-char hex secret", result.stderr)
        self.assertNotIn(rejected, result.stdout + result.stderr)
        self.assertFalse((self.log / "docker").exists())
        self.assertEqual(list(self.runtime.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

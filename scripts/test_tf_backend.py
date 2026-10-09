"""tf-backend.sh: the patched backend when it can be pulled or is cached, the stock one only otherwise.

Each test runs the script with a fake `docker` first on PATH that records its calls, and the
three credentials set in the environment, so it needs no docker daemon, registry or OpenBao. A
fake `bao` fails the test if the script reaches for OpenBao anyway.

Run: python3 -m unittest discover -s scripts
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "tf-backend.sh"
DOCKERFILE = Path(__file__).resolve().parent.parent / "support/iac-image/Dockerfile"
STOCK = "ghcr.io/plumber-cd/terraform-backend-git:v0.1.11"

# The build srviac's iac image copies the binary from: the script must run that same build.
PATCHED = re.search(
    r"^COPY --from=(\S+/terraform-backend-git:\S+)", DOCKERFILE.read_text(), re.MULTILINE
).group(1)

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:1] == ["inspect"]:
    if os.environ.get("FAKE_RUNNING"):
        print("true")
        sys.exit(0)
    sys.exit(1)
if args[:1] == ["pull"]:
    if os.environ.get("FAKE_PULL"):
        print(args[-1])
        sys.exit(0)
    print("Error response from daemon: registry unreachable", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if os.environ.get("FAKE_CACHED") else 1)
if args[:1] == ["run"]:
    print("0123456789ab")
"""

FAKE_BAO = """#!/bin/sh
echo "bao called: $*" >&2
exit 99
"""


class ImageChoice(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for tool, body in (("docker", FAKE_DOCKER), ("bao", FAKE_BAO)):
            (bin_dir / tool).write_text(body)
            (bin_dir / tool).chmod(0o755)
        self.log = root / "docker.log"
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(root),
            "FAKE_DOCKER_LOG": str(self.log),
            "GITHUB_TOKEN": "token",
            "TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS": "age1recipient",
            "SOPS_AGE_KEY": "AGE-SECRET-KEY-1",
        }

    def run_script(self, **fake: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={**self.env, **fake},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def calls(self, verb: str) -> list[list[str]]:
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [call for call in calls if call[0] == verb]

    def assert_started(self, image: str, stdout: str):
        self.assertEqual(
            self.calls("run"),
            [
                [
                    "run", "-d", "--network", "host", "--name", "tf-backend",
                    "-e", "GIT_USERNAME", "-e", "GITHUB_TOKEN",
                    "-e", "TF_BACKEND_HTTP_ENCRYPTION_PROVIDER=sops",
                    "-e", "TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS", "-e", "SOPS_AGE_KEY",
                    image, "terraform-backend-git", "--access-logs",
                ]
            ],
        )
        self.assertIn(f"tf-backend up on 127.0.0.1:6061 from {image}", stdout)

    def test_the_patched_build_is_a_registry_build_not_the_stock_image(self):
        self.assertRegex(PATCHED, r"^registry:5000/terraform-backend-git:\d+$")

    def test_pulls_and_starts_the_patched_build(self):
        result = self.run_script(FAKE_PULL="1")
        self.assertEqual(self.calls("pull"), [["pull", "-q", PATCHED]])
        self.assert_started(PATCHED, result.stdout)
        self.assertNotIn("WARNING", result.stderr)

    def test_starts_the_cached_patched_build_when_the_pull_fails(self):
        result = self.run_script(FAKE_CACHED="1")
        self.assertEqual(self.calls("image"), [["image", "inspect", PATCHED]])
        self.assert_started(PATCHED, result.stdout)
        self.assertIn(f"cannot pull {PATCHED}; starting the copy cached here", result.stderr)
        self.assertNotIn("WARNING", result.stderr)

    def test_starts_the_stock_image_with_a_warning_when_neither_exists(self):
        result = self.run_script()
        self.assert_started(STOCK, result.stdout)
        self.assertIn(f"WARNING: {PATCHED} is neither pullable nor cached here.", result.stderr)
        self.assertIn(f"Starting the STOCK backend, {STOCK}", result.stderr)
        self.assertIn('"non-fast-forward', result.stderr)
        self.assertIn("loses a push race", result.stderr)

    def test_a_running_backend_is_left_alone(self):
        result = self.run_script(FAKE_RUNNING="1", FAKE_PULL="1")
        self.assertEqual(result.stdout, "tf-backend already running on 127.0.0.1:6061\n")
        self.assertEqual(self.calls("pull"), [])
        self.assertEqual(self.calls("run"), [])


if __name__ == "__main__":
    unittest.main()

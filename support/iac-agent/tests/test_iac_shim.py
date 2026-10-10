"""bin/iac: the iac container can write srviac's copy of the Ansible key, and no other host path.

The test renders bin/iac.j2 as the iac_agent role does, with IAC_IMPL pointed at a stub, and runs
it with a stub `docker` first on PATH that records its argv.

Run: python3 -m unittest discover -s support/iac-agent/tests
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "bin" / "iac.j2"
IAC_IMPL_LINE = 'IAC_IMPL="/usr/local/bin/iac-impl"'
KEY_DIR = "/var/lib/iac/ansible-ssh-key"


class Shim(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        stubs = root / "stubs"
        stubs.mkdir()
        docker = stubs / "docker"
        docker.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >"$STUB_LOG"\n')
        docker.chmod(0o755)
        impl = root / "iac-impl"
        impl.write_text("#!/bin/sh\n")
        impl.chmod(0o755)
        template = TEMPLATE.read_text()
        self.assertIn(IAC_IMPL_LINE, template)
        rendered = template.replace("{{ homelab_timezone }}", "UTC").replace(
            IAC_IMPL_LINE, f'IAC_IMPL="{impl}"'
        )
        self.assertNotIn("{{", rendered)
        self.shim = root / "iac"
        self.shim.write_text(rendered)
        self.log = root / "docker-argv"
        self.env = {"PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(self.log)}

    def test_the_key_copy_is_the_one_writable_mount(self):
        result = subprocess.run(
            ["bash", str(self.shim), "-c", "true"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.log.read_text().splitlines()
        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
        volumes = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]
        writable = [m for m in mounts if "readonly" not in m.split(",")]
        writable += [v for v in volumes if not v.endswith(":ro")]
        self.assertEqual(writable, [f"type=bind,source={KEY_DIR},target={KEY_DIR}"])


if __name__ == "__main__":
    unittest.main()

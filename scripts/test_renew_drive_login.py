"""renew-drive-login.sh: one guarded write of a Drive remote's client and token, values on stdin only.

Each test runs the script with a fake `cexec` first on PATH. The fake answers `kubectl get jobs`
from FAKE_JOBS, and runs what `kubectl exec` would run in backup-server's container here instead,
against a scratch rclone.conf, with a fake `rclone` that edits the section and, like the real one,
prints it, and a fake `date` that reads the clock from FAKE_NOW. It logs every argv it is given,
so a test can see that neither the secret nor the token was ever on a command line.

Run: python3 -m unittest discover -s scripts
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "renew-drive-login.sh"
REAL_DATE = shutil.which("date")

CLIENT_ID = "123-abc.apps.googleusercontent.com"
SECRET = "GOCSPX-unit-test-secret"
TOKEN = (
    '{"access_token":"ya29.unit-test-access","token_type":"Bearer",'
    '"refresh_token":"1//unit-test-refresh","expiry":"2026-11-08T12:00:00Z"}'
)

# team_drive's empty value keeps the trailing blank rclone writes after " = ".
CONF = """[gdrive-x]
type = drive
scope = drive.readonly
token = {"access_token":"ya29.old","refresh_token":"1//old","expiry":"2026-11-08T09:00:00Z"}
team_drive =\x20

[onedrive-x]
type = onedrive
token = {"access_token":"EwB.old","refresh_token":"M.old"}
drive_id = ABC
"""

FAKE_CEXEC = f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "get" in args and "jobs" in args:
    sys.stdout.write(os.environ.get("FAKE_JOBS", ""))
    sys.exit(0)
if "exec" in args:
    sys.exit(subprocess.run(args[args.index("--") + 1:]).returncode)
sys.exit("fake cexec: unexpected call")
"""

# rclone --config <conf> config update <remote> <key> <value>... config_refresh_token false
FAKE_RCLONE = f"""#!{sys.executable}
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["rclone"] + args) + "\\n")
conf, remote, kv = args[1], args[4], args[5:]
pairs = dict(zip(kv[0::2], kv[1::2]))
pairs.pop("config_refresh_token")
parts = re.split(r"(?m)^(?=\\[)", open(conf).read())
for i, part in enumerate(parts):
    if part.startswith("[" + remote + "]\\n"):
        lines = part.rstrip("\\n").split("\\n")
        for j, line in enumerate(lines):
            key = line.split(" = ", 1)[0]
            if j and key in pairs:
                lines[j] = key + " = " + pairs.pop(key)
        lines += [k + " = " + v for k, v in pairs.items()]
        parts[i] = "\\n".join(lines) + "\\n\\n"
        print(parts[i])
open(conf, "w").write("".join(parts))
"""

FAKE_DATE = f"""#!/bin/sh
exec {REAL_DATE} -d "$FAKE_NOW" "$@"
"""


def sections(text):
    return {p.split("\n", 1)[0]: p for p in re.split(r"(?m)^(?=\[)", text) if p}


class RenewDriveLogin(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        fakes = self.root / "bin"
        fakes.mkdir()
        for name, body in (("cexec", FAKE_CEXEC), ("rclone", FAKE_RCLONE), ("date", FAKE_DATE)):
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        self.data = self.root / "data"
        self.data.mkdir()
        self.conf = self.data / "rclone.conf"
        self.conf.write_text(CONF)
        self.conf.chmod(0o600)
        self.log = self.root / "argv.log"
        self.log.touch()
        self.env = {
            **os.environ,
            "PATH": f"{fakes}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_NOW": "2026-11-08T10:30:00Z",
            "FAKE_JOBS": "storage-refresh-keys-cronjob-1 \n",
            "CONF": str(self.conf),
        }

    def run_script(self, remote="gdrive-x", client_id=CLIENT_ID, secret=SECRET, token=TOKEN, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), remote],
            input=f"{client_id}\n{secret}\n{token}\n",
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def copies(self):
        return sorted(p.name for p in self.data.iterdir() if p != self.conf)

    def assert_refused(self, result, why):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("REFUSED: ", result.stdout)
        self.assertIn(why, result.stdout)
        self.assertEqual(self.conf.read_text(), CONF)
        self.assertEqual(self.copies(), [])

    def test_writes_the_three_keys_and_keeps_the_rest(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = sections(self.conf.read_text())
        before = sections(CONF)
        self.assertEqual(after["[onedrive-x]"], before["[onedrive-x]"])
        section = after["[gdrive-x]"]
        for line in ("type = drive", "scope = drive.readonly", f"client_id = {CLIENT_ID}",
                     f"client_secret = {SECRET}", f"token = {TOKEN}", "team_drive = "):
            self.assertIn(line + "\n", section)
        copy = "rclone.conf.pre-gdrive-x-20261108T103000Z"
        self.assertEqual(self.copies(), [copy])
        self.assertEqual((self.data / copy).read_text(), CONF)
        self.assertEqual(oct((self.data / copy).stat().st_mode & 0o777), "0o600")
        self.assertIn(f"copy {self.data / copy}\n", result.stdout)
        self.assertIn("sections: 2 before, 2 after\n", result.stdout)
        self.assertIn("[gdrive-x] type=drive scope=drive.readonly client_id=matches client_secret=set",
                      result.stdout)
        self.assertRegex(result.stdout, r"refresh_token fingerprint: matches the paste \([0-9a-f]{12}\)")

    def test_the_secret_and_the_token_never_reach_a_command_line_or_the_screen(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exec_calls = [c for c in self.calls() if "exec" in c]
        self.assertEqual(len(exec_calls), 1)
        for call in exec_calls:
            for value in (SECRET, TOKEN, "unit-test-refresh"):
                self.assertFalse(any(value in arg for arg in call), call)
        for value in (SECRET, "unit-test-access", "unit-test-refresh"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_a_second_run_takes_a_copy_of_its_own(self):
        self.assertEqual(self.run_script().returncode, 0)
        first = self.conf.read_text()
        result = self.run_script(FAKE_NOW="2026-11-08T10:31:00Z")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.copies(), ["rclone.conf.pre-gdrive-x-20261108T103000Z",
                                         "rclone.conf.pre-gdrive-x-20261108T103100Z"])
        self.assertEqual((self.data / "rclone.conf.pre-gdrive-x-20261108T103100Z").read_text(), first)

    def test_never_overwrites_a_copy(self):
        self.assertEqual(self.run_script().returncode, 0)
        first = self.conf.read_text()
        result = self.run_script(token=TOKEN.replace("unit-test-refresh", "another-refresh"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("exists already; nothing written", result.stdout)
        self.assertEqual(self.conf.read_text(), first)
        self.assertEqual((self.data / "rclone.conf.pre-gdrive-x-20261108T103000Z").read_text(), CONF)

    def test_refuses_bad_values_before_reaching_the_cluster(self):
        cases = (
            ({"client_id": "123-abc.example.com"}, "does not end in .apps.googleusercontent.com"),
            ({"client_id": "123 abc.apps.googleusercontent.com"}, "is not one word"),
            ({"secret": "not-a-google-secret"}, "does not start with GOCSPX-"),
            ({"token": '{"access_token":"ya29.x"}'}, "not one line of JSON"),
            ({"remote": "gdrive x"}, "is not a remote name"),
            ({"remote": "-x"}, "is not a remote name"),
        )
        for values, why in cases:
            with self.subTest(**values):
                self.assert_refused(self.run_script(**values), why)
                self.assertEqual(self.calls(), [])

    def test_refuses_while_a_job_runs(self):
        result = self.run_script(FAKE_JOBS="storage-sync-cronjob-2 1\n")
        self.assert_refused(result, "a Job is running in storage-prd (storage-sync-cronjob-2)")
        self.assertFalse([c for c in self.calls() if "exec" in c])

    def test_refuses_outside_the_window(self):
        for now in ("2026-11-08T06:30:00Z", "2026-11-08T22:10:00Z", "2026-11-08T10:02:00Z",
                    "2026-11-08T10:57:00Z"):
            with self.subTest(now=now):
                self.assert_refused(self.run_script(FAKE_NOW=now), "UTC is outside 07:00-22:00")

    def test_refuses_a_section_that_is_missing_or_not_drive(self):
        self.assert_refused(self.run_script(remote="gdrive-z"), "no section [gdrive-z]")
        self.assert_refused(self.run_script(remote="onedrive-x"), "is not of type drive")

    def test_refuses_while_rclone_runs(self):
        other = self.root / "other"
        other.mkdir()
        shutil.copy(shutil.which("sleep"), other / "rclone")
        proc = subprocess.Popen([str(other / "rclone"), "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.2)
        self.assert_refused(self.run_script(), "an rclone process")


if __name__ == "__main__":
    unittest.main()

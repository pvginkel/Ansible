"""renew-drive-login.sh: one guarded write of a Drive or OneDrive remote's login, values on stdin only.

Each test runs the script with a fake `cexec` first on PATH. The fake answers `kubectl get jobs`
from FAKE_JOBS, and runs what `kubectl exec` would run in backup-server's container here instead,
the type read and the write, against a scratch rclone.conf. Beside it are a fake `rclone` that
edits the section and, like the real one, prints it, and a fake `date` that reads the clock from
FAKE_NOW. The fakes log every argv they are given, so a test can see that neither the secret nor
the token was ever on a command line outside the container; the fake `cexec` keeps what an
`exec -i` gets on stdin.

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

# As rclone writes the file: a blank line after every section, the last one's included, and
# team_drive's empty value with the trailing blank after " = ".
CONF = """[gdrive-x]
type = drive
scope = drive.readonly
token = {"access_token":"ya29.old","refresh_token":"1//old","expiry":"2026-11-08T09:00:00Z"}
team_drive =\x20

[onedrive-x]
type = onedrive
token = {"access_token":"EwB.old","refresh_token":"M.old"}
drive_id = ABC
drive_type = personal

"""

# FAKE_CONF_AT_WRITE, when set, replaces the file just before an `exec -i` runs: the file as it is
# by the time the write reaches the container, after the type read.
FAKE_CEXEC = f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "get" in args and "jobs" in args:
    sys.stdout.write(os.environ.get("FAKE_JOBS", ""))
    sys.exit(0)
if "exec" in args:
    cmd = args[args.index("--") + 1:]
    if "-i" not in args:
        sys.exit(subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode)
    data = sys.stdin.buffer.read()
    with open(os.environ["FAKE_STDIN"], "ab") as f:
        f.write(data)
    if "FAKE_CONF_AT_WRITE" in os.environ:
        with open(os.environ["CONF"], "w") as f:
            f.write(os.environ["FAKE_CONF_AT_WRITE"])
    sys.exit(subprocess.run(cmd, input=data).returncode)
sys.exit("fake cexec: unexpected call")
"""

# rclone --config <conf> config update <remote> <key> <value>... config_refresh_token false
# [--non-interactive]. FAKE_RCLONE_ALSO=<key>=<value> writes one key more, as a write gone wrong,
# into the section FAKE_RCLONE_ALSO_IN names, the remote's own when it is unset.
FAKE_RCLONE = f"""#!{sys.executable}
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["rclone"] + args) + "\\n")
if args[0] != "--config" or args[2:4] != ["config", "update"]:
    sys.exit("fake rclone: unexpected call")
conf, remote = args[1], args[4]
kv = [a for a in args[5:] if a != "--non-interactive"]
pairs = dict(zip(kv[0::2], kv[1::2]))
if len(kv) % 2 or pairs.pop("config_refresh_token") != "false":
    sys.exit("fake rclone: unexpected call")
edits = {{remote: pairs}}
if "FAKE_RCLONE_ALSO" in os.environ:
    key, value = os.environ["FAKE_RCLONE_ALSO"].split("=", 1)
    edits.setdefault(os.environ.get("FAKE_RCLONE_ALSO_IN", remote), {{}})[key] = value
parts = re.split(r"(?m)^(?=\\[)", open(conf).read())
for i, part in enumerate(parts):
    other = [n for n in edits if n != remote and part.startswith("[" + n + "]\\n")]
    if other:
        lines = part.rstrip("\\n").split("\\n")
        for j, line in enumerate(lines):
            key = line.split(" = ", 1)[0]
            if j and key in edits[other[0]]:
                lines[j] = key + " = " + edits[other[0]][key]
        parts[i] = "\\n".join(lines) + "\\n\\n"
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


def with_token(section, token):
    return re.sub(r"(?m)^token = .*$", lambda _: f"token = {token}", section)


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
        self.write_conf(CONF)
        self.log = self.root / "argv.log"
        self.stdin = self.root / "stdin.log"
        self.env = {
            **os.environ,
            "PATH": f"{fakes}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_STDIN": str(self.stdin),
            "FAKE_NOW": "2026-11-08T10:30:00Z",
            "FAKE_JOBS": "storage-refresh-keys-cronjob-1 \n",
            "CONF": str(self.conf),
        }

    def write_conf(self, text):
        self.conf.write_text(text)
        self.conf.chmod(0o600)
        self.start = text

    def run_with(self, remote, stdin, env):
        self.log.write_text("")
        self.stdin.write_text("")
        return subprocess.run(
            ["bash", str(SCRIPT), remote],
            input=stdin,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_script(self, remote="gdrive-x", client_id=CLIENT_ID, secret=SECRET, token=TOKEN, **env):
        return self.run_with(remote, f"{client_id}\n{secret}\n{token}\n", env)

    def run_onedrive(self, remote="onedrive-x", token=TOKEN, **env):
        return self.run_with(remote, f"{token}\n", env)

    def calls(self):
        """The last run's argv, every one the fakes logged, in order."""
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def type_read(self, remote):
        return ["iac", "kubectl", "--kubeconfig", str(self.root / ".kube/config-prd-write"),
                "--context", "prd", "-n", "storage-prd", "exec", "deploy/backup-server", "--",
                "sed", "-n", f"/^\\[{remote}\\]$/,/^\\[/s/^type = //p", str(self.conf)]

    def copies(self):
        return sorted(p.name for p in self.data.iterdir() if p != self.conf)

    def assert_refused(self, result, why, conf=None):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("REFUSED: ", result.stdout)
        self.assertIn(why, result.stdout)
        self.assertEqual(self.conf.read_text(), self.start if conf is None else conf)
        self.assertEqual(self.copies(), [])
        self.assertEqual([c for c in self.calls() if c[0] == "rclone"], [])

    def assert_no_value_on_a_command_line(self, values):
        host = [c for c in self.calls() if c[0] == "iac"]
        self.assertEqual(len([c for c in host if "exec" in c]), 2, host)
        for call in host:
            for value in values:
                self.assertFalse(any(value in arg for arg in call), call)

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
        self.assert_no_value_on_a_command_line((SECRET, TOKEN, "unit-test-access", "unit-test-refresh"))
        self.assertEqual(self.stdin.read_text(), f"{SECRET}\n{TOKEN}\n")
        for value in (SECRET, "unit-test-access", "unit-test-refresh"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_onedrive_writes_the_token_alone_and_keeps_the_rest(self):
        result = self.run_onedrive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = sections(self.conf.read_text())
        before = sections(CONF)
        self.assertEqual(after["[gdrive-x]"], before["[gdrive-x]"])
        self.assertEqual(after["[onedrive-x]"], with_token(before["[onedrive-x]"], TOKEN))
        self.assertEqual([c for c in self.calls() if c[0] == "rclone"],
                         [["rclone", "--config", str(self.conf), "config", "update", "onedrive-x",
                           "token", TOKEN, "config_refresh_token", "false", "--non-interactive"]])
        copy = "rclone.conf.pre-onedrive-x-20261108T103000Z"
        self.assertEqual(self.copies(), [copy])
        self.assertEqual((self.data / copy).read_text(), CONF)
        self.assertEqual(oct((self.data / copy).stat().st_mode & 0o777), "0o600")
        self.assertIn(f"copy {self.data / copy}\n", result.stdout)
        self.assertIn("sections: 2 before, 2 after\n", result.stdout)
        self.assertIn("[onedrive-x] type=onedrive drive_type=personal client_id=none\n", result.stdout)
        self.assertIn("the file but [onedrive-x]'s token: unchanged\n", result.stdout)
        self.assertNotIn("client_secret=", result.stdout)
        self.assertRegex(result.stdout, r"refresh_token fingerprint: matches the paste \([0-9a-f]{12}\)")

    def test_onedrive_takes_the_token_alone_and_never_shows_it(self):
        result = self.run_onedrive()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_value_on_a_command_line((TOKEN, "unit-test-access", "unit-test-refresh"))
        self.assertEqual(self.stdin.read_text(), f"{TOKEN}\n")
        for value in ("unit-test-access", "unit-test-refresh"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_onedrive_checks_show_a_write_that_touched_more_than_the_token(self):
        # The last case changes another remote's token line: the check compares the whole file,
        # all but [onedrive-x]'s token line, not [onedrive-x]'s section alone.
        cases = (
            ("onedrive-x", "drive_type=business", "drive_type=business client_id=none"),
            ("onedrive-x", "client_id=abc", "drive_type=personal client_id=SET"),
            ("gdrive-x", 'token={"access_token":"ya29.other","refresh_token":"1//other"}',
             "drive_type=personal client_id=none"),
        )
        for i, (section, also, line) in enumerate(cases):
            with self.subTest(section=section, also=also):
                self.write_conf(CONF)
                result = self.run_onedrive(FAKE_RCLONE_ALSO=also, FAKE_RCLONE_ALSO_IN=section,
                                           FAKE_NOW=f"2026-11-08T10:3{i}:00Z")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotEqual(sections(self.conf.read_text())[f"[{section}]"],
                                    sections(CONF)[f"[{section}]"])
                self.assertIn(f"[onedrive-x] type=onedrive {line}\n", result.stdout)
                self.assertIn("the file but [onedrive-x]'s token: CHANGED\n", result.stdout)

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

    def test_refuses_a_bad_remote_name_without_a_call(self):
        for remote in ("gdrive x", "-x"):
            with self.subTest(remote=remote):
                self.assert_refused(self.run_script(remote=remote), "is not a remote name")
                self.assertEqual(self.calls(), [])

    def test_refuses_bad_values_after_the_type_read_alone(self):
        cases = (
            (self.run_script, {"client_id": "123-abc.example.com"}, "does not end in .apps.googleusercontent.com"),
            (self.run_script, {"client_id": "123 abc.apps.googleusercontent.com"}, "is not one word"),
            (self.run_script, {"secret": "not-a-google-secret"}, "does not start with GOCSPX-"),
            (self.run_script, {"token": '{"access_token":"ya29.x"}'}, "not one line of JSON"),
            (self.run_onedrive, {"token": '{"access_token":"EwB.x"}'}, "not one line of JSON"),
        )
        for run, values, why in cases:
            with self.subTest(run=run.__name__, **values):
                self.assert_refused(run(**values), why)
                remote = "onedrive-x" if run == self.run_onedrive else "gdrive-x"
                self.assertEqual(self.calls(), [self.type_read(remote)])

    def test_refuses_while_a_job_runs(self):
        for run, remote in ((self.run_script, "gdrive-x"), (self.run_onedrive, "onedrive-x")):
            with self.subTest(remote=remote):
                result = run(FAKE_JOBS="storage-sync-cronjob-2 1\n")
                self.assert_refused(result, "a Job is running in storage-prd (storage-sync-cronjob-2)")
                self.assertEqual([c for c in self.calls() if "exec" in c], [self.type_read(remote)])

    def test_refuses_outside_the_window(self):
        for now in ("2026-11-08T06:30:00Z", "2026-11-08T22:10:00Z", "2026-11-08T10:02:00Z",
                    "2026-11-08T10:57:00Z"):
            with self.subTest(now=now):
                self.assert_refused(self.run_script(FAKE_NOW=now), "UTC is outside 07:00-22:00")

    def test_refuses_a_section_that_is_missing_or_neither_drive_nor_onedrive(self):
        self.assert_refused(self.run_script(remote="gdrive-z"), "no section [gdrive-z]")
        self.assertEqual(self.calls(), [self.type_read("gdrive-z")])
        self.write_conf(CONF + "[s3-x]\ntype = s3\nprovider = Minio\n\n")
        self.assert_refused(self.run_onedrive(remote="s3-x"),
                            f"[s3-x] in {self.conf} is of type s3, not drive or onedrive")
        self.assertEqual(self.calls(), [self.type_read("s3-x")])

    def test_refuses_a_onedrive_section_with_a_client_of_its_own(self):
        for key in ("client_id", "client_secret"):
            with self.subTest(key=key):
                self.write_conf(CONF.replace("drive_id = ABC\n", f"drive_id = ABC\n{key} = abc\n"))
                self.assert_refused(self.run_onedrive(), "[onedrive-x] names a client of its own")

    def test_refuses_a_section_that_changed_after_the_type_read(self):
        cases = (
            (CONF.replace("[gdrive-x]", "[gdrive-y]"), f"no section [gdrive-x] in {self.conf}"),
            (CONF.replace("type = drive", "type = onedrive"),
             f"[gdrive-x] in {self.conf} is not of type drive"),
        )
        for text, why in cases:
            with self.subTest(why=why):
                self.write_conf(CONF)
                self.assert_refused(self.run_script(FAKE_CONF_AT_WRITE=text), why, conf=text)
                execs = [c for c in self.calls() if "exec" in c]
                self.assertEqual(len(execs), 2, execs)
                self.assertEqual(execs[0], self.type_read("gdrive-x"))

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

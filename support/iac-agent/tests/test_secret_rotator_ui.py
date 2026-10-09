"""secret-rotator-ui: one UI in the tmux session `secret-rotator`, reattached after a lost session.

Each test runs the script under a pseudo-terminal, as `ssh -t` does, against the real tmux on a
server of its own (TMUX_TMPDIR, HOME and so the config are scratch directories). Stub `sudo` and
`iac` come first on PATH: `iac` records its arguments and then stands in for the UI, either running
until the server is killed or printing a failure and exiting with STUB_EXIT.

Run: python3 -m unittest discover -s support/iac-agent/tests
"""

import base64
import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "secret-rotator-ui"
SESSION = "secret-rotator"
COPIED = "the new password"

STUBS = {
    "sudo": 'exec "$@"\n',
    "iac": (
        'printf \'%s\\n\' "$*" >>"$STUB_LOG"\n'
        'if [ -n "$STUB_EXIT" ]; then echo "the ui failed"; exit "$STUB_EXIT"; fi\n'
        "printf '\\033]52;c;%s\\a' \"$STUB_COPY\"\n"
        "exec sleep 600\n"
    ),
}


class Client:
    """A tmux client on a pseudo-terminal, its output drained so it never blocks on a full one."""

    def __init__(self, env: dict[str, str]):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.proc = subprocess.Popen(
            ["bash", str(SCRIPT)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        self.master = master
        self.chunks: list[bytes] = []
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.chunks.append(chunk)

    def output(self) -> bytes:
        return b"".join(self.chunks)

    def lose(self) -> None:
        """What a dropped SSH connection does to the client: a hangup."""
        self.proc.send_signal(signal.SIGHUP)
        self.proc.wait(timeout=10)
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.reader.join(timeout=10)
        os.close(self.master)


class SecretRotatorUi(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        stubs = root / "stubs"
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text("#!/bin/sh\n" + body)
            (stubs / name).chmod(0o755)
        self.log = root / "iac.log"
        home = root / "home"
        home.mkdir()
        sockets = root / "tmux"
        sockets.mkdir(mode=0o700)
        self.env = {
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(home),
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color",
            "TMUX_TMPDIR": str(sockets),
            "STUB_LOG": str(self.log),
            "STUB_COPY": base64.b64encode(COPIED.encode()).decode(),
        }
        self.addCleanup(self.tmux, "kill-server")
        self.clients: list[Client] = []
        self.addCleanup(lambda: [c.close() for c in self.clients if c.proc.poll() is None])

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True)

    def start(self, **env: str) -> Client:
        client = Client({**self.env, **env})
        self.clients.append(client)
        return client

    def wait_for(self, what: str, condition) -> None:
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.05)

    def attached(self) -> list[str]:
        return self.tmux("list-clients", "-t", SESSION, "-F", "#{client_name}").stdout.split()

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_creates_the_session_running_the_ui(self):
        self.start()

        self.wait_for("the UI", lambda: self.calls() and self.attached())
        self.assertEqual(self.calls(), ["-c secret-rotator ui"])
        self.assertEqual(self.tmux("list-sessions", "-F", "#{session_name}").stdout.split(), [SESSION])

    def test_reattaches_after_a_lost_session_without_a_second_ui(self):
        first = self.start()
        self.wait_for("the UI", lambda: self.calls() and self.attached())
        first.lose()
        self.wait_for("the client to go", lambda: not self.attached())
        self.assertEqual(self.tmux("has-session", "-t", SESSION).returncode, 0)

        self.start()

        self.wait_for("the reattach", self.attached)
        self.assertEqual(self.calls(), ["-c secret-rotator ui"])
        self.assertEqual(self.tmux("list-sessions", "-F", "#{session_name}").stdout.split(), [SESSION])
        self.assertEqual(len(self.tmux("list-panes", "-s", "-t", SESSION).stdout.splitlines()), 1)

    def test_copy_reaches_the_terminal(self):
        client = self.start()

        payload = self.env["STUB_COPY"].encode()
        self.wait_for("the OSC 52 sequence", lambda: payload in client.output())
        self.assertEqual(self.tmux("show-options", "-s", "set-clipboard").stdout.strip(),
                         "set-clipboard on")

    def test_a_failed_ui_holds_its_output_until_enter(self):
        client = self.start(STUB_EXIT="3")

        self.wait_for("the hold", lambda: b"Enter closes the session" in client.output())
        screen = self.tmux("capture-pane", "-p", "-t", SESSION).stdout
        self.assertIn("the ui failed", screen)
        self.assertIn("secret-rotator ui exited with status 3.", screen)

        self.tmux("send-keys", "-t", SESSION, "Enter")

        self.wait_for("the session to close", lambda: self.tmux("has-session", "-t", SESSION).returncode)
        client.proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

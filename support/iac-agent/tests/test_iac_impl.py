"""iac-impl clone_repos: the GitHub token reaches git only through the GIT_ASKPASS helper.

Each test runs clone_repos in a child Python, as iac-impl does inside the iac container, against
a local smart-HTTP server (git http-backend behind HTTP basic auth). git's `url.insteadOf`
points the https://github.com/ clone URLs at that server. GIT_TRACE records every argv git
runs, the askpass helper's included. The child's combined output stands in for the Jenkins
build log.

Run: python3 -m unittest discover -s support/iac-agent/tests
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IAC_IMPL = Path(__file__).resolve().parent.parent / "bin" / "iac-impl"
TOKEN = "ghp_" + "S3cr3tT0ken" * 3

DRIVER = """
import importlib.machinery, importlib.util, os, sys, types
sys.modules["hvac"] = types.ModuleType("hvac")
loader = importlib.machinery.SourceFileLoader("iac_impl", sys.argv[1])
spec = importlib.util.spec_from_loader("iac_impl", loader)
iac_impl = sys.modules["iac_impl"] = importlib.util.module_from_spec(spec)
loader.exec_module(iac_impl)
iac_impl.clone_repos(os.environ["TEST_TOKEN"], tuple(sys.argv[2:]))
"""


class GitHttpBackend(BaseHTTPRequestHandler):
    """git http-backend as CGI, behind basic auth for `x-access-token:<TOKEN>`."""

    def do_GET(self):
        self.serve()

    def do_POST(self):
        self.serve()

    def log_message(self, *args):
        pass

    def serve(self):
        self.server.authorizations.append(self.headers.get("Authorization"))
        expected = "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        env = {
            "PATH": os.environ["PATH"],
            "GIT_PROJECT_ROOT": self.server.root,
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "REMOTE_USER": "x-access-token",
        }
        cgi = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=True
        ).stdout
        head, _, payload = cgi.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(": ")
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


class CloneRepos(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

        source = self.root / "source"
        source.mkdir()
        git("init", "--quiet", "--initial-branch=main", cwd=source)
        (source / "README").write_text("private\n")
        git("add", "README", cwd=source)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "c", cwd=source)
        served = self.root / "served"
        (served / "pvginkel").mkdir(parents=True)
        git("clone", "--quiet", "--bare", str(source), str(served / "pvginkel/Private.git"), cwd=self.root)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GitHttpBackend)
        self.server.root = str(served)
        self.server.authorizations = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.home = self.root / "home"
        self.home.mkdir()
        self.work = self.root / "work"
        self.trace = self.root / "git-trace"

    def clone(self, token: str, *repos: str) -> subprocess.CompletedProcess:
        port = self.server.server_address[1]
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "WORK": str(self.work),
            "TEST_TOKEN": token,
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.http://127.0.0.1:{port}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "GIT_TRACE": str(self.trace),
        }
        return subprocess.run(
            [sys.executable, "-c", DRIVER, str(IAC_IMPL), *repos],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )

    def test_clone_authenticates_through_askpass(self):
        result = self.clone(TOKEN, "Private")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.work / "Private/README").read_text(), "private\n")
        self.assertIn(
            "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(),
            self.server.authorizations,
        )
        trace = self.trace.read_text()
        self.assertIn("/askpass 'Password for ", trace)
        self.assertIn("x-access-token@127.0.0.1", trace)
        self.assertNotIn(TOKEN, trace)
        config = (self.work / "Private/.git/config").read_text()
        self.assertIn("url = https://github.com/pvginkel/Private.git", config)
        self.assertNotIn(TOKEN, config)
        self.assertNotIn(TOKEN, result.stdout)
        self.assertEqual(list(self.home.iterdir()), [self.home / ".gitconfig"])
        self.assertNotIn(TOKEN, (self.home / ".gitconfig").read_text())

    def test_failed_clone_prints_no_token(self):
        wrong = TOKEN[::-1]
        result = self.clone(wrong, "Private")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CalledProcessError", result.stdout)
        self.assertIn("Authentication failed", result.stdout)
        self.assertIn(
            "Basic " + base64.b64encode(f"x-access-token:{wrong}".encode()).decode(),
            self.server.authorizations,
        )
        self.assertNotIn(wrong, result.stdout)
        self.assertNotIn(wrong, self.trace.read_text())


if __name__ == "__main__":
    unittest.main()

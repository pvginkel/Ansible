"""argo-hand-render.py: an entry with `path:` renders from that directory, one without from the root.

Each test runs the script against a scratch registry and one scratch checkout that holds both a
root-layout app (chart/, config/prd/) and apps in directories, so reading the wrong one shows.
A fake `helm` first on PATH records its calls and answers `template` with one ConfigMap naming
the chart it was given, the stage values it read and the `--set` values, so the test needs no
helm, chart repository or Charts checkout.

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

import yaml

SCRIPT = Path(__file__).resolve().parent / "argo-hand-render.py"
KUBE_VERSION = "v1.33.4"

FAKE_HELM = f"""#!{sys.executable}
import json, os, sys
import yaml
args = sys.argv[1:]
with open(os.environ["FAKE_HELM_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] == "package":
    dest = args[args.index("-d") + 1]
    os.makedirs(dest, exist_ok=True)
    open(os.path.join(dest, "homelab-shared-0.0.0.tgz"), "w").close()
elif args[0] == "pull":
    dest = os.path.join(args[args.index("-d") + 1], args[1])
    os.makedirs(dest)
    with open(os.path.join(dest, "Chart.yaml"), "w") as f:
        f.write("name: " + args[1] + "\\n")
elif args[0] == "template":
    with open(os.path.join(args[2], "Chart.yaml")) as f:
        chart = yaml.safe_load(f)["name"]
    values = ""
    if "--values" in args:
        with open(args[args.index("--values") + 1]) as f:
            values = f.read().strip()
    sets = [args[i + 1] for i, arg in enumerate(args) if arg == "--set"]
    print(json.dumps({{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {{"name": chart}},
                      "data": {{"values": values, "sets": json.dumps(sets)}}}}))
"""

REGISTRY = {
    "apps": {
        "site": {
            "repo": "https://github.com/pvginkel/SiteDeploy.git",
            "stages": {"prd": {}},
        },
        "web": {
            "repo": "https://github.com/pvginkel/HomelabAppsDeploy.git",
            "path": "web",
            "stages": {"prd": {}},
        },
        "ingress": {
            "repo": "https://github.com/pvginkel/IngressDeploy.git",
            "upstream": {"repo": "https://charts.example", "chart": "ingress-nginx"},
            "stages": {"prd": {"version": "4.5.6"}},
        },
        "certs": {
            "repo": "https://github.com/pvginkel/PlatformAddOnsDeploy.git",
            "path": "addons/certs",
            "upstream": {"repo": "https://charts.example", "chart": "cert-manager"},
            "stages": {"prd": {"version": "1.2.3"}},
        },
    }
}

# Directory in the checkout -> the name its chart/Chart.yaml carries.
LAYOUTS = {"": "root-chart", "web": "web-chart", "addons/certs": "certs-companion"}


class HandRender(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "helm").write_text(FAKE_HELM)
        (bin_dir / "helm").chmod(0o755)
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        self.log = root / "helm.log"
        self.registry = root / "values.yaml"
        self.registry.write_text(yaml.safe_dump(REGISTRY))
        self.charts = root / "Charts"
        self.checkout = root / "checkout"
        for directory, chart in LAYOUTS.items():
            app = self.checkout / directory
            (app / "chart").mkdir(parents=True)
            (app / "chart" / "Chart.yaml").write_text(f"name: {chart}\n")
            (app / "config" / "prd").mkdir(parents=True)
            (app / "config" / "prd" / "values.yaml").write_text(f"from: {directory or 'root'}\n")
        git = ["git", "-C", str(self.checkout)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "commit", "-q", "-m", "seed"], check=True)
        self.revision = subprocess.run([*git, "rev-parse", "HEAD"], check=True,
                                       capture_output=True, text=True).stdout.strip()
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(root),
            "TMPDIR": str(self.tmp),
            "FAKE_HELM_LOG": str(self.log),
        }

    def render(self, app):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), app, str(self.checkout), "--kube-version", KUBE_VERSION,
             "--charts", str(self.charts), "--registry", str(self.registry)],
            env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        tmp = re.compile(re.escape(str(self.tmp)) + r"/[^/\"]+")
        calls = [json.loads(tmp.sub("<tmp>", line)) for line in self.log.read_text().splitlines()]
        return list(yaml.safe_load_all(result.stdout)), calls

    def hook(self, app, repo, path=None):
        sets = ["--set", f"hook.repo={repo}", "--set", f"hook.revision={self.revision}",
                "--set", "hook.stage=prd", "--set", f"hook.namespace={app}-prd"]
        return sets + ["--set", f"hook.path={path}"] if path else sets

    def template(self, app, chart, *extra):
        return ["template", f"{app}-prd", chart, "--namespace", f"{app}-prd",
                "--kube-version", KUBE_VERSION, "--include-crds", *extra]

    def package(self):
        return ["package", f"{self.charts}/charts/homelab-shared", "-d", "<tmp>/chart/charts"]

    def assert_rendered(self, doc, app, chart, values, sets):
        self.assertEqual(doc["metadata"]["name"], chart)
        self.assertEqual(doc["metadata"]["namespace"], f"{app}-prd")
        self.assertEqual(doc["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"],
                         f"{app}-prd:/ConfigMap:{app}-prd/{chart}")
        self.assertEqual(doc["data"]["values"], values)
        self.assertEqual(json.loads(doc["data"]["sets"]), sets[1::2])

    def test_local_app_without_path_renders_the_checkout_root(self):
        docs, calls = self.render("site")
        hook = self.hook("site", REGISTRY["apps"]["site"]["repo"])
        self.assertEqual(calls, [
            self.package(),
            self.template("site", "<tmp>/chart",
                          "--values", f"{self.checkout}/config/prd/values.yaml", *hook),
        ])
        self.assertEqual(len(docs), 1)
        self.assert_rendered(docs[0], "site", "root-chart", "from: root", hook)

    def test_local_app_with_path_renders_its_directory(self):
        docs, calls = self.render("web")
        hook = self.hook("web", REGISTRY["apps"]["web"]["repo"], "web")
        self.assertEqual(calls, [
            self.package(),
            self.template("web", "<tmp>/chart",
                          "--values", f"{self.checkout}/web/config/prd/values.yaml", *hook),
        ])
        self.assertEqual(len(docs), 1)
        self.assert_rendered(docs[0], "web", "web-chart", "from: web", hook)

    def test_upstream_app_without_path_reads_the_checkout_root(self):
        docs, calls = self.render("ingress")
        hook = self.hook("ingress", REGISTRY["apps"]["ingress"]["repo"])
        values = f"{self.checkout}/config/prd/values.yaml"
        self.assertEqual(calls, [
            self.package(),
            ["pull", "ingress-nginx", "--repo", "https://charts.example", "--version", "4.5.6",
             "--untar", "-d", "<tmp>/up"],
            self.template("ingress", "<tmp>/up/ingress-nginx", "--values", values),
            self.template("ingress", "<tmp>/chart", *hook),
        ])
        self.assertEqual(len(docs), 2)
        self.assert_rendered(docs[0], "ingress", "ingress-nginx", "from: root", [])
        self.assert_rendered(docs[1], "ingress", "root-chart", "", hook)

    def test_upstream_app_with_path_reads_its_directory(self):
        docs, calls = self.render("certs")
        hook = self.hook("certs", REGISTRY["apps"]["certs"]["repo"], "addons/certs")
        values = f"{self.checkout}/addons/certs/config/prd/values.yaml"
        self.assertEqual(calls, [
            self.package(),
            ["pull", "cert-manager", "--repo", "https://charts.example", "--version", "1.2.3",
             "--untar", "-d", "<tmp>/up"],
            self.template("certs", "<tmp>/up/cert-manager", "--values", values),
            self.template("certs", "<tmp>/chart", *hook),
        ])
        self.assertEqual(len(docs), 2)
        self.assert_rendered(docs[0], "certs", "cert-manager", "from: addons/certs", [])
        self.assert_rendered(docs[1], "certs", "certs-companion", "", hook)


if __name__ == "__main__":
    unittest.main()

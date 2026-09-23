#!/usr/bin/env python3
"""Moves one HelmCharts app stage onto Argo CD: the bulk migration's tool (argo-cd D51-D54).

It mechanises the steps KubeCoder's cutover ran by hand (docs/runbooks/kubecoder-cutover.md,
run record ANS-102). The steps, in order, are one subcommand each:

    scaffold   build /work/<Repo> from HelmCharts: chart, stage values, Terraform, producer files
    verify     the render must equal the live Helm release, object for object
    publish    create the GitHub repo and push main; create and build AaC/<Repo>
    register   add the producer to Architecture's pipeline-producers.yaml (commit only)
    flip       HelmCharts registry entry, autoSync false (commit only)
    surgery    move the stage's Terraform state from HelmCharts' key to the hook's
    plan       the no-destroy plan: only the webhook may be created
    preflight  stuck fields, and a server-side diff of the render against live
    sync       the manual sync, and its checks
    autosync   HelmCharts registry entry, autoSync true (commit only)

Every step checks what it needs and exits non-zero with a STOP line when anything differs
from the expected. A stopped app is parked: it keeps running as last deployed.

Pushes to HelmCharts and Architecture are left to the caller, so several apps can go in one push.

Run from the dev container; helm, kubectl and terraform run in the `iac` sidecar via cexec.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import yaml

HC = Path("/work/HelmCharts")
WORK = Path("/work")
HOME = Path.home()
KC = ["--kubeconfig", str(HOME / ".kube/config-prd-write"), "--context", "prd"]
HKC = ["--kubeconfig", str(HOME / ".kube/config-prd-write"), "--kube-context", "prd"]
TFB = ("http://127.0.0.1:6061/?type=git&repository=https%3A%2F%2Fgithub.com%2Fpvginkel%2F"
       "TerraformState&ref=main&state=")
LIB_VERSION = "0.2.1"
JENKINS = "https://jenkins.webathome.org"
GIT_CRED = "5f6fbd66-b41c-405f-b107-85ba6fd97f10"
RELAY = "https://deploy-hooks.webathome.org/api/webhook"
HOOK_PARAMS = "hook.repo={repo},hook.revision=0123456789abcdef0123456789abcdef01234567,hook.stage={stage},hook.namespace={ns}"


class Stop(Exception):
    pass


def log(msg: str) -> None:
    print(f"[argo-migrate] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True, env: dict | None = None,
        capture: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, env=env)
    if check and r.returncode != 0:
        raise Stop(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def iac(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None):
    return run(["cexec", "iac", *args], cwd=cwd, check=check, env=env)


# ---------------------------------------------------------------------------------------------
# The app


class App:
    def __init__(self, name: str, stage: str = "prd"):
        self.name = name
        self.stage = stage
        self.ns = f"{name}-{stage}"
        self.cfg = HC / "configs/prd" / name
        self.stage_dir = self.cfg / stage
        rel = self.stage_dir / "release.yaml"
        self.release = yaml.safe_load(rel.read_text()) if rel.exists() else {}
        self.release = self.release or {}
        self.chart_name = self.release.get("chart") or name
        self.repo_name = "".join(p.capitalize() for p in name.split("-")) + "Deploy"
        self.path = WORK / self.repo_name
        self.url = f"https://github.com/pvginkel/{self.repo_name}.git"
        self.producer = f"{name}-deploy"

    @property
    def chart_src(self) -> Path:
        return HC / "charts" / self.chart_name

    def load_state(self) -> dict:
        p = self.state_file
        return json.loads(p.read_text()) if p.exists() else {}

    def save_state(self, **kw) -> None:
        s = self.load_state()
        s.update(kw)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(s, indent=1))

    @property
    def state_file(self) -> Path:
        return HOME / "bulk-migration/state" / f"{self.ns}.json"


# ---------------------------------------------------------------------------------------------
# scaffold

NAMESPACE_TPL = """{{/*
The stage namespace is a tracked chart manifest (argo-cd D25), applied at wave -1 so it exists
before the PreSync hook runs. `Prune=false` blocks only the sync-time prune path (D26); the
Application-delete cascade still removes it.
*/}}
apiVersion: v1
kind: Namespace
metadata:
  name: {{ .Release.Namespace }}
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
    argocd.argoproj.io/sync-options: Prune=false
"""

HOOK_TPL = '{{- include "homelab-shared.tf-presync-hook" . -}}\n'

GITIGNORE = """# The pinned library chart is resolved by `helm dependency build` from chart/Chart.lock, here
# and on Argo's repo-server alike; the tarball is never committed.
/chart/charts/
# tests/terraform.sh's `terraform init`; the hook initialises its own clone.
/terraform/.terraform/
/terraform/.terraform.lock.hcl
# The architecture artifact is a build output (Jenkinsfile.architecture).
/docs/architecture/
"""

BUILD_DEPS = """#!/bin/sh
# Resolve chart/Chart.lock into chart/charts/, the same verb Argo's repo-server runs (D17).
set -eu
cd "$(dirname "$0")/.."
helm repo add charts-home https://charts.home --force-update >/dev/null
helm dependency build chart
"""

TERRAFORM_SH = """#!/bin/sh
# terraform/ is formatted and valid. No backend and no credentials.
set -eu
cd "$(dirname "$0")/.."
terraform fmt -check -diff -recursive
terraform -chdir=terraform init -backend=false -input=false -no-color
terraform -chdir=terraform validate -no-color
"""

JENKINSFILE_ARCH = """// Architecture producer pipeline for the federated Architecture-as-Code model: this repo
// publishes {app}'s {stage} stage as producer `{producer}` (argo-cd D50).
//
// The artifact is generated, never committed. gen-architecture renders chart/ the way Argo CD
// renders it, reads the judgment layer (architecture.yaml at the repo root) and writes
// docs/architecture/{producer}.yaml. One pipeline publishes one stage, from the branch Argo
// syncs it from: `main`.

library identifier: 'JenkinsPipelineUtils', changelog: false

podTemplate(inheritFrom: 'jenkins-agent', containers: [
    containerTemplates.aac_tools('aac-tools')
]) {{
    node(POD_LABEL) {{
        stage('Cloning repo') {{
            git branch: 'main',
                credentialsId: '{cred}',
                url: '{url}'
        }}

        stage('Architecture') {{
            container('aac-tools') {{
                sh 'gen-architecture --stage {stage} --producer {producer}'
                sh 'arch-validate docs/architecture/*.yaml'
            }}
            archiveArtifacts artifacts: 'docs/architecture/*.yaml', fingerprint: true
        }}
    }}
}}
"""

ARCHITECTURERC = """generated: true
sources:
  - architecture.yaml
  - chart/
  - config/{stage}/
instructions: >
  Generated producer (id {producer}). The artifact docs/architecture/{producer}.yaml is a
  build output: gitignored, produced by gen-architecture from the aac-tools image, which
  renders chart/ with config/{stage}/values.yaml the way Argo CD renders it. The
  AaC/{repo} Jenkins job runs it. Edit only the judgment layer, architecture.yaml at the
  repo root, whose schema is the generator's docstring.
"""

PROJECT_YAML = """# Curated entry points for {repo}. Helm and Terraform live in the `iac` toolchain sidecar.
projects:
  root:
    description: >-
      {app}'s deploy repo: its Helm chart on the homelab-shared library dependency, the
      per-stage configuration Argo CD syncs {app} from, the Terraform the PreSync hook
      applies, and the judgment layer of the `{producer}` architecture producer. There is no
      deploy pipeline: Argo deploys it (argo-cd D51).
    jenkins: AaC/{repo}
    lint:
      - cexec iac tests/build-deps.sh
      - >-
        cexec iac helm lint chart --namespace {ns} --values config/{stage}/values.yaml
        --set {hook}
      - cexec iac terraform fmt -check -diff -recursive
    test:
      - cexec iac tests/build-deps.sh
      - >-
        cexec iac helm template {ns} chart --namespace {ns} --values config/{stage}/values.yaml
        --set {hook}
      - cexec iac tests/terraform.sh
      - cexec aac-tools gen-architecture --stage {stage} --producer {producer}
      - cexec aac-tools arch-validate docs/architecture/{producer}.yaml
"""

README = """# {repo}

{app}'s deploy repository. Argo CD syncs the `{ns}` Application from `main`
(`chart/` with `config/{stage}/values.yaml`); the PreSync hook applies `terraform/` with
`config/{stage}/*.tfvars` against `argocd/{repo}/{stage}/terraform.tfstate`.

- **Images** are pinned in `config/{stage}/values.yaml`. The builds that produce them
  write the pins (argo-cd D53). Git is the deployed state; a rollback is a `git revert`.
- **Terraform** carries HelmCharts' modules under `terraform/modules/`, unchanged, so the
  state moved across with its addresses. The namespace is the chart's (`templates/namespace.yaml`),
  never Terraform's. `terraform/webhook.tf` owns this repository's GitHub webhook to the relay.
- **Architecture**: `Jenkinsfile.architecture` publishes producer `{producer}` (D50).

## Copied from HelmCharts

Migrated by the bulk migration (AnsibleSpecs `argo-cd/bulk-migration.md`, run record ANS-103)
from HelmCharts `{sha}`: `charts/{chart}`, `configs/prd/{app}/{stage}`,
`configs/prd/{app}/_shared` and `terraform-modules/`. HelmCharts no longer deploys this stage.
"""


def pascal_hook(app: App) -> str:
    return HOOK_PARAMS.format(repo=app.url, stage=app.stage, ns=app.ns)


# HelmCharts' Jenkinsfile hands every release its clone PAT (`--set-file gitToken=`); only
# version-poller reads it. It never enters a deploy repo.
SECRET_VALUES = {"gitToken"}


def helm_values(app: App) -> dict:
    r = iac("helm", *HKC, "get", "values", app.ns, "-n", app.ns, "-o", "json")
    live = json.loads(r.stdout) or {}
    for k in SECRET_VALUES:
        live.pop(k, None)
    return live


def compose_values(app: App, live: dict) -> str:
    """The stage values: HelmCharts' file verbatim, plus what the deploy CLI passed on top
    (global.environment and the resolved image digests), so the render equals the release."""
    src = app.stage_dir / "values.yaml"
    text = src.read_text() if src.exists() else ""
    cfg = yaml.safe_load(text) or {}
    extra = {}
    for k, v in live.items():
        if cfg.get(k) == v:
            continue
        if k in cfg:
            # A key the file has and the CLI overrode below it: rewrite the whole file from
            # the live values, comments lost, rather than splice YAML by hand.
            log(f"values: {k} differs below the top level; writing live values whole")
            return ("# The release's values as HelmCharts last deployed them (helm get values),\n"
                    f"# comments dropped; the commented original is HelmCharts' configs/prd/{app.name}/"
                    f"{app.stage}/values.yaml.\n" + yaml.safe_dump(live, sort_keys=False))
        extra[k] = v
    for k in cfg:
        if k not in live:
            raise Stop(f"values: {k} is in the config file but not in the release")
    if extra:
        text = text.rstrip("\n") + "\n\n" + (
            "# Passed by HelmCharts' deploy CLI on top of the file, now pinned here: the stage\n"
            "# name, and each image at the digest the release runs (argo-cd D53).\n"
        ) + yaml.safe_dump(extra, sort_keys=False)
    return text


def rewrite_tf(src: str) -> str:
    """HelmCharts' release Terraform, minus the namespace module (the chart owns it)."""
    out = re.sub(r'module\s+"namespace"\s*\{[^}]*\}\n?', "", src)
    out = out.replace("module.namespace.name", "var.namespace")
    out = out.replace("./terraform-modules/", "./modules/")
    if "module.namespace" in out:
        raise Stop("terraform: module.namespace is still referenced")
    return out


def providers_tf(app: App) -> str:
    src = (HC / "_providers/providers.tf").read_text()
    src = re.sub(r"^#.*\n(#.*\n)*\n", "", src, count=1)
    header = (
        "# Applied only by the Argo CD PreSync hook, from its own clone. It fills the empty http\n"
        "# backend at init and exports its environment (TF_VAR_*, HOMELAB_*, GITHUB_TOKEN,\n"
        "# KUBE_CONFIG_PATH). Adapted from HelmCharts' _providers/providers.tf: the same providers\n"
        "# and variables, plus github for this repository's webhook.\n\n"
    )
    src = src.replace(
        "  required_providers {\n",
        "  required_providers {\n    github = {\n      source = \"integrations/github\"\n    }\n", 1)
    src += '''
# Authenticates with GITHUB_TOKEN.
provider "github" {
  owner = "pvginkel"
}

variable "github_webhook_secret" {
  description = "Argo CD's shared webhook secret, the one the relay verifies deliveries against."
  type        = string
  sensitive   = true
  default     = ""
}

variable "manage_webhook" {
  description = "Whether this stage's state owns the repository's GitHub webhook. True in exactly one stage."
  type        = bool
  default     = false
}
'''
    return header + src


WEBHOOK_TF = """# This repository's push deliveries, to the webhook relay, which verifies the signature and
# forwards each delivery to argocd-server (argo-cd D39, D49).
resource "github_repository_webhook" "argocd" {{
  count = var.manage_webhook ? 1 : 0

  repository = "{repo}"
  active     = true
  events     = ["push"]

  configuration {{
    url          = "{relay}"
    content_type = "json"
    insecure_ssl = false
    secret       = var.github_webhook_secret
  }}
}}
"""


STAMP_DEFINE = re.compile(r'(\{\{-? define "deployment\.timestamp" -?\}\}\n)deployment: \{\{ dateInZone[^\n]*\n')


def pin_deployment_stamp(app: App, p: Path) -> str | None:
    """Replace the shared helper's render-time timestamp with a value from the stage config.

    Returns the live release's stamp, which must be the same on every pod template."""
    helpers = [f for f in (p / "chart/templates").glob("*.tpl") if "deployment.timestamp" in f.read_text()]
    used = any("deployment.timestamp" in f.read_text() for f in (p / "chart/templates").rglob("*.yaml"))
    if not used:
        return None
    if len(helpers) != 1:
        raise Stop(f"deployment.timestamp defined in {len(helpers)} helper files")
    text = helpers[0].read_text()
    new, n = STAMP_DEFINE.subn(r'\1deployment: {{ required "deploymentStamp" .Values.deploymentStamp | quote -}}\n', text)
    if n != 1:
        raise Stop("deployment.timestamp helper not in the expected shape")
    helpers[0].write_text(new)
    manifest = iac("helm", *HKC, "get", "manifest", app.ns, "-n", app.ns).stdout
    stamps = set(re.findall(r"^\s+deployment: ['\"]([^'\"]+)['\"]$", manifest, re.M))
    if len(stamps) != 1:
        raise Stop(f"live deployment stamps: {sorted(stamps)}")
    return stamps.pop()


def cmd_scaffold(app: App, args) -> None:
    if app.release.get("reconciler") == "argo-cd":
        raise Stop("already on Argo")
    if app.release.get("disabled"):
        raise Stop("disabled in HelmCharts")
    if app.release.get("upstream"):
        raise Stop("upstream chart: not handled by this tool yet")
    if not (app.chart_src / "Chart.yaml").exists():
        raise Stop(f"no local chart at {app.chart_src}")
    for h in ("post-render.sh", "post-install.sh", "post-rollout.sh", "pre-install.sh"):
        if (app.chart_src / h).exists():
            raise Stop(f"chart has {h}: late-migration set (D18)")
    stage_tf = [p for p in app.stage_dir.glob("*.tf")]
    if stage_tf:
        raise Stop(f"stage-level Terraform: {stage_tf}")
    if app.path.exists() and not args.force:
        raise Stop(f"{app.path} exists (use --force to rebuild it)")
    if app.path.exists():
        shutil.rmtree(app.path)

    sha = run(["git", "-C", str(HC), "rev-parse", "HEAD"]).stdout.strip()
    p = app.path
    (p / "chart").mkdir(parents=True)
    # The chart, minus HelmCharts-only files.
    for item in app.chart_src.iterdir():
        if item.name in ("architecture.yaml", "resources-entry-map.json"):
            continue
        dst = p / "chart" / item.name
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, dst)
    tdir = p / "chart/templates"
    tdir.mkdir(exist_ok=True)
    for t in tdir.iterdir():
        text = t.read_text() if t.is_file() else ""
        if re.search(r"kind:\s*Namespace\b", text):
            raise Stop(f"chart already renders a Namespace ({t.name})")
    (tdir / "namespace.yaml").write_text(NAMESPACE_TPL)
    (tdir / "tf-presync-hook.yaml").write_text(HOOK_TPL)
    manifests = app.stage_dir / "manifests.yaml"
    if manifests.exists():
        text = manifests.read_text()
        if "{{" in text:
            raise Stop("manifests.yaml contains template syntax")
        (tdir / "stage-manifests.yaml").write_text(
            "# HelmCharts applied these with kubectl after the release (configs/prd/"
            f"{app.name}/{app.stage}/manifests.yaml); they are chart content now.\n" + text)
    stamp = pin_deployment_stamp(app, p)
    chart_yaml = (p / "chart/Chart.yaml").read_text()
    if "dependencies:" in chart_yaml:
        raise Stop("chart already has dependencies")
    chart_yaml = chart_yaml.rstrip("\n") + (
        "\ndependencies:\n"
        "  # Exact, not a range (D17): the hook Job this chart renders moves on a deliberate commit.\n"
        "  - name: homelab-shared\n"
        f"    version: \"{LIB_VERSION}\"\n"
        "    repository: https://charts.home\n")
    (p / "chart/Chart.yaml").write_text(chart_yaml)
    name_in_chart = yaml.safe_load(chart_yaml)["name"]
    if name_in_chart != app.name:
        raise Stop(f"chart name {name_in_chart} != app {app.name}: the producer ids need them equal")

    # Stage configuration.
    cdir = p / "config" / app.stage
    cdir.mkdir(parents=True)
    live = helm_values(app)
    values_text = compose_values(app, live)
    if stamp:
        values_text = values_text.rstrip("\n") + (
            "\n\n# The pod-template `deployment` annotation, fixed at the value HelmCharts' last deploy\n"
            "# stamped: a render-time timestamp would leave the Application forever OutOfSync. Pods\n"
            "# roll when their spec changes (an image pin); bump this to force a restart.\n"
            f"deploymentStamp: {stamp!r}\n")
    (cdir / "values.yaml").write_text(values_text)
    for tv in app.stage_dir.glob("*.tfvars"):
        shutil.copy2(tv, cdir / tv.name)
    (cdir / "terraform.tfvars").write_text(
        "# This stage owns the repository's webhook; it is the only stage.\nmanage_webhook = true\n")

    # Terraform.
    tdir = p / "terraform"
    (tdir / "modules").mkdir(parents=True)
    body = ""
    for f in sorted((app.cfg / "_shared").glob("*.tf")):
        body += rewrite_tf(f.read_text())
    mods = sorted(set(re.findall(r"\./modules/([\w-]+)", body)))
    for m in mods:
        shutil.copytree(HC / "terraform-modules" / m, tdir / "modules" / m)
    (tdir / "main.tf").write_text(body.strip() + "\n" if body.strip() else
                                  "# No durable resources beyond the webhook.\n")
    (tdir / "providers.tf").write_text(providers_tf(app))
    (tdir / "webhook.tf").write_text(WEBHOOK_TF.format(repo=app.repo_name, relay=RELAY))

    # Architecture producer.
    arch = app.chart_src / "architecture.yaml"
    introduced = run(["git", "-C", str(HC), "log", "--diff-filter=A", "--reverse", "--format=%ad",
                      "--date=short", "--", f"charts/{app.chart_name}"]).stdout.split("\n")[0].strip()
    atext = arch.read_text() if arch.exists() else ""
    if re.search(r"^introduced:", atext, re.M):
        raise Stop("architecture.yaml already states introduced:")
    (p / "architecture.yaml").write_text(
        "# The judgment layer the aac-tools generator reads (Jenkinsfile.architecture); copied\n"
        f"# verbatim from HelmCharts' charts/{app.chart_name}/architecture.yaml.\n\n"
        "# The date HelmCharts derives from the first commit adding the chart, which every\n"
        "# published element carries.\n"
        f"introduced: '{introduced}'\n\n" + atext)
    (p / "Jenkinsfile.architecture").write_text(JENKINSFILE_ARCH.format(
        app=app.name, stage=app.stage, producer=app.producer, cred=GIT_CRED, url=app.url))
    (p / ".architecturerc").write_text(ARCHITECTURERC.format(
        stage=app.stage, producer=app.producer, repo=app.repo_name))
    (p / ".gitignore").write_text(GITIGNORE)
    (p / ".kubecoder").mkdir()
    (p / ".kubecoder/project.yaml").write_text(PROJECT_YAML.format(
        repo=app.repo_name, app=app.name, producer=app.producer, ns=app.ns, stage=app.stage,
        hook=pascal_hook(app)))
    (p / "tests").mkdir()
    for n, body in (("build-deps.sh", BUILD_DEPS), ("terraform.sh", TERRAFORM_SH)):
        (p / "tests" / n).write_text(body)
        (p / "tests" / n).chmod(0o755)
    (p / "README.md").write_text(README.format(
        repo=app.repo_name, app=app.name, ns=app.ns, stage=app.stage, producer=app.producer,
        sha=sha[:7], chart=app.chart_name))

    # Chart.lock, then the gates.
    iac("helm", "repo", "add", "charts-home", "https://charts.home", "--force-update", cwd=p)
    iac("helm", "dependency", "update", "chart", cwd=p)
    iac("terraform", "fmt", "-recursive", cwd=p)
    run(["git", "init", "-q", "-b", "main"], cwd=p)
    run(["git", "add", "-A"], cwd=p)
    run(["git", "commit", "-q", "-m",
         f"{app.name}: deploy repo, migrated from HelmCharts {sha[:7]} (argo-cd D51)"], cwd=p)
    app.save_state(scaffolded=sha)
    log(f"scaffolded {p} from HelmCharts {sha[:7]}; modules: {', '.join(mods) or 'none'}")


# ---------------------------------------------------------------------------------------------
# verify: the render equals the live release


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def key(d: dict) -> tuple:
    m = d["metadata"]
    return (d.get("kind"), m.get("name") or m.get("generateName"))


def render(app: App, revision: str = "0123456789abcdef0123456789abcdef01234567") -> str:
    iac("tests/build-deps.sh", cwd=app.path)
    hook = HOOK_PARAMS.replace("0123456789abcdef0123456789abcdef01234567", revision).format(
        repo=app.url, stage=app.stage, ns=app.ns)
    r = iac("helm", "template", app.ns, "chart", "--namespace", app.ns, "--values",
            f"config/{app.stage}/values.yaml", "--set", hook, cwd=app.path)
    return r.stdout


def cmd_verify(app: App, args) -> None:
    rendered = {key(d): d for d in docs(render(app))}
    live_text = iac("helm", *HKC, "get", "manifest", app.ns, "-n", app.ns).stdout
    live = {key(d): d for d in docs(live_text)}
    manifests = app.stage_dir / "manifests.yaml"
    if manifests.exists():
        for d in docs(manifests.read_text()):
            live[key(d)] = d
    expected_extra = {("Namespace", app.ns)}
    extra = set(rendered) - set(live)
    hook = {k for k in extra if k[0] == "Job" and k[1].startswith("tf-presync")}
    extra -= hook | expected_extra
    missing = set(live) - set(rendered)
    diffs = []
    for k in set(rendered) & set(live):
        if rendered[k] != live[k]:
            diffs.append(k)
    problems = []
    if not hook:
        problems.append("no tf-presync hook Job rendered")
    if extra:
        problems.append(f"objects only in the render: {sorted(extra)}")
    if missing:
        problems.append(f"objects only in the live release: {sorted(missing)}")
    for k in diffs:
        a = yaml.safe_dump(live[k], sort_keys=True).splitlines()
        b = yaml.safe_dump(rendered[k], sort_keys=True).splitlines()
        import difflib
        problems.append(f"{k} differs:\n" + "\n".join(difflib.unified_diff(a, b, "live", "render", lineterm="", n=1)))
    if problems:
        raise Stop("render != live release:\n" + "\n".join(problems))
    log(f"render equals the live release: {len(live)} objects, plus the Namespace and the hook Job")
    app.save_state(verified=True)


# ---------------------------------------------------------------------------------------------
# publish: GitHub repo, AaC job


def jenkins(method: str, path: str, data: bytes | None = None, ctype: str = "application/xml"):
    cmd = ["curl", "-sS", "-g", "-o", "/dev/stderr", "-w", "%{http_code}", "-u",
           f"admin:{os.environ['JENKINS_TOKEN']}", "-X", method, f"{JENKINS}{path}"]
    if data is not None:
        cmd += ["-H", f"Content-Type: {ctype}", "--data-binary", "@-"]
    r = subprocess.run(cmd, input=data.decode() if data else None, text=True, capture_output=True)
    return r.stdout.strip(), r.stderr


def cmd_publish(app: App, args) -> None:
    p = app.path
    iac("sh", "-c", "tests/build-deps.sh && tests/terraform.sh", cwd=p)
    r = run(["gh", "repo", "view", f"pvginkel/{app.repo_name}"], check=False)
    if r.returncode != 0:
        run(["gh", "repo", "create", f"pvginkel/{app.repo_name}", "--private",
             "--description", f"{app.name}'s deploy repository: Argo CD syncs it (argo-cd D51)."])
    if not run(["git", "remote"], cwd=p).stdout.strip():
        run(["git", "remote", "add", "origin", app.url], cwd=p)
    run(["git", "push", "-q", "-u", "origin", "main"], cwd=p)
    # AaC/<Repo>, from AaC/KubeCoderDeploy's config.
    code, _ = jenkins("GET", f"/job/AaC/job/{app.repo_name}/api/json")
    if code != "200":
        tpl = subprocess.run(["curl", "-sS", "-g", "-u", f"admin:{os.environ['JENKINS_TOKEN']}",
                              f"{JENKINS}/job/AaC/job/KubeCoderDeploy/config.xml"],
                             text=True, capture_output=True).stdout
        if "KubeCoderDeploy.git" not in tpl:
            raise Stop("could not read AaC/KubeCoderDeploy's config.xml")
        cfg = tpl.replace("KubeCoderDeploy", app.repo_name).replace("*/prd", "*/main")
        cfg = re.sub(r"<description>.*?</description>",
                     f"<description>Architecture producer {app.producer}</description>", cfg, flags=re.S)
        code, err = jenkins("POST", f"/job/AaC/createItem?name={app.repo_name}", cfg.encode())
        if code not in ("200", "302"):
            raise Stop(f"createItem AaC/{app.repo_name}: HTTP {code} {err[:300]}")
    build = aac_build(app)
    app.save_state(published=True, aac_build=build)
    log(f"{app.url} pushed; AaC/{app.repo_name} #{build} green")


def aac_build(app: App) -> int:
    job = f"/job/AaC/job/{app.repo_name}"
    before = json.loads(subprocess.run(
        ["curl", "-sS", "-g", "-u", f"admin:{os.environ['JENKINS_TOKEN']}",
         f"{JENKINS}{job}/api/json?tree=nextBuildNumber"], text=True, capture_output=True).stdout)
    n = before["nextBuildNumber"]
    code, err = jenkins("POST", f"{job}/build", b"", "application/x-www-form-urlencoded")
    if code not in ("200", "201", "302"):
        raise Stop(f"build AaC/{app.repo_name}: HTTP {code} {err[:300]}")
    for _ in range(120):
        time.sleep(10)
        r = subprocess.run(["curl", "-sS", "-g", "-u", f"admin:{os.environ['JENKINS_TOKEN']}",
                            f"{JENKINS}{job}/{n}/api/json?tree=result,building"],
                           text=True, capture_output=True)
        try:
            b = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        if not b.get("building") and b.get("result"):
            if b["result"] != "SUCCESS":
                raise Stop(f"AaC/{app.repo_name} #{n}: {b['result']}")
            return n
    raise Stop(f"AaC/{app.repo_name} #{n} did not finish in 20 min")


# ---------------------------------------------------------------------------------------------
# register, flip, autosync: commits only


def cmd_register(app: App, args) -> None:
    arch = WORK / "Architecture"
    f = arch / "pipeline-producers.yaml"
    text = f.read_text()
    if f"id: {app.producer}\n" in text:
        log("producer already registered")
        return
    text = text.rstrip("\n") + (f"\n  - id: {app.producer}\n    repo: pvginkel/{app.repo_name}\n"
                                f"    jenkinsJob: AaC/{app.repo_name}\n")
    f.write_text(text)
    run(["git", "add", "pipeline-producers.yaml"], cwd=arch)
    run(["git", "commit", "-q", "-m", f"Registry: the {app.producer} producer"], cwd=arch)
    log(f"registered {app.producer} (Architecture, local commit)")


def registry_entry(app: App, auto: bool) -> str:
    return ("reconciler: argo-cd\ndeployed: true\n"
            f"autoSync: {'true' if auto else 'false'}\n"
            f"repo: {app.url}\ntargetRevision: main\n")


def cmd_flip(app: App, args) -> None:
    d = app.stage_dir
    (d / "release.yaml").write_text(registry_entry(app, False))
    for n in ("values.yaml", "manifests.yaml"):
        if (d / n).exists():
            run(["git", "rm", "-q", str(d / n)], cwd=HC)
    run(["git", "add", str(d)], cwd=HC)
    run(["git", "commit", "-q", "-m",
         f"{app.name} {app.stage}: Argo CD reconciles the stage from {app.repo_name} (argo-cd D51)"], cwd=HC)
    app.save_state(flipped=True)
    log("registry flipped, autoSync false (HelmCharts, local commit)")


def cmd_autosync(app: App, args) -> None:
    (app.stage_dir / "release.yaml").write_text(registry_entry(app, True))
    run(["git", "add", str(app.stage_dir)], cwd=HC)
    run(["git", "commit", "-q", "-m", f"{app.name} {app.stage}: Argo CD auto-syncs the stage (argo-cd D51)"], cwd=HC)
    app.save_state(autosync=True)
    log("autoSync true (HelmCharts, local commit)")


# ---------------------------------------------------------------------------------------------
# surgery


def tfinit(workdir: Path, url: str, *extra: str, env: dict | None = None) -> None:
    iac("terraform", "init", "-input=false", "-reconfigure", *extra,
        f"-backend-config=address={url}", f"-backend-config=lock_address={url}",
        f"-backend-config=unlock_address={url}", cwd=workdir, env=env)


def enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def keys(app: App) -> tuple[str, str]:
    src = TFB + enc(f"helm-charts/prd/{app.name}/{app.stage}/infra.tfstate")
    dst = TFB + enc(f"argocd/{app.repo_name}/{app.stage}/terraform.tfstate")
    return src, dst


def cmd_surgery(app: App, args) -> None:
    st = app.load_state()
    if not st.get("flipped"):
        raise Stop("not flipped: the registry commit comes before the state surgery")
    w = HOME / "bulk-migration/tf" / app.ns
    shutil.rmtree(w, ignore_errors=True)
    for sub in ("src", "dst", "moves"):
        (w / sub).mkdir(parents=True)
    for sub in ("src", "dst"):
        (w / sub / "backend.tf").write_text('terraform {\n  backend "http" {}\n}\n')
    src, dst = keys(app)
    try:
        tfinit(w / "src", src)
        addrs = iac("terraform", "state", "list", cwd=w / "src").stdout.split()
        ns_addr = "module.namespace.kubernetes_namespace_v1.this"
        if ns_addr not in addrs:
            raise Stop(f"source state lacks {ns_addr}: {addrs}")
        rest = [a for a in addrs if not a.startswith("module.namespace.")]
        if any(not a.startswith("module.") for a in rest):
            raise Stop(f"source state holds non-module addresses: {rest}")
        log(f"source state: {addrs}")
        tfinit(w / "dst", dst)
        pulled = json.loads(iac("terraform", "state", "pull", cwd=w / "dst").stdout or "{}")
        if pulled.get("resources"):
            raise Stop(f"destination state is not empty: serial {pulled.get('serial')}")
        r = iac("terraform", "state", "rm", "module.namespace", cwd=w / "src")
        if "Successfully removed 1 resource instance(s)." not in r.stdout:
            raise Stop(f"state rm: {r.stdout}")
        if rest:
            (w / "moves/src.tfstate").write_text(iac("terraform", "state", "pull", cwd=w / "src").stdout)
            mods = sorted({".".join(a.split(".")[:2]) for a in rest})
            for m in mods:
                iac("terraform", "state", "mv", "-state=src.tfstate", "-state-out=dst.tfstate", m, m,
                    cwd=w / "moves")
            moved = iac("terraform", "state", "list", "-state=dst.tfstate", cwd=w / "moves").stdout.split()
            left = iac("terraform", "state", "list", "-state=src.tfstate", cwd=w / "moves").stdout.split()
            if sorted(moved) != sorted(rest) or left:
                raise Stop(f"local move: moved {moved}, left {left}")
            iac("terraform", "state", "push", "../moves/dst.tfstate", cwd=w / "dst")
            iac("terraform", "state", "push", "../moves/src.tfstate", cwd=w / "src")
            after_dst = iac("terraform", "state", "list", cwd=w / "dst").stdout.split()
            after_src = iac("terraform", "state", "list", cwd=w / "src").stdout.split()
            if sorted(after_dst) != sorted(rest) or after_src:
                raise Stop(f"after push: dst {after_dst}, src {after_src}")
        log(f"state moved: {len(rest)} address(es) to argocd/{app.repo_name}/{app.stage}; namespace released")
        app.save_state(surgery=rest)
    finally:
        shutil.rmtree(w / "moves", ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# plan


def cmd_plan(app: App, args) -> None:
    clusters = yaml.safe_load((HC / "_providers/clusters.yaml").read_text())["prd"]
    exports = dict(clusters.get("env", {}))
    for k, v in clusters.get("tf_vars", {}).items():
        exports[f"TF_VAR_{k}"] = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    w = HOME / "bulk-migration/tf" / app.ns / "plan"
    w.mkdir(parents=True, exist_ok=True)
    exports.update({
        "TF_DATA_DIR": str(w), "TF_VAR_namespace": app.ns, "TF_VAR_stage": app.stage,
        "TF_VAR_github_webhook_secret": "plan-placeholder",
    })
    _, dst = keys(app)
    q = shlex_quote
    varfiles = " ".join(q(f"-var-file=../config/{app.stage}/{f.name}")
                        for f in sorted((app.path / "config" / app.stage).glob("*.tfvars")))
    backend = " ".join(q(f"-backend-config={k}={dst}") for k in ("address", "lock_address", "unlock_address"))
    script = "\n".join([
        "set -e",
        "cd /work/Ansible && . scripts/bao-login.sh >/dev/null",
        "cd /work/HelmCharts && . scripts/setup-env.sh prd",
        *[f"export {k}={q(v)}" for k, v in exports.items()],
        'export GITHUB_TOKEN="$GH_TOKEN"',
        f"cd {q(str(app.path / 'terraform'))}",
        f"cexec iac terraform init -input=false -reconfigure -upgrade {backend} >/dev/null",
        f"cexec iac terraform plan -input=false -no-color {varfiles}",
    ])
    r = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    out = r.stdout + r.stderr
    (HOME / "bulk-migration/logs").mkdir(parents=True, exist_ok=True)
    (HOME / "bulk-migration/logs" / f"{app.ns}.plan.txt").write_text(out)
    shutil.rmtree(w, ignore_errors=True)
    if r.returncode != 0:
        raise Stop(f"plan failed:\n{out[-3000:]}")
    if "0 missing" not in out:
        raise Stop("setup-env did not export every credential")
    want = "Plan: 1 to add, 0 to change, 0 to destroy."
    adds = re.findall(r"# (\S+) will be created", out)
    if want not in out or adds != ["github_repository_webhook.argocd[0]"]:
        summary = [l for l in out.splitlines() if re.search(r"#.*will be|must be replaced|Plan:|No changes", l)]
        raise Stop("plan is not the webhook alone:\n" + "\n".join(summary))
    log("plan: the webhook alone (1 to add, 0 to change, 0 to destroy)")
    app.save_state(planned=True)


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------------------------
# preflight: stuck fields and a server-side diff


EXPECTED_STUCK = {"metadata.labels", "metadata.annotations"}


def cmd_preflight(app: App, args) -> None:
    run(["git", "pull", "-q", "--ff-only"], cwd=app.path, check=False)
    sha = run(["git", "rev-parse", "origin/main"], cwd=app.path).stdout.strip()
    rtext = render(app, sha)
    tmp = HOME / "bulk-migration/tmp" / app.ns
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        (tmp / "render.yaml").write_text(rtext)
        kinds = ("serviceaccount,configmap,secret,persistentvolumeclaim,service,deployment,statefulset,"
                 "daemonset,job,cronjob,ingress,networkpolicy,role,rolebinding,externalsecrets")
        live = json.loads(iac("kubectl", *KC, "get", kinds, "-n", app.ns, "-o", "json",
                              "--show-managed-fields").stdout)
        for it in live["items"]:
            if it["kind"] == "Secret":
                it.pop("data", None)
                it.pop("stringData", None)
                it["metadata"].get("annotations", {}).pop(
                    "kubectl.kubernetes.io/last-applied-configuration", None)
        (tmp / "live.json").write_text(json.dumps(live))
        extras = []
        for d in docs(rtext):
            if d["kind"] in ("Namespace", "ClusterRole", "ClusterRoleBinding", "PersistentVolume",
                             "StorageClass", "CustomResourceDefinition", "ValidatingWebhookConfiguration",
                             "MutatingWebhookConfiguration", "PriorityClass", "IngressClass"):
                n = d["metadata"]["name"]
                f = tmp / f"{d['kind']}-{n}.json"
                r = iac("kubectl", *KC, "get", d["kind"].lower(), n, "-o", "json", "--show-managed-fields",
                        check=False)
                if r.returncode == 0:
                    f.write_text(r.stdout)
                    extras.append(str(f))
        r = run(["python3", "/work/AnsibleSpecs/handovers/argo-adoption-blind-spot/stuck_fields.py",
                 app.ns, str(tmp / "render.yaml"), str(tmp / "live.json"), *extras], check=False)
        out = r.stdout + r.stderr
        (HOME / "bulk-migration/logs" / f"{app.ns}.preflight.txt").write_text(out)
        problems = preflight_problems(out, app)
        # Server-side diff of the render against live: what the sync would change.
        objs = [d for d in docs(rtext) if not (d["kind"] == "Job" and key(d)[1].startswith("tf-presync"))]
        (tmp / "apply.yaml").write_text(yaml.safe_dump_all(objs))
        d = iac("kubectl", *KC, "diff", "--server-side=false", "-f", str(tmp / "apply.yaml"),
                check=False)
        (HOME / "bulk-migration/logs" / f"{app.ns}.diff.txt").write_text(d.stdout + d.stderr)
        changed = diff_problems(d.stdout)
        if d.returncode not in (0, 1):
            problems.append(f"kubectl diff failed: {d.stderr[-500:]}")
        problems += changed
        if problems:
            raise Stop("preflight:\n" + "\n".join(problems))
        log(f"preflight clean at {sha[:7]}: residue is Helm labels/annotations only; diff is metadata only")
        app.save_state(preflight=sha)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def preflight_problems(out: str, app: App) -> list[str]:
    problems = []
    # stuck_fields prints "A." objects Helm made that the render lacks, "B." stuck fields.
    section = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("A.") or s.lower().startswith("objects"):
            section = "A"
        elif s.startswith("B.") or s.lower().startswith("stuck"):
            section = "B"
        elif s.startswith("TOTAL"):
            section = None
        elif section == "A" and s and not s.startswith("(") and "Secret/" not in s:
            problems.append(f"object not in the render: {s}")
        elif section == "B" and s:
            field = s.split()[-1] if s.split() else ""
            if not any(field.startswith(f) or f in s for f in EXPECTED_STUCK):
                problems.append(f"stuck field: {s}")
    return problems


def diff_problems(diff: str) -> list[str]:
    """Changed lines in `kubectl diff` other than metadata bookkeeping."""
    ok = re.compile(r"^[+-]\s*(generation:|resourceVersion:|kubectl\.kubernetes\.io/last-applied-configuration|"
                    r"\{\"apiVersion\"|argocd\.argoproj\.io/|annotations:\s*$|managedFields|"
                    r"- apiVersion:|fieldsType:|fieldsV1:|f:|manager:|operation:|time:|\.:\s*\{\}|"
                    r"k:|v:|apiVersion:|$)")
    bad = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "diff ")):
            continue
        if line.startswith(("+", "-")) and not ok.match(line):
            bad.append(f"diff: {line[:200]}")
    return bad[:40]


# ---------------------------------------------------------------------------------------------
# sync


def app_status(app: App) -> dict:
    r = iac("kubectl", *KC, "get", "application", "-n", "argocd-prd", app.ns, "-o", "json", check=False)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def cmd_sync(app: App, args) -> None:
    st = app.load_state()
    for need in ("surgery", "planned", "preflight"):
        if need not in st:
            raise Stop(f"{need} not done")
    a = app_status(app)
    if not a:
        raise Stop("no Application")
    rev = st["preflight"]
    if a["status"].get("sync", {}).get("revision") != rev:
        raise Stop(f"Application compares {a['status'].get('sync', {}).get('revision')}, not {rev}")
    before = {p["metadata"]["name"] for p in json.loads(iac(
        "kubectl", *KC, "get", "pods", "-n", app.ns, "-o", "json").stdout)["items"]}
    patch = {"operation": {"initiatedBy": {"username": "claude-bulk-migration"},
                           "sync": {"revision": rev}}}
    iac("kubectl", *KC, "patch", "application", "-n", "argocd-prd", app.ns, "--type", "merge",
        "-p", json.dumps(patch))
    log(f"sync started at {rev[:7]}")
    phase = None
    for _ in range(90):
        time.sleep(10)
        a = app_status(app)
        op = a.get("status", {}).get("operationState", {})
        if op.get("syncResult", {}).get("revision") == rev and op.get("phase") in ("Succeeded", "Failed", "Error"):
            phase = op["phase"]
            break
    if phase != "Succeeded":
        raise Stop(f"sync phase {phase}: {a.get('status', {}).get('operationState', {}).get('message')}")
    # Health can lag a rollout.
    for _ in range(60):
        a = app_status(app)
        s = a["status"]
        if s["sync"]["status"] == "Synced" and s["health"]["status"] == "Healthy":
            break
        time.sleep(10)
    s = a["status"]
    jobs = json.loads(iac("kubectl", *KC, "get", "jobs", "-n", "argocd-hooks", "-o", "json").stdout)["items"]
    hook = [j for j in jobs if j["metadata"]["name"].startswith(f"tf-presync-{rev[:7]}")
            and any(a2.get("value") == app.ns for c in j["spec"]["template"]["spec"]["containers"]
                    for a2 in [{"value": x} for x in c.get("args", [])])]
    logs = ""
    if hook:
        logs = iac("kubectl", *KC, "logs", "-n", "argocd-hooks", f"job/{hook[-1]['metadata']['name']}",
                   check=False).stdout
    (HOME / "bulk-migration/logs" / f"{app.ns}.hook.txt").write_text(logs)
    applied = re.findall(r"Apply complete! Resources: .*", logs)
    problems = []
    if s["sync"]["status"] != "Synced" or s["health"]["status"] != "Healthy":
        problems.append(f"Application {s['sync']['status']} {s['health']['status']}")
    if applied != ["Apply complete! Resources: 1 added, 0 changed, 0 destroyed."]:
        problems.append(f"hook: {applied or 'no apply line'}")
    if problems:
        raise Stop("sync checks:\n" + "\n".join(problems))
    log(f"synced {rev[:7]}: Synced Healthy; hook 1 added (webhook)")
    app.save_state(synced=rev)


# ---------------------------------------------------------------------------------------------


STEPS = {
    "scaffold": cmd_scaffold, "verify": cmd_verify, "publish": cmd_publish,
    "register": cmd_register, "flip": cmd_flip, "surgery": cmd_surgery, "plan": cmd_plan,
    "preflight": cmd_preflight, "sync": cmd_sync, "autosync": cmd_autosync,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=list(STEPS))
    ap.add_argument("apps", nargs="+")
    ap.add_argument("--stage", default="prd")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    rc = 0
    for name in args.apps:
        app = App(name, args.stage)
        try:
            STEPS[args.step](app, args)
        except Stop as e:
            print(f"[argo-migrate] STOP {app.ns} at {args.step}: {e}", flush=True)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

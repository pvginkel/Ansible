#!/usr/bin/env python3
"""Moves one HelmCharts app stage onto Argo CD: the bulk migration's tool (argo-cd D51-D54).

It mechanises the steps KubeCoder's cutover ran by hand (docs/runbooks/kubecoder-cutover.md,
run record ANS-102). The steps, in order, are one subcommand each:

    scaffold   build /work/<Repo> from HelmCharts: chart, stage values, Terraform, producer files
    verify     the render must equal the live Helm release, object for object
    arch       the architecture producer handover: nothing helm-charts publishes may be lost,
               and HelmCharts still builds and draws its edges without the app
    pins       DockerImages deploy-pins.json entries; lists app-built images for their Jenkinsfiles
    publish    create the GitHub repo and push main; create and build AaC/<Repo>
    register   add the producer to Architecture's pipeline-producers.yaml (commit only)
    flip       HelmCharts registry entry, autoSync false (commit only)
    surgery    move the stage's Terraform state from HelmCharts' key to the hook's
    plan       the no-destroy plan: only the webhook may be created
    preflight  stuck fields, and a server-side diff of the render against live
    sync       the manual sync, and its checks
    unreplace  after the first sync, drop the SSE rewrite's Replace=true from the Deployment (pushed)
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
import urllib.request
from pathlib import Path

import yaml

HC = Path("/work/HelmCharts")
WORK = Path("/work")
HOME = Path.home()
KC = ["--kubeconfig", str(HOME / ".kube/config-prd-write"), "--context", "prd"]
HKC = ["--kubeconfig", str(HOME / ".kube/config-prd-write"), "--kube-context", "prd"]
TFB = ("http://127.0.0.1:6061/?type=git&repository=https%3A%2F%2Fgithub.com%2Fpvginkel%2F"
       "TerraformState&ref=main&state=")
LIB_VERSION = "0.3.0"
JENKINS = "https://jenkins.webathome.org"
GIT_CRED = "5f6fbd66-b41c-405f-b107-85ba6fd97f10"
RELAY = "https://deploy-hooks.webathome.org/api/webhook"
DATASET_URL = "https://architecture.webathome.org/data/v0.1/architecture.yaml"
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
        # HelmCharts' shape before the flip ({repo_name, repo_url, chart: <repo>/<chart>}),
        # the registry's after it ({repo, chart, version}).
        self.upstream = self.release.get("upstream")
        self.repo_name = "".join(p.capitalize() for p in name.split("-")) + "Deploy"
        self.path = WORK / self.repo_name
        self.url = f"https://github.com/pvginkel/{self.repo_name}.git"
        # One pipeline publishes one stage (D50): prd keeps the plain names, another stage of the
        # same deploy repo gets its own producer, AaC job and Jenkinsfile.
        self.producer = f"{name}-deploy" if stage == "prd" else f"{name}-{stage}-deploy"
        self.aac_job = self.repo_name if stage == "prd" else f"{self.repo_name}-{stage}"
        self.arch_file = "Jenkinsfile.architecture" if stage == "prd" else f"Jenkinsfile.architecture-{stage}"

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


COMPANION_CHART = """apiVersion: v2
name: {app}
description: >-
  {app}'s companion chart (argo-cd D56): the stage Namespace, the Terraform PreSync hook and the
  manifests HelmCharts applied beside the release. The app itself is the upstream chart
  {up_chart} {up_version} from {up_repo}, which Argo CD renders as a separate source; this chart never
  depends on it.
type: application
version: 0.1.0
"""

UPSTREAM_PROJECT_YAML = """# Curated entry points for {repo}. Helm and Terraform live in the `iac` toolchain sidecar.
projects:
  root:
    description: >-
      {app}'s deploy repo: the stage values for the upstream chart {up_chart} {up_version}, the
      companion chart (the Namespace, the PreSync hook on the homelab-shared library and the
      estate's own manifests), the Terraform the hook applies, and the judgment layer of the
      `{producer}` architecture producer. There is no deploy pipeline: Argo deploys it
      (argo-cd D51, D56).
    jenkins: AaC/{repo}
    lint:
      - cexec iac tests/build-deps.sh
      - cexec iac helm lint chart --namespace {ns} --set {hook}
      - cexec iac terraform fmt -check -diff -recursive
    test:
      - cexec iac tests/build-deps.sh
      - >-
        cexec iac helm template {ns} {up_chart} --repo {up_repo} --version {up_version}
        --namespace {ns} --values config/{stage}/values.yaml
      - cexec iac helm template {ns} chart --namespace {ns} --set {hook}
      - cexec iac tests/terraform.sh
      - cexec aac-tools gen-architecture --stage {stage} --producer {producer}
      - cexec aac-tools arch-validate docs/architecture/{producer}.yaml
"""

UPSTREAM_README = """# {repo}

{app}'s deploy repository. Argo CD syncs the `{ns}` Application from three sources
(argo-cd D18, D56): the upstream chart `{up_chart}` {up_version} from {up_repo} with
`config/{stage}/values.yaml`, this repository for those values, and `chart/`, the companion.
The PreSync hook applies `terraform/` with `config/{stage}/*.tfvars` against
`argocd/{repo}/{stage}/terraform.tfstate`.

- **The chart version** is pinned in two places that must agree: this app's registry entry
  (HelmCharts `configs/prd/{app}/{stage}/release.yaml`, `upstream.version`), which Argo
  renders, and `architecture.yaml`'s `upstream:` block, which the architecture generator
  renders (D57). Bump both in one change.
- **The companion** `chart/` renders only estate content: the Namespace (D25), the hook
  include, and what HelmCharts applied as `manifests.yaml`. It takes the hook parameters and
  no values file.
- **Terraform**: `terraform/webhook.tf` owns this repository's GitHub webhook to the relay.
  The namespace is the companion's (`templates/namespace.yaml`), never Terraform's.
- **Architecture**: `Jenkinsfile.architecture` publishes producer `{producer}` (D50).

## Copied from HelmCharts

Migrated by the bulk migration (AnsibleSpecs `argo-cd/bulk-migration.md`, run record ANS-103)
from HelmCharts `{sha}`: `charts/{app}/architecture.yaml`, `configs/prd/{app}/{stage}` and
`configs/prd/{app}/_shared`. HelmCharts no longer deploys this stage.
"""


def pascal_hook(app: App) -> str:
    return HOOK_PARAMS.format(repo=app.url, stage=app.stage, ns=app.ns)


def dependency_repos(p: Path) -> list[tuple[str, str]]:
    """The chart's remote dependency repositories other than charts.home, for helm repo add."""
    meta = yaml.safe_load((p / "chart/Chart.yaml").read_text())
    out = []
    for d in meta.get("dependencies") or []:
        url = d.get("repository") or ""
        if url.startswith("https://") and url != "https://charts.home":
            out.append((re.sub(r"[^a-z0-9]+", "-", url.split("://", 1)[1].lower()).strip("-"), url))
    return sorted(set(out))


CLEARTEXT = re.compile(r"^ *# [\w-]+: [A-Za-z0-9]{40,}\n", re.M)


def strip_cleartext_passwords(text: str) -> str:
    """mosquitto's values file lists its users' cleartext passwords in a comment beside the
    bcrypt entries; the deploy repo carries the entries alone."""
    new, n = CLEARTEXT.subn("", text)
    if n:
        new = new.replace("  # /mosquitto/pw -h bcrypt -p <password>\n",
                          "  # /mosquitto/pw -h bcrypt -p <password>\n"
                          "  # (The cleartext passwords HelmCharts kept here were not copied.)\n", 1)
    return new


CA_TEMPLATE = """{{/*
The homelab root CA, which ESO needs to verify OpenBao's step-ca-issued listener
(argo-cd D59; HelmCharts' post-rollout.sh applied it on every deploy). Rotate it with
the other copies: Ansible roles/baseline/files/homelab-root.crt is the source.
*/}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: homelab-root-ca
  namespace: {{ .Release.Namespace }}
data:
  ca.crt: |
{{ .Files.Get "files/homelab-root.crt" | indent 4 }}
"""


def write_extra(app: App, p: Path, extra: str) -> None:
    tdir = p / "chart/templates"
    if extra == "homelab-root-ca":
        (p / "chart/files").mkdir(parents=True, exist_ok=True)
        shutil.copy2(HC / "homelab-root.crt", p / "chart/files/homelab-root.crt")
        (tdir / "homelab-root-ca.yaml").write_text(CA_TEMPLATE)
    elif extra == "post-rollout-manifests":
        body = ""
        for m in app.release.get("post_rollout_manifests") or []:
            text = (app.stage_dir / m).read_text()
            if "{{" in text:
                raise Stop(f"{m} contains template syntax")
            body += (f"# HelmCharts applied configs/prd/{app.name}/{app.stage}/{m} after ESO's rollout gate; wave 1\n"
                     "# waits for the chart's wave-0 webhook to be healthy (argo-cd D59).\n")
            out = []
            for d in docs(text):
                d["metadata"].setdefault("annotations", {})["argocd.argoproj.io/sync-wave"] = "1"
                out.append(yaml.safe_dump(d, sort_keys=False))
            body += "---\n".join(out)
        (tdir / "post-rollout-manifests.yaml").write_text(body)
    else:
        raise Stop(f"unknown D59 extra {extra}")


def extra_live(app: App) -> list[dict]:
    """HelmCharts' live copies of a D59 app's companion extras, which verify compares against."""
    out = []
    for extra in D59.get(app.name, {}).get("extras", []):
        if extra == "homelab-root-ca":
            live = json.loads(iac("kubectl", *KC, "get", "configmap", "homelab-root-ca", "-n", app.ns, "-o",
                                  "json").stdout)
            out.append({"apiVersion": "v1", "kind": "ConfigMap",
                        "metadata": {"name": "homelab-root-ca", "namespace": app.ns}, "data": live["data"]})
        elif extra == "post-rollout-manifests":
            for m in app.release.get("post_rollout_manifests") or []:
                for d in docs((app.stage_dir / m).read_text()):
                    d["metadata"].setdefault("annotations", {})["argocd.argoproj.io/sync-wave"] = "1"
                    out.append(d)
    return out


def live_upstream(app: App) -> dict:
    """The upstream chart the live release runs, in the registry's shape (argo-cd D57).

    HelmCharts' release.yaml names the chart unversioned, so each deploy took the newest; the
    version is what the release last installed, read off `helm list`."""
    up = app.upstream or {}
    if "repo_url" not in up:
        raise Stop(f"release.yaml upstream is not HelmCharts' shape: {up}")
    chart = up["chart"].split("/", 1)[-1]
    rel = [r for r in json.loads(iac("helm", *HKC, "list", "-n", app.ns, "-o", "json").stdout)
           if r["name"] == app.ns]
    if len(rel) != 1:
        raise Stop(f"helm list -n {app.ns}: {len(rel)} release(s) named {app.ns}")
    m = re.fullmatch(re.escape(chart) + r"-(v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", rel[0]["chart"])
    if not m:
        raise Stop(f"live chart {rel[0]['chart']!r} is not {chart}-<version>")
    return {"repo": up["repo_url"], "chart": chart, "version": m[1]}


def repo_upstream(app: App) -> dict | None:
    """The deploy repo's `upstream:` block, which the generator renders from (D57)."""
    f = app.path / "architecture.yaml"
    ann = yaml.safe_load(f.read_text()) if f.exists() else {}
    up = (ann or {}).get("upstream")
    return {k: str(v) for k, v in up.items()} if up else None


def up_fmt(upstream: dict | None) -> dict:
    return {f"up_{k}": v for k, v in (upstream or {}).items()}


def check_upstream_pin(app: App) -> None:
    """D57: the version lives twice, in the deploy repo and in the registry. Once flipped, the
    two must be one."""
    mine = repo_upstream(app)
    reg = app.upstream if app.release.get("reconciler") == "argo-cd" else None
    if reg is not None and mine != {k: str(v) for k, v in reg.items()}:
        raise Stop(f"registry upstream {reg} != the deploy repo's architecture.yaml {mine}")


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
    """Replace the shared helper's render-time timestamp with the live release's stamp, literal.

    A render-time timestamp leaves an Argo Application forever OutOfSync. The literal keeps the
    first sync from restarting anything; pods roll when their spec changes (an image pin), and
    editing the literal forces a restart. It is a literal rather than a value because templates
    include the helper from inside `range`, where the root values are out of reach."""
    helpers = [f for f in (p / "chart/templates").glob("*.tpl") if "deployment.timestamp" in f.read_text()]
    used = any("deployment.timestamp" in f.read_text() for f in (p / "chart/templates").rglob("*.yaml"))
    if not used:
        return None
    if len(helpers) != 1:
        raise Stop(f"deployment.timestamp defined in {len(helpers)} helper files")
    manifest = iac("helm", *HKC, "get", "manifest", app.ns, "-n", app.ns).stdout
    stamps = set(re.findall(r"^\s+deployment: ['\"]([^'\"]+)['\"]$", manifest, re.M))
    if len(stamps) != 1:
        raise Stop(f"live deployment stamps: {sorted(stamps)}")
    stamp = stamps.pop()
    text = helpers[0].read_text()
    new, n = STAMP_DEFINE.subn(
        lambda m: m.group(1) + "{{- /* Fixed at HelmCharts' last deploy (argo-cd bulk migration): edit to roll the pods. */ -}}\n"
        + f'deployment: {json.dumps(stamp)}' + " -}}\n".replace(" -}}", ""), text)
    if n != 1:
        raise Stop("deployment.timestamp helper not in the expected shape")
    helpers[0].write_text(new)
    return stamp


SSE_TEMPLATE = """{{/*
The SSE gateway's callback secret, generated once by ESO (refreshInterval 0) instead of
`randAlphaNum` at render time: a random render leaves an Argo Application forever OutOfSync
and rolls the pod on every sync (argo-cd bulk migration). Wave -1, so the Secret exists
before the Deployment that reads it is replaced.
*/}}
apiVersion: generators.external-secrets.io/v1alpha1
kind: Password
metadata:
  name: sse-callback
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
spec:
  length: 64
  digits: 10
  symbols: 0
  noUpper: false
  allowRepeat: true
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: sse-callback
  annotations:
    argocd.argoproj.io/sync-wave: "-1"
spec:
  refreshInterval: "0"
  target:
    name: sse-callback
  dataFrom:
    - sourceRef:
        generatorRef:
          apiVersion: generators.external-secrets.io/v1alpha1
          kind: Password
          name: sse-callback
"""

# The first sync replaces the Deployment the SSE rewrite changed: Helm owns the old
# `env[SSE_CALLBACK_SECRET].value`, and neither client-side nor server-side apply removes another
# manager's field, so an apply leaves it beside the new `valueFrom` and the API server rejects
# both. `unreplace` drops the annotation once the first sync is through.
REPLACE_NOTE = "    # First sync only (argo-cd bulk migration): replace, so Helm's SSE_CALLBACK_SECRET value goes.\n"
REPLACE_LINE = "    argocd.argoproj.io/sync-options: Replace=true\n"
DEPLOYMENT_HEAD = re.compile(r"^(kind: Deployment\nmetadata:\n  name: [^\n]+\n)(?!  annotations:)", re.M)


def mark_replace(name: str, text: str) -> str:
    new, n = DEPLOYMENT_HEAD.subn(lambda m: m.group(1) + "  annotations:\n" + REPLACE_NOTE + REPLACE_LINE, text)
    if n != 1:
        raise Stop(f"{name}: expected one Deployment head without annotations, found {n}")
    return new


def cmd_unreplace(app: App, args) -> None:
    """Drop the first-sync Replace=true once the sync is through, and push."""
    if "synced" not in app.load_state():
        raise Stop("not synced: the Replace annotation is for the first sync")
    block = "  annotations:\n" + REPLACE_NOTE + REPLACE_LINE
    hit = []
    for t in (app.path / "chart/templates").glob("*.yaml"):
        text = t.read_text()
        if block in text:
            t.write_text(text.replace(block, ""))
            hit.append(t.name)
    if not hit:
        log("no Replace annotation left")
        return
    run(["git", "add", "-A"], cwd=app.path)
    run(["git", "commit", "-q", "-m", "chart: the Deployment's first sync is through; drop Replace=true (argo-cd bulk migration)"],
        cwd=app.path)
    run(["git", "push", "-q"], cwd=app.path)
    log(f"Replace=true dropped from {', '.join(hit)}; pushed")


SSE_DEF = re.compile(r"^\{\{-? \$sseCallbackSecret := randAlphaNum 64 -?\}\}\n", re.M)
SSE_VALUE = re.compile(r"^( *)value: \{\{ \$sseCallbackSecret \| quote \}\}\n", re.M)
SSE_URL = re.compile(r'^( *)- name: CALLBACK_URL\n( *)value: "(.*?)\{\{ \$sseCallbackSecret \}\}"\n', re.M)


def fix_sse_secret(p: Path) -> bool:
    """Rewrite the SSE gateway charts' render-time random callback secret. True if rewritten."""
    hit = False
    for t in (p / "chart/templates").glob("*.yaml"):
        text = t.read_text()
        if not SSE_DEF.search(text):
            continue
        if "$sseCallbackSecret" in SSE_DEF.sub("", text) and not (SSE_VALUE.search(text) or SSE_URL.search(text)):
            raise Stop(f"{t.name}: sseCallbackSecret used in an unexpected shape")
        text = SSE_DEF.sub("", text)
        text = SSE_VALUE.sub(lambda m: (f"{m.group(1)}valueFrom:\n{m.group(1)}  secretKeyRef:\n"
                                        f"{m.group(1)}    name: sse-callback\n{m.group(1)}    key: password\n"), text)
        text = SSE_URL.sub(lambda m: (f"{m.group(1)}- name: SSE_CALLBACK_SECRET\n{m.group(2)}valueFrom:\n"
                                      f"{m.group(2)}  secretKeyRef:\n{m.group(2)}    name: sse-callback\n"
                                      f"{m.group(2)}    key: password\n"
                                      f"{m.group(1)}- name: CALLBACK_URL\n"
                                      f'{m.group(2)}value: "{m.group(3)}$(SSE_CALLBACK_SECRET)"\n'), text)
        if "$sseCallbackSecret" in text:
            raise Stop(f"{t.name}: sseCallbackSecret left after the rewrite")
        text = mark_replace(t.name, text)
        t.write_text(text)
        hit = True
    if hit:
        (p / "chart/templates/sse-callback-secret.yaml").write_text(SSE_TEMPLATE)
    return hit


# argo-cd D59: the chart hook scripts are replaced, not carried. Per app:
#   scripts  the HelmCharts hook scripts the replacement covers (never copied);
#   values   exact text edits to the stage values, each (old, new), each applied exactly once;
#   drops    fields the script wrote into a live object that the render no longer carries,
#            as (kind, name, path). Argo's client-side apply leaves a field that neither the
#            render nor last-applied-configuration names, so the live value stays; verify
#            compares without them and preflight's diff shows them unchanged;
#   extras   companion templates for what the script applied, beside HelmCharts' `live`
#            copies verify compares them to.
D59 = {
    "mosquitto": {
        # The post-render stamped the t3n subchart's Deployment, which has no pod-annotation
        # value; the checksum annotation still rolls the pod when the config changes.
        "scripts": ["post-render.sh"],
        "drops": [("Deployment", "mosquitto", ("spec", "template", "metadata", "annotations", "deployment"))],
    },
    "grafana": {
        "scripts": ["post-render.sh", "post-install.sh"],
        "values": [("  storageClassName: csi-rbd-sc\n",
                    "  # The claim binds the Terraform PV by name (argo-cd D59; HelmCharts' post-render.sh\n"
                    "  # set it, and an empty storageClassName this chart cannot render: the live claim\n"
                    "  # keeps that).\n"
                    "  volumeName: grafana-pv\n")],
        "drops": [("PersistentVolumeClaim", "grafana", ("spec", "storageClassName"))],
    },
    "prometheus": {
        "scripts": ["post-render.sh", "post-install.sh"],
        "values": [("    storageClass: csi-cephfs-sc\n",
                    "    # No class, so the claim binds the Terraform PV, which is pre-bound to it by\n"
                    "    # claimRef (argo-cd D59). HelmCharts' post-render.sh also named the volume,\n"
                    "    # which this chart cannot; the first sync recreated the StatefulSet without it.\n"
                    "    storageClass: \"-\"\n")],
        "drops": [("StatefulSet", "prometheus-prd-alertmanager",
                   ("spec", "volumeClaimTemplates", 0, "spec", "volumeName"))],
    },
    "nginx": {
        # It annotated the microk8s addon's kubernetes-dashboard Service in kube-system; the
        # annotations are live and survive an addon re-apply (README).
        "scripts": ["post-install.sh"],
        "readme": """
## kubernetes.home

HelmCharts' `charts/nginx/post-install.sh` annotated the microk8s addon's
`kubernetes-dashboard` Service in `kube-system` on every deploy, so nginx routes
`kubernetes.home` to it. Argo CD does not carry that script (argo-cd D59): the Service is the
addon's, not this chart's. The annotations are live and survive an addon re-apply, since they
are not in its last-applied configuration. After a cluster rebuild, re-add them by hand:

```sh
kubectl patch service kubernetes-dashboard -n kube-system --patch '{"metadata": {"annotations": {
  "nginx.webathome.org/server-name": "kubernetes.home, kubernetes",
  "nginx.webathome.org/enable-ssl": "yes",
  "nginx.webathome.org/target-port": "443/ssl"}}}'
```
""",
    },
    "external-secrets": {
        # post-rollout.sh made the homelab-root-ca ConfigMap; the ClusterSecretStore waited for
        # ESO's webhook. Both are companion content now, the store a wave after the chart.
        "scripts": ["post-rollout.sh"],
        "extras": ["homelab-root-ca", "post-rollout-manifests"],
    },
}

# Objects only Argo's render carries, accepted by the operator (ANS-103, 2026-09-24): step-ca's
# chart renders its bootstrap RBAC on `.Release.IsInstall`, which `helm template` always is, and
# Helm dropped it after the install. Unused while bootstrap is off. The first sync creates them.
RENDER_ONLY = {
    "step-ca": {("Role", "step-ca-config"), ("RoleBinding", "step-ca-config"), ("ServiceAccount", "step-ca-config")},
}

# Changes the operator accepted for an app's first sync (ANS-103), per app: the live objects the
# render drops, the objects only the render carries, and a regex every changed line of the named
# objects must match. grafana's chart generated its admin password with `lookup`, empty under
# Argo, so each render made a new one; the password is in OpenBao now, read by an ExternalSecret,
# and the first sync rolls the pod once. Helm's `grafana` Secret is left for the cleanup, and
# its checksum annotation stays on the pod template as a stuck field (removing it by hand would
# roll the pod again).
ACCEPTED = {
    "grafana": {
        "live_only": {("Secret", "grafana")},
        "stuck": ["spec.template.metadata.annotations.checksum/secret"],
        "render_only": {("ExternalSecret", "grafana-admin")},
        "changed": {("Deployment", "grafana")},
        "lines": re.compile(r"^[+-]\s*name: grafana(-admin)?$|^-\s*checksum/secret: [0-9a-f]{64}$"),
    },
}

# StatefulSets whose claim template changes (ANS-103). The list is atomic, so apply replaces it
# whole, and the API server refuses any claim-template change: preflight's diff leaves them out,
# and the sync deletes each with --cascade=orphan just before it starts, so Argo creates it
# afresh. Its pod and PVC keep running, and the new StatefulSet adopts them; the pod template is
# unchanged (verify), so the pod keeps its revision. prometheus's alertmanager claim loses the
# volumeName HelmCharts' post-render set: the PV is pre-bound to the claim by claimRef.
RECREATE = {
    "prometheus": {("StatefulSet", "prometheus-prd-alertmanager")},
}

HELM_TEST_HOOKS = {"test", "test-success", "test-failure"}


def helm_hooks(d: dict) -> set[str]:
    value = (d["metadata"].get("annotations") or {}).get("helm.sh/hook") or ""
    return {v.strip() for v in value.split(",") if v.strip()} - {"crd-install"}


def helm_test(d: dict) -> bool:
    """A Helm test hook: Argo CD never runs one, and `helm get manifest` never lists it."""
    return bool(helm_hooks(d)) and helm_hooks(d) <= HELM_TEST_HOOKS


def drop_path(obj: dict, path: tuple) -> None:
    *head, last = path
    for k in head:
        try:
            obj = obj[k]
        except (KeyError, IndexError, TypeError):
            return
    if isinstance(obj, dict):
        obj.pop(last, None)


# A Job named with `randAlphaNum` renders a new name on every render: an Argo Application stays
# OutOfSync and runs a new Job on every sync. The deploy repo names it after a hash of its image
# pin instead, stable between syncs and new when the image changes, as HelmCharts' every deploy
# re-ran it. The generator strips a 5-character Job suffix, so the element ids hold.
RANDOM_JOB_NAME = re.compile(r"^(  name: [\w-]+-)\{\{ randAlphaNum 5 \| lower \}\}$", re.M)
JOB_IMAGE_VALUE = re.compile(r"image: [^\n]*\{\{-?\s*(\.Values\.[\w.]+)\s*-?\}\}")


def fix_random_job_names(p: Path) -> list[str]:
    hit = []
    for t in (p / "chart/templates").glob("*.yaml"):
        text = t.read_text()
        if not RANDOM_JOB_NAME.search(text):
            continue
        if not re.search(r"^kind: Job$", text, re.M) or text.count("\n---") > 0:
            raise Stop(f"{t.name}: randAlphaNum name outside a single-Job template")
        images = set(JOB_IMAGE_VALUE.findall(text))
        if len(images) != 1:
            raise Stop(f"{t.name}: the Job's image pin is not one .Values expression: {sorted(images)}")
        value = images.pop()
        text = RANDOM_JOB_NAME.sub(
            lambda m: ("  # Named after its image pin (argo-cd bulk migration): stable between syncs, new, and\n"
                       "  # so re-run, when the image changes.\n"
                       f"{m.group(1)}{{{{ {value} | sha256sum | trunc 5 }}}}"), text)
        t.write_text(text)
        hit.append(t.name)
    return hit


# Charts HelmCharts named other than their app. The deploy repo's copy takes the app's name:
# gen-architecture derives the namespace, and so every element id, from Chart.yaml's name.
CHART_RENAMES = {"iot": "iotsupport"}


STAMP_LITERAL = re.compile(r'^deployment: ("[^"\n]+")$', re.M)


def add_stage(app: App, args) -> None:
    """A further stage of an app whose deploy repo exists: its values, tfvars and producer files.

    The chart is the repo's, which must be the one this stage's HelmCharts release renders. A
    deploy stamp the first stage pinned as a literal becomes a per-stage value, since the stages
    were deployed at different times."""
    p = app.path
    if not (p / ".git").exists():
        raise Stop(f"{p} is not a deploy repo")
    if (p / "config" / app.stage).exists():
        raise Stop(f"config/{app.stage} exists")
    first = App(app.name, "prd").load_state()
    if not first.get("scaffolded"):
        raise Stop("the prd stage was not scaffolded by this tool")
    moved = run(["git", "-C", str(HC), "diff", "--stat", first["scaffolded"], "HEAD", "--",
                 f"charts/{app.chart_name}"]).stdout.strip()
    if moved:
        raise Stop(f"charts/{app.chart_name} moved since the repo was scaffolded:\n{moved}")
    live = helm_values(app)
    values_text = compose_values(app, live)
    helpers = [f for f in (p / "chart/templates").glob("*.tpl") if STAMP_LITERAL.search(f.read_text())]
    stamp = None
    if helpers:
        if len(helpers) != 1:
            raise Stop("deployment stamp literal in several helpers")
        manifest = iac("helm", *HKC, "get", "manifest", app.ns, "-n", app.ns).stdout
        stamps = set(re.findall(r"^\s+deployment: ['\"]([^'\"]+)['\"]$", manifest, re.M))
        if len(stamps) != 1:
            raise Stop(f"live deployment stamps: {sorted(stamps)}")
        stamp = stamps.pop()
        text = helpers[0].read_text()
        literal = STAMP_LITERAL.search(text).group(1)
        text = text.replace(
            "{{- /* Fixed at HelmCharts' last deploy (argo-cd bulk migration): edit to roll the pods. */ -}}",
            "{{- /* Each stage's HelmCharts deploy time, pinned in its values (argo-cd bulk migration): edit to roll\n"
            "the pods. Included from the root context only, where the values are reachable. */ -}}")
        text = STAMP_LITERAL.sub("deployment: {{ required \"deploymentStamp\" .Values.deploymentStamp | quote }}", text)
        helpers[0].write_text(text)
        for cfg in sorted((p / "config").glob("*/values.yaml")):
            cfg.write_text(cfg.read_text().rstrip("\n") + (
                "\n\n# The deploy stamp HelmCharts last set; edit it to roll the pods.\n"
                f"deploymentStamp: {literal}\n"))
        values_text = values_text.rstrip("\n") + (
            "\n\n# The deploy stamp HelmCharts last set; edit it to roll the pods.\n"
            f"deploymentStamp: {json.dumps(stamp)}\n")
        schema = p / "chart/values.schema.json"
        if schema.exists():
            sj = json.loads(schema.read_text())
            sj.setdefault("properties", {}).setdefault("deploymentStamp", {"type": "string"})
            schema.write_text(json.dumps(sj, indent=2) + "\n")
    cdir = p / "config" / app.stage
    cdir.mkdir(parents=True)
    (cdir / "values.yaml").write_text(values_text)
    for tv in app.stage_dir.glob("*.tfvars"):
        shutil.copy2(tv, cdir / tv.name)
    (cdir / "terraform.tfvars").write_text(
        "# The prd stage owns the repository's webhook; this one does not.\nmanage_webhook = false\n")
    (p / app.arch_file).write_text(JENKINSFILE_ARCH.format(
        app=app.name, stage=app.stage, producer=app.producer, cred=GIT_CRED, url=app.url))
    rc = (p / ".architecturerc").read_text()
    rc = rc.replace("  - config/prd/\n", f"  - config/prd/\n  - config/{app.stage}/\n", 1)
    (p / ".architecturerc").write_text(rc)
    (p / "README.md").write_text((p / "README.md").read_text().rstrip("\n") + (
        f"\n\n## The {app.stage} stage\n\n"
        f"Argo CD syncs `{app.ns}` from `main` too, with `config/{app.stage}/values.yaml`; every stage follows\n"
        f"`main` (argo-cd D53). Its state is `argocd/{app.repo_name}/{app.stage}/terraform.tfstate`, and\n"
        f"`{app.arch_file}` publishes it as producer `{app.producer}` (D50).\n"))
    sha = run(["git", "-C", str(HC), "rev-parse", "HEAD"]).stdout.strip()
    run(["git", "add", "-A"], cwd=p)
    run(["git", "commit", "-q", "-m", f"{app.name}: the {app.stage} stage, migrated from HelmCharts {sha[:7]} (argo-cd D51)"],
        cwd=p)
    app.save_state(scaffolded=sha, sse=first.get("sse"), upstream=first.get("upstream"),
                   renamed_jobs=first.get("renamed_jobs"), stamp=stamp)
    log(f"added stage {app.stage} to {p} from HelmCharts {sha[:7]}" + (f"; stamps per stage now" if stamp else ""))


def cmd_scaffold(app: App, args) -> None:
    if app.release.get("reconciler") == "argo-cd":
        raise Stop("already on Argo")
    if app.release.get("disabled"):
        raise Stop("disabled in HelmCharts")
    upstream = live_upstream(app) if app.upstream else None
    if not upstream and not (app.chart_src / "Chart.yaml").exists():
        raise Stop(f"no local chart at {app.chart_src}")
    if upstream and (app.chart_src / "Chart.yaml").exists():
        raise Stop(f"upstream app with a local chart at {app.chart_src}")
    profile = D59.get(app.name, {})
    for h in ("post-render.sh", "post-install.sh", "post-rollout.sh", "pre-install.sh"):
        if (app.chart_src / h).exists() and h not in profile.get("scripts", []):
            raise Stop(f"chart has {h}: late-migration set (D18) with no D59 replacement")
    if app.release.get("post_rollout_manifests") and "post-rollout-manifests" not in profile.get("extras", []):
        raise Stop("post_rollout_manifests with no D59 replacement")
    stage_tf = [p for p in app.stage_dir.glob("*.tf")]
    if stage_tf:
        raise Stop(f"stage-level Terraform: {stage_tf}")
    if getattr(args, "add_stage", False):
        return add_stage(app, args)
    if app.path.exists() and not args.force:
        raise Stop(f"{app.path} exists (use --force to rebuild it)")
    if app.path.exists():
        shutil.rmtree(app.path)

    sha = run(["git", "-C", str(HC), "rev-parse", "HEAD"]).stdout.strip()
    p = app.path
    (p / "chart").mkdir(parents=True)
    if upstream:
        # The companion (argo-cd D56): estate content only, never a wrapper of the upstream chart.
        (p / "chart/Chart.yaml").write_text(COMPANION_CHART.format(app=app.name, **up_fmt(upstream)))
    else:
        # The chart, minus HelmCharts-only files.
        for item in app.chart_src.iterdir():
            if item.name in ("architecture.yaml", "resources-entry-map.json", *profile.get("scripts", [])):
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
    sse = fix_sse_secret(p)
    renamed_jobs = fix_random_job_names(p)
    for extra in profile.get("extras", []):
        write_extra(app, p, extra)
    schema = p / "chart/values.schema.json"
    if schema.exists():
        # A closed schema must admit the keys the deploy repo adds: the library's own block,
        # the ApplicationSet's hook parameters and the pinned stamp.
        sj = json.loads(schema.read_text())
        props = sj.setdefault("properties", {})
        props.setdefault("homelab-shared", {"type": "object"})
        props.setdefault("hook", {"type": "object"})
        schema.write_text(json.dumps(sj, indent=2) + "\n")
    chart_yaml = (p / "chart/Chart.yaml").read_text()
    if app.name in CHART_RENAMES:
        old_name = CHART_RENAMES[app.name]
        chart_yaml, n = re.subn(rf"^name: {re.escape(old_name)}$",
                                f"# HelmCharts named this chart {old_name}; the producer ids need it named after the app.\n"
                                f"name: {app.name}", chart_yaml, flags=re.M)
        if n != 1:
            raise Stop(f"Chart.yaml does not name the chart {old_name}")
        if any(".Chart.Name" in t.read_text() for t in (p / "chart/templates").rglob("*") if t.is_file()):
            raise Stop("a template reads .Chart.Name: the rename would change the render")
    library = (
        "  # Exact, not a range (D17): the hook Job this chart renders moves on a deliberate commit.\n"
        "  - name: homelab-shared\n"
        f"    version: \"{LIB_VERSION}\"\n"
        "    repository: https://charts.home\n")
    if "dependencies:" in chart_yaml:
        # A wrapper chart: its own dependencies stay, each pinned at what HelmCharts' Chart.lock
        # resolved, so the render is the one the live release came from.
        meta = yaml.safe_load(chart_yaml)
        if list(meta)[-1] != "dependencies" or not re.search(r"^dependencies:$", chart_yaml, re.M):
            raise Stop("Chart.yaml: dependencies is not its last key")
        lock = yaml.safe_load((app.chart_src / "Chart.lock").read_text())
        locked = {d["name"]: d["version"] for d in lock["dependencies"]}
        for dep in meta["dependencies"]:
            chart_yaml, n = re.subn(rf'(\n  - name: {re.escape(dep["name"])}\n    version: )"?{re.escape(str(dep["version"]))}"?\n',
                                    lambda m: f'{m.group(1)}"{locked[dep["name"]]}"\n', chart_yaml)
            if n != 1:
                raise Stop(f"Chart.yaml: cannot pin dependency {dep['name']}")
        chart_yaml = chart_yaml.rstrip("\n") + "\n" + library
        (p / "chart/Chart.lock").unlink(missing_ok=True)
    else:
        chart_yaml = chart_yaml.rstrip("\n") + "\ndependencies:\n" + library
    (p / "chart/Chart.yaml").write_text(chart_yaml)
    name_in_chart = yaml.safe_load(chart_yaml)["name"]
    if name_in_chart != app.name:
        raise Stop(f"chart name {name_in_chart} != app {app.name}: the producer ids need them equal")

    # Stage configuration.
    cdir = p / "config" / app.stage
    cdir.mkdir(parents=True)
    live = helm_values(app)
    values_text = compose_values(app, live)
    for old, new in profile.get("values", []):
        if values_text.count(old) != 1:
            raise Stop(f"values: D59 edit expects one {old!r}")
        values_text = values_text.replace(old, new)
    if app.name == "mosquitto":
        values_text = strip_cleartext_passwords(values_text)
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
    up_text = ""
    if upstream:
        if re.search(r"^upstream:", atext, re.M):
            raise Stop("architecture.yaml already states upstream:")
        up_text = (
            "# The chart Argo CD renders as source 0 (argo-cd D57). The version is the one this app's\n"
            "# registry entry pins (HelmCharts configs/prd/<app>/<stage>/release.yaml): bump both.\n"
            + yaml.safe_dump({"upstream": upstream}, sort_keys=False) + "\n")
    (p / "architecture.yaml").write_text(
        "# The judgment layer the aac-tools generator reads (Jenkinsfile.architecture); copied\n"
        f"# verbatim from HelmCharts' charts/{app.chart_name}/architecture.yaml.\n\n"
        "# The date HelmCharts derives from the first commit adding the chart, which every\n"
        "# published element carries.\n"
        f"introduced: '{introduced}'\n\n" + up_text + atext)
    (p / "Jenkinsfile.architecture").write_text(JENKINSFILE_ARCH.format(
        app=app.name, stage=app.stage, producer=app.producer, cred=GIT_CRED, url=app.url))
    (p / ".architecturerc").write_text(ARCHITECTURERC.format(
        stage=app.stage, producer=app.producer, repo=app.repo_name))
    (p / ".gitignore").write_text(GITIGNORE)
    (p / ".kubecoder").mkdir()
    (p / ".kubecoder/project.yaml").write_text((UPSTREAM_PROJECT_YAML if upstream else PROJECT_YAML).format(
        repo=app.repo_name, app=app.name, producer=app.producer, ns=app.ns, stage=app.stage,
        hook=pascal_hook(app), **up_fmt(upstream)))
    (p / "tests").mkdir()
    for n, body in (("build-deps.sh", BUILD_DEPS), ("terraform.sh", TERRAFORM_SH)):
        (p / "tests" / n).write_text(body)
        (p / "tests" / n).chmod(0o755)
    (p / "README.md").write_text((UPSTREAM_README if upstream else README).format(
        repo=app.repo_name, app=app.name, ns=app.ns, stage=app.stage, producer=app.producer,
        sha=sha[:7], chart=app.chart_name, **up_fmt(upstream)) + profile.get("readme", ""))

    # Chart.lock, then the gates.
    repos = dependency_repos(p)
    if repos:
        body = BUILD_DEPS.replace("helm dependency build chart\n", "".join(
            f"helm repo add {n} {u} --force-update >/dev/null\n" for n, u in repos) + "helm dependency build chart\n")
        (p / "tests/build-deps.sh").write_text(body)
    iac("helm", "repo", "add", "charts-home", "https://charts.home", "--force-update", cwd=p)
    for n, u in repos:
        iac("helm", "repo", "add", n, u, "--force-update", cwd=p)
    iac("helm", "dependency", "update", "chart", cwd=p)
    iac("terraform", "fmt", "-recursive", cwd=p)
    run(["git", "init", "-q", "-b", "main"], cwd=p)
    run(["git", "add", "-A"], cwd=p)
    run(["git", "commit", "-q", "-m",
         f"{app.name}: deploy repo, migrated from HelmCharts {sha[:7]} (argo-cd D51)"], cwd=p)
    app.save_state(scaffolded=sha, sse=sse, upstream=upstream, renamed_jobs=renamed_jobs)
    log(f"scaffolded {p} from HelmCharts {sha[:7]}; modules: {', '.join(mods) or 'none'}")


# ---------------------------------------------------------------------------------------------
# verify: the render equals the live release


TRAILING_TABS = re.compile(r"\t+$", re.M)


def docs(text: str) -> list[dict]:
    # A trailing tab is YAML Go parses and PyYAML refuses (prometheus' alertmanager subchart
    # renders `- apiVersion: v1<TAB>`); Argo reads the render with Go's parser.
    return [d for d in yaml.safe_load_all(TRAILING_TABS.sub("", text)) if d]


def key(d: dict) -> tuple:
    m = d["metadata"]
    return (d.get("kind"), m.get("name") or m.get("generateName"))


def is_hook(d: dict) -> bool:
    """Argo's own sync hooks: the PreSync Job and, since homelab-shared 0.3.0, its RoleBinding."""
    return "argocd.argoproj.io/hook" in (d["metadata"].get("annotations") or {})


def render(app: App, revision: str = "0123456789abcdef0123456789abcdef01234567") -> str:
    """What Argo renders: the chart with the stage values, or for an upstream app the upstream
    chart with the stage values followed by the companion with the hook parameters alone."""
    iac("tests/build-deps.sh", cwd=app.path)
    hook = HOOK_PARAMS.replace("0123456789abcdef0123456789abcdef01234567", revision).format(
        repo=app.url, stage=app.stage, ns=app.ns)
    up = repo_upstream(app)
    if not up:
        r = iac("helm", "template", app.ns, "chart", "--namespace", app.ns, "--values",
                f"config/{app.stage}/values.yaml", "--set", hook, cwd=app.path)
        return r.stdout
    u = iac("helm", "template", app.ns, up["chart"], "--repo", up["repo"], "--version", up["version"],
            "--namespace", app.ns, "--values", f"config/{app.stage}/values.yaml", cwd=app.path)
    c = iac("helm", "template", app.ns, "chart", "--namespace", app.ns, "--set", hook, cwd=app.path)
    return u.stdout + "\n---\n" + c.stdout


SSE_OK = re.compile(r"SSE_CALLBACK_SECRET|sse/callback|sse-callback|secretKeyRef:|valueFrom:|key: password|"
                    r"^[+-]\s*value: [A-Za-z0-9]{64}$|^\+\s*annotations:$|^\+\s*argocd\.argoproj\.io/sync-options: Replace=true$")


def reserialized(d: dict) -> dict:
    """An object as a Helm post-renderer's re-serialisation stores it: empty metadata maps
    dropped and a ConfigMap value's final newlines trimmed. `helm get manifest` of a
    post-rendered release holds that form; the render Argo applies holds the chart's."""
    import copy
    d = copy.deepcopy(d)
    meta = d.get("metadata") or {}
    for field in ("annotations", "labels"):
        if meta.get(field) == {}:
            meta.pop(field)
    if d.get("kind") == "ConfigMap" and isinstance(d.get("data"), dict):
        d["data"] = {k: v.rstrip("\n") if isinstance(v, str) else v for k, v in d["data"].items()}
    return d


SECRET_ENV = re.compile(r"SECRET|PASSWORD|TOKEN|KEY|CREDENTIAL", re.I)
SECRET_QUERY = re.compile(r"([?&](?:secret|token|password|key)=)([^&$\s]+)", re.I)


def redact(d: dict) -> dict:
    """A secret never reaches a diff this tool prints or logs: a Secret's values, and a
    container env value whose name says secret, are replaced by a digest, so a changed value
    still shows as changed."""
    import copy
    import hashlib

    def digest(v) -> str:
        return "<redacted sha256:" + hashlib.sha256(str(v).encode()).hexdigest()[:12] + ">"

    out = copy.deepcopy(d)
    if d.get("kind") == "Secret":
        for field in ("data", "stringData"):
            if isinstance(out.get(field), dict):
                out[field] = {k: digest(v) for k, v in out[field].items()}
        return out

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and "value" in node and SECRET_ENV.search(node["name"]):
                node["value"] = digest(node["value"])
            elif isinstance(node.get("value"), str) and SECRET_QUERY.search(node["value"]):
                node["value"] = SECRET_QUERY.sub(lambda m: m.group(1) + digest(m.group(2)), node["value"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(out)
    return out


# The API server's cap on all of an object's annotations; client-side apply writes the whole
# object into last-applied-configuration (D62).
LAST_APPLIED_LIMIT = 262144


def cmd_verify(app: App, args) -> None:
    up = repo_upstream(app)
    if up and app.release.get("reconciler") != "argo-cd" and live_upstream(app) != up:
        raise Stop(f"the live release runs {live_upstream(app)}, the deploy repo pins {up}")
    check_upstream_pin(app)
    everything = docs(render(app))
    tests = [key(d) for d in everything if helm_test(d)]
    if tests:
        log(f"Helm test hooks, which Argo never runs: {tests}")
    helm_run = [(key(d), sorted(helm_hooks(d))) for d in everything if helm_hooks(d) and not helm_test(d)]
    everything = [d for d in everything if not helm_test(d)]
    rendered = {key(d): d for d in everything if not is_hook(d)}
    hook = {key(d) for d in everything if is_hook(d)}
    live_text = iac("helm", *HKC, "get", "manifest", app.ns, "-n", app.ns).stdout
    live = {key(d): d for d in docs(live_text)}
    manifests = app.stage_dir / "manifests.yaml"
    if manifests.exists() and app.release.get("reconciler") != "argo-cd":
        for d in docs(manifests.read_text()):
            live[key(d)] = d
    if app.release.get("reconciler") != "argo-cd":
        for d in extra_live(app):
            live[key(d)] = d
    for kind, name, path in D59.get(app.name, {}).get("drops", []):
        if (kind, name) not in live:
            raise Stop(f"D59 drop names {kind}/{name}, which the live release lacks")
        drop_path(live[(kind, name)], path)
    # A Job renamed from randAlphaNum pairs with the live Job of the same prefix.
    for t in app.load_state().get("renamed_jobs") or []:
        new = [k for k in rendered if k[0] == "Job" and k not in live]
        old = [k for k in live if k[0] == "Job" and k not in rendered]
        pairs = [(n, o) for n in new for o in old if n[1][:-5] == o[1][:-5]]
        if len(pairs) != 1:
            raise Stop(f"{t}: no single live Job to pair the renamed one with: new {new}, old {old}")
        n, o = pairs[0]
        old_job = live.pop(o)
        live[n] = dict(old_job, metadata=dict(old_job["metadata"], name=n[1]))
        log(f"Job {o[1]} is {n[1]} in the render: the first sync runs it once, and {o[1]} is left for the cleanup")
    accepted = ACCEPTED.get(app.name, {})
    expected_extra = {("Namespace", app.ns)} | RENDER_ONLY.get(app.name, set()) | accepted.get("render_only", set())
    sse = app.load_state().get("sse")
    if sse:
        expected_extra |= {("Password", "sse-callback"), ("ExternalSecret", "sse-callback")}
    extra = set(rendered) - set(live)
    extra -= expected_extra
    missing = set(live) - set(rendered) - accepted.get("live_only", set())
    for k in sorted(accepted.get("live_only", set()) & set(live)):
        log(f"{k}: dropped from the render (accepted, ANS-103); left for the cleanup")
    diffs = []
    post_rendered = "post-render.sh" in D59.get(app.name, {}).get("scripts", [])
    for k in set(rendered) & set(live):
        if rendered[k] != live[k]:
            if post_rendered and reserialized(rendered[k]) == reserialized(live[k]):
                continue
            diffs.append(k)
    problems = [f"Helm {', '.join(h)} hook {k}: Argo runs it on every sync" for k, h in helm_run]
    if not any(k[0] == "Job" and k[1].startswith("tf-presync") for k in hook):
        problems.append("no tf-presync hook Job rendered")
    if extra:
        problems.append(f"objects only in the render: {sorted(extra)}")
    if missing:
        problems.append(f"objects only in the live release: {sorted(missing)}")
    for k in diffs:
        import difflib
        # Judged on the objects themselves; only the printed diff is redacted.
        raw_a = yaml.safe_dump(live[k], sort_keys=True).splitlines()
        raw_b = yaml.safe_dump(rendered[k], sort_keys=True).splitlines()
        changed = [l for l in difflib.unified_diff(raw_a, raw_b, lineterm="", n=0)
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        if sse and k[0] == "Deployment" and all(SSE_OK.search(l) for l in changed):
            log(f"{k}: only the SSE callback secret changes (expected: this sync rolls it)")
            continue
        if k in accepted.get("changed", set()) and all(accepted["lines"].search(l) for l in changed):
            log(f"{k}: only the accepted change (ANS-103; this sync rolls it)")
            continue
        a = yaml.safe_dump(redact(live[k]), sort_keys=True).splitlines()
        b = yaml.safe_dump(redact(rendered[k]), sort_keys=True).splitlines()
        problems.append(f"{k} differs:\n" + "\n".join(difflib.unified_diff(a, b, "live", "render", lineterm="", n=1)))
    if problems:
        raise Stop("render != live release:\n" + "\n".join(problems))
    log(f"render equals the live release: {len(live)} objects, plus the Namespace and the hooks")
    oversized = [f"{k[0]}/{k[1]}" for k, d in rendered.items()
                 if len(json.dumps(d, separators=(",", ":"))) > LAST_APPLIED_LIMIT]
    if oversized:
        log(f"over the last-applied limit, so the app syncs server-side (D62): {oversized}")
    app.save_state(verified=True, ssa=bool(oversized))


# ---------------------------------------------------------------------------------------------
# publish: GitHub repo, AaC job


def jenkins(method: str, path: str, data: bytes | None = None, ctype: str = "application/xml"):
    cmd = ["curl", "-sS", "-g", "-o", "/dev/stderr", "-w", "%{http_code}", "-u",
           f"admin:{os.environ['JENKINS_TOKEN']}", "-X", method, f"{JENKINS}{path}"]
    if data is not None:
        cmd += ["-H", f"Content-Type: {ctype}", "--data-binary", "@-"]
    r = subprocess.run(cmd, input=data.decode() if data is not None else None, text=True, capture_output=True)
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
    run(["git", "push", "-q", "-u", *(["--force"] if args.force else []), "origin", "main"], cwd=p)
    # AaC/<Repo>, from AaC/KubeCoderDeploy's config.
    code, _ = jenkins("GET", f"/job/AaC/job/{app.aac_job}/api/json")
    if code != "200":
        tpl = subprocess.run(["curl", "-sS", "-g", "-u", f"admin:{os.environ['JENKINS_TOKEN']}",
                              f"{JENKINS}/job/AaC/job/KubeCoderDeploy/config.xml"],
                             text=True, capture_output=True).stdout
        if "KubeCoderDeploy.git" not in tpl:
            raise Stop("could not read AaC/KubeCoderDeploy's config.xml")
        cfg = tpl.replace("KubeCoderDeploy", app.repo_name).replace("*/prd", "*/main")
        cfg = cfg.replace("<scriptPath>Jenkinsfile.architecture</scriptPath>",
                          f"<scriptPath>{app.arch_file}</scriptPath>")
        cfg = re.sub(r"<description>.*?</description>",
                     f"<description>Architecture producer {app.producer}</description>", cfg, flags=re.S)
        code, err = jenkins("POST", f"/job/AaC/createItem?name={app.aac_job}", cfg.encode())
        if code not in ("200", "302"):
            raise Stop(f"createItem AaC/{app.aac_job}: HTTP {code} {err[:300]}")
    build = aac_build(app)
    app.save_state(published=True, aac_build=build)
    log(f"{app.url} pushed; AaC/{app.aac_job} #{build} green")


def aac_build(app: App) -> int:
    job = f"/job/AaC/job/{app.aac_job}"
    before = json.loads(subprocess.run(
        ["curl", "-sS", "-g", "-u", f"admin:{os.environ['JENKINS_TOKEN']}",
         f"{JENKINS}{job}/api/json?tree=nextBuildNumber"], text=True, capture_output=True).stdout)
    n = before["nextBuildNumber"]
    code, err = jenkins("POST", f"{job}/build", b"", "application/x-www-form-urlencoded")
    if code not in ("200", "201", "302"):
        raise Stop(f"build AaC/{app.aac_job}: HTTP {code} {err[:300]}")
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
                raise Stop(f"AaC/{app.aac_job} #{n}: {b['result']}")
            return n
    raise Stop(f"AaC/{app.aac_job} #{n} did not finish in 20 min")


URL = re.compile(r"^[a-z][a-z0-9+.\-]*://")
DRAWN = re.compile(r"^  drawn by (\S+): (\S+)$")
ADDED = re.compile(r"^(element|relation) generated but not published: ")


def dataset_snapshot(app: App, source: str) -> Path:
    """The one dataset both halves read: a file as given, a URL fetched once.

    Both halves read it in the iac sidecar, which sees /work and $HOME at the same paths.
    """
    if not URL.match(source):
        return Path(source).resolve()
    snap = HOME / "bulk-migration/logs" / f"{app.ns}.dataset.yaml"
    try:
        with urllib.request.urlopen(source, timeout=60) as resp:
            snap.write_bytes(resp.read())
    except OSError as e:  # URLError, HTTPError and a timeout are all OSError
        raise Stop(f"dataset fetch failed: {source}: {e}") from e
    return snap


def hc_releases_without(app: App) -> list[str]:
    """gen-architecture's names for every HelmCharts release but the app's stage.

    The bare chart name is the only name of a chart's prd release, and it selects the chart's
    other stages too, so a non-prd stage cannot be left out while its prd is rendered.
    """
    def flipped(chart: Path, stage: str) -> bool:
        rel = chart / stage / "release.yaml"
        return rel.exists() and (yaml.safe_load(rel.read_text()) or {}).get("reconciler") == "argo-cd"

    if app.stage != "prd" and (app.cfg / "prd").is_dir() and not flipped(app.cfg, "prd"):
        raise Stop(f"gen-architecture cannot render {app.name}'s prd release without its {app.stage} one: "
                   f"flip prd first (HC-16)")
    names = []
    for chart in sorted(p for p in (HC / "configs/prd").iterdir() if p.is_dir()):
        for stage in sorted(p.name for p in chart.iterdir() if p.is_dir() and p.name != "_shared"):
            # A flipped stage renders nothing in HelmCharts; naming it would select the chart's
            # other stages too, the moving one among them.
            if (chart.name, stage) != (app.name, app.stage) and not flipped(chart, stage):
                names.append(chart.name if stage == "prd" else f"{chart.name}@{stage}")
    return names


def helm_charts_without(app: App, dataset: Path, edges: list[str], logfile: Path) -> list[str]:
    """HelmCharts' half: rendering every release but the app's against `dataset`, nothing
    overlaid, it must build and draw each of `edges` exactly as `dataset` publishes it."""
    r = iac("env", f"ARCH_DATASET_URL={dataset}", "ARCH_DATASET_OVERLAY=", "poetry", "run",
            "gen-architecture", *hc_releases_without(app), cwd=HC, check=False)
    with logfile.open("a") as f:
        f.write(f"\n# HelmCharts without {app.ns}\n{r.stdout}{r.stderr}")
    if r.returncode != 0:
        errors = [l for l in r.stderr.splitlines() if not l.startswith("gap: ")]
        return [f"HelmCharts does not build without {app.ns}:", *errors[-15:]]
    published = {x["id"]: x for x in yaml.safe_load(dataset.read_text())["relations"]}
    artifact = yaml.safe_load((HC / "docs/architecture/helm-charts.yaml").read_text())
    drawn = {x["id"]: x for x in artifact["relations"]}
    lost = []
    for rid in edges:
        if rid not in drawn:
            lost.append(f"helm-charts would no longer draw: {rid}")
        elif drawn[rid] != published[rid]:
            lost.append(f"helm-charts would redraw {rid}: published {published[rid]!r} "
                        f"!= generated {drawn[rid]!r}")
    return lost


def cmd_arch(app: App, args) -> None:
    """The producer handover (argo-cd D50), both halves, against one dataset snapshot with
    nothing overlaid. The app's new producer must publish every id helm-charts publishes for the
    stage, every field equal; ids it adds are accepted. HelmCharts, rendering every release but
    the app's, must build and still draw every edge the check lists under helm-charts: from the
    flip until HelmCharts' build clears the app's ids, the collector publishes nothing. Edges
    other producers draw resolve against the kept ids, so they are reported, not gated."""
    if not run(["git", "remote"], cwd=app.path).stdout.strip():
        run(["git", "remote", "add", "origin", app.url], cwd=app.path)
    dataset = dataset_snapshot(app, args.dataset)
    r = iac("python3", "aac-tools/checks/handover_equality.py", "--deploy-repo", str(app.path),
            "--stage", app.stage, "--producer", app.producer, "--dataset", str(dataset),
            cwd=WORK / "ArgoCDTools", check=False)
    out = r.stdout + r.stderr
    logfile = HOME / "bulk-migration/logs" / f"{app.ns}.arch.txt"
    logfile.write_text(out)
    if "Traceback" in out:
        last = [l for l in out.splitlines() if "gen-architecture:" in l][-3:]
        raise Stop("generator failed:\n" + "\n".join(last or out.splitlines()[-3:]))
    lines = out.splitlines()
    diffs = [l for l in lines if l.startswith(("element ", "relation "))]
    added = [l for l in diffs if ADDED.match(l)]
    lost = [l for l in diffs if not ADDED.match(l)]
    drawn: dict[str, list[str]] = {}
    for m in filter(None, map(DRAWN.match, lines)):
        drawn.setdefault(m[1], []).append(m[2])
    hc_edges = drawn.pop("helm-charts", [])
    lost += helm_charts_without(app, dataset, hc_edges, logfile)
    if lost:
        raise Stop("the handover does not hold:\n" + "\n".join(lost))
    elsewhere = ", ".join(f"{p} {len(ids)}" for p, ids in sorted(drawn.items())) or "none"
    log(f"{app.ns}: architecture handover holds ({len(added)} addition(s); HelmCharts without the "
        f"app draws its {len(hc_edges)} edge(s); edges other producers draw: {elsewhere})")
    app.save_state(arch_added=added)


IMAGE_REF = re.compile(r'image:\s*"?registry:5000/([\w.-]+)\{\{-?\s*\$?\.Values\.([\w.]+)\s*-?\}\}')


def cmd_pins(app: App, args) -> None:
    """Who writes each in-house image's pin (argo-cd D53). DockerImages' images get a
    deploy-pins.json entry (committed locally); app-built images are listed for their
    build's Jenkinsfile."""
    found = set()
    for t in (app.path / "chart/templates").rglob("*.yaml"):
        found |= set(IMAGE_REF.findall(t.read_text()))
    di = WORK / "DockerImages"
    manual = []
    for image, path in sorted(found):
        entry = {"repo": f"pvginkel/{app.repo_name}", "file": f"config/{app.stage}/values.yaml", "path": path}
        if (di / image / "Dockerfile").exists():
            f = di / image / "deploy-pins.json"
            entries = json.loads(f.read_text()) if f.exists() else []
            if entry not in entries:
                entries.append(entry)
                f.write_text(json.dumps(entries, indent=2) + "\n")
                run(["git", "add", str(f)], cwd=di)
                run(["git", "commit", "-q", "-m", f"{image}: pin into {app.repo_name} (argo-cd D53)"], cwd=di)
            log(f"{image} -> DockerImages/{image}/deploy-pins.json ({path})")
        else:
            manual.append((image, path))
            log(f"{image} -> an app build writes {path} in config/{app.stage}/values.yaml")
    app.save_state(pins_manual=manual)


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
                                f"    jenkinsJob: AaC/{app.aac_job}\n")
    f.write_text(text)
    run(["git", "add", "pipeline-producers.yaml"], cwd=arch)
    run(["git", "commit", "-q", "-m", f"Registry: the {app.producer} producer"], cwd=arch)
    log(f"registered {app.producer} (Architecture, local commit)")


def registry_entry(app: App, auto: bool) -> str:
    text = ("reconciler: argo-cd\ndeployed: true\n"
            f"autoSync: {'true' if auto else 'false'}\n"
            f"repo: {app.url}\ntargetRevision: main\n")
    if app.load_state().get("ssa"):
        # Objects over the last-applied limit: the Application applies server-side (D62).
        text += "syncOptions:\n  - ServerSideApply=true\n"
    up = repo_upstream(app)
    if up:
        # The chart releases-upstream renders; the deploy repo's architecture.yaml pins the same
        # version for the generator (argo-cd D57): bump both.
        text += yaml.safe_dump({"upstream": up}, sort_keys=False)
    return text


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
    check_upstream_pin(app)
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
            # Modules move whole; a plain resource moves by its own address. Nothing is renamed.
            mods = sorted({".".join(a.split(".")[:2]) if a.startswith("module.") else a for a in rest})
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
    # An `import` block adopts a live object the chart no longer renders; each is declared.
    imports = sum(len(re.findall(r"^import\s*\{", f.read_text(), re.M)) for f in (app.path / "terraform").glob("*.tf"))
    hooked = owns_webhook(app)
    want = (f"Plan: {imports} to import, {int(hooked)} to add, 0 to change, 0 to destroy." if imports
            else "Plan: 1 to add, 0 to change, 0 to destroy." if hooked
            else "No changes. Your infrastructure matches the configuration.")
    adds = re.findall(r"# (\S+) will be created", out)
    if want not in out or adds != (["github_repository_webhook.argocd[0]"] if hooked else []):
        summary = [l for l in out.splitlines() if re.search(r"#.*will be|must be replaced|Plan:|No changes", l)]
        raise Stop("plan is not the webhook alone:\n" + "\n".join(summary))
    log(f"plan: {want}")
    app.save_state(planned=True)


def owns_webhook(app: App) -> bool:
    """Whether this stage's state owns the repository's webhook: true in exactly one stage."""
    f = app.path / "config" / app.stage / "terraform.tfvars"
    return bool(re.search(r"^manage_webhook\s*=\s*true", f.read_text(), re.M)) if f.exists() else False


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------------------------
# preflight: stuck fields and a server-side diff


EXPECTED_STUCK = {"metadata.labels", "metadata.annotations"}


def cmd_preflight(app: App, args) -> None:
    run(["git", "pull", "-q", "--ff-only"], cwd=app.path, check=False)
    check_upstream_pin(app)
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
        objs = [d for d in docs(rtext) if not is_hook(d) and not helm_test(d)]
        # A Job renamed from randAlphaNum is new by design: kubectl diff shows it whole.
        renamed = (new_jobs(app, objs) | RENDER_ONLY.get(app.name, set())
                   | ACCEPTED.get(app.name, {}).get("render_only", set()))
        objs = [d for d in objs if key(d) not in renamed]
        replaced = [d for d in objs if "Replace=true" in
                    ((d["metadata"].get("annotations") or {}).get("argocd.argoproj.io/sync-options") or "")]
        problems = preflight_problems(out, app, replaced)
        problems += replace_problems(app, replaced, tmp)
        objs = [d for d in objs if d not in replaced]
        # The SSE rewrite's generator and ExternalSecret are new by design (verify accepts them);
        # kubectl diff shows a new object whole.
        if app.load_state().get("sse"):
            objs = [d for d in objs if key(d) not in (("Password", "sse-callback"), ("ExternalSecret", "sse-callback"))]
        for k in sorted(RECREATE.get(app.name, set())):
            log(f"{k[0]}/{k[1]}: left out of the diff; the sync recreates it (claim template)")
        objs = [d for d in objs if key(d) not in RECREATE.get(app.name, set())]
        # Server-side diff of the render against live: what the sync would change.
        (tmp / "apply.yaml").write_text(yaml.safe_dump_all(objs))
        side = (["--server-side", "--field-manager=argocd-controller", "--force-conflicts"]
                if app.load_state().get("ssa") else ["--server-side=false"])
        d = iac("kubectl", *KC, "diff", "-n", app.ns, *side, "-f", str(tmp / "apply.yaml"), check=False)
        (HOME / "bulk-migration/logs" / f"{app.ns}.diff.txt").write_text(d.stdout + d.stderr)
        changed = diff_problems(d.stdout, ACCEPTED.get(app.name, {}).get("lines"))
        if d.returncode not in (0, 1):
            problems.append(f"kubectl diff failed: {d.stderr[-500:]}")
        problems += changed
        if problems:
            raise Stop("preflight:\n" + "\n".join(problems))
        log(f"preflight clean at {sha[:7]}: residue is Helm labels/annotations only; diff is metadata only")
        app.save_state(preflight=sha)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def eso_targets(app: App) -> set[str]:
    """Names of the Secrets the render's ExternalSecrets materialise."""
    out = set()
    for d in docs(render(app)):
        if d.get("kind") == "ExternalSecret":
            out.add(d["spec"].get("target", {}).get("name") or d["metadata"]["name"])
    return out


# What a replace also resets, harmlessly, on a Deployment the SSE rewrite rolls anyway: the pull
# policy the API server defaulted to Always when the image was a `:latest` tag (a digest defaults
# to IfNotPresent, as KubeCoderDeploy declares), and a `kubectl rollout restart` stamp.
REPLACE_OK = re.compile(r"^-\s*imagePullPolicy: Always$|^\+\s*imagePullPolicy: IfNotPresent$|"
                        r"^-\s*kubectl\.kubernetes\.io/restartedAt: ")


def replace_problems(app: App, replaced: list[dict], tmp: Path) -> list[str]:
    """What replacing each Replace=true object changes: a server-side dry run, and the spec it
    would leave against the live one. Only the SSE callback secret may move."""
    import difflib
    problems = []
    for d in replaced:
        f = tmp / f"replace-{d['kind']}-{d['metadata']['name']}.yaml"
        f.write_text(yaml.safe_dump(d))
        r = iac("kubectl", *KC, "replace", "-n", app.ns, "--dry-run=server", "-o", "json", "-f", str(f),
                check=False)
        if r.returncode != 0:
            problems.append(f"replace {d['kind']}/{d['metadata']['name']} dry run: {r.stderr.strip()[-400:]}")
            continue
        live = json.loads(iac("kubectl", *KC, "get", d["kind"].lower(), d["metadata"]["name"], "-n", app.ns,
                              "-o", "json").stdout)
        a = yaml.safe_dump(live["spec"], sort_keys=True).splitlines()
        b = yaml.safe_dump(json.loads(r.stdout)["spec"], sort_keys=True).splitlines()
        changed = [l for l in difflib.unified_diff(a, b, lineterm="", n=0)
                   if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        shown = [l for l in difflib.unified_diff(
            yaml.safe_dump(redact({"spec": live["spec"]}), sort_keys=True).splitlines(),
            yaml.safe_dump(redact({"spec": json.loads(r.stdout)["spec"]}), sort_keys=True).splitlines(),
            lineterm="", n=0) if l[:1] in "+-" and not l.startswith(("+++", "---"))]
        (HOME / "bulk-migration/logs" / f"{app.ns}.replace.txt").write_text("\n".join(shown) + "\n")
        bad = [l for l in changed if not (SSE_OK.search(l) or REPLACE_OK.search(l))]
        if bad:
            problems.append(f"replace {d['kind']}/{d['metadata']['name']} changes more than the SSE secret: "
                            f"see {app.ns}.replace.txt")
        else:
            log(f"replace {d['kind']}/{d['metadata']['name']}: only the SSE callback secret changes")
    return problems


def new_jobs(app: App, objs: list[dict]) -> set[tuple]:
    """The render's Jobs a randAlphaNum rename made (verify paired each with a live one)."""
    if not app.load_state().get("renamed_jobs"):
        return set()
    live = {i["metadata"]["name"] for i in json.loads(iac("kubectl", *KC, "get", "jobs", "-n", app.ns, "-o",
                                                          "json").stdout)["items"]}
    return {key(d) for d in objs if d["kind"] == "Job" and d["metadata"]["name"] not in live}


def dotted(path: tuple) -> str:
    """A D59 drop's path as stuck_fields prints it, up to the first list index."""
    out = []
    for k in path:
        if isinstance(k, int):
            break
        out.append(k)
    return ".".join(out)


def preflight_problems(out: str, app: App, replaced: list[dict] = ()) -> list[str]:
    problems = []
    drops = [dotted(p) for _, _, p in D59.get(app.name, {}).get("drops", [])]
    renamed_prefixes: set[str] = set()
    if app.load_state().get("renamed_jobs"):
        renamed_prefixes = {d["metadata"]["name"][:-5] for d in docs(render(app))
                            if d["kind"] == "Job" and not is_hook(d)}
    # A replaced object loses every field its render lacks, so Helm's SSE value is no residue there.
    replaced_names = {d["metadata"]["name"] for d in replaced}
    targets = None
    # stuck_fields prints "A." objects Helm made that the render lacks, "B." stuck fields.
    section = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("A."):
            section = "A"
            inline = s.split(":", 1)[1].strip() if ":" in s else ""
            for obj in [o.strip() for o in inline.split(",") if o.strip() and o.strip() != "none"]:
                # ESO-materialised Secrets are expected: the render carries their ExternalSecret.
                if obj.startswith("Secret/") and targets is None:
                    targets = eso_targets(app)
                if obj.startswith("Job/") and obj[4:-5] in renamed_prefixes:
                    log(f"{obj}: the Helm-made run of a renamed Job, left for the cleanup")
                    continue
                if tuple(obj.split("/", 1)) in ACCEPTED.get(app.name, {}).get("live_only", set()):
                    log(f"{obj}: dropped from the render (accepted, ANS-103); left for the cleanup")
                    continue
                if not (obj.startswith("Secret/") and obj[7:] in targets):
                    problems.append(f"object not in the render: {obj}")
        elif s.startswith("B.") or s.lower().startswith("stuck"):
            section = "B"
        elif s.startswith("TOTAL"):
            section = None
        elif section == "A" and s:
            problems.append(f"object not in the render: {s}")
        elif section == "B" and s:
            field = s.split()[0] if s.split() else ""
            if replaced_names and ".env[name=SSE_CALLBACK_SECRET].value" in field:
                continue
            if any(field.startswith(f) for f in ACCEPTED.get(app.name, {}).get("stuck", [])):
                log(f"stuck field {field}: accepted (ANS-103), which the live object keeps")
                continue
            if any(d and field.startswith(d) for d in drops):
                log(f"stuck field {field}: a D59 drop, which the live object keeps")
                continue
            if not any(field.startswith(f) for f in EXPECTED_STUCK):
                problems.append(f"stuck field: {s}")
    return problems


def diff_problems(diff: str, accepted: re.Pattern | None = None) -> list[str]:
    """Changed lines in `kubectl diff` other than metadata bookkeeping and an app's accepted change."""
    ok = re.compile(r"^[+-]\s*(generation:|resourceVersion:|kubectl\.kubernetes\.io/last-applied-configuration|"
                    r"\{\"apiVersion\"|argocd\.argoproj\.io/|annotations:\s*$|managedFields|"
                    r"- apiVersion:|fieldsType:|fieldsV1:|f:|manager:|operation:|time:|\.:\s*\{\}|"
                    r"k:|v:|apiVersion:|$)")
    bad = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "diff ")):
            continue
        if line.startswith(("+", "-")) and not ok.match(line) and not (accepted and accepted.search(line)):
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
    # A multi-source Application (an upstream app) reports one revision per source: the chart
    # version, then the deploy repo twice (the values ref and the companion).
    up = repo_upstream(app)
    want = [up["version"], rev, rev] if up else rev
    field = "revisions" if up else "revision"
    if a["status"].get("sync", {}).get(field) != want:
        raise Stop(f"Application compares {a['status'].get('sync', {}).get(field)}, not {want}")
    before = {p["metadata"]["name"] for p in json.loads(iac(
        "kubectl", *KC, "get", "pods", "-n", app.ns, "-o", "json").stdout)["items"]}
    sync = {"revisions": want, "sourcePositions": [1, 2, 3]} if up else {"revision": rev}
    # A manual operation does not inherit the Application's sync options: without them here, an
    # app on ServerSideApply=true (D62) applies client-side and its large CRDs are refused.
    options = (a["spec"].get("syncPolicy") or {}).get("syncOptions")
    if options:
        sync["syncOptions"] = options
    for kind, name in sorted(RECREATE.get(app.name, set())):
        r = iac("kubectl", *KC, "delete", kind.lower(), name, "-n", app.ns, "--cascade=orphan",
                "--ignore-not-found", check=False)
        if r.returncode != 0:
            raise Stop(f"orphan delete of {kind}/{name}: {r.stderr.strip()[-300:]}")
        log(f"{kind}/{name} deleted with --cascade=orphan ({r.stdout.strip() or 'already gone'}); the sync creates it")
    patch = {"operation": {"initiatedBy": {"username": "claude-bulk-migration"}, "sync": sync}}
    # The previous operation stays in status until the new one replaces it, and it can carry the
    # same revision: only an operation started after this patch is this sync's.
    previous = (a["status"].get("operationState") or {}).get("startedAt")
    iac("kubectl", *KC, "patch", "application", "-n", "argocd-prd", app.ns, "--type", "merge",
        "-p", json.dumps(patch))
    log(f"sync started at {rev[:7]}")
    phase = None
    for _ in range(90):
        time.sleep(10)
        a = app_status(app)
        op = a.get("status", {}).get("operationState", {})
        if op.get("startedAt") == previous:
            continue
        if op.get("syncResult", {}).get(field) == want and op.get("phase") in ("Succeeded", "Failed", "Error"):
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
    # Found by its arguments, not its name: Argo names a generateName hook after the
    # Application's short revision, which a multi-source (upstream) Application does not have
    # (`tf-presync--presync-<ts>`). The hook's own `hook.revision` argument is the full SHA.
    jobs.sort(key=lambda j: j["metadata"]["creationTimestamp"])
    hook = [j for j in jobs if any({app.ns, rev} <= set(c.get("args", []))
                                   for c in j["spec"]["template"]["spec"]["containers"])]
    logs = ""
    if hook:
        logs = iac("kubectl", *KC, "logs", "-n", "argocd-hooks", f"job/{hook[-1]['metadata']['name']}",
                   check=False).stdout
    (HOME / "bulk-migration/logs" / f"{app.ns}.hook.txt").write_text(logs)
    logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
    applied = re.findall(r"Apply complete! Resources: .*", logs)
    problems = []
    if s["sync"]["status"] != "Synced" or s["health"]["status"] != "Healthy":
        problems.append(f"Application {s['sync']['status']} {s['health']['status']}")
    imports = sum(len(re.findall(r"^import\s*\{", f.read_text(), re.M)) for f in (app.path / "terraform").glob("*.tf"))
    added = int(owns_webhook(app))
    want_apply = (f"Apply complete! Resources: {imports} imported, {added} added, 0 changed, 0 destroyed." if imports
                  else f"Apply complete! Resources: {added} added, 0 changed, 0 destroyed.")
    # A re-sync after a sync-phase failure finds the webhook the first hook already made.
    again = want_apply.replace(f" {added} added,", " 0 added,")
    if [a.strip() for a in applied] not in ([want_apply], [again]):
        problems.append(f"hook: {applied or 'no apply line'}")
    if problems:
        raise Stop("sync checks:\n" + "\n".join(problems))
    log(f"synced {rev[:7]}: Synced Healthy; hook: {want_apply}")
    app.save_state(synced=rev)


# ---------------------------------------------------------------------------------------------


STEPS = {
    "scaffold": cmd_scaffold, "verify": cmd_verify, "arch": cmd_arch, "pins": cmd_pins, "publish": cmd_publish,
    "register": cmd_register, "flip": cmd_flip, "surgery": cmd_surgery, "plan": cmd_plan,
    "preflight": cmd_preflight, "sync": cmd_sync, "unreplace": cmd_unreplace, "autosync": cmd_autosync,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=list(STEPS))
    ap.add_argument("apps", nargs="+")
    ap.add_argument("--stage", default="prd")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--add-stage", action="store_true",
                    help="scaffold: add --stage to the app's existing deploy repo")
    ap.add_argument("--dataset", default=DATASET_URL,
                    help="arch: the merged published dataset, a URL (fetched once) or a file")
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

#!/usr/bin/env python3
"""Render one Argo CD app-stage by hand, the way Argo renders it, for kubectl apply.

For the rebuilt-cluster bootstrap (docs/runbooks/cluster-bootstrap.md): until
https://charts.home answers, Argo cannot render any deploy repo, because every
chart takes the homelab-shared library from there. This renders an app from its
deploy-repo checkout with the library packaged from a Charts checkout instead,
drops Argo's hook resources (the PreSync Terraform Job and its RBAC; Argo runs
those itself once it can render), and stamps every object with Argo's
annotation tracking-id and the destination namespace, so Argo adopts the
applied objects in place on its first sync.

The registry entry (ArgoCDDeploy releases/values.yaml) supplies what Argo
passes: an upstream app is the upstream chart with the stage's values, followed
by the deploy repo's companion chart; a local app is the deploy repo's chart.
Namespaces and CRDs are moved to the front so a single apply can create them
before the objects that need them.

Run in the iac sidecar; it writes YAML to stdout:

    cexec iac python3 scripts/argo-hand-render.py <app> <deploy-repo-checkout> \
        --kube-version <server version> > /tmp/<app>-prd.yaml
"""

import argparse
import os
import subprocess
import sys
import tempfile

import yaml

# Cluster-scoped kinds the estate's charts render. CRDs declared Cluster in the
# render itself are added at run time.
CLUSTER_KINDS = {
    "APIService", "CSIDriver", "CSINode", "ClusterRole", "ClusterRoleBinding",
    "ClusterSecretStore", "ClusterExternalSecret", "ClusterGenerator",
    "ClusterPushSecret", "CustomResourceDefinition", "IngressClass",
    "MutatingWebhookConfiguration", "Namespace", "Node", "PersistentVolume",
    "PriorityClass", "RuntimeClass", "StorageClass",
    "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration", "VolumeSnapshotClass",
}
HOOK_ANNOTATIONS = ("argocd.argoproj.io/hook", "helm.sh/hook")


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def helm_template(name, chart, namespace, kube_version, extra):
    return run(["helm", "template", name, chart, "--namespace", namespace,
                "--kube-version", kube_version, "--include-crds", *extra])


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("app", help="the registry's app key, e.g. registry")
    p.add_argument("checkout", help="the deploy repo's checkout, at the stage's revision")
    p.add_argument("--stage", default="prd")
    p.add_argument("--kube-version", required=True,
                   help="the cluster's Server Version, as kubectl version prints it")
    p.add_argument("--charts", default="/work/Charts",
                   help="Charts checkout holding charts/homelab-shared")
    p.add_argument("--registry", default="/work/ArgoCDDeploy/releases/values.yaml")
    p.add_argument("--part", choices=("all", "upstream", "companion"), default="all",
                   help="an upstream app's upstream chart or companion chart alone")
    a = p.parse_args()

    entry = yaml.safe_load(open(a.registry))["apps"].get(a.app)
    if entry is None:
        sys.exit(f"{a.app}: no such app in {a.registry}")
    if a.stage not in entry["stages"]:
        sys.exit(f"{a.app}: no stage {a.stage} in the registry")
    name = f"{a.app}-{a.stage}"
    checkout = os.path.abspath(a.checkout)
    values = os.path.join(checkout, "config", a.stage, "values.yaml")
    revision = run(["git", "-C", checkout, "rev-parse", "HEAD"]).strip()
    hook = ["--set", f"hook.repo={entry['repo']}", "--set", f"hook.revision={revision}",
            "--set", f"hook.stage={a.stage}", "--set", f"hook.namespace={name}"]

    with tempfile.TemporaryDirectory() as tmp:
        # A copy of chart/, so the checkout's chart/charts is left as it was.
        chart = os.path.join(tmp, "chart")
        subprocess.run(["cp", "-r", os.path.join(checkout, "chart"), chart], check=True)
        subprocess.run(["rm", "-rf", os.path.join(chart, "charts")], check=True)
        run(["helm", "package", os.path.join(a.charts, "charts", "homelab-shared"),
             "-d", os.path.join(chart, "charts")])

        rendered = ""
        upstream = entry.get("upstream")
        if upstream:
            if a.part in ("all", "upstream"):
                version = entry["stages"][a.stage]["version"]
                run(["helm", "pull", upstream["chart"], "--repo", upstream["repo"],
                     "--version", str(version), "--untar", "-d", os.path.join(tmp, "up")])
                rendered += helm_template(name, os.path.join(tmp, "up", upstream["chart"]),
                                          name, a.kube_version, ["--values", values])
            if a.part in ("all", "companion"):
                rendered += "\n---\n" + helm_template(name, chart, name, a.kube_version, hook)
        else:
            if a.part != "all":
                sys.exit(f"{a.app} is not an upstream app; --part does not apply")
            rendered = helm_template(name, chart, name, a.kube_version,
                                     ["--values", values, *hook])

    docs = [d for d in yaml.safe_load_all(rendered) if d and d.get("kind")]
    cluster_kinds = set(CLUSTER_KINDS)
    for d in docs:
        if d["kind"] == "CustomResourceDefinition" and d["spec"].get("scope") == "Cluster":
            cluster_kinds.add(d["spec"]["names"]["kind"])

    out = []
    for d in docs:
        meta = d.setdefault("metadata", {})
        annotations = meta.get("annotations") or {}
        if any(h in annotations for h in HOOK_ANNOTATIONS):
            continue
        if d["kind"] not in cluster_kinds:
            meta.setdefault("namespace", name)
        group = d["apiVersion"].split("/")[0] if "/" in d["apiVersion"] else ""
        annotations["argocd.argoproj.io/tracking-id"] = (
            f"{name}:{group}/{d['kind']}:{meta.get('namespace', name)}/{meta['name']}")
        meta["annotations"] = annotations
        out.append(d)

    first = {"Namespace": 0, "CustomResourceDefinition": 1}
    out.sort(key=lambda d: first.get(d["kind"], 2))
    yaml.safe_dump_all(out, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    main()

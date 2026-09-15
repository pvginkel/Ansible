# Reaching live infrastructure

The mechanics behind `CLAUDE.md`'s standing rule that the operator runs every `terraform apply`,
`terraform destroy` and `ansible-playbook` against real infrastructure. The rule is about
**authority, not access** — the operator works in this same pod and sees the same `/work/<repo>`
paths, so a command handed over is one Claude could technically have run. It holds regardless:
`changed=N>0` and terraform state mutations are the operator's keystroke.

## The toolchain

The toolchain lives in the `iac` sidecar, not the dev container: `cexec iac <cmd>` for anything
needing poetry, ansible, terraform, kubectl, helm, `bao` or `step`. Curated entry points are
`kc project setup|lint|test` (`kc project info` lists them).

**Ansible** runs from the `ansible/` directory, where `ansible.cfg` lives. Default inventory is
`inventories/prd` (every production-grade host); `inventories/scratch` holds the disposable scratch
fleet, reached with `-i inventories/scratch`.

**Terraform** lives in `terraform/`, and all of it works from this pod via `cexec iac`: `init`,
state reads, `plan` and `apply`. Provider is `bpg/proxmox`; `terraform/{prd,scratch}/backend.tf`
points at an http backend on `127.0.0.1:6061`, served here by the `terraform-backend-git` catalog
service that `.kubecoder/config.yaml` runs as a sidecar. The backend URL names the git store
(`pvginkel/TerraformState`, ref `main`), so this daemon and the one `iac-impl` starts on **srviac**
resolve to the same state — reads here are the real thing, not a private copy. `terraform fmt`
needs no state at all.

`terraform/prd` takes its credentials as variables (`proxmox_endpoint`, `proxmox_username`,
`proxmox_password`, `dns_reservation_token`, `backup_server_token`). Only
`terraform.tfvars.example` is checked in; the values reach this pod's environment as `TF_VAR_*`, in
the dev container and the `iac` sidecar alike, so nothing needs sourcing.

The `IaC/*` Jenkins pipelines run the same Terraform on **srviac** through `iac -c '…'`. The setup
matches this pod's, but srviac is its own VM, so that path still works when Kubernetes — and with it
this pod — is down. Each `iac` run is a throwaway container with a fresh clone of `main`: it sees
pushed state, not the working tree, and `terraform init -input=false` has to come first. From this
pod, go in as `ansible` and use `sudo`, because only `pvginkel` is in srviac's `docker` group (the
SSH options are the ones under "What still needs SSH"):

```
cd ansible && ssh <options> ansible@srviac \
  "sudo iac -c 'cd /work/Ansible/terraform/prd && terraform init -input=false && terraform plan'"
```

A **HelmCharts release's** Terraform also needs OpenBao-held provider credentials. Load them and
plan in one shell — the verb is `deploy plan`, there is no bare `plan` script:

```
. scripts/bao-login.sh && cd /work/HelmCharts && . scripts/setup-env.sh prd && cexec iac poetry run deploy plan prd/<chart>
```

`setup-env.sh` reads OpenBao values into the environment, so it falls under `CLAUDE.md`'s "What
Claude doesn't read on its own" — ask first. On srviac the equivalent is the `IAC_SETUP` prelude in
HelmCharts' `Jenkinsfile`, then `/tmp/hcvenv/bin/deploy plan prd/<chart>`.

**Linting is manual.** There is no pre-commit hook — it was removed because it was breaking
commits. Run `kc project lint` before proposing a commit. For a single path, reach past it:
`cexec iac poetry run ansible-lint <path>`.

**`tools/ai_workflow/track_build.py` looks dead and is not.** Nothing in this repo calls it; it is
on PATH in the KubeCoder environment, where it waits out a pushed Jenkins build and the pipeline
that build triggers. Don't delete it as dead code.

**Notifications are not a script.** `send_message.py` used to live beside it and is gone: the
`IaC/*` pipelines report through jenkins-telegram-bot, which watches every build, and raise
anything the build result does not say through JenkinsPipelineUtils' `notify` var. For yourself,
ask for a notification in plain words — pushing to the operator is built into this environment.

## What is safe to run without asking

Read-only state inspection on managed hosts (`qm config <vmid>`, `lsblk`, file reads) needs an SSH
identity. Those keys come from the KubeCoder secret catalog: `scripts/kubecoder-keys.sh`, driven by
`kc project setup`, lands them at `~/.ssh/id_ed25519_ansible` and `~/.ssh/id_ed25519_pve`.

Read-only Ansible is fine when it is clearly read-only: `ansible -m setup`, or
`ansible-playbook --check --diff` against a host where the role itself has no side effects. When in
doubt, hand the command to the operator.

## Canonical command shape

When handing a command to the operator, use this exact shape:

- **Paths are shared.** `/work/<repo>` means the same thing to both of you — no path translation.
  Prefer repo-relative paths, with `/work/<repo>/…` for cross-repo hops.
- **One line, `cd <dir> && <command>`.** A single copy-paste runs cleanly; if the `cd` fails, the
  second half doesn't fire.
- **Prefix with `cexec iac`.** It mirrors the cwd and carries the environment over, so
  `cd <dir> && cexec iac <cmd>` behaves as if the tool were local.
- **Ansible:** `cd ansible && cexec iac poetry run ansible-playbook playbooks/<play>.yml --limit
  <host>`. Inventory defaults to `inventories/prd` per `ansible.cfg`; pass `-i inventories/scratch`
  only for scratch-fleet runs. Don't pass `--diff` — `ansible.cfg` sets `diff_always = True`. For
  the check-mode preflight, append `--check` to the **very end** of the apply command so the
  operator converts it to an apply by deleting the trailing flag — never put `--check`
  mid-command. Never include `--ask-vault-pass`: `ANSIBLE_VAULT_PASSWORD_FILE` is projected by
  `.kubecoder/config.yaml` and survives into the sidecar, so the vault unlocks automatically.
- **Terraform:** `cd terraform/prd && cexec iac terraform apply`. It applies the working tree, so
  push first and state never runs ahead of `main`. A push to `main` does not apply: `iac-on-push`
  only plans, and convergence is the manual `iac-apply` job. The srviac shape is
  `iac -c 'cd /work/Ansible/terraform/prd && terraform init -input=false && terraform apply'` — it
  applies pushed `main`, not the working tree, and it is the one that still works with Kubernetes
  down.

## Cluster access: `config-prd-write` is cluster-admin on prd

`~/.kube/config-prd-write` is the `kubecoder-rw` identity, bound to `cluster-admin` since
**2026-09-04** (Trello #725). It holds every verb on every resource of the prd cluster,
cluster-scoped included — Nodes, PersistentVolumes, namespaces, cluster RBAC. `kubectl cordon` /
`uncordon` / `drain` work from this pod, as does everything else that used to need the SSH detour.

**The base `~/.kube/config` is unchanged and stays narrow**: it is the separate `kubecoder-ro`
identity — cluster-wide `view` (which excludes Secrets) plus `edit` in the `development` namespace.
It is also the *default* kubeconfig, so cluster-scoped work needs the flag spelled out:

```
cexec iac kubectl --kubeconfig ~/.kube/config-prd-write --context prd cordon srvk8s2
```

The widening swapped the `kubecoder-rw-edit` ClusterRoleBinding for `kubecoder-rw-admin`. The
ServiceAccount and its OpenBao-held token are untouched, so nothing was re-minted and no pod
restarted. **Nothing in this repo reconciles that binding** — it is hand-created out-of-band per
KubeCoder slice 012's K1 recipe, and a cluster rebuild does not restore it.

Same weight as any other production write: say what you are about to change and why before doing
it, and don't leave a node cordoned at the end of a task. The credential is wide now; the care is
what keeps it safe.

Docs written before 2026-09-04 say cluster-scoped work must go over SSH — the identity was
cluster-wide `edit` then, with no cluster-scoped verb at all, and slice 007's PV reattach proof hit
that wall and built its fixtures over SSH. That constraint is gone; treat those citations as
historical.

### What still needs SSH

Node-*host* work rather than cluster-scoped API objects: the microk8s snap itself (`snap restart`,
channel refreshes), `k8s-dqlite` / kubelite recovery, and reading files on the node. `sudo microk8s
kubectl` on a node also stays the break-glass path when the token or the apiserver VIP is itself
the broken thing.

The dev cluster is the other case. `~/.kube/config-dev-write` addresses `srvk8sdev` as
`kubecoder-rw`, which is still only `edit`-bound there, so cluster-scoped work on dev goes over SSH.
When srvk8sdev is running it answers from this pod on 22, 16443 and RGW's 80 (checked 2026-09-15);
docs written before then call it unreachable from here.

```
cd ansible && ssh -o UserKnownHostsFile=files/known_hosts.d/homelab -o GlobalKnownHostsFile=/dev/null \
  -o HostKeyAlgorithms=ssh-ed25519-cert-v01@openssh.com,ssh-ed25519 \
  -o IdentityFile=~/.ssh/id_ed25519_ansible -o IdentitiesOnly=yes \
  ansible@srvk8s1 'sudo snap restart microk8s.daemon-k8s-dqlite'
```

The option pile mirrors `ansible.cfg`'s `ssh_args`: hosts present an SSH CA certificate rather than
a plain host key, and the CA lives in `ansible/files/known_hosts.d/homelab` — hence running from
`ansible/` (or spelling that path absolutely).

## Writing OpenBao secrets via `bao kv put`

`bao kv put` accepts a value from stdin when the key's RHS is `-`. Prefer this over inline
`key=value` whenever the value is sensitive: positional args land in the controller's terminal
scrollback and shell history (`~/.bash_history`); stdin doesn't.

```
# single-key leaf — pipe the value, don't quote it on the command line
printf %s "$VALUE" | bao kv put -mount=kv iac/foo bar=-

# multi-key leaf — assemble a JSON dict and use the @file form
jq -n --arg a "$AKEY" --arg s "$SKEY" '{access_key_id:$a, secret_access_key:$s}' \
  > /tmp/kv.json
bao kv put -mount=kv shared/ceph-rgw/s3 @/tmp/kv.json
shred -u /tmp/kv.json
```

The same logic applies to `bao kv metadata put -custom-metadata=...` for non-sensitive
annotations: those are fine inline, since they are not secret material.

Reading values is a different matter — see `CLAUDE.md`'s "What Claude doesn't read on its own".

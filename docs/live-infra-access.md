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

**`bao` needs a login first, in the same Bash call.** `. scripts/bao-login.sh && cexec iac bao …`
from `/work/Ansible`: the script logs in as the openbao-admin AppRole (credentials from
ansible-vault) and exports `BAO_ADDR=https://secrets` and `BAO_TOKEN`, which `cexec` carries over.
A bare `cexec iac bao` either talks to `127.0.0.1:8200` (no `BAO_ADDR`) or gets 403 on the
sidecar's stale `~/.vault-token`. Environment does not persist between Bash calls, so source it
every time. Listing, metadata and policy reads are fine; a value read is still a per-path ask
(`CLAUDE.md`).

**The sidecar sets `TF_PLUGIN_CACHE_DIR`**, so a `terraform init` measured here says nothing about
what a fresh `iac` container on srviac or the Argo CD PreSync hook downloads: both start with an
empty cache, and a committed lock plus pinned providers still fetches `SHA256SUMS` and its `.sig`
on every init. Before measuring init or network behaviour meant for those, run it as
`cexec iac sh -c 'unset TF_PLUGIN_CACHE_DIR; …'` and grep `TF_LOG=trace` output for `SHA256SUMS`.

**Ansible** runs from the `ansible/` directory, where `ansible.cfg` lives. Default inventory is
`inventories/prd` (every production-grade host); `inventories/scratch` holds the disposable scratch
fleet, reached with `-i inventories/scratch`.

**Terraform** lives in `terraform/`, and all of it works from this pod via `cexec iac`: `init`,
state reads, `plan` and `apply`. Provider is `bpg/proxmox`; `terraform/{prd,scratch}/backend.tf`
points at an http backend on `127.0.0.1:6061`, served here by the `terraform-backend-git` catalog
service that `.kubecoder/config.yaml` runs as a sidecar. The backend URL names the git store
(`pvginkel/TerraformState`, ref `main`), so this daemon and the one `iac-impl` starts on **srviac**
resolve to the same state — reads here are the real thing, not a private copy. That sidecar runs the
stock upstream release, not the estate's patched build, so beside another writer of the state repo —
an Argo CD hook, an `IaC/*` job — a run here can fail with `non-fast-forward update`, and can keep
failing for as long as that daemon runs (AnsibleSpecs `decisions.md`, "Concurrency control").
`terraform fmt` needs no state at all.

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

A **deploy repo's** Terraform is applied by Argo CD's PreSync hook on each sync of its stage
([argocd.md](runbooks/argocd.md)). It is planned as that hook applies it by the hook image's plan
mode, run as a one-off Job in `argocd-hooks` with the hook's own credentials, so none of them
enters this pod ([argocd.md](runbooks/argocd.md#planning-a-stages-terraform-as-its-hook-applies-it)).
A retired stage's Terraform is destroyed by an `IaC/Destroy Stage` build, which shows that
destroy's plan before it asks ([argocd.md](runbooks/argocd.md#destroying-a-retired-stage)).

**Lint before you commit.** There is no pre-commit hook — it was removed because it was breaking
commits. Run `kc project lint` before proposing a commit. `IaC/Build-Main` runs the same ansible and
`terraform fmt` gates, plus `terraform validate`, on every push to `main`, and a finding turns it
red. For a single path, reach past it: `cexec iac poetry run ansible-lint <path>`.

**`track_build.py` lives in DockerImages.** The script on PATH in every KubeCoder environment
that waits out a pushed Jenkins build, the pipeline that build triggers and the Argo CD sync it
hands off to ships in the local-home image, built from DockerImages `kube-coder-dev-local-home/`,
where its tests live too. This repo keeps no copy.

**Notifications are not a script.** `send_message.py` used to live in this repo and is gone: the
`IaC/*` pipelines report through jenkins-telegram-bot, which watches every build, and raise
anything the build result does not say through JenkinsPipelineUtils' `notify` var. For yourself,
ask for a notification in plain words — pushing to the operator is built into this environment.

**Jenkins runs three agent pods at a time, on purpose.** The Kubernetes cloud's container cap of
3 is sized to the nodes' capacity, not a leftover (operator, 2026-09-21). Every pod pipeline
waits for one of those three slots; the `IaC/*` jobs instead share the IaC Agent's single
executor, which is the "iac lock". So a push to many repos at once queues, and nothing is wrong
while it drains: push everything in one go, then check every job's `lastBuild` once the queue
is empty and no build is running. An item that waits on "All nodes of label … are offline" for
about an hour is the plugin's slot-counter leak, not load. Read the cloud's `cloudCounts`
against the live agent pods over several samples before resetting it from the Script Console.

## What is safe to run without asking

Read-only state inspection on managed hosts (`qm config <vmid>`, `lsblk`, file reads) needs an SSH
identity. Those keys come from the KubeCoder secret catalog: `scripts/kubecoder-keys.sh`, driven by
`kc project setup`, lands them at `~/.ssh/id_ed25519_ansible` and `~/.ssh/id_ed25519_pve`.

Read-only Ansible is fine when it is clearly read-only: `ansible -m setup`, or
`ansible-playbook --check --diff` against a host where the role itself has no side effects. When in
doubt, hand the command to the operator.

**Probe `.home` hosts over http, with `curl -sL`.** Internal `.home` hosts are HTTP-only behind
`nginx-prd/nginx`; a few (`charts.home`) redirect to https and serve there. There is no per-host
TLS server for the rest, so `https://<anything>.home` lands on nginx's *default* TLS server and
returns a healthy-looking 200 from an unrelated app (`CN=architecture.webathome.org` as of
2026-10-08, for `kibana`, `grafana` and `headlamp` alike). Check the status and redirect over http
first; if you must use https, confirm the certificate subject
(`openssl s_client -connect <h>:443 -servername <h>`) is the host you asked for, or the body says
nothing about your service.

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
  only for scratch-fleet runs. Don't pass `--diff` — `ansible.cfg` sets `always = True` under `[diff]`. For
  the check-mode preflight, append `--check` to the **very end** of the apply command so the
  operator converts it to an apply by deleting the trailing flag — never put `--check`
  mid-command. Never include `--ask-vault-pass`: `ANSIBLE_VAULT_PASSWORD_FILE` is projected by
  `.kubecoder/config.yaml` and survives into the sidecar, so the vault unlocks automatically.
- **Terraform:** `cd terraform/prd && cexec iac terraform apply`. It applies the working tree, so
  push first and state never runs ahead of `main`. A push to `main` does not apply: `IaC/Build-Main`
  only lints, validates and plans; convergence is the manual `IaC/Apply` job. The srviac shape is
  `iac -c 'cd /work/Ansible/terraform/prd && terraform init -input=false && terraform apply'` — it
  applies pushed `main`, not the working tree, and it is the one that still works with Kubernetes
  down.

## Cluster access: `config-prd-write` is cluster-admin on prd

`~/.kube/config-prd-write` is the `kubecoder-rw` identity, bound to `cluster-admin` since
**2026-09-04**. It holds every verb on every resource of the prd cluster,
cluster-scoped included — Nodes, PersistentVolumes, namespaces, cluster RBAC. `kubectl cordon` /
`uncordon` / `drain` work from this pod, as does everything else that used to need the SSH detour.

**The base `~/.kube/config` reads everything but Secrets**: it is the separate `kubecoder-ro`
identity — get/list/watch on every resource of both clusters, cluster-scoped ones and RBAC objects
included, **except** Secrets and the `exec`, `attach`, `portforward` and `proxy` subresources, plus
`edit` in prd's `development` namespace. Nodes, PersistentVolumes, `kubectl top nodes` and cluster
RBAC read through it, so a read needs neither `config-prd-write` nor SSH. The grant is ClusterRole
`kubecoder-ro-read`, applied by hand from KubeCoder's `docs/operations/kubecoder-ro-read.yaml` per
`cluster-identity-remint.md` beside it. The role names API groups one by one, so a kind from a group
added since reads Forbidden until that manifest names the group.
It is also the *default* kubeconfig, so a cluster-scoped write needs the flag spelled out:

```
cexec iac kubectl --kubeconfig ~/.kube/config-prd-write --context prd cordon srvk8s2
```

The widening swapped the `kubecoder-rw-edit` ClusterRoleBinding for `kubecoder-rw-admin`. The
ServiceAccount and its OpenBao-held token are untouched, so nothing was re-minted and no pod
restarted. **Nothing in this repo reconciles that binding** — it is hand-created out-of-band, and a
cluster rebuild does not restore it; KubeCoder's `docs/operations/cluster-identity-remint.md` is the
recipe that re-mints it.

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
`kubecoder-rw`, which is still only `edit`-bound there, so cluster-scoped writes on dev go over
SSH. Cluster-scoped reads on dev do not: the base `~/.kube/config` makes them with `--context dev`.
When srvk8sdev is running it answers from this pod on 22, 16443 and RGW's 80 (checked 2026-09-15);
docs written before then call it unreachable from here.

srvk8sdev is VM 919 on PVE node `pve`, and **off by default** (`on_boot = false` in
`terraform/prd/vms.tf`, restored 2026-10-03; Terraform ignores `started`). `qm status 919` saying
`stopped` is the normal state: ask before starting it. SecretRotator's nightly run starts it for a
due plan with a step on the dev cluster, and shuts it down again after if it started it
([openbao.md](runbooks/openbao.md) §5). `IaC/Scheduled Update` goes UNSTABLE on its
srvk8sdev stage while it is off, by design ([iac-agent.md](runbooks/iac-agent.md)). When SSH to it
times out while it is up (seen 2026-09-11, cause not found), the guest agent runs commands as
root from the PVE side:

```
ssh <homelab CA options> -i ~/.ssh/id_ed25519_pve root@pve \
  "qm guest exec 919 --timeout 90 -- bash -c 'echo <base64 script> | base64 -d | bash'"
```

It returns JSON; decode `out-data` on pve with
`perl -MJSON -0777 -ne 'print decode_json($_)->{"out-data"}'`. Base64 the script to get past the
nested quoting. `qm status`, `qm reset` and `qm agent <id> ping` are the PVE-side controls; after a
reset, read `journalctl -b -1` rather than the serial console, which captured nothing.

**The prd Ceph nodes are not Ansible-managed yet** (`site-ceph.yml` targets `ceph_dev` only), so
`srvceph1/2/3` have neither the `ansible` user nor the host CA: SSH from the pod fails with "Host
key verification failed" then "Permission denied". Read them the same way through their PVE host —
srvceph1 is VM 113 on `pve1`:

```
ssh -i ~/.ssh/id_ed25519_pve root@pve1 "qm guest exec 113 --timeout 60 -- sh -c 'radosgw-admin bucket stats'"
```

Filter `radosgw-admin user info` through `jq 'del(.keys)'`: it returns the user's secret keys.

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

A leaf's custom metadata is not secret material, so it is fine inline:
`bao kv metadata patch -custom-metadata=<key>=<value>`. Never use `bao kv metadata put`, which
replaces the leaf's whole custom metadata and so drops its rotation annotations. A new leaf needs
those annotations too: [`runbooks/openbao.md`](runbooks/openbao.md#a-new-leaf) §5, "A new leaf".

Reading values is a different matter — see `CLAUDE.md`'s "What Claude doesn't read on its own".

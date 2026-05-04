# Project plan

Master navigation document. Pair this with the current phase document at the start of each conversation.

## How to use this document

Each new conversation loads the same minimal context:

1. `CLAUDE.md` — repo conventions and commit discipline.
2. `README.md` — layout and quickstart.
3. `docs/decisions.md` — authoritative decision record.
4. **This file** (`docs/plan.md`) — where we are and where we're going.
5. **The current phase document** from `docs/phases/` — working context for the conversation.

When a phase is complete, mark its status here, commit, and the next conversation picks up from the next phase document.

## Principles

- Each phase must leave the repo in a **stable, usable state**. No phase may depend on the next phase landing to remain functional.
- **Scratch VMs first.** Real infrastructure is never the first target for a new role or playbook.
- **Runbooks are code.** When a phase introduces an operational procedure (cluster upgrade, seal migration, VM rebuild), it ships with a runbook in `docs/runbooks/`.
- **Phase docs stay lean.** Once a phase is done, strip build-time notes from its document and keep only what remains useful for future operations.

## Phase overview

| # | Phase | Status | Doc |
|---|---|---|---|
| 0 | Foundation | ✅ Done | `docs/decisions.md` + initial commits |
| 1 | Bootstrap + baseline + scratch VM | ✅ Done | [`phases/phase-1-bootstrap-baseline.md`](phases/phase-1-bootstrap-baseline.md) |
| 2 | Proxmox host management | ✅ Done | [`phases/phase-2-proxmox-hosts.md`](phases/phase-2-proxmox-hosts.md) |
| 3 | VM fleet via Terraform — `disk_resize` | ✅ Done | [`phases/phase-3-vm-fleet.md`](phases/phase-3-vm-fleet.md) |
| 3a | VM fleet under Terraform state | ✅ Done | [`phases/phase-3a-vm-fleet-import.md`](phases/phase-3a-vm-fleet-import.md) |
| 4 | microk8s role (scratch exercise) | ✅ Done | [`phases/phase-4-microk8s.md`](phases/phase-4-microk8s.md) |
| 4a | microk8s alignment + upgrade | ✅ Done | [`phases/phase-4a-microk8s-alignment.md`](phases/phase-4a-microk8s-alignment.md) |
| 4b | microk8s VM rebuild scaffolding | ✅ Done | [`phases/phase-4b-microk8s-rebuild.md`](phases/phase-4b-microk8s-rebuild.md) |
| 4b1 | Rebuild prerequisites (registry, CoreDNS, ZFS) | ✅ Done | [`phases/phase-4b1-rebuild-prerequisites.md`](phases/phase-4b1-rebuild-prerequisites.md) |
| 4c | microk8s VM rebuild — first worker + static-IP pivot | ✅ Done | [`phases/phase-4c-microk8s-rebuild-execution.md`](phases/phase-4c-microk8s-rebuild-execution.md) |
| 4d | microk8s VM rebuild completion | ⏳ Planned | [`phases/phase-4d-microk8s-rebuild-completion.md`](phases/phase-4d-microk8s-rebuild-completion.md) |
| 5 | microceph roles and upgrade | ⏳ Planned | — |
| 6 | OpenBao + secrets wiring | ⏳ Planned | — |
| 7 | Storage — Ceph resources + CSIs (RBD, CephFS, SMB) | ⏳ Planned | — |
| 8 | Keycloak provisioning | ⏳ Planned | — |
| 9 | DNS automation | ⏳ Planned | — |
| 10 | CI integration + drift detection | ⏳ Planned | — |

Phase documents are written as each phase is reached. Don't pre-populate detail for phases we haven't committed to.

## Phase summaries

### 0 — Foundation (done)

Repo skeleton, tool selection, scope, secrets strategy, DNS policy, environment mapping. Captured in `docs/decisions.md`.

### 1 — Bootstrap + baseline + scratch VM

Build the two foundational Ansible roles (`bootstrap`, `baseline`) and a disposable Terraform-provisioned scratch VM to exercise them against. After this phase, any new Ubuntu VM can be brought to a consistent managed state with a single command.

### 2 — Proxmox host management

Build `adopt.yml` (the onboarding playbook for non-cloud-init'd hosts), use it to bring `pve`, `pve1`, `pve2` (and as a bonus, `wrkdev` from Phase 1's deferred smoke test) under Ansible management. Port the host-config tunables from `/work/Obsidian/Proxmox.md` into a `proxmox_host` role, and reconcile per-VM CPU affinity from inventory per the model in `docs/decisions.md`. No destructive changes to the live cluster; `--check` runs match current state before any apply.

### 3 — VM fleet via Terraform — `disk_resize` (done)

`disk_resize` role: idempotent reconciliation of guest filesystem against the Terraform-managed disk size — `growpart` + `resize2fs` only on drift, no-op otherwise. Read from `qm config` on the VM's `pve_node` so Ansible stays decoupled from `tfstate`. Lands early because grow-disk is a recurring operation today.

### 3a — VM fleet under Terraform state

Model the six existing managed VMs as Terraform resources and adopt them into state. Establish the "rebuild a VM from scratch" workflow (terraform + bootstrap + baseline + role) that becomes the upgrade path for everything downstream. Implement the `pve_node_backup_datastore` attribute to drive per-disk `backup` flags. Normalize `srvk8ss2` to UEFI. Add `lifecycle { prevent_destroy = true }` on the (future) Jenkins agent VM and OpenBao VM resources per `docs/decisions.md` "Production execution model" — those are created in Phases 6 and 10 respectively, so the requirement is carried forward, not solved here.

### 4 — microk8s role (scratch exercise) (done)

Built and exercised the `microk8s` role on a fresh two-node scratch cluster: kernel modules, Ceph client tooling, snap install pinned to channel, `.microk8s.yaml`, Calico autodetect, addon enablement, idempotent multi-node join via primary/secondary split, OS-user group + kubectl alias. `--check --diff` and re-runs report `changed=0`. Capability-label and MetalLB pool reconciliation, live-cluster adoption, the upgrade playbook, the per-VM TF rework, and the actual rebuild are all in 4a.

### 4a — microk8s alignment + upgrade (done)

Completed the role's missing reconciliation pieces (capability labels, MetalLB IPAddressPool), adopted the live prod and dev clusters under the role additively, drove the HelmCharts label migration to a clean slate, removed the legacy `size=*` labels and `PreferNoSchedule` taint, and delivered the drain-aware upgrade playbook (`update-k8s.yml`) plus the addon-refresh playbook (`refresh-k8s-addons.yml`). Cluster is in target shape pre-rebuild.

### 4b — microk8s VM rebuild scaffolding (done)

Reworked the per-VM TF modules for the four k8s VMs to the from-scratch shape, built `rebuild-k8s.yml`, wrote the `k8s-rebuild` runbook, and made the "Ansible never invokes Terraform" rule explicit. Staging only — no `terraform apply` ran. Sets phase 4c up to drive the actual rebuilds.

### 4b1 — Rebuild prerequisites (registry, CoreDNS, ZFS) (done)

Closed the bring-up gaps the live nodes carried as hand-edits: containerd registry mirrors, node-local `/etc/hosts`, full-Corefile authoritative CoreDNS reconcile, `zfsutils-linux` on every k8s node. Replaced the static `microk8s_primary_host` inventory key with per-cluster runtime election so rebuilding the labeled primary doesn't strand survivors. Reverted Ceph from the dynamic dnsmasq reservation API to static infrastructure (cold-boot ordering: registry depends on Ceph; dnsmasq depends on registry). Live dev reconciled clean; live prd's first contact happens via the 4c rebuilds.

### 4c — microk8s VM rebuild — first worker + static-IP pivot (done)

Drove the first rebuild (`srvk8ss1` → `srvk8s2`) and resolved the structural gap that surfaced mid-rebuild: cloud-init wasn't configuring the secondary NICs, the workload-VLAN address used for cluster DNS was unreachable from a fresh node, and image pulls failed. Pivoted k8s VMs to the same shape Ceph uses (`static_ip = true`, per-NIC `addresses`/`gateway`/`nameservers` in `vms.tf`, cloud-init renders netplan), extended decisions.md's bring-up-tier rationale to k8s nodes, fixed two latent TF issues along the way (`dns_ipv4` output's empty-tuple ternary; `proxmox_download_file` over an existing image). srvk8s2 is on the new shape and soaking.

### 4d — microk8s VM rebuild completion

Finish the parity event: `srvk8s3`, `srvk8s1`, `wrkdevk8s`. Static-IP scaffolding is in place from 4c so the remaining rebuilds use the originally-planned simpler flow. Includes the close-the-parity-event commit (retire adoption known_hosts files), the runbook fold-in for everything 4c learned, and the parked old-VM destroys.

### 5 — microceph roles and upgrade

Same shape as Phase 4 for microceph. Source: `/work/Obsidian/Ceph.md`. Includes upgrade-then-rebuild of the existing nodes per `docs/decisions.md` (upgrade to target LTS first, soak, then rebuild reattaching OSDs; migration via temp cluster on `pve`'s spare as the documented fallback).

### 6 — OpenBao + secrets wiring

Stand up `srvvault`. Azure Key Vault auto-unseal with firewall-pinned SP. AppRole credentials for Ansible, Jenkins, External Secrets Operator. Backup/DR script per the decisions doc. Migrate a first set of HelmCharts secrets to validate the path.

### 7 — Storage — Ceph resources + CSIs (RBD, CephFS, SMB)

Ansible playbooks that provision RBD images and CephFS subvolumes on demand, so Helm charts no longer require manual Ceph operator steps; hooks for the HelmCharts deploy pipeline. Plus: install all three CSI drivers (`ceph-csi-rbd`, `ceph-csi-cephfs`, `csi-driver-smb`) from their upstream Helm charts under Ansible — they're cluster infrastructure with version coupling to the kernel + Ceph layer below, not application workloads. Once Ansible owns them, the matching subcharts in `/work/HelmCharts/charts` retire; `shared/_helpers.tpl`'s StorageClass names (`csi-cephfs-sc`, `csi-rbd-sc`, `smb`) become the contract Ansible holds stable for HelmCharts.

### 8 — Keycloak provisioning

Realms, clients, users, and roles as code via `community.general.keycloak_*`. Secrets pulled from OpenBao.

### 9 — DNS automation

Stand up a dnsmasq sidecar that exposes a CRUD API for dynamic reservations alongside the existing operator-curated static file, and a Terraform resource that calls it. After this phase, adding a managed VM is one `terraform apply` (reservation + VM together); no manual edit of `static-hosts.yaml` for managed hosts. Sidecar is Helm-deployed (in HelmCharts), not Ansible. Specs: [`specs/dns-reservation-api.md`](specs/dns-reservation-api.md), [`specs/dns-reservation-terraform.md`](specs/dns-reservation-terraform.md).

### 10 — CI integration + drift detection

Jenkins jobs for scheduled `--check --diff` runs against the full inventory, drift alerting, and CI-triggered playbook execution for operational tasks.

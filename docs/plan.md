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
| 3a | VM fleet under Terraform state | ⏳ Planned | [`phases/phase-3a-vm-fleet-import.md`](phases/phase-3a-vm-fleet-import.md) |
| 4 | microk8s roles and upgrade | ⏳ Planned | — |
| 5 | microceph roles and upgrade | ⏳ Planned | — |
| 6 | OpenBao + secrets wiring | ⏳ Planned | — |
| 7 | Ceph storage resources | ⏳ Planned | — |
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

### 4 — microk8s roles and upgrade

Install, join, and HA-configure microk8s nodes. Deliver the upgrade playbook (cordon/drain/upgrade/uncordon, `serial: 1`) for both the 3-node prod cluster and the single-node dev cluster. Source: `/work/Obsidian/Kubernetes.md`. Includes rebuilding the existing prod nodes from scratch as the parity event per `docs/decisions.md`.

### 5 — microceph roles and upgrade

Same shape as Phase 4 for microceph. Source: `/work/Obsidian/Ceph.md`. Includes upgrade-then-rebuild of the existing nodes per `docs/decisions.md` (upgrade to target LTS first, soak, then rebuild reattaching OSDs; migration via temp cluster on `pve`'s spare as the documented fallback).

### 6 — OpenBao + secrets wiring

Stand up `srvvault`. Azure Key Vault auto-unseal with firewall-pinned SP. AppRole credentials for Ansible, Jenkins, External Secrets Operator. Backup/DR script per the decisions doc. Migrate a first set of HelmCharts secrets to validate the path.

### 7 — Ceph storage resources

Ansible playbooks that provision RBD images and CephFS subvolumes on demand, so Helm charts no longer require manual Ceph operator steps. Hooks for the HelmCharts deploy pipeline.

### 8 — Keycloak provisioning

Realms, clients, users, and roles as code via `community.general.keycloak_*`. Secrets pulled from OpenBao.

### 9 — DNS automation

Stand up a dnsmasq sidecar that exposes a CRUD API for dynamic reservations alongside the existing operator-curated static file, and a Terraform resource that calls it. After this phase, adding a managed VM is one `terraform apply` (reservation + VM together); no manual edit of `static-hosts.yaml` for managed hosts. Sidecar is Helm-deployed (in HelmCharts), not Ansible. Specs: [`specs/dns-reservation-api.md`](specs/dns-reservation-api.md), [`specs/dns-reservation-terraform.md`](specs/dns-reservation-terraform.md).

### 10 — CI integration + drift detection

Jenkins jobs for scheduled `--check --diff` runs against the full inventory, drift alerting, and CI-triggered playbook execution for operational tasks.

# 01 — Switch the bpg/proxmox provider to root@pam credentials

## Goal

Authenticate the Terraform-side Proxmox provider as `root@pam` with username
+ password, retiring the API-token workarounds that live in
`modules/managed-vm` and `proxmox_host`. Unblocks Phase 4c's rebuild path:
TF can now create passthrough disks directly, so `srvk8s1`'s NVMe + zpool2
reattach is one apply, not two stages.

Decisions taken with the operator:

- `root@pam` has no MFA — straight password auth works.
- No stray `PROXMOX_VE_API_TOKEN` in shell init or `.envrc`.
- Credentials live in `terraform.tfvars` (gitignored), not env vars.

## Steps

### Provider blocks

`terraform/prd/providers.tf` and `terraform/scratch/providers.tf`:

- Replace `api_token = var.proxmox_api_token` with
  `username = var.proxmox_username` + `password = var.proxmox_password`.
- Keep `ssh { agent = true, username = "root" }` unchanged — separate channel
  for snippet uploads.

### Variables

`terraform/prd/variables.tf` and `terraform/scratch/variables.tf`:

- Drop `variable "proxmox_api_token"`.
- Add `variable "proxmox_username"` (string, default `"root@pam"`).
- Add `variable "proxmox_password"` (string, sensitive, no default).

### tfvars examples

- Update `terraform/prd/terraform.tfvars.example` (and the scratch equivalent
  if separate): remove the api-token line, add `proxmox_username` and
  `proxmox_password` placeholders.
- Confirm `terraform.tfvars` is in `.gitignore`.

### Module

`terraform/modules/managed-vm/main.tf`:

- Remove `cpu[0].affinity` from `lifecycle.ignore_changes`.
- Remove `disk[2]` and `disk[3]` from `lifecycle.ignore_changes`.
- Inside `cpu {}`, set `affinity = var.cpu_affinity`.
- Strip the comment paragraphs that document the workaround on the
  `dynamic "disk"` blocks and on `lifecycle`.

`terraform/modules/managed-vm/variables.tf`:

- Add `variable "cpu_affinity"` (string, default `null`, description points
  at `docs/decisions.md`).

### Per-VM declarations

`terraform/prd/vms.tf`:

- Pass `cpu_affinity` to each module instance. Source from a TF-side local
  map keyed on `workload_class` (mirroring `host_vars/pve.yml`'s
  `proxmox_workload_affinity_cores`). Move the map authoritatively into
  `vms.tf` and remove from `host_vars/pve.yml` — affinity is now TF's
  concern and one source of truth is better than two.
- Apply the affinity only to VMs whose `pve_node` is `pve` (the only node
  with a core-zoning policy today). VMs on `pve1`/`pve2` keep
  `cpu_affinity = null`.

### Ansible role retirement

`ansible/roles/proxmox_host/`:

- `tasks/main.yml`: drop the "Reconcile per-VM CPU affinity" block and the
  "Reconcile per-VM passthrough disks" block.
- `meta/main.yml`: trim the description.
- `README.md`: drop the affinity + passthrough sections; the role's remaining
  concern is sysctl tuning + cluster vzdump job + cluster cleanups.
- `host_vars/pve.yml`: remove `proxmox_workload_affinity_cores` (now in
  `vms.tf`).

### Decisions doc

`docs/decisions.md`:

- Rewrite "Proxmox VM CPU affinity" — affinity is now owned by TF; the
  post-Phase-2 Ansible reconciliation step is gone.
- Rewrite "Disk passthrough on managed VMs" — passthroughs are first-class
  TF resources; the index-based slot reservation is gone; the rebuild flow
  collapses from "TF creates bare VM, Ansible reattaches" to "TF apply does
  both."

### Phase docs

- `docs/phases/phase-3a-vm-fleet-import.md`: trim references to the
  `disk[2]/disk[3]` slot reservation.
- `docs/phases/phase-4b-microk8s-rebuild.md`: trim references to the
  Ansible-side passthrough reattach in the rebuild flow.
- `docs/phases/phase-4c-microk8s-rebuild-execution.md`: adjust the per-VM
  flow for `srvk8s1` so it's one TF apply rather than apply +
  Ansible-reattach.

## Verification

- `terraform plan` against `prd` shows no diff on the adopted ceph VMs'
  passthrough entries. Verify on `cephs1` (the riskier of the adopted
  nodes); state matches config from import.
- `terraform plan` shows the `affinity` field appearing on VMs that
  previously had it set out-of-band by Ansible — values must match what the
  Ansible task wrote, otherwise the plan is going to flip live affinity on
  apply. Inspect each VM's current affinity (`qm config <vmid> | grep
  affinity`) before applying.
- After apply: `qm config <vmid> | grep affinity` matches what TF wrote on
  every pve-resident VM with a workload_class.

## Caveats

- The bpg ticket lifetime is 2h with transparent re-auth; long applies are
  fine.
- The root password is a higher-value secret than the API token. tfvars
  must be gitignored — confirm before the first commit.

## Commits

1. TF change: providers + variables + tfvars examples + module + `vms.tf`
   reshuffle.
2. Ansible cleanup: `proxmox_host` role + host_vars edit.
3. Docs: `decisions.md` + phase doc trims.

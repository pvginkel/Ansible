# `terraform/managed-vms` — per-VM Terraform modules

One root module per existing managed VM. Each subdirectory holds a self-contained Terraform configuration that **models the VM as it currently runs** — not as it would be built from scratch. This is the Phase 3a adoption pattern: capture the live state in `tfstate`, get a zero-diff plan, and stop.

The from-scratch shape (cloud-init snippet, `tls_private_key` for the host key, `local_file` writing into `ansible/files/known_hosts.d/`) — see `terraform/scratch/` — lands per VM at *rebuild* time, in Phase 4 (k8s) and Phase 5 (Ceph). Until then, the module shape here is intentionally narrower.

## Layout per VM

```
terraform/managed-vms/<name>/
├── main.tf                  # `module "vm"` call into ../../modules/managed-vm with VM-specific inputs.
├── variables.tf             # Provider inputs only (token, endpoint).
├── providers.tf             # bpg/proxmox provider config. Identical across VMs.
├── versions.tf              # Binds local "proxmox" name to bpg/proxmox so `provider "proxmox" {}` resolves.
└── terraform.tfvars.example # Template for terraform.tfvars (gitignored).
```

Provider **version** constraint and required Terraform CLI version are declared once in `../modules/managed-vm/versions.tf` and inherited at init. The per-VM `versions.tf` only carries the minimal `required_providers { proxmox = { source = "bpg/proxmox" } }` needed to bind the local name — without it, TF defaults to `hashicorp/proxmox` (which doesn't exist) when it sees the root's `provider "proxmox" {}` block.

The shared resource shape lives in `../modules/managed-vm/`. Each per-VM `main.tf` is a thin caller — vmid, name, smbios uuid, the disk and NIC lists, etc. There is one root module per VM precisely because the VMs are not interchangeable: per-VM `terraform.tfstate` keeps blast radius scoped, and `terraform plan` / `apply` / `state mv` operate on one VM at a time.

No per-VM `outputs.tf` — every value worth exposing is either an input we just set or already an output of the child module. Adding a re-export shim per VM is duplication without payoff.

## Backup flag — single source of truth

Each module reads `pve_node_backup_datastore` from `ansible/inventories/prd/host_vars/<pve_node>.yml` and uses its presence to set `backup` on managed disks. Passthrough disks are always `backup = false` regardless of node — vzdump of a multi-TB raw passthrough is neither crash-consistent nor useful (Ceph/ZFS owns redundancy). See `docs/decisions.md` "Backup".

## Provider credentials

Each per-VM root module reads the same Proxmox API token. Either:

- copy `terraform.tfvars.example` to `terraform.tfvars` in each per-VM directory and fill in (gitignored, so this is six edits over time), or
- set `TF_VAR_proxmox_api_token` (and friends) in your shell so all modules pick it up — recommended.

## Workflow per VM

1. `cd terraform/managed-vms/<name>`
2. `terraform init`
3. `terraform import 'proxmox_virtual_environment_vm.<name>' <pve_node>/<vmid>`
4. `terraform plan` — surfaces the drift between the module declaration and live state.
5. Tune `lifecycle.ignore_changes` (and the module declaration where appropriate) until the plan is empty.
6. Commit when zero-diff.

## State files

Each per-VM directory carries its own `terraform.tfstate`. Six VMs = six tfstates, all gitignored (the future production execution model commits state to a dedicated git repo per `decisions.md`; for Phase 3a, state is local-only on the operator workstation).

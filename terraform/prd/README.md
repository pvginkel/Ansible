# `terraform/prd` — production VM Terraform root

Single root module managing every adopted production VM. The inventory of VMs lives in [`vms.tf`](vms.tf) as `local.vms`; the per-VM resource shape lives in [`../modules/managed-vm/`](../modules/managed-vm). One `module "vm"` call with `for_each = local.vms` instantiates the lot.

## Layout

```
terraform/prd/
├── main.tf                  # `module "vm"` call into ../modules/managed-vm with for_each = local.vms.
├── vms.tf                   # local.vms — the per-VM inputs (vmid, smbios uuid, disks, NICs, tags, etc.).
├── variables.tf             # Provider inputs only (token, endpoint).
├── providers.tf             # bpg/proxmox provider config.
├── versions.tf              # Binds local "proxmox" name to bpg/proxmox so `provider "proxmox" {}` resolves.
├── outputs.tf               # vm_ids and nic_macs maps, keyed by VM name.
└── terraform.tfvars.example # Template for terraform.tfvars (gitignored).
```

Provider **version** constraint and required Terraform CLI version are declared once in [`../modules/managed-vm/versions.tf`](../modules/managed-vm/versions.tf) and inherited at init. The local `versions.tf` only carries the minimal `required_providers { proxmox = { source = "bpg/proxmox" } }` needed to bind the local name — without it, TF defaults to `hashicorp/proxmox` (which doesn't exist) when it sees the `provider "proxmox" {}` block.

## Backup flag — single source of truth

Each module instance reads `pve_node_backup_datastore` from `ansible/inventories/prd/host_vars/<pve_node>.yml` and uses its presence to set `backup` on managed disks. Passthrough disks are always `backup = false` regardless of node — vzdump of a multi-TB raw passthrough is neither crash-consistent nor useful (Ceph/ZFS owns redundancy). See `docs/decisions.md` "Backup".

## Provider credentials

Either:

- copy `terraform.tfvars.example` to `terraform.tfvars` and fill in (gitignored), or
- set `TF_VAR_proxmox_api_token` / `TF_VAR_proxmox_endpoint` in the shell — recommended.

## Adding or importing a VM

1. Add an entry to `local.vms` in `vms.tf`.
2. `terraform init`
3. `terraform import 'module.vm["<name>"].proxmox_virtual_environment_vm.this' <pve_node>/<vmid>`
4. `terraform plan` — surfaces drift between the new entry and live state.
5. Tune the entry (and `lifecycle.ignore_changes` in the module if needed) until plan is empty.
6. Commit when zero-diff.

To touch a single VM during plan/apply, target it: `terraform plan -target='module.vm["<name>"]'`.

## State

`terraform.tfstate` is local-only on the operator workstation today. The future production execution model commits state to a dedicated git repo per `decisions.md`.

# `terraform/prd` — production VM Terraform root

Single root module managing every adopted production VM. The inventory of VMs lives in [`vms.tf`](vms.tf) as `local.vms`; the per-VM resource shape lives in [`../modules/managed-vm/`](../modules/managed-vm). One `module "vm"` call with `for_each = local.vms` instantiates the lot.

## Layout

```
terraform/prd/
├── main.tf                  # `module "vm"` call into ../modules/managed-vm with for_each = local.vms; from-scratch resources (image download, host keys, cloud-init snippets, known_hosts.d/prd) gated on `from_scratch = true` per VM.
├── vms.tf                   # local.vms — the per-VM inputs (vmid, smbios uuid, disks, NICs, tags, etc.).
├── cloud-init.yaml.tftpl    # cloud-init template for from-scratch VMs (creates ansible user, pins SSH host key, installs qemu-guest-agent).
├── variables.tf             # Provider inputs only (username, password, endpoint).
├── providers.tf             # bpg/proxmox provider config (incl. SSH for snippet uploads).
├── versions.tf              # Provider name bindings: bpg/proxmox + tls + local.
├── outputs.tf               # vm_ids and nic_macs maps, keyed by VM name.
└── terraform.tfvars.example # Template for terraform.tfvars (gitignored).
```

Provider **version** constraint and required Terraform CLI version are declared once in [`../modules/managed-vm/versions.tf`](../modules/managed-vm/versions.tf) and inherited at init. The local `versions.tf` only carries the minimal `required_providers { proxmox = { source = "bpg/proxmox" } }` needed to bind the local name — without it, TF defaults to `hashicorp/proxmox` (which doesn't exist) when it sees the `provider "proxmox" {}` block.

## Backup flag — single source of truth

Each module instance reads `pve_node_backup_datastore` from `ansible/inventories/prd/host_vars/<pve_node>.yml` and uses its presence to set `backup` on managed disks. Passthrough disks are always `backup = false` regardless of node — vzdump of a multi-TB raw passthrough is neither crash-consistent nor useful (Ceph/ZFS owns redundancy). See `/work/AnsibleSpecs/decisions.md` "Backup".

## Provider credentials

Either:

- copy `terraform.tfvars.example` to `terraform.tfvars` and fill in (gitignored), or
- set `TF_VAR_proxmox_password` / `TF_VAR_proxmox_endpoint` in the shell.

See [`docs/runbooks/proxmox-credentials.md`](../../docs/runbooks/proxmox-credentials.md).

## Adding or importing a VM

**Adopting an existing VM** (no `from_scratch` flag, smbios/MAC pinned to current values):

1. Add an entry to `local.vms` in `vms.tf`.
2. `terraform init`
3. `terraform import 'module.vm["<name>"].proxmox_virtual_environment_vm.this' <pve_node>/<vmid>`
4. `terraform plan` — surfaces drift between the new entry and live state.
5. Tune the entry (and `lifecycle.ignore_changes` in the module if needed) until plan is empty.
6. Commit when zero-diff.

**Building a VM from scratch** (set `from_scratch = true` on the entry):

The from-scratch shape generates a per-VM SSH host keypair (`tls_private_key`), uploads a cloud-init snippet to the VM's `pve_node`, downloads the Ubuntu cloud image once per node, and writes `ansible/files/known_hosts.d/prd` with one entry per from-scratch VM. The module wires `initialization { user_data_file_id = ... }` onto the VM and stamps the cloud image as `scsi0`'s `file_id`. No SMBIOS UUID is pinned (bpg generates one on first apply); MACs are deterministic from `vm_id`.

To touch a single VM during plan/apply, target it: `terraform plan -target='module.vm["<name>"]'`.

For a fresh state file or recovery from state loss, [`./import.sh`](import.sh) runs the import step for every entry in `local.vms` and skips ones already in state.

## State

`terraform.tfstate` is local-only on the operator workstation today. The future production execution model commits state to a dedicated git repo per `decisions.md`.

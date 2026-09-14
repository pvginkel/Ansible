# Terraform — Proxmox VM provisioning

Provider: [`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox/latest).

Terraform creates VMs (disks, network, cloud-init user-data). Ansible picks up from cloud-init and does all OS-level configuration.

Terraform never destroys a production VM: `managed-vm`'s VM resource carries `prevent_destroy`, so a `prd/` plan that would delete or replace one fails. A prd VM is rebuilt or removed by destroying it on Proxmox first — see [`prd/README.md`](prd/README.md). The scratch root has its own VM resource and destroys or replaces its VMs freely.

## Layout

```
terraform/
├── prd/                # Production-VM root. One module call with for_each over local.vms — see prd/README.md.
├── scratch/            # Disposable scratch VM for exercising roles.
└── modules/
    └── managed-vm/     # Shared resource shape (proxmox_virtual_environment_vm + disks + NICs) used by prd/.
```

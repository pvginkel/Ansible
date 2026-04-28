# Terraform — Proxmox VM provisioning

Provider: [`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox/latest).

Terraform creates and destroys VMs (disks, network, cloud-init user-data). Ansible picks up from cloud-init and does all OS-level configuration.

## Layout

```
terraform/
├── prd/                # Production-VM root. One module call with for_each over local.vms — see prd/README.md.
├── scratch/            # Disposable scratch VM for exercising roles.
└── modules/
    └── managed-vm/     # Shared resource shape (proxmox_virtual_environment_vm + disks + NICs) used by prd/.
```

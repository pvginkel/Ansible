# Terraform — Proxmox VM provisioning

Provider: [`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox/latest).

Terraform creates and destroys VMs (disks, network, cloud-init user-data). Ansible picks up from cloud-init and does all OS-level configuration.

Layout will grow as we build it out — first target is a throwaway scratch VM used for exercising roles.
